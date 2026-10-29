"""Hand written poll loops (ADR 0007 Decision 7).

A sleep in, or reached from, the body of a loop is a poll interval when the loop rechecks state
on every pass, checks it before the first sleep, is bounded by a clock or a counter, and throws,
fails or asserts when the bound runs out. A loop that leaves silently also qualifies when the
call that runs it is followed directly by an assertion of the loop's exit condition, or its
result is the argument of an assertion. Any other loop around a sleep is a fixed sleep.

Conditions are read with each call's arguments put in place of the helper's parameters, through
every helper between the call and the loop. A word of a condition is a clock reading
(`System.currentTimeMillis`, `System.nanoTime`, a clock's `now()`), the loop's own counter, a
literal or constant, or state; a local or field is read through its definitions, so one that
only ever holds a clock reading is a clock.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field

from rootflake.gate.checker.assertions import (
    _close,
    _declarators,
    _split_arguments,
    _text,
    is_assertion_name,
)
from rootflake.gate.checker.calls import Call, calls_in
from rootflake.gate.checker.scope import Program
from rootflake.gate.checker.syntax import (
    Block,
    CompilationUnit,
    Field,
    Lambda,
    Method,
    Statement,
)
from rootflake.gate.checker.tree import path_to, walk

LOOPS = frozenset(["for", "while", "do"])
_KEYWORDS = frozenset(
    "true false null this super new instanceof return int long short byte char double float "
    "boolean var final class".split()
)
_CLOCK_CALLS = frozenset(["currentTimeMillis", "nanoTime"])
_CLOCK_OWNERS = frozenset([
    "Instant", "LocalDateTime", "LocalTime", "ZonedDateTime", "OffsetDateTime", "Clock", "clock",
])  # fmt: skip
_TRANSPARENT = frozenset([
    "equals", "booleanValue", "intValue", "longValue", "valueOf", "max", "min", "abs",
    "toNanos", "toMillis", "toSeconds", "toMicros", "isAfter", "isBefore", "compareTo",
    "plus", "plusMillis", "plusSeconds", "plusNanos", "minus",
])  # fmt: skip
_FUNCTIONAL = frozenset([
    "get", "getAsBoolean", "getAsInt", "getAsLong", "test", "call", "apply", "run",
])  # fmt: skip
_OPERATORS = frozenset("+ - * / % ? : && || & | ^ == != < > <= >= ! -> instanceof".split())
_ASSERTING = ("fail",)


def initializer_spans(tokens, start: int, end: int) -> list[tuple[str, int, int]]:
    """Each declarator of the declaration in tokens[start:end] with its initializer span."""
    out = []
    for at in _declarators(tokens, start, end):
        if at + 1 >= end or tokens[at + 1].text != "=":
            continue
        depth, k = 0, at + 2
        while k < end:
            text = tokens[k].text if tokens[k].kind == "op" else ""
            if text in ("(", "[", "{"):
                depth += 1
            elif text in (")", "]", "}"):
                depth -= 1
            elif depth == 0 and text in (",", ";"):
                break
            k += 1
        out.append((tokens[at].text, at + 2, k))
    return out


def _assignments(unit: CompilationUnit, start: int, end: int) -> Iterator[tuple[str, str]]:
    """Each `name = value` and `this.name = value` in tokens[start:end], with the value."""
    tokens = unit.tokens
    for node in walk(unit):
        if not isinstance(node, Statement) or node.kind != "expression":
            continue
        if not (start <= node.start < end):
            continue
        for k in range(node.start + 1, node.end):
            token = tokens[k]
            if token.kind == "op" and token.text == "=" and tokens[k - 1].kind == "ident":
                yield tokens[k - 1].text, _text(unit, k + 1, node.end - 1)
                break


def _local_definitions(unit: CompilationUnit, body) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for node in walk(body):
        if isinstance(node, Statement) and node.kind == "local_var":
            for name in _declared(unit, node):
                out.setdefault(name, [])
            for name, s, e in initializer_spans(unit.tokens, node.start, node.end):
                out[name].append(_text(unit, s, e))
    for name, value in _assignments(unit, body.start, body.end):
        if name in out:
            out[name].append(value)
    return out


def _declared(unit: CompilationUnit, node: Statement) -> list[str]:
    return [unit.tokens[k].text for k in _declarators(unit.tokens, node.start, node.end)]


def _field_definitions(program: Program) -> dict[str, list[str]]:
    key = ("poll_field_definitions",)
    if key in program.memo:
        return program.memo[key]
    out: dict[str, list[str]] = {}
    for unit in program.units.values():
        for node in walk(unit):
            if isinstance(node, Field):
                for name in node.names:
                    out.setdefault(name, [])
                for name, span in node.initializers.items():
                    out[name].append(_text(unit, span.start, span.end))
    for unit in program.units.values():
        for name, value in _assignments(unit, 0, len(unit.tokens)):
            if name in out:
                out[name].append(value)
    program.memo[key] = out
    return out


@dataclass
class Scope:
    """What the words of a condition mean where it is read."""

    program: Program
    mapping: dict[str, str]  # helper parameters to the arguments they were called with
    counters: frozenset[str]
    locals: dict[str, list[str]]  # the loop's method
    caller: dict[str, list[str]] = field(default_factory=dict)  # the method of the first call


def _lambda_body(text: str) -> str | None:
    words = _strip(text).split()
    if "->" not in words:
        return None
    body = words[words.index("->") + 1 :]
    if body[:1] == ["{"] and body[-1:] == ["}"]:
        body = body[1:-1]
    return " ".join(body)


def kinds(text: str, scope: Scope, seen: frozenset = frozenset()) -> set[str]:
    """What a condition reads: some of clock, counter and state."""
    words = text.split()
    out: set[str] = set()
    for k, word in enumerate(words):
        if not (word[:1].isalpha() or word[:1] in ("_", "$")) or word in _KEYWORDS:
            continue
        previous = words[k - 1] if k else ""
        receiver = words[k - 2] if previous == "." and k >= 2 else None
        if previous == "new":
            out.add("state")
            continue
        is_call = k + 1 < len(words) and words[k + 1] == "("
        if is_call:
            out |= _call_kinds(word, receiver, scope, seen)
            continue
        if receiver is not None and receiver != "this":
            continue  # a field of something already read
        if k + 1 < len(words) and words[k + 1] == "." and word[:1].isupper():
            continue  # a type name before a member
        out |= _name_kinds(word, scope, seen)
    return out


def _call_kinds(word: str, receiver: str | None, scope: Scope, seen: frozenset) -> set[str]:
    if word in _CLOCK_CALLS and receiver == "System":
        return {"clock"}
    if word in ("now", "millis", "instant") and receiver in _CLOCK_OWNERS:
        return {"clock"}
    if word in _TRANSPARENT:
        return set()
    if word in _FUNCTIONAL and receiver in scope.mapping:
        body = _lambda_body(scope.mapping[receiver])
        if body is not None and ("param", receiver) not in seen:
            return kinds(body, scope, seen | {("param", receiver)})
    return {"state"}


def _name_kinds(word: str, scope: Scope, seen: frozenset) -> set[str]:
    if word in scope.counters:
        return {"counter"}
    if word.isupper() or (word[:1].isupper() and "_" not in word and len(word) > 1):
        return set()  # a constant or a type
    if ("name", word) in seen:
        return set()
    seen = seen | {("name", word)}
    if word in scope.mapping:
        value = scope.mapping[word]
        if _lambda_body(value) is not None:
            return set()  # a function passed on, read where it is called
        return kinds(value, scope, seen)
    for table in (scope.locals, scope.caller):
        if word in table:
            out: set[str] = set()
            for value in table[word]:
                out |= kinds(value, scope, seen)
            return out
    fields = _field_definitions(scope.program)
    if word in fields and fields[word]:
        out = set()
        for value in fields[word]:
            out |= kinds(value, scope, seen)
        return out
    return {"state"}


def substitute(text: str, mapping: dict[str, str]) -> str:
    words, out = text.split(), []
    for k, word in enumerate(words):
        previous = words[k - 1] if k else ""
        if word in mapping and previous != ".":
            value = mapping[word]
            if any(w in _OPERATORS for w in value.split()):
                value = f"( {value} )"
            out.append(value)
        else:
            out.append(word)
    return " ".join(out)


def call_mapping(unit: CompilationUnit, call: Call, method: Method, outer: dict) -> dict:
    """The helper's parameters bound to the call's arguments, read through the outer mapping."""
    if call.reference or call.arity is None:
        return {}
    args = _split_arguments(unit, call.end, _close(unit, call.end))
    out = {}
    for k, param in enumerate(method.params):
        if k < len(args):
            out[param.name] = substitute(args[k], outer)
    return out


# -- one loop -------------------------------------------------------------------------------


def _condition(unit: CompilationUnit, loop: Statement) -> str:
    if loop.header is None:
        return ""
    words = _text(unit, loop.header.start + 1, loop.header.end - 1)
    if loop.kind != "for":
        return words
    parts = [p.strip() for p in _top_split(words.split(), ";")]
    return parts[1] if len(parts) == 3 else ""


def _top_split(words: list[str], separator: str) -> list[str]:
    parts, depth, current = [], 0, []
    for word in words:
        depth += word in ("(", "[", "{")
        depth -= word in (")", "]", "}")
        if word == separator and depth == 0:
            parts.append(" ".join(current))
            current = []
        else:
            current.append(word)
    parts.append(" ".join(current))
    return parts


def _counters(unit: CompilationUnit, loop: Statement) -> frozenset[str]:
    tokens, out = unit.tokens, set()
    if loop.kind == "for" and loop.header is not None:
        first = next(
            (k for k in range(loop.header.start, loop.header.end) if tokens[k].text == ";"),
            loop.header.end,
        )
        out |= {tokens[k].text for k in _declarators(tokens, loop.header.start + 1, first)}
    for k in range(loop.start, loop.end - 1):
        if tokens[k].kind == "ident" and tokens[k + 1].text in ("++", "--", "+=", "-="):
            out.add(tokens[k].text)
        if tokens[k].text in ("++", "--") and tokens[k + 1].kind == "ident":
            out.add(tokens[k + 1].text)
    return frozenset(out)


def _outside_lambdas(nodes: list) -> bool:
    return not any(isinstance(n, Lambda) for n in nodes)


@dataclass
class Loop:
    reads_state: bool
    bounded: bool
    checked_first: bool
    asserts: bool
    exits: list[str]


def read_loop(
    program: Program, path: str, method: Method, loop: Statement, sleep_at: int, scope: Scope
) -> Loop:
    unit = program.units[path]
    header = _condition(unit, loop)
    guards: list[tuple[str, Statement]] = []  # if conditions in the body, with their statement
    asserted: list[str] = []
    inner_headers: list[str] = []
    throws = returns = False
    for body in loop.blocks:
        for node in walk(body):
            if not isinstance(node, Statement):
                continue
            if not _outside_lambdas(path_to(unit, node.start)[:-1]):
                continue
            if node.kind == "if" and node.header is not None:
                guards.append((_text(unit, node.header.start + 1, node.header.end - 1), node))
            elif node.kind in LOOPS and node.header is not None:
                inner_headers.append(_condition(unit, node))
            elif node.kind == "throw":
                throws = True
            elif node.kind == "return":
                returns = True
        for call in calls_in(unit, body):
            if call.reference or call.arity is None:
                continue
            if not _outside_lambdas(path_to(unit, call.start)):
                continue
            if is_assertion_name(call.name) or call.name in _ASSERTING:
                asserted.append(
                    " , ".join(_split_arguments(unit, call.end, _close(unit, call.end)))
                )
    conditions = [header, *(g for g, _ in guards), *inner_headers]
    read = [kinds(c, scope) for c in conditions if c]
    reads_state = any("state" in r for r in read)
    bound_reads = read + [kinds(a, scope) for a in asserted]
    bounded = any(r & {"clock", "counter"} for r in bound_reads)
    checked_first = loop.kind != "do" or any(g.start < sleep_at for _, g in guards)
    # After a loop that returns once its condition holds, the code that follows runs only when
    # the bound ran out; after any other loop it runs either way, so it is the silent check.
    asserts = throws or bool(asserted) or (returns and _asserts_after(unit, loop))
    exits = []
    state_conjuncts = [
        c for c in _top_split(header.split(), "&&")
        if c.strip() and "state" in kinds(c, scope)
    ]  # fmt: skip
    if state_conjuncts:
        exits.append(_negate(" && ".join(state_conjuncts)))
    for guard, node in guards:
        if _leaves(unit, node):
            exits.append(guard)
    return Loop(reads_state, bounded, checked_first, asserts, exits)


def _leaves(unit: CompilationUnit, node: Statement) -> bool:
    if not node.blocks:
        return False
    first = node.blocks[0]
    statements = first.statements
    if len(statements) == 1 and statements[0].kind == "block":
        statements = statements[0].blocks[0].statements
    return bool(statements) and statements[-1].kind in ("break", "return")


def _negate(text: str) -> str:
    words = text.split()
    if words[:1] == ["!"] and len(_top_split(words[1:], "&&")) == 1:
        rest = words[1:]
        if rest[:1] == ["("] and rest[-1:] == [")"]:
            rest = rest[1:-1]
        return " ".join(rest)
    if len(_top_split(words, "&&")) == 1 and len(_top_split(words, "||")) == 1:
        if words.count("!=") == 1 and len(_top_split(words, "!=")) == 2:
            return text.replace(" != ", " == ")
        if words.count("==") == 1 and len(_top_split(words, "==")) == 2:
            return text.replace(" == ", " != ")
    return f"! ( {text} )"


def _asserts_after(unit: CompilationUnit, loop: Statement) -> bool:
    nodes = path_to(unit, loop.start)
    for k in range(len(nodes) - 1, 0, -1):
        if nodes[k] is loop and isinstance(nodes[k - 1], Block):
            block = nodes[k - 1]
            after = [s for s in block.statements if s.start >= loop.end]
            for statement in after:
                if statement.kind == "throw":
                    return True
                for call in calls_in(unit, statement):
                    if is_assertion_name(call.name) or call.name in _ASSERTING:
                        return True
            return False
    return False


def qualifies(loop: Loop) -> bool:
    """Whether the loop is a poll loop apart from how a silent one is checked by its caller."""
    return loop.reads_state and loop.bounded and loop.checked_first


# -- the caller's check of a silent loop ------------------------------------------------------


def _strip(text: str) -> str:
    words = text.split()
    while words[:1] == ["("] and words[-1:] == [")"] and _balanced(words[1:-1]):
        words = words[1:-1]
    return " ".join(words)


def _balanced(words: list[str]) -> bool:
    depth = 0
    for word in words:
        depth += word == "("
        depth -= word == ")"
        if depth < 0:
            return False
    return depth == 0


def _equality(text: str) -> tuple[str, str] | None:
    words = _strip(text).split()
    for operator in ("==",):
        parts = _top_split(words, operator)
        if len(parts) == 2:
            return _strip(parts[0]), _strip(parts[1])
    if len(words) > 4 and words[-1] == ")":
        # a . equals ( b )
        for k in range(len(words) - 2, 0, -1):
            if words[k] == "equals" and words[k - 1] == "." and words[k + 1] == "(":
                if _balanced(words[k + 2 : -1]):
                    return _strip(" ".join(words[: k - 1])), _strip(" ".join(words[k + 2 : -1]))
                break
    return None


def asserts_condition(unit: CompilationUnit, statement: Statement, exits: list[str]) -> bool:
    """Whether the statement is an assertion of one of the exit conditions (kind 3 forms)."""
    if statement.kind != "expression":
        return False
    calls = [c for c in calls_in(unit, statement) if not c.reference and c.arity]
    if not calls or not is_assertion_name(calls[0].name):
        return False
    call = calls[0]
    args = [_strip(a) for a in _split_arguments(unit, call.end, _close(unit, call.end))]
    for exit_text in exits:
        exit_text = _strip(exit_text)
        if call.name == "assertTrue" and exit_text in args:
            return True
        if call.name == "assertFalse" and _strip(_negate(exit_text)) in args:
            return True
        pair = _equality(exit_text)
        if call.name == "assertEquals" and pair is not None and len(args) >= 2:
            for candidate in (tuple(args[:2]), tuple(args[-2:])):
                if candidate in (pair, pair[::-1]):
                    return True
    return False
