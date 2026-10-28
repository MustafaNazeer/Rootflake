"""The strict assertion principle (ADR 0007 Decision 5): collection, removal and kind 1 matches.

Every method body in test code that exists before and after the patch has its assertions
collected on both sides: its own, those in lambdas and anonymous classes inside it, and those
in the helpers it calls (followed as rejection scans follow before the patch and as credit
follows after it). An assertion before the patch must reappear unchanged and no more guarded,
or be matched by an added assertion that keeps its strength; otherwise it was removed.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from rootflake.gate.checker.calls import Call, ExpansionBudget, calls_in, reach
from rootflake.gate.checker.lexer import Unparsable, tokenize
from rootflake.gate.checker.model import Context, FileChange, Finding
from rootflake.gate.checker.scope import Program, is_library
from rootflake.gate.checker.survive import SKIP_EXCEPTIONS, attributes
from rootflake.gate.checker.syntax import (
    Block,
    Catch,
    CompilationUnit,
    Lambda,
    Method,
    Statement,
    TypeDecl,
)
from rootflake.gate.checker.tree import path_to, walk

# Public static member names of commonly imported test library classes, read from each pinned
# release (docs/source/test-library-semantics.md section 18).
LIBRARY_MEMBERS = {
    owner: frozenset(entry["methods"]) | frozenset(entry["fields"])
    for owner, entry in json.loads(
        Path(__file__).with_name("library_members.json").read_text(encoding="utf-8")
    ).items()
}
_ROOT_EXACT = frozenset(
    ["fail", "await", "waitAtMost", "failBecauseExceptionWasNotThrown", "shouldHaveThrown",
     "expectThrows"]
)  # fmt: skip
_ROOT_PREFIXES = ("assert", "verify")
# Factory names a matcher sequence is built from (Hamcrest, Mockito modes, AssertJ helpers). A
# call inside an assertion counts as a matcher only under one of these names and only when it
# resolves to a test library; any other call is part of an argument expression.
MATCHER_FACTORIES = frozenset(
    "is not equalTo notNullValue nullValue sameInstance instanceOf any anything containsString "
    "startsWith endsWith matchesPattern equalToIgnoringCase hasItem hasItems contains "
    "containsInAnyOrder containsInRelativeOrder hasSize empty emptyIterable emptyArray hasEntry "
    "hasKey hasValue everyItem allOf anyOf closeTo greaterThan greaterThanOrEqualTo lessThan "
    "lessThanOrEqualTo comparesEqualTo arrayContaining arrayContainingInAnyOrder hasProperty "
    "times never atLeast atLeastOnce atMost atMostOnce only within offset byLessThan "
    "withinPercentage withPrecision entry tuple".split()
)
_EXITS = frozenset(["return", "break", "continue", "throw"])
_LOOPS = frozenset(["for", "while", "do"])
_RETHROW_ENDS = frozenset(["throw", "fail"])


def is_assertion_name(name: str) -> bool:
    return name in _ROOT_EXACT or name.startswith(_ROOT_PREFIXES)


@dataclass(frozen=True)
class Assertion:
    text: str
    signature: tuple[str, ...]
    arguments: tuple[str, ...]
    guard: tuple[str, ...]
    exits_before: frozenset[str]
    credited: bool
    line: int
    start: int
    scanned_only: bool = False
    origin: tuple = ()
    subject: frozenset = frozenset()
    # ("value", name, number) for a name bound only to a numeric literal, ("set", name) for a
    # name declared with a Set type, from the file and method the assertion sits in.
    facts: frozenset = frozenset()


def _text(unit: CompilationUnit, start: int, end: int) -> str:
    return " ".join(t.text for t in unit.tokens[start:end])


def _close(unit: CompilationUnit, open_paren: int) -> int:
    depth = 0
    for i in range(open_paren, len(unit.tokens)):
        token = unit.tokens[i]
        if token.kind == "op" and token.text in "([{":
            depth += 1
        elif token.kind == "op" and token.text in ")]}":
            depth -= 1
            if depth == 0:
                return i
    return len(unit.tokens) - 1


def _split_arguments(unit: CompilationUnit, open_paren: int, close: int) -> tuple[str, ...]:
    pieces, depth, start = [], 0, open_paren + 1
    creating, angles = False, 0  # type arguments of `new T<A, B>(...)` hold commas too
    for i in range(open_paren + 1, close):
        token = unit.tokens[i]
        if token.kind == "keyword" and token.text == "new":
            # `Foo::new` is a reference, not a creation with type arguments to follow.
            creating = unit.tokens[i - 1].text != "::"
        elif creating and token.kind == "op" and token.text == "<":
            angles += 1
        elif creating and token.kind == "op" and token.text == ">":
            angles = max(angles - 1, 0)
        elif creating and not (
            token.kind == "ident"
            or token.text in (".", ",", "?", "&", "extends", "super")
            or token.text in _PRIMITIVES
        ):
            creating, angles = False, 0  # only a type can sit between `new` and its bracket
        if token.kind == "op" and token.text in "([{":
            depth += 1
            creating, angles = False, 0
        elif token.kind == "op" and token.text in ")]}":
            depth -= 1
        elif token.kind == "op" and token.text == "," and depth == 0 and angles == 0:
            pieces.append(_text(unit, start, i))
            start = i + 1
    if start < close:
        pieces.append(_text(unit, start, close))
    return tuple(pieces)


def _guard(
    unit: CompilationUnit, body: Block, index: int, declared: frozenset[str] = frozenset()
) -> tuple[str, ...]:
    """The conditions between the method body and the token at index, outermost first."""
    guard = []
    path = path_to(unit, index)
    inside = False
    for depth, node in enumerate(path):
        if node is body:
            inside = True
            continue
        if not inside:
            continue
        if isinstance(node, Statement):
            child = path[depth + 1] if depth + 1 < len(path) else None
            if node.kind == "if":
                branch = "else" if len(node.blocks) > 1 and child is node.blocks[1] else "then"
                guard.append(f"if {_text(unit, node.header.start, node.header.end)} {branch}")
            elif node.kind in _LOOPS:
                guard.append(f"loop {_text(unit, node.header.start, node.header.end)}")
            elif node.kind == "switch":
                guard.append(f"switch {_text(unit, node.header.start, node.header.end)}")
            elif node.kind == "try" and node.blocks and child is node.blocks[0]:
                if any(not _rethrows(unit, c) for c in node.catches):
                    guard.append("try swallowing")
        elif isinstance(node, Catch):
            guard.append("catch")
        elif isinstance(node, Lambda):
            guard.append("wait" if _wait_lambda(unit, node, declared) else "lambda")
        elif isinstance(node, TypeDecl):
            guard.append("class")
    return tuple(guard)


WAIT_CONDITIONS = frozenset(["until", "untilAsserted", "untilAtomic", "untilAccumulated"])


AWAIT_ROOTS = frozenset(["await", "waitAtMost", "given", "with", "Awaitility"])


def _chain_root(unit: CompilationUnit, name_at: int) -> str | None:
    """The root of the fluent chain that ends in the call named at name_at."""
    tokens, n = unit.tokens, name_at
    while n >= 2 and tokens[n - 1].text == ".":
        before = n - 2
        if tokens[before].text == ")" and tokens[before].kind == "op":
            depth, j = 0, before
            while j > 0:
                if tokens[j].text == ")" and tokens[j].kind == "op":
                    depth += 1
                elif tokens[j].text == "(" and tokens[j].kind == "op":
                    depth -= 1
                    if depth == 0:
                        break
                j -= 1
            n = j - 1
        elif tokens[before].kind == "ident":
            # Only the Awaitility type itself may be a bare receiver root; a variable or field
            # named like await is not a condition factory.
            return tokens[before].text if tokens[before].text == "Awaitility" else None
        else:
            return None
    return tokens[n].text if n != name_at else None


def _wait_lambda(
    unit: CompilationUnit, lam: Lambda, declared: frozenset[str] = frozenset()
) -> bool:
    """Whether the lambda is an argument of an Awaitility condition call: a call named like one,
    at the end of a chain rooted in await, waitAtMost, given or Awaitility, none of which a
    file the checker reads declares."""
    depth, i = 0, lam.start - 1
    while i > 0:
        text = unit.tokens[i].text if unit.tokens[i].kind == "op" else ""
        if text in (")", "]", "}"):
            depth += 1
        elif text in ("(", "[", "{"):
            if depth == 0:
                name = unit.tokens[i - 1].text
                if text != "(" or name not in WAIT_CONDITIONS or name in declared:
                    return False
                root = _chain_root(unit, i - 1)
                return root in AWAIT_ROOTS and root not in declared
            depth -= 1
        elif text == ";" and depth == 0:
            return False
        i -= 1
    return False


def _rethrows(unit: CompilationUnit, catch: Catch) -> bool:
    statements = catch.block.statements
    if not statements:
        return False
    last = statements[-1]
    first = unit.tokens[last.start].text
    return last.kind == "throw" or (last.kind == "expression" and first in _RETHROW_ENDS)


def _chain(unit: CompilationUnit, call: Call) -> tuple[list[str], int]:
    """The call names of a fluent chain starting at the root call, and the chain's end."""
    names = [call.name]
    end = _close(unit, call.start + 1)
    tokens = unit.tokens
    while (
        end + 3 < len(tokens)
        and tokens[end + 1].text == "."
        and tokens[end + 2].kind == "ident"
        and tokens[end + 3].text == "("
    ):
        names.append(tokens[end + 2].text)
        end = _close(unit, end + 3)
    return names, end + 1


def _declared_helper(program: Program, path: str, call: Call) -> bool:
    """Whether the call reaches a method a read file declares: one of an enclosing class or its
    supertypes, or of a declared receiver type. A same named method elsewhere does not hide an
    assertion; it only voids the assertion's credit."""
    if not program.methods_by_name.get(call.name):
        return False
    if call.receiver in (None, "this", "super"):
        return program.resolve_method(path, call.name, call.start).kind == "declared"
    if call.receiver == "<expr>" or call.receiver_at is None:
        return False
    return program.resolve_type(path, call.receiver, call.receiver_at).kind == "declared"


def _library_call(program: Program, path: str, name: str, index: int) -> bool:
    if program.methods_by_name.get(name):
        return False
    return program.resolve_method(path, name, index).kind == "library"


_ASSIGNMENTS = frozenset(
    ["=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">="]
)  # `>>=` and `>>>=` lex as `>` tokens followed by `>=`


def _throws_skip_local(unit: CompilationUnit, body: Block, statement: Statement) -> bool:
    """`throw x;` where the local x is assigned exactly once in the whole body, by its own
    declaration, and that initializer is nothing but `new <skip exception>(...)`."""
    tokens = unit.tokens[statement.start : statement.end]
    if len(tokens) != 3 or tokens[1].kind != "ident":
        return False
    name = tokens[1].text
    all_tokens = unit.tokens
    sites = [
        i
        for i in range(body.start, body.end - 1)
        if all_tokens[i].text == name
        and all_tokens[i].kind == "ident"
        and all_tokens[i + 1].text in _ASSIGNMENTS
        and all_tokens[i + 1].kind == "op"
        and all_tokens[i - 1].text != "."
    ]
    if len(sites) != 1:
        return False
    at = sites[0]
    before = all_tokens[at - 1]
    if all_tokens[at + 1].text != "=" or not (
        before.kind in ("ident", "keyword") or before.text in (">", "]")
    ):
        return False  # not a declaration with a plain initializer
    if all_tokens[at + 2].text != "new":
        return False
    m = at + 3
    while m + 2 < body.end and all_tokens[m + 1].text == ".":
        m += 2
    if all_tokens[m].text not in SKIP_EXCEPTIONS or all_tokens[m + 1].text != "(":
        return False
    close = _close(unit, m + 1)
    if all_tokens[close + 1].text not in (";", ","):
        return False
    # Any other mention may be a second binding (a catch, for each, lambda or pattern
    # variable), so the throw must be the only one, later in the declaring block.
    mentions = [
        i
        for i in range(body.start, body.end)
        if all_tokens[i].text == name
        and all_tokens[i].kind == "ident"
        and all_tokens[i - 1].text != "."
    ]
    if mentions != [at, statement.start + 1]:
        return False
    blocks = [b for b in walk(body) if isinstance(b, Block) and b.start < at < b.end]
    home = min(blocks, key=lambda b: b.end - b.start)
    return home.start < statement.start < home.end and any(
        s.kind == "local_var" and s.start <= at < s.end for s in home.statements
    )


def _exits(unit: CompilationUnit, body: Block) -> list[tuple[int, str]]:
    out = []
    for node in walk(body):
        if isinstance(node, Statement) and node.kind in _EXITS:
            words = {t.text for t in unit.tokens[node.start : node.end]}
            if node.kind == "throw" and (
                words & SKIP_EXCEPTIONS or _throws_skip_local(unit, body, node)
            ):
                continue  # judged as an assumption under Decision 6
            if "lambda" in _guard(unit, body, node.start) or "class" in _guard(
                unit, body, node.start
            ):
                continue
            # Mustafa 2026-10-23: an exit is its statement plus its full guard text, so an
            # existing exit under a changed condition counts as newly guarded.
            guard = " / ".join(_guard(unit, body, node.start))
            out.append((node.start, f"{_text(unit, node.start, node.end)} @ {guard}"))
    return out


def _top_operators(unit: CompilationUnit, start: int, end: int) -> list[tuple[int, str]]:
    """Operators at the top level of an expression, other than member access and brackets."""
    out, depth = [], 0
    for i in range(start, end):
        token = unit.tokens[i]
        if token.kind == "op" and token.text in "([{":
            depth += 1
        elif token.kind == "op" and token.text in ")]}":
            depth -= 1
        elif depth == 0 and (
            (token.kind == "op" and token.text not in (".", "::")) or token.text == "instanceof"
        ):
            out.append((i, token.text))
    return out


def _equality_operands(unit: CompilationUnit, start: int, end: int) -> tuple[str, str] | None:
    """`a == b` or `a.equals(b)` as the whole expression, with no other top level operator."""
    operators = _top_operators(unit, start, end)
    if [text for _, text in operators] == ["=="]:
        at = operators[0][0]
        if start < at < end - 1:
            return _text(unit, start, at), _text(unit, at + 1, end)
        return None
    if operators:
        return None
    tokens = unit.tokens
    if end - start >= 5 and tokens[end - 1].text == ")":
        for i in range(start, end - 3):
            if (
                tokens[i].text == "."
                and tokens[i + 1].text == "equals"
                and tokens[i + 2].text == "("
            ):
                if _close(unit, i + 2) == end - 1:
                    return _text(unit, start, i), _text(unit, i + 3, end - 1)
    return None


def _local_definitions(unit: CompilationUnit, body: Block) -> dict[str, set[str]]:
    """Each local's roots on the right of its declarations and assignments. A value with no
    root that is not a literal, such as `f()`, stands for itself, as `#name`."""
    out: dict[str, set[str]] = {}
    for node in walk(body):
        if not isinstance(node, Statement) or node.kind not in ("local_var", "expression"):
            continue
        tokens = unit.tokens[node.start : node.end]
        for i, token in enumerate(tokens[:-1]):
            if token.kind == "ident" and tokens[i + 1].text == "=" and tokens[i + 1].kind == "op":
                rhs = " ".join(t.text for t in tokens[i + 2 :])
                roots = roots_of((rhs,))
                if not roots and not _literal_expression(rhs.rstrip(" ;")):
                    roots = {f"#{token.text}"}
                out.setdefault(token.text, set()).update(roots)
    return out


def _in_type_arguments(tokens, i: int) -> bool:
    """Whether token i sits between a `<` that follows a type name and its `>`."""
    depth, j = 0, i - 1
    while j >= 0:
        text = tokens[j].text
        if text == ">":
            depth += 1
        elif text == "<":
            if depth == 0:
                return j > 0 and tokens[j - 1].kind == "ident" and tokens[j - 1].text[:1].isupper()
            depth -= 1
        elif text in ("(", ")", ";", "{", "}", "=", "&&", "||"):
            return False
        j -= 1
    return False


def roots_of(arguments: tuple[str, ...]) -> set[str]:
    """The variable, field and receiver roots an expression reads; method names and names
    after a dot are not roots, except a field read through `this`."""
    out = set()
    for arg in arguments:
        try:
            tokens = tokenize(_without_literals(arg))
        except Unparsable:
            out.add(f"#{arg}")
            continue
        for i, token in enumerate(tokens):
            if token.kind != "ident":
                continue
            before = tokens[i - 1].text if i else ""
            after = tokens[i + 1].text if i + 1 < len(tokens) else ""
            if after == "(" or before in ("::", "new"):
                continue  # a call, or a constructed type
            if token.text[:1].isupper() and after in (".", "<"):
                continue  # a type that qualifies a member or takes type arguments
            if token.text[:1].isupper() and (before in ("<", ",") or after in (">", ",")):
                if _in_type_arguments(tokens, i):
                    continue
            if before == "." and not (i >= 2 and tokens[i - 2].text == "this"):
                continue
            out.add(token.text)
    return out


def _followed(ids: set[str], definitions: dict[str, set[str]]) -> frozenset[str]:
    """Identifiers with locals replaced by what defines them, so a local set only from
    literals stands for no identifier at all."""
    out, pending, seen = set(), list(ids), set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in definitions:
            pending.extend(definitions[name])
        else:
            out.add(name)
    return frozenset(out)


def declared_waits(program: Program) -> frozenset[str]:
    return frozenset(
        name for name in program.methods_by_name if name in WAIT_CONDITIONS | AWAIT_ROOTS
    )


def collect(program: Program, path: str, body: Block) -> list[Assertion]:
    unit = program.units[path]
    declared = declared_waits(program)
    definitions = _local_definitions(unit, body)
    exits = _exits(unit, body)
    facts = _facts(program, path, body)
    rules = _rule_fields(program, path)
    out: list[Assertion] = []
    consumed: set[int] = set()
    for call in calls_in(unit, body):
        rule = rules.get(call.receiver) if call.receiver else None
        if rule is not None and _shadowed(unit, call.start, call.receiver):
            rule = None  # a local of the same name shadows the rule field
        if call.name == "expect" and rule and rule[0] == "ExpectedException" and call.arity == 1:
            arguments = _split_arguments(unit, call.start + 1, _close(unit, call.start + 1))
            out.append(Assertion(
                f"assertThrows ( {arguments[0]} )", ("assertThrows",), arguments,
                _guard(unit, body, call.start, declared),
                frozenset(t for at, t in exits if at < call.start), rule[1],
                unit.tokens[call.start].line, call.start, facts=facts,
            ))  # fmt: skip
            continue
        other = _other_form(program, path, call, rule)
        if call.start in consumed or call.reference:
            continue
        if other is None and not is_assertion_name(call.name):
            continue
        if _declared_helper(program, path, call):
            continue  # a helper the checker reads: its assertions come in by following it
        if (
            other is None
            and call.receiver not in (None, "<expr>")
            and not call.receiver[:1].isupper()
        ):
            continue
        names, end = _chain(unit, call)
        start = call.receiver_at if call.receiver not in (None, "<expr>") else call.start
        while (
            start >= 2
            and unit.tokens[start - 1].text == "."
            and (unit.tokens[start - 2].kind == "ident")
        ):
            start -= 2  # a fully qualified class name belongs to the assertion's text
        for inner in calls_in(unit, _Span(call.start + 1, end)):
            # A call inside a lambda handed to the chain, such as untilAsserted's, stays an
            # assertion of its own.
            if not any(isinstance(n, Lambda) and n.start > call.start
                       for n in path_to(unit, inner.start)):  # fmt: skip
                consumed.add(inner.start)
        matchers = tuple(
            inner.name
            for inner in calls_in(unit, _Span(call.start + 1, end))
            if inner.name in MATCHER_FACTORIES
            and (
                (inner.receiver is None and _library_call(program, path, inner.name, inner.start))
                or _library_receiver(program, path, unit, inner)
                or _timed_mode(program, path, unit, inner)
            )
        )
        credited = (
            other
            if other is not None
            else (
                _library_call(program, path, call.name, call.start)
                or _library_receiver(program, path, unit, call)
            )
        )
        out.append(
            Assertion(
                text=_text(unit, start, end),
                signature=_with_default_mode(
                    (*(n for n in names if n not in _DESCRIPTIONS), *matchers),
                    _mode_argument(unit, call, names),
                ),
                arguments=_split_arguments(unit, call.start + 1, _close(unit, call.start + 1)),
                guard=_guard(unit, body, call.start, declared),
                exits_before=frozenset(text for at, text in exits if at < call.start),
                credited=credited,
                line=unit.tokens[call.start].line,
                start=call.start,
                subject=_followed(
                    roots_of(_split_arguments(unit, call.start + 1, _close(unit, call.start + 1))),
                    definitions,
                ),
                facts=facts
                | _slot_fact(program, path, unit, call)
                | _mode_trust(program, path, unit, call)
                | _definition_facts(program, path, body, start, end),
            )
        )
    for node in walk(body):
        span = _lambda_value(node) if isinstance(node, Lambda) else None
        if span is not None and _wait_lambda(unit, node, declared):
            if unit.tokens[node.start - 2].text != "until":
                continue
            first, last = span
            expression = _text(unit, first, last)
            operands = _equality_operands(unit, first, last)
            after = unit.tokens[node.end].text if node.end < len(unit.tokens) else ""
            if after == ",":
                expected = _equal_to_argument(program, path, unit, node.end + 1)
                operands = (expected, expression) if expected is not None else None
            elif after != ")":
                operands = None
            elif operands is None:
                negated = unit.tokens[first].text == "!" and not _top_operators(
                    unit, first + 1, last
                )
                name, condition = ("assertFalse", _text(unit, first + 1, last)) if negated else (
                    "assertTrue", expression)  # fmt: skip
                out.append(_wait_assertion(unit, body, declared, exits, first, name,
                                           (condition,), node.start))  # fmt: skip
            if operands:
                out.append(_wait_assertion(unit, body, declared, exits, first,
                                           "assertEquals", operands, node.start))  # fmt: skip
    for call in calls_in(unit, body):
        tokens = unit.tokens
        at = call.receiver_at
        if (
            call.reference
            and at is not None
            and at >= 2
            and tokens[at - 1].text == "("
            and tokens[at - 2].text == "until"
            and tokens[call.end].text in (")", ",")
            and (tokens[at].kind == "ident" or tokens[at].text == "this")
            and _chain_root(unit, at - 2) in AWAIT_ROOTS
            and _chain_root(unit, at - 2) not in declared
        ):
            value = f"{_text(unit, at, at + 1)} . {call.name} ( )"
            if tokens[call.end].text == ")":
                out.append(_wait_assertion(unit, body, declared, exits, at, "assertTrue",
                                           (value,), at))  # fmt: skip
                continue
            expected = _equal_to_argument(program, path, unit, call.end + 1)
            if expected is not None:
                out.append(_wait_assertion(unit, body, declared, exits, at, "assertEquals",
                                           (expected, value), at))  # fmt: skip
    for node in walk(body):
        if isinstance(node, Statement) and node.kind == "try" and node.blocks:
            statements = node.blocks[0].statements
            last = statements[-1] if statements else None
            if last is None or last.kind != "expression" or not _ends_in_fail(unit, last):
                continue
            credited = _fail_resolves(program, path, unit, last)
            # One expectation per try, listing every caught type, so a type added beside the
            # old one is a broadening, not an added assertion.
            types = tuple(
                f"{' . '.join(caught.split('.'))} . class"
                for catch in node.catches
                for caught in catch.types
            )
            if types:
                out.append(Assertion(
                    f"assertThrows ( {' , '.join(types)} )", ("assertThrows",), types,
                    _guard(unit, body, node.start, declared),
                    frozenset(t for at, t in exits if at < node.start), credited,
                    node.catches[0].line, node.catches[0].start, facts=facts,
                ))  # fmt: skip
    for node in walk(body):
        if isinstance(node, Statement) and node.kind == "assert":
            out.append(
                Assertion(_text(unit, node.start, node.end), ("assert",), (), _guard(unit, body,
                          node.start), frozenset(t for at, t in exits if at < node.start), True,
                          node.line, node.start)
            )  # fmt: skip
    return out


def _wait_assertion(unit, body, declared, exits, start, name, arguments, before) -> Assertion:
    """The assertion an Awaitility condition stands for under kind 3, with the subject its
    condition reads, so a moved assertion keeps its subject."""
    return Assertion(
        f"{name} ( {' , '.join(arguments)} )", (name,), arguments,
        _guard(unit, body, start, declared), frozenset(t for at, t in exits if at < before),
        True, unit.tokens[start].line, start,
        subject=_followed(roots_of(arguments), _local_definitions(unit, body)),
    )  # fmt: skip


def _equal_to_argument(program: Program, path: str, unit: CompilationUnit, i: int) -> str | None:
    """X in a closing `equalTo(X))`, `is(X))` or `is(equalTo(X)))` argument that resolves to
    Hamcrest; `is` of a value is `is(equalTo(value))`."""
    tokens = unit.tokens
    if tokens[i].text not in ("equalTo", "is") or tokens[i + 1].text != "(":
        return None
    close = _close(unit, i + 1)
    if tokens[close + 1].text != ")" or not _library_call(program, path, tokens[i].text, i):
        return None
    if tokens[i].text == "is" and tokens[i + 2].text in ("equalTo", "is"):
        inner = _equal_to_argument(program, path, unit, i + 2)
        return inner if inner is not None and _close(unit, i + 3) == close - 1 else None
    if tokens[i].text == "is" and tokens[i + 2].text in MATCHER_FACTORIES:
        return None  # is(matcher) is that matcher, not an equality
    return _text(unit, i + 2, close)


def _lambda_value(lam: Lambda) -> tuple[int, int] | None:
    """The token span of what a lambda returns: its expression, or a block whose only statement
    is `return c;`."""
    if lam.expression is not None:
        return lam.expression.start, lam.expression.end
    if lam.body is not None and len(lam.body.statements) == 1:
        only = lam.body.statements[0]
        if only.kind == "return" and only.end - only.start > 2:
            return only.start + 1, only.end - 1
    return None


def _fail_resolves(program: Program, path: str, unit: CompilationUnit, last: Statement) -> bool:
    """Whether the `fail` ending a try block is a test library's, as credit needs."""
    if program.methods_by_name.get("fail"):
        return False
    tokens = unit.tokens
    if tokens[last.start].text == "fail":
        return _library_call(program, path, "fail", last.start)
    receiver = tokens[last.start].text
    return program.resolve_type(path, receiver, last.start).kind == "library"


def _ends_in_fail(unit: CompilationUnit, statement: Statement) -> bool:
    words = [t.text for t in unit.tokens[statement.start : statement.end]]
    return (words[:2] == ["fail", "("]) or (words[1:4] == [".", "fail", "("])


# Rule types whose fields assert: the call names that do, by the field's type.
_RULE_CALLS = {
    "ExpectedException": frozenset(["expect"]),
    "ErrorCollector": frozenset(["checkThat", "checkSucceeds"]),
    "Expect": frozenset(["that", "withMessage", "about"]),
}
_BDD_OWNERS = frozenset(["BDDAssertions", "BDDMockito"])
_NEEDS_RULE = frozenset(["ExpectedException", "ErrorCollector"])


def _is_rule(program: Program, path: str, field) -> bool:
    """A field annotated with org.junit.Rule or org.junit.ClassRule. On the old side a bare
    Rule or ClassRule that nothing else names also counts under `import org.junit.*;`, so the
    checks of a rule field are seen removed; the new side needs the name resolved."""
    on_demand = program.old_side and any(
        imp.wildcard and not imp.static and imp.name == "org.junit"
        for imp in program.units[path].imports
    )
    for annotation in field.annotations:
        binding = program.resolve_type(path, annotation.name, annotation.start)
        if binding.kind == "library" and binding.fqn in ("org.junit.Rule", "org.junit.ClassRule"):
            return True
        if on_demand and binding.kind == "unresolved" and annotation.name in ("Rule", "ClassRule"):
            return True
    return False


_CLOSERS = {">": 1, ">>": 2, ">>>": 3}


def _declared_names(tokens, start: int, end: int) -> set[str]:
    return {tokens[i].text for i in _declarators(tokens, start, end)}


def _declarators(tokens, start: int, end: int) -> list[int]:
    """Where the names a declaration in tokens[start:end] binds sit, as in
    `Map<K, V> a = f(), b[]` or `final T x : xs`: each name its type or an earlier declarator
    leads to, outside every initializer and type argument list."""
    out: list[int] = []
    depth, angle, initializer, previous = 0, 0, False, None
    for i in range(start, end):
        text = tokens[i].text
        if text in ("(", "[", "{"):
            depth += 1
        elif text in (")", "]", "}"):
            depth -= 1
        elif depth == 0 and not initializer and text == "<":
            angle += 1
        elif depth == 0 and not initializer and text in _CLOSERS:
            angle -= _CLOSERS[text]
        elif depth == 0 and angle == 0 and text == "=":
            initializer = True
        elif depth == 0 and angle == 0 and text == ",":
            initializer = False
        elif depth == 0 and angle == 0 and not initializer and tokens[i].kind == "ident":
            following = tokens[i + 1].text if i + 1 < end else ";"
            leads = previous is not None and (
                previous.kind == "ident" or previous.text in _PRIMITIVES
                or previous.text in (">", ">>", ">>>", "]")
                or (previous.text == "," and out)
            )  # fmt: skip
            if leads and following in ("=", ",", ";", ":", "["):
                out.append(i)
        previous = tokens[i]
    return out


def _segments(tokens, span, separators: tuple[str, ...]) -> list[tuple[int, int]]:
    """The top level pieces of a parenthesized span, split at the separators."""
    pieces, depth, piece = [], 0, span.start + 1
    for i in range(span.start + 1, span.end - 1):
        text = tokens[i].text
        depth += text in ("(", "[", "{")
        depth -= text in (")", "]", "}")
        if depth == 0 and text in separators:
            pieces.append((piece, i))
            piece = i + 1
    pieces.append((piece, span.end - 1))
    return pieces


def _ends_a_type(tokens, j: int) -> bool:
    """Whether the tokens up to j end a type, as in `List<Map<K, V>>` or `int[]`, so the name
    after them is declared rather than read after a comparison such as `x() > name`."""
    while j >= 1 and tokens[j].text == "]" and tokens[j - 1].text == "[":
        j -= 2
    if j < 0 or tokens[j].text == "]":
        return False
    if tokens[j].kind == "ident" or tokens[j].text in _PRIMITIVES:
        return True
    depth = 0
    while j >= 0 and (tokens[j].text in _CLOSERS or depth > 0):
        text = tokens[j].text
        if text in _CLOSERS:
            depth += _CLOSERS[text]
        elif text == "<":
            depth -= 1
            if depth == 0:
                return j > 0 and tokens[j - 1].kind == "ident"
        elif not (
            tokens[j].kind == "ident"
            or text in _PRIMITIVES
            or text in (".", ",", "?", "&", "[", "]", "extends", "super")
        ):
            return False
        j -= 1
    return False


def _shadowed(unit: CompilationUnit, at: int, name: str) -> bool:
    """Whether a declaration of name is in scope at token at: a parameter of an enclosing
    method or lambda, typed or not, an enclosing catch, for loop or try resource, or a local
    declared earlier in an enclosing block."""
    tokens = unit.tokens
    for node in path_to(unit, at):
        if isinstance(node, Method) and any(p.name == name for p in node.params):
            return True
        if isinstance(node, Lambda) and name in node.params:
            return True
        if isinstance(node, Catch) and node.name == name and node.block.start <= at:
            return True
        if isinstance(node, Block):
            for statement in node.statements:
                if statement.start >= at:
                    break
                if statement.kind == "local_var" and name in _declared_names(
                    tokens, statement.start, statement.end - 1
                ):
                    return True
        if isinstance(node, Statement) and node.kind == "for" and node.header is not None:
            first = _segments(tokens, node.header, (";",))[0]
            if name in _declared_names(tokens, *first):
                return True
        if isinstance(node, Statement) and node.kind == "try" and node.resources is not None:
            if node.blocks and node.blocks[0].start <= at < node.blocks[0].end:
                for piece in _segments(tokens, node.resources, (";",)):
                    if name in _declared_names(tokens, *piece):
                        return True
    return False


def _rule_fields(program: Program, path: str) -> dict[str, tuple[str, bool]]:
    """Fields of the JUnit 4 ExpectedException and ErrorCollector rule types and of Truth's
    Expect, with whether the type resolves to its test library. An ExpectedException or
    ErrorCollector field without `@Rule` or `@ClassRule` checks nothing (JUnit 4.13.2), so it is
    left out; an Expect without its rule fails the test, so it stays."""
    key = ("rule_fields", path)
    if key not in program.memo:
        out = {}
        for decl in program.all_types(path):
            for field in decl.fields:
                simple = field.type.rsplit(".", 1)[-1]
                if simple not in _RULE_CALLS:
                    continue
                if simple in _NEEDS_RULE and not _is_rule(program, path, field):
                    continue
                credited = (
                    is_library(field.type) and field.type not in program.declared_fqns
                ) or program.resolve_type(path, simple, field.start).kind == "library"
                for name in field.names:
                    out[name] = (simple, credited)
        program.memo[key] = out
    return program.memo[key]


def _other_form(program: Program, path: str, call: Call, rule) -> bool | None:
    """Credit for an assertion outside the assert name list: AssertJ and BDDMockito `then`,
    and the asserting calls of a rule field. None when the call is not one of these."""
    if rule is not None and rule[0] != "ExpectedException":
        return rule[1] if call.name in _RULE_CALLS[rule[0]] else None
    if call.name != "then":
        return None
    if call.receiver is None:
        return True if _library_call(program, path, "then", call.start) else None
    if call.receiver in _BDD_OWNERS and call.receiver_at is not None:
        unit = program.units[path]
        return True if _qualified_library(program, path, unit, call.receiver_at) else None
    return None


def _number(text: str) -> float | None:
    words = text.split()
    sign = -1.0 if words[:1] == ["-"] else 1.0
    words = words[1:] if words[:1] in (["-"], ["+"]) else words
    if len(words) != 1:
        return None
    try:
        return sign * float(words[0].rstrip("dDfFlL").replace("_", ""))
    except ValueError:
        return None


def _assignment_texts(
    program: Program, path: str, start: int = 0, end: int | None = None
) -> dict[str, tuple[str, ...]]:
    """Every value assigned to each name in a token range (the whole file by default),
    initializers included, in order."""
    tokens = program.units[path].tokens
    end = len(tokens) if end is None else end
    key = ("assignment_texts", path, start, end)
    if key not in program.memo:
        out: dict[str, list[str]] = {}
        for i in range(start, end - 1):
            token, after = tokens[i], tokens[i + 1]
            if token.kind != "ident" or after.kind != "op" or after.text not in _ASSIGNMENTS:
                continue
            depth, j = 0, i + 2
            while j < len(tokens):
                text = tokens[j].text if tokens[j].kind == "op" else ""
                if text in ("(", "[", "{"):
                    depth += 1
                elif text in (")", "]", "}"):
                    if depth == 0:
                        break
                    depth -= 1
                elif text in (";", ",") and depth == 0:
                    break
                j += 1
            value = " ".join(t.text for t in tokens[i + 1 : j])
            out.setdefault(token.text, []).append(value)
        program.memo[key] = {name: tuple(found) for name, found in out.items()}
    return program.memo[key]


def _definition_facts(program: Program, path: str, body: Block, start: int, end: int) -> frozenset:
    """For each name the assertion reads, every value assigned to it: within the method for a
    name the method declares, across the file otherwise. An assertion whose text is unchanged
    but whose tolerance, bound or mode local changed is then not a twin."""
    unit = program.units[path]
    file_wide = _assignment_texts(program, path)
    local = _assignment_texts(program, path, body.start, body.end)
    names = {
        unit.tokens[i].text
        for i in range(start, end)
        if unit.tokens[i].kind == "ident" and unit.tokens[i - 1].text != "."
    }
    out = set()
    for name in names:
        texts = local.get(name) if _shadowed(unit, start, name) else file_wide.get(name)
        if texts:
            out.add(("defs", name, texts))
    return frozenset(out)


def _defs(a: Assertion) -> frozenset:
    return frozenset(f for f in a.facts if f[0] == "defs")


def _resolved_argument(a: Assertion, text: str) -> str:
    """A name assigned exactly once in the file stands for what it is assigned."""
    found = [f[2] for f in a.facts if f[0] == "defs" and f[1] == text]
    if found and len(found[0]) == 1 and found[0][0].startswith("= "):
        return found[0][0][2:]
    return text


def _assignment_counts(tokens, start: int, end: int) -> dict[str, int]:
    """How often each name is the target of an assignment, `++` or `--` in a token range,
    qualified or not, so `this.eps = 0.5` counts against `eps`."""
    out: dict[str, int] = {}
    for i in range(start, end):
        token = tokens[i]
        if token.kind != "ident":
            continue
        after = tokens[i + 1].text if i + 1 < end else ""
        before = tokens[i - 1].text if i else ""
        if after in _ASSIGNMENTS or after in ("++", "--") or before in ("++", "--"):
            out[token.text] = out.get(token.text, 0) + 1
    return out


def _field_facts(program: Program, path: str) -> tuple[dict, dict, dict]:
    """Each field name's literal values and declared types. A field keeps a value only when it
    is declared once and nothing in the file assigns it again."""
    key = ("field_facts", path)
    if key not in program.memo:
        unit = program.units[path]
        assigned = _assignment_counts(unit.tokens, 0, len(unit.tokens))
        values: dict[str, list] = {}
        types: dict[str, list] = {}
        literals: dict[str, list] = {}
        for decl in (n for n in walk(unit) if isinstance(n, TypeDecl)):
            for field in decl.fields:
                for name in field.names:
                    span = field.initializers.get(name)
                    text = _text(unit, span.start, span.end) if span else ""
                    reassigned = assigned.get(name, 0) > (1 if span else 0)
                    values.setdefault(name, []).append(None if reassigned else _number(text))
                    types.setdefault(name, []).append(field.type)
                    literals.setdefault(name, []).append(
                        bool(span) and not reassigned and _literal_expression(text)
                    )
        program.memo[key] = (values, types, literals)
    return program.memo[key]


def _facts(program: Program, path: str, body: Block) -> frozenset:
    """Names bound only to a numeric literal, and names every declaration of which in scope has
    a Set type or a String type, from the file's fields and the method's parameters and locals.
    A name declared more than once, or assigned, incremented or decremented after its
    declaration, has no value."""
    unit = program.units[path]
    field_values, field_types, field_literals = _field_facts(program, path)
    literals = {name: list(found) for name, found in field_literals.items()}
    values = {name: list(found) for name, found in field_values.items()}
    types: dict[str, list] = {name: list(found) for name, found in field_types.items()}
    assigned = _assignment_counts(unit.tokens, body.start, body.end)
    method = next((n for n in reversed(path_to(unit, body.start)) if isinstance(n, Method)), None)
    if method is not None and method.body is body:
        for param in method.params:
            values.setdefault(param.name, []).append(None)
            types.setdefault(param.name, []).append(param.type)
            literals.setdefault(param.name, []).append(False)
    recorded: set[int] = set()
    for node in walk(body):
        if isinstance(node, Catch):
            types.setdefault(node.name, []).append(None)
            literals.setdefault(node.name, []).append(False)
        elif isinstance(node, Lambda):
            for name in node.params:
                values.setdefault(name, []).append(None)
                types.setdefault(name, []).append(None)
                literals.setdefault(name, []).append(False)
        elif isinstance(node, Statement) and node.kind == "local_var":
            tokens = unit.tokens[node.start : node.end]
            declared = {
                at - node.start for at in _declarators(unit.tokens, node.start, node.end - 1)
            }
            for i, token in enumerate(tokens[:-1]):
                follows = tokens[i + 1].text
                if i not in declared or follows not in ("=", ";", ","):
                    continue  # not a declared name, such as a name read in an initializer
                end = i + 2
                while end < len(tokens) and tokens[end].text not in (",", ";"):
                    end += 1
                initialized = follows == "="
                recorded.add(node.start + i)
                value = _number(" ".join(t.text for t in tokens[i + 2 : end]))
                reassigned = assigned.get(token.text, 0) > (1 if initialized else 0)
                values.setdefault(token.text, []).append(
                    value if initialized and not reassigned else None
                )
                initializer = " ".join(t.text for t in tokens[i + 2 : end])
                literals.setdefault(token.text, []).append(
                    initialized and not reassigned and _literal_expression(initializer)
                )
                types.setdefault(token.text, []).append(" ".join(t.text for t in tokens[:i]))
    # Any other `Type name` pair declares the name again (a for header, a resource, a pattern),
    # so the name keeps no fact.
    tokens = unit.tokens
    for i in range(body.start + 1, body.end - 1):
        token = tokens[i]
        if token.kind != "ident" or i in recorded or tokens[i + 1].text == "(":
            continue
        if _ends_a_type(tokens, i - 1):
            values.setdefault(token.text, []).append(None)
            types.setdefault(token.text, []).append(None)
            literals.setdefault(token.text, []).append(False)
    out = set()
    for name, found in literals.items():
        if len(found) == 1 and found[0]:
            out.add(("literal", name))
    for name, found in types.items():
        if found and all(t is not None and _set_type(t) for t in found):
            out.add(("set", name))
        if found and all(t is not None and _base_type(t) == "String" for t in found):
            out.add(("string", name))
    for name, found in values.items():
        if len(found) == 1 and found[0] is not None:
            out.add(("value", name, found[0]))
    return frozenset(out)


def _base_type(type_text: str) -> str:
    """The simple name of a declared type, without modifiers, annotations or type arguments."""
    words = [w for w in type_text.replace("<", " < ").split() if w not in ("final",)]
    base = next((w for w in words if not w.startswith("@")), "")
    return base.replace(" ", "").rsplit(".", 1)[-1]


def _set_type(type_text: str) -> bool:
    return _base_type(type_text).endswith("Set")


def _value(a: Assertion, text: str) -> float | None:
    number = _number(text)
    if number is not None:
        return number
    name = text.strip()
    found = [f[2] for f in a.facts if f[0] == "value" and f[1] == name]
    return found[0] if found else None


_MODES = frozenset(
    ["times", "never", "atLeast", "atLeastOnce", "atMost", "atMostOnce", "only", "calls"]
)


def _with_default_mode(signature: tuple[str, ...], mode: str | None) -> tuple[str, ...]:
    """A Mockito verification with no mode verifies `times(1)`, and so does one under a bare
    `timeout(n)` or `after(n)` (Mockito 5.24.0), so the implicit mode joins the signature. Any
    other mode argument, such as a project's own VerificationMode, gets no implicit mode."""
    verifies = signature[0] == "verify" or (signature[0] == "then" and "should" in signature)
    if verifies and not _MODES & set(signature) and (mode is None or _bare_timed(mode)):
        return (*signature, "times")
    return signature


def _bare_timed(mode: str) -> bool:
    """`timeout(n)` or `after(n)`, optionally qualified by `Mockito.`, with nothing chained."""
    words = mode.split()
    if words[:2] == ["Mockito", "."]:
        words = words[2:]
    if len(words) < 3 or words[0] not in _TIMED or words[1] != "(" or words[-1] != ")":
        return False
    depth = 0
    for i, word in enumerate(words[1:], start=1):
        depth += word == "("
        depth -= word == ")"
        if depth == 0:
            return i == len(words) - 1
    return False


def _mode_argument(unit: CompilationUnit, call: Call, names: list[str]) -> str | None:
    """The verification mode argument: verify's second argument, or what should is given."""
    if call.name == "verify":
        arguments = _split_arguments(unit, call.start + 1, _close(unit, call.start + 1))
        return arguments[1] if len(arguments) > 1 else None
    if call.name == "then" and "should" in names:
        names_end = _chain(unit, call)[1]
        for i in range(call.start, names_end):
            if unit.tokens[i].text == "should" and unit.tokens[i + 1].text == "(":
                close = _close(unit, i + 1)
                return _text(unit, i + 2, close) or None
    return None


# AssertJ calls that only describe a failure; they change no check.
_DESCRIPTIONS = frozenset(
    ["as", "describedAs", "withFailMessage", "overridingErrorMessage", "withRepresentation"]
)
_TIMED = frozenset(["timeout", "after"])


def _timed_mode(program: Program, path: str, unit: CompilationUnit, call: Call) -> bool:
    """A Mockito mode chained on `timeout(n)` or `after(n)`, as in `timeout(100).times(2)`."""
    tokens = unit.tokens
    if call.receiver != "<expr>" or tokens[call.start - 2].text != ")":
        return False
    depth, j = 0, call.start - 2
    while j > 0:
        if tokens[j].kind == "op" and tokens[j].text == ")":
            depth += 1
        elif tokens[j].kind == "op" and tokens[j].text == "(":
            depth -= 1
            if depth == 0:
                break
        j -= 1
    name_at = j - 1
    if tokens[name_at].text not in _TIMED:
        return False
    if tokens[name_at - 1].text == ".":
        return _qualified_library(program, path, unit, name_at - 2)
    return _library_call(program, path, tokens[name_at].text, name_at)


def _qualified_name(unit: CompilationUnit, last: int) -> str:
    """The dotted name ending at the identifier token at last, such as `org.mockito.Mockito`."""
    tokens, parts, j = unit.tokens, [unit.tokens[last].text], last
    while j >= 2 and tokens[j - 1].text == "." and tokens[j - 2].kind == "ident":
        parts.append(tokens[j - 2].text)
        j -= 2
    return ".".join(reversed(parts))


def _qualified_library(program: Program, path: str, unit: CompilationUnit, at: int) -> bool:
    """Whether the type name ending at token at is a test library class, by its fully
    qualified spelling or by resolution."""
    if at < 0 or unit.tokens[at].kind != "ident":
        return False
    name = _qualified_name(unit, at)
    if "." in name and name in program.declared_fqns:
        return False
    if "." in name and is_library(name):
        return True
    return program.resolve_type(path, unit.tokens[at].text, at).kind == "library"


def _library_receiver(program: Program, path: str, unit: CompilationUnit, call: Call) -> bool:
    """A factory called on a test library class, as in `Mockito.times(1)`, which is the same
    method as the statically imported `times(1)`."""
    if call.receiver in (None, "<expr>") or call.receiver_at is None:
        return False
    if program.methods_by_name.get(call.name):
        return False
    return _qualified_library(program, path, unit, call.receiver_at)


@dataclass(frozen=True)
class _Span:
    start: int
    end: int


def _origin(program: Program, path: str, method: Method) -> tuple:
    owner = program.method_owner.get(id(method), "<local>")
    return (path, owner, method.name, tuple(p.type for p in method.params))


def _context(program: Program, reached, cache: dict) -> tuple[tuple[str, ...], frozenset[str]]:
    """The guard and the exits that sit between the root method and a reached helper, joined
    along the chain of call sites that reached it."""
    key = id(reached)
    if key in cache:
        return cache[key]
    if reached.parent is None or reached.call is None:
        result: tuple[tuple[str, ...], frozenset[str]] = ((), frozenset())
    else:
        parent_guard, parent_exits = _context(program, reached.parent, cache)
        unit = program.units[reached.parent.path]
        body = reached.parent.method.body
        site = reached.call.start
        guard = parent_guard + _guard(unit, body, site, declared_waits(program))
        exits = parent_exits | {text for at, text in _exits(unit, body) if at < site}
        result = (guard, exits)
    cache[key] = result
    return result


def _with_helpers(
    program: Program, path: str, method: Method, scan: bool, budget: ExpansionBudget
) -> list[Assertion]:
    out = []
    collected: list[tuple[str, int, int]] = []
    cache: dict = {}
    for reached in reach(program, [(path, method)], scan=scan, budget=budget):
        body = reached.method.body
        if body is None:
            continue
        if any(p == reached.path and s <= body.start < e for p, s, e in collected):
            continue  # declared inside a body already collected, such as an anonymous class
        collected.append((reached.path, body.start, body.end))
        guard, exits = _context(program, reached, cache)
        origin = _origin(program, reached.path, reached.method)
        for found in collect(program, reached.path, body):
            out.append(
                Assertion(
                    found.text, found.signature, found.arguments, guard + found.guard,
                    exits | found.exits_before, found.credited, found.line, found.start,
                    False, origin, found.subject, found.facts,
                )
            )  # fmt: skip
    return out


def _as_scanned(assertion: Assertion) -> Assertion:
    return Assertion(
        assertion.text, assertion.signature, assertion.arguments, assertion.guard,
        assertion.exits_before, False, assertion.line, assertion.start, True, assertion.origin,
        assertion.subject, assertion.facts,
    )  # fmt: skip


def _both_ways(program: Program, path: str, method: Method, budget: ExpansionBudget):
    """Helpers followed as credit is, plus those only a rejection scan reaches, marked so.

    Before the patch the union is exactly the scan reach Decision 5 asks for. After it, an
    assertion reached only by name may stand in for one that was also reached only by name, in
    the same helper; where the before side reached a helper through a resolved call, the after
    side must too."""
    credited = _with_helpers(program, path, method, False, budget)
    seen = {(a.text, a.origin) for a in credited}
    extra = [
        _as_scanned(a)
        for a in _with_helpers(program, path, method, True, budget)
        if (a.text, a.origin) not in seen
    ]
    return credited + extra


def _is_subsequence(small: tuple[str, ...], big: tuple[str, ...]) -> bool:
    it = iter(big)
    return all(item in it for item in small)


_CONSTANT_WORDS = frozenset(["true", "false", "null"])


def _strip_parens(arg: str) -> str:
    while arg.startswith("( ") and arg.endswith(" )") and _balanced(arg[2:-2]):
        arg = arg[2:-2]
    return arg


def _balanced(text: str) -> bool:
    depth = 0
    for word in text.split():
        depth += word == "("
        depth -= word == ")"
        if depth < 0:
            return False
    return depth == 0


def _without_literals(arg: str) -> str:
    """The argument text with string and character literals blanked out."""
    out, i, quote = [], 0, None
    while i < len(arg):
        char = arg[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
                out.append(" 0 ")
        elif char in "\"'":
            quote = char
        else:
            out.append(char)
        i += 1
    return "".join(out)


def _constant(arg: str) -> bool:
    """No identifier anywhere: a literal or an expression over literals."""
    return not any(
        (w[:1].isalpha() or w[:1] in "_$") and w not in _CONSTANT_WORDS
        for w in _without_literals(arg).split()
    )


def _or_true(arg: str) -> bool:
    """A disjunction with a literal true at the top level, with `|` or `||`."""
    words, depth = arg.split(), 0
    for i, word in enumerate(words):
        depth += word == "("
        depth -= word == ")"
        if depth == 0 and word in ("|", "||"):
            if words[i - 1 : i] == ["true"] or words[i + 1 : i + 2] == ["true"]:
                return True
    return False


def _same_on_both_sides(arg: str) -> bool:
    for operator in (" == ", " != "):
        if operator in arg:
            left, _, right = arg.partition(operator)
            if _strip_parens(left) == _strip_parens(right):
                return True
    return False


# The fewest arguments each assertion takes without a message. JUnit 4 and TestNG put a
# message first, JUnit 5 last (or a supplier); anything beyond these is a delta or a message.
_PAYLOAD = {
    "assertEquals": 2, "assertNotEquals": 2, "assertSame": 2, "assertNotSame": 2,
    "assertArrayEquals": 2, "assertTrue": 1, "assertFalse": 1, "assertNull": 1,
    "assertNotNull": 1,
}  # fmt: skip


_FORMATS = (
    ["String", ".", "format", "("], ["java", ".", "lang", ".", "String", ".", "format", "("],
    ["MessageFormat", ".", "format", "("],
    ["java", ".", "text", ".", "MessageFormat", ".", "format", "("], ["0", ".", "formatted", "("],
)  # fmt: skip


def _is_message(arg: str, facts: frozenset = frozenset()) -> bool:
    """A String valued argument: a lone string literal, a concatenation that starts with one
    (Java evaluates `+` left to right, so the result is a String), a supplier lambda, a
    `String.format`, `MessageFormat.format` or `formatted` call, or a name every declaration of
    which is a String. A literal followed by anything else, such as `"0.5".length()`, is not."""
    if arg.startswith("( ) ->"):
        return True
    words = _without_literals(arg).split()
    if arg.startswith('"') and words[:1] == ["0"] and (len(words) == 1 or words[1] == "+"):
        return True
    for prefix in _FORMATS:
        if words[: len(prefix)] == prefix and (prefix[0] != "0" or arg.startswith('"')):
            depth = 0
            for i in range(len(prefix) - 1, len(words)):
                depth += words[i] == "("
                depth -= words[i] == ")"
                if depth == 0:
                    rest = words[i + 1 :]
                    return not rest or rest[0] == "+"
            return False
    return len(words) == 1 and ("string", words[0]) in facts


def payload(assertion: Assertion) -> tuple[str, ...]:
    """The arguments without a message argument. Where the assertion's class is known, only its
    message position is read; otherwise a literal or formatted message is read at either end,
    and a String typed name at neither, since it may be the value compared."""
    args = assertion.arguments
    least = _PAYLOAD.get(assertion.signature[0])
    if least is None:
        return args
    slot = next((f[1] for f in assertion.facts if f[0] == "slot"), None)
    facts = assertion.facts if slot else frozenset()
    if slot != "first" and len(args) > least and _is_message(args[-1], facts):
        args = args[:-1]
    if slot != "last" and len(args) > least and _is_message(args[0], facts):
        args = args[1:]
    return args


# Where each assertion class takes its message.
_MESSAGE_FIRST = frozenset(
    ["org.junit.Assert", "junit.framework.Assert", "junit.framework.TestCase",
     "org.testng.AssertJUnit", "org.junit.Assume", "org.skyscreamer.jsonassert.JSONAssert"]
)  # fmt: skip
_MESSAGE_LAST = frozenset(
    ["org.junit.jupiter.api.Assertions", "org.junit.jupiter.api.Assumptions", "org.testng.Assert"]
)


def _assertion_owner(program: Program, path: str, unit: CompilationUnit, call: Call) -> str | None:
    """The class an assertion call belongs to, when the file's names settle it."""
    if call.receiver not in (None, "<expr>") and call.receiver_at is not None:
        name = _qualified_name(unit, call.receiver_at)
        if "." in name:
            return name if is_library(name) and name not in program.declared_fqns else None
        binding = program.resolve_type(path, name, call.receiver_at)
        return binding.fqn if binding.kind == "library" else None
    if call.receiver is not None or program.methods_by_name.get(call.name):
        return None
    singles = [
        imp.name
        for imp in unit.imports
        if imp.static and not imp.wildcard and imp.name.rsplit(".", 1)[-1] == call.name
    ]
    if singles:
        return _owner(singles[0]) if len(singles) == 1 else None
    wild = [imp.name for imp in unit.imports if imp.static and imp.wildcard]
    if any(owner not in LIBRARY_MEMBERS for owner in wild):
        return wild[0] if len(wild) == 1 else None
    having = [owner for owner in wild if call.name in LIBRARY_MEMBERS[owner]]
    return having[0] if len(having) == 1 else None


def _candidate_owners(program: Program, unit: CompilationUnit, call: Call) -> frozenset[str]:
    """Every class an unqualified assertion call may belong to by the file's static imports,
    when they do not settle one."""
    if call.receiver is not None or program.methods_by_name.get(call.name):
        return frozenset()
    singles = {
        _owner(imp.name)
        for imp in unit.imports
        if imp.static and not imp.wildcard and imp.name.rsplit(".", 1)[-1] == call.name
    }
    wild = {
        imp.name
        for imp in unit.imports
        if imp.static
        and imp.wildcard
        and (imp.name not in LIBRARY_MEMBERS or call.name in LIBRARY_MEMBERS[imp.name])
    }
    return frozenset(singles | wild)


JSON_ASSERT = "org.skyscreamer.jsonassert.JSONAssert"
JSON_COMPARE_MODE = "org.skyscreamer.jsonassert.JSONCompareMode"
_JSON_MODES = frozenset(["LENIENT", "STRICT", "NON_EXTENSIBLE", "STRICT_ORDER"])


def _field_names(program: Program) -> frozenset[str]:
    key = ("field_names",)
    if key not in program.memo:
        program.memo[key] = frozenset(
            name
            for path in program.units
            for decl in program.all_types(path)
            for field in decl.fields
            for name in field.names
        )
    return program.memo[key]


def _mode_trust(program: Program, path: str, unit: CompilationUnit, call: Call) -> frozenset:
    """The spellings of the JSONCompareMode constants that name them at this call: qualified
    through a name that resolves to the class, fully qualified when no read file declares the
    class, or bare when a static import of the class supplies the name and no field, local or
    other static import can."""
    if call.arity < 3:
        return frozenset()
    out = set()
    qualified = program.resolve_type(path, "JSONCompareMode", call.start)
    for mode in _JSON_MODES:
        if qualified.kind == "library" and qualified.fqn == JSON_COMPARE_MODE:
            out.add(("trusted_mode", f"JSONCompareMode . {mode}"))
        if JSON_COMPARE_MODE not in program.declared_fqns:
            out.add(("trusted_mode", f"org . skyscreamer . jsonassert . JSONCompareMode . {mode}"))
        static = [imp for imp in unit.imports if imp.static]
        suppliers = {
            imp.name if imp.wildcard else _owner(imp.name)
            for imp in static
            if (
                imp.name.rsplit(".", 1)[-1] == mode
                if not imp.wildcard
                else imp.name not in LIBRARY_MEMBERS or mode in LIBRARY_MEMBERS[imp.name]
            )
        }
        if (
            suppliers == {JSON_COMPARE_MODE}
            and mode not in _field_names(program)
            and not _shadowed(unit, call.start, mode)
        ):
            out.add(("trusted_mode", mode))
    return frozenset(out)


def _slot_fact(program: Program, path: str, unit: CompilationUnit, call: Call) -> frozenset:
    """The assertion's resolved class, and where that class takes its message."""
    owner = _assertion_owner(program, path, unit, call)
    found = {("owner", owner)} if owner else set()
    found |= {("candidate", other) for other in _candidate_owners(program, unit, call)}
    if owner in _MESSAGE_FIRST:
        found.add(("slot", "first"))
    if owner in _MESSAGE_LAST:
        found.add(("slot", "last"))
    return frozenset(found)


def _trivially_true(assertion: Assertion) -> bool:
    name = assertion.signature[0]
    args = tuple(_strip_parens(a) for a in payload(assertion))
    if name in _LITERAL_JUDGED and args and _all_literal(assertion):
        return True
    if name in ("assertEquals", "assertSame") and len(args) >= 2:
        return _same_values(args[-1], args[-2])
    if name in ("assertTrue", "assertFalse") and args:
        condition = args[-1]
        passing = "true" if name == "assertTrue" else "false"
        branches = _branches(condition)
        return (
            _constant(condition)
            or _same_on_both_sides(condition)
            or _or_true(_without_literals(condition))
            or _equals_self(condition)
            or (branches is not None and passing in branches)
        )
    if name == "assertNotNull" and args:
        return _constant(args[-1]) or _whole_new(args[-1])
    if name in ("assertThat", "checkThat") and args:
        return _trivial_chain(assertion, args)
    return False


# Factories that build a value from literals only (approved 2026-10-24).
_LITERAL_FACTORIES = {
    "List": frozenset(["of"]), "Set": frozenset(["of"]), "Map": frozenset(["of", "entry"]),
    "Arrays": frozenset(["asList"]),
    "Collections": frozenset(["singletonList", "singleton", "emptyList", "emptySet",
                              "emptyMap"]),
}  # fmt: skip
_LITERAL_JUDGED = frozenset([*_PAYLOAD, "assertThat", "then", "checkThat"])


def _literal_tokens(tokens, names: frozenset[str], skip: frozenset[str] = frozenset()) -> bool:
    """Whether every value the tokens name is a literal: each name not after a dot is a name
    bound only to a literal, or a literal factory such as `List.of`; a member called on a literal
    is a literal. A creation, `this` or any other name is not."""
    for i, token in enumerate(tokens):
        if token.kind == "keyword" and token.text in ("new", "this", "super"):
            return False
        if token.kind != "ident" or (i and tokens[i - 1].text in (".", "::")):
            continue
        after = tokens[i + 1].text if i + 1 < len(tokens) else ""
        if token.text in skip and after == "(":
            continue
        if token.text in names:
            continue
        factory = _LITERAL_FACTORIES.get(token.text)
        if factory and after == "." and i + 2 < len(tokens) and tokens[i + 2].text in factory:
            continue
        return False
    return True


def _literal_expression(text: str, names: frozenset[str] = frozenset()) -> bool:
    if not text.strip():
        return False
    try:
        tokens = tokenize(_without_literals(text))
    except Unparsable:
        return False
    return _literal_tokens(tokens, names)


def _all_literal(assertion: Assertion) -> bool:
    """Every value an assertion compares, past its own call name, is a literal, so it can never
    fail or never pass whatever the code under test does."""
    try:
        tokens = tokenize(_without_literals(assertion.text))
    except Unparsable:
        return False
    first = next((i for i, t in enumerate(tokens) if t.text == "("), None)
    if first is None:
        return False
    names = frozenset(f[1] for f in assertion.facts if f[0] == "literal")
    return _literal_tokens(tokens[first:], names, MATCHER_FACTORIES | _DESCRIPTIONS)


def _same_values(a: str, b: str) -> bool:
    """Both sides the same expression, both literals, or a conditional on either side with a
    branch that is the other side, so the comparison can always pass. A conditional whose
    branches are both other values, such as a platform line ending, is a real expectation."""
    if a == b or (_constant(a) and _constant(b)):
        return True
    return any(
        branches is not None and other in branches
        for branches, other in ((_branches(a), b), (_branches(b), a))
    )


def _branches(text: str) -> tuple[str, str] | None:
    """The two results of a conditional `c ? x : y` at the top level of text."""
    try:
        tokens = tokenize(text)
    except Unparsable:
        return None
    depth, question, colon, nested = 0, None, None, 0
    for i, token in enumerate(tokens):
        if token.kind == "op" and token.text in "([{":
            depth += 1
        elif token.kind == "op" and token.text in ")]}":
            depth -= 1
        elif depth == 0 and token.text == "?":
            if question is None:
                question = i
            else:
                nested += 1
        elif depth == 0 and token.text == ":" and question is not None:
            if nested:
                nested -= 1
            elif colon is None:
                colon = i
    if question is None or colon is None:
        return None
    then = _strip_parens(" ".join(t.text for t in tokens[question + 1 : colon]))
    other = _strip_parens(" ".join(t.text for t in tokens[colon + 1 :]))
    return then, other


def _equals_self(condition: str) -> bool:
    """`x.equals(x)` or `Objects.equals(x, x)`, with the same expression on both sides."""
    words = condition.split()
    if words[:4] == ["java", ".", "util", "."]:
        words = words[4:]
    if words[:4] == ["Objects", ".", "equals", "("] and words[-1] == ")":
        found = _first_arguments(words, frozenset(["equals"]))
        if found and len(found[0][1]) == 2:
            left, right = (" ".join(a) for a in found[0][1])
            return _strip_parens(left) == _strip_parens(right)
        return False
    if words[-1:] != [")"]:
        return False
    depth = 0
    for i in range(len(words) - 1, -1, -1):
        depth += words[i] == ")"
        depth -= words[i] == "("
        if depth == 0:
            if i >= 2 and words[i - 1] == "equals" and words[i - 2] == ".":
                receiver = _strip_parens(" ".join(words[: i - 2]))
                argument = _strip_parens(" ".join(words[i + 1 : -1]))
                return bool(receiver) and receiver == argument
            return False
    return False


def _whole_new(text: str) -> bool:
    """Exactly one creation expression, which is never null: `new T(...)`, with or without an
    anonymous class body, or an array creation, with no member access or call after it at the
    top level, so `new File(dir, name).exists()` is not one."""
    words = text.split()
    if words[:1] != ["new"]:
        return False
    depth, groups = 0, 0
    for word in words[1:]:
        if word in ("(", "[", "{"):
            if depth == 0 and groups and word == "(":
                return False  # a call on what the creation made
            depth += 1
        elif word in (")", "]", "}"):
            depth -= 1
            if depth == 0:
                groups += 1
        elif depth == 0 and groups:
            return False  # anything after the creation, such as `.exists()`
    return groups > 0 and depth == 0


_SELF_CHECKS = frozenset(["isEqualTo", "isSameAs", "isEqualToComparingFieldByField"])


def _trivial_chain(assertion: Assertion, args: tuple[str, ...]) -> bool:
    """An AssertJ chain or a Hamcrest matcher that compares the subject with itself or a
    literal with a literal, or asserts a literal true, false or non null."""
    words = _without_literals(assertion.text).split()
    calls = _first_arguments(words, _SELF_CHECKS | {"isTrue", "isFalse", "isNotNull", "is",
                                                    "equalTo"})  # fmt: skip
    if len(args) == 1:
        subject = args[0]
        for name, call_args in calls:
            if name in ("isTrue", "isFalse", "isNotNull") and (
                _constant(subject) or (name == "isNotNull" and _whole_new(subject))
            ):
                return True
            if name in ("isTrue", "isFalse"):
                branches = _branches(subject)
                if branches is not None and ("true" if name == "isTrue" else "false") in branches:
                    return True
            if name in _SELF_CHECKS and call_args and call_args[0]:
                other = _strip_parens(" ".join(call_args[0]))
                if _same_values(_strip_parens(_without_literals(subject)), other):
                    return True
        return False
    actual = _strip_parens(_without_literals(args[-2]))
    for name, call_args in calls:
        if name in ("is", "equalTo") and call_args and call_args[0]:
            if call_args[0][0] in ("equalTo", "is"):
                continue  # the inner call is judged on its own
            other = _strip_parens(" ".join(call_args[0]))
            if _same_values(actual, other):
                return True
    return False


_KIND_TWO = (
    (" containsExactly (", " containsExactlyInAnyOrder ("),
    (" containsExactlyElementsOf (", " containsExactlyInAnyOrderElementsOf ("),
    (" containsExactlyEntriesOf (", " containsExactlyInAnyOrderEntriesOf ("),
    (" contains (", " containsInAnyOrder ("),
    ("JSONCompareMode . STRICT", "JSONCompareMode . NON_EXTENSIBLE"),
)


def _literal(token) -> str | None:
    """The printed text of a string or number literal token, as toString would show it."""
    if token.kind == "number":
        return token.text
    if token.kind == "string" and "\\" not in token.text:
        return token.text[1:-1]
    return None


def _to_string_equality(a: Assertion) -> tuple[str, str] | None:
    """The subject and the string literal of `assertEquals("[..]", subject.toString())`, in
    either argument order, `assertThat(subject.toString()).isEqualTo("[..]")`, or Hamcrest
    `assertThat(subject.toString(), equalTo("[..]"))`."""
    args = payload(a)
    if a.signature in (("assertThat", "isEqualTo"), ("assertThat", "equalTo")):
        words = a.text.split()
        name = a.signature[1]
        found = [w for w in words if w == name]
        if len(found) != 1:
            return None
        inner = a.text.split(f" {name} ( ", 1)[1]
        if not inner.endswith(" )" if name == "isEqualTo" else " ) )"):
            return None
        literal = inner[: -2 if name == "isEqualTo" else -4]
        actual = args[0]
        args = (literal, actual)
    elif a.signature != ("assertEquals",) or len(args) != 2:
        return None
    for literal, actual in (args, args[::-1]):
        tokens = tokenize(literal)
        if actual.endswith(" . toString ( )") and len(tokens) == 1 and tokens[0].kind == "string":
            value = _literal(tokens[0])
            if value is not None:
                return actual[: -len(" . toString ( )")], value
    return None


def _order_free_elements(new: Assertion) -> tuple[str, list[str], bool] | None:
    """The subject, elements and map form of an order insensitive full assertion:
    `assertThat(s).containsExactlyInAnyOrder(..)`, `assertThat(s, containsInAnyOrder(..))`, or
    `assertThat(s).containsOnly(entry(k, v), ..)`."""
    tokens = tokenize(new.text)
    texts = [t.text for t in tokens]
    if texts[:2] != ["assertThat", "("]:
        return None
    depth, i = 0, 1
    for i in range(1, len(texts)):
        depth += texts[i] == "("
        depth -= texts[i] == ")"
        if (depth == 1 and texts[i] == ",") or depth == 0:
            break
    subject = " ".join(texts[2:i])
    while (
        texts[i] == ")"
        and texts[i + 1 : i + 2] == ["."]
        and texts[i + 2 : i + 3]
        and texts[i + 2] in _DESCRIPTIONS
        and texts[i + 3 : i + 4] == ["("]
    ):
        depth, j = 0, i + 3
        for j in range(i + 3, len(texts)):
            depth += texts[j] == "("
            depth -= texts[j] == ")"
            if depth == 0:
                break
        i = j  # a description call changes no check
    if texts[i] == ")":
        rest, closing = texts[i + 1 : i + 4], 1
        name = rest[1] if rest[:1] == ["."] and len(rest) > 2 and rest[2] == "(" else None
        start = i + 4
        if name not in ("containsExactlyInAnyOrder", "containsOnly"):
            return None
    else:
        name, start, closing = texts[i + 1], i + 3, 2
        if name != "containsInAnyOrder" or texts[i + 2] != "(":
            return None
    inner, ending = tokens[start : len(tokens) - closing], texts[len(texts) - closing :]
    if ending != [")"] * closing:
        return None
    is_map = name == "containsOnly"
    elements, j = [], 0
    while j < len(inner):
        if is_map:
            if [t.text for t in inner[j : j + 2]] != ["entry", "("] or j + 6 > len(inner):
                return None
            key, comma, value, close = inner[j + 2], inner[j + 3], inner[j + 4], inner[j + 5]
            key_text, value_text = _literal(key), _literal(value)
            if comma.text != "," or close.text != ")" or None in (key_text, value_text):
                return None
            elements.append(f"{key_text}={value_text}")
            j += 6
        else:
            text = _literal(inner[j])
            if text is None:
                return None
            elements.append(text)
            j += 1
        if j < len(inner):
            if inner[j].text != ",":
                return None
            j += 1
    return subject, elements, is_map


def _collection_text(old: Assertion, new: Assertion) -> bool:
    """Kind 2: a collection's toString compared with a literal, replaced by an order insensitive
    assertion over exactly the literal's elements (ADR 0007 Decision 5, amended 2026-10-24)."""
    found, order_free = _to_string_equality(old), _order_free_elements(new)
    if found is None or order_free is None:
        return False
    subject, literal = found
    new_subject, elements, is_map = order_free
    brackets = "{}" if is_map else "[]"
    if subject != new_subject or literal[:1] + literal[-1:] != brackets:
        return False
    inner = literal[1:-1]
    printed = inner.split(", ") if inner else []
    readable = all(", " not in e and e.count("=") == (1 if is_map else 0) for e in elements)
    return readable and sorted(printed) == sorted(elements)


_EXACT_CHAIN = frozenset(["isEqualTo", "isSameAs", "isEqualToComparingFieldByField"])
_RELAXED_CHAIN = frozenset(
    "isGreaterThan isGreaterThanOrEqualTo isLessThan isLessThanOrEqualTo isBetween "
    "isStrictlyBetween isPositive isNegative isNotNull isNotEmpty isNotBlank contains "
    "containsAnyOf startsWith endsWith matches containsPattern".split()
)
_RELAXED_MATCHERS = frozenset(
    "greaterThan greaterThanOrEqualTo lessThan lessThanOrEqualTo notNullValue containsString "
    "startsWith endsWith matchesPattern".split()
)
_SUMMARIES = (". size ( )", ". length", ". isEmpty ( )", ". hashCode ( )", ". getClass ( )",
              ". toString ( )", "!= null")  # fmt: skip
_STRING_RELAXED = (". contains (", ". startsWith (", ". endsWith (", ". matches (")
_BROAD_EXCEPTIONS = frozenset(
    ["Exception . class", "RuntimeException . class", "Throwable . class", "Error . class"]
)
_LOOSE_MODES = frozenset(["atLeast", "atLeastOnce", "atMost", "atMostOnce"])
_TOLERANCE_NAMES = frozenset(
    ["isBetween", "isStrictlyBetween", "isCloseTo", "closeTo", "within", "offset", "byLessThan",
     "withinPercentage", "withPrecision"]
)  # fmt: skip


def _kind_two(old: Assertion, new: Assertion) -> bool:
    if old.text.endswith(" . inOrder ( )") and new.text == old.text[: -len(" . inOrder ( )")]:
        return True
    if _collection_text(old, new):
        return True
    return any(
        before in old.text and new.text == old.text.replace(before, after, 1)
        for before, after in _KIND_TWO
    )


def _is_exact(a: Assertion) -> bool:
    root = a.signature[0]
    if root in ("assertEquals", "assertSame") or _EXACT_CHAIN & set(a.signature):
        return True
    if "equalTo" in a.signature:
        return True
    return (
        root == "assertTrue"
        and bool(a.arguments)
        and (" == " in a.arguments[-1] or ". equals (" in a.arguments[-1])
    )


def _is_relaxed(a: Assertion) -> bool:
    if a.signature[0] == "assertNotNull":
        return True
    if _RELAXED_CHAIN & set(a.signature[1:]) or _RELAXED_MATCHERS & set(a.signature):
        return True
    if a.signature[0] == "assertTrue" and a.arguments:
        cond = a.arguments[-1]
        return any(m in cond for m in _STRING_RELAXED) or any(
            f" {op} " in cond for op in ("!=", ">=", "<=", ">", "<")
        )
    return False


def _numbers(text: str) -> list[float]:
    out = []
    for word in text.split():
        try:
            out.append(abs(float(word.rstrip("dDfFlL").replace("_", ""))))
        except ValueError:
            continue
    return out


def _tolerance(a: Assertion) -> list[float] | None:
    """The numbers that set an assertion's tolerance: the delta of a three argument
    assertEquals, or the arguments of a tolerance call such as within, closeTo or isBetween.
    A tolerance named by something other than a literal or a name bound only to one is None."""
    text = _tolerance_text(a)
    return [] if text is None else _resolved(a, text)


def _tolerance_text(a: Assertion) -> str | None:
    if a.signature[0] == "assertEquals" and len(payload(a)) == 3:
        return payload(a)[-1]
    words = _without_literals(a.text).split()
    for names in _TOLERANCE_ORDER:
        found = _tolerance_call(words, names)
        if found is not None:
            return found
    return None


def _resolved(a: Assertion, text: str) -> list[float] | None:
    value = _value(a, text)
    if value is not None:
        return [abs(value)]
    return _numbers(text) or None


# Innermost tolerance calls first, so isCloseTo(1.0, within(0.01)) reads 0.01, not 1.0.
_TOLERANCE_ORDER = (
    frozenset(["within", "offset", "byLessThan", "withinPercentage", "withPrecision"]),
    frozenset(["closeTo"]),
    frozenset(["isBetween", "isStrictlyBetween"]),
)


def _tolerance_call(words: list[str], names: frozenset[str]) -> str | None:
    for i, word in enumerate(words):
        if word in names and i + 1 < len(words) and words[i + 1] == "(":
            depth, j = 0, i + 1
            while j < len(words):
                depth += words[j] == "("
                depth -= words[j] == ")"
                if depth == 0:
                    break
                j += 1
            inner = words[i + 2 : j]
            if word == "closeTo":
                commas = [k for k, w in enumerate(inner) if w == ","]
                inner = inner[commas[-1] + 1 :] if commas else inner
            return " ".join(inner)
    return None


_BROAD = frozenset(["Exception", "RuntimeException", "Throwable", "Error"])
_PRIMITIVES = frozenset("boolean byte char short int long float double var".split())
_EXCEPTION_CALLS = frozenset(
    ["assertThrows", "expectThrows", "assertThatExceptionOfType", "isInstanceOf", "instanceOf",
     "expect"]
)  # fmt: skip
_LOWER = frozenset(["isGreaterThan", "isGreaterThanOrEqualTo"])
_UPPER = frozenset(["isLessThan", "isLessThanOrEqualTo"])
_SET_CONVERSIONS = (
    "new HashSet", "new TreeSet", "new LinkedHashSet", "Set . copyOf (", "Sets . newHashSet (",
    "ImmutableSet . copyOf (", "Collectors . toSet ( )",
)  # fmt: skip


def _first_arguments(words: list[str], names: frozenset[str]) -> list[tuple[str, list[str]]]:
    """Each call named in names, with its top level arguments as word lists."""
    out = []
    for i, word in enumerate(words[:-1]):
        if word not in names or words[i + 1] != "(":
            continue
        depth, args, current = 0, [], []
        for w in words[i + 1 :]:
            if w == "(":
                depth += 1
                if depth == 1:
                    continue
            elif w == ")":
                depth -= 1
                if depth == 0:
                    break
            if w == "," and depth == 1:
                args.append(current)
                current = []
            else:
                current.append(w)
        args.append(current)
        out.append((word, args))
    return out


def _verifies(a: Assertion) -> bool:
    return a.signature[0] == "verify" or (a.signature[0] == "then" and "should" in a.signature)


def _expected_types(a: Assertion) -> frozenset[str]:
    """Simple names of the exception types an assertion expects, from `X.class` arguments."""
    out = set()
    for name, args in _first_arguments(_without_literals(a.text).split(), _EXCEPTION_CALLS):
        # A synthesized expectation lists every caught or expected type; a real call names one.
        candidates = args if name == "assertThrows" else args[:1]
        for arg in candidates:
            if len(arg) >= 3 and arg[-2:] == [".", "class"]:
                out.add(arg[-3])
    return frozenset(out)


def _json_mode(a: Assertion) -> bool:
    """A credited JSONAssert call whose last argument is its strictness: a boolean literal or
    a JSONCompareMode constant."""
    qualified = ("JSONAssert . ", "org . skyscreamer . jsonassert . JSONAssert . ")
    if not a.credited or not a.text.startswith(qualified) or not a.arguments:
        return False
    return len(a.arguments) >= 3  # JSONAssert has no delta; its last argument is the mode


_JSON_STRICT = frozenset(["true", "JSONCompareMode . STRICT", "JSONCompareMode . NON_EXTENSIBLE"])
_PLAIN_MODES = frozenset(["true", "false", *(f"JSONCompareMode . {m}" for m in _JSON_MODES)])


def _mode_shaped(a: Assertion) -> bool:
    """Whether the assertion's last argument, read through a local, reads as a JSONAssert mode:
    a boolean literal, a JSONCompareMode constant, or a comparator."""
    if len(a.arguments) < 3:
        return False
    mode = _strip_parens(_resolved_argument(a, a.arguments[-1]))
    words = mode.split()
    if not words or mode in ("true", "false"):
        return bool(words)
    if words[-1] in _JSON_MODES and (len(words) == 1 or "JSONCompareMode" in words):
        return True
    return len(words) > 1 and words[0] == "new" and words[1].endswith("Comparator")


def _is_json_assert(a: Assertion) -> bool:
    """Whether the assertion call is JSONAssert's, qualified or statically imported, or may be
    by static imports that do not settle its class and has a mode shaped last argument."""
    qualified = ("JSONAssert . ", "org . skyscreamer . jsonassert . JSONAssert . ")
    return (
        a.text.startswith(qualified)
        or ("owner", JSON_ASSERT) in a.facts
        or (("candidate", JSON_ASSERT) in a.facts and _mode_shaped(a))
    )


def _mode(a: Assertion) -> str:
    """The last argument read through a local. A JSONCompareMode constant comes back as
    `JSONCompareMode . NAME` where its spelling names the constant at the call, and marked
    unknown with a leading `?` where it does not."""
    mode = _strip_parens(_resolved_argument(a, a.arguments[-1])) if a.arguments else ""
    words = mode.split()
    if (
        words
        and words[-1] in _JSON_MODES
        and (len(words) == 1 or words[-3:-1] == ["JSONCompareMode", "."])
    ):
        return (
            f"JSONCompareMode . {words[-1]}" if ("trusted_mode", mode) in a.facts else f"? {mode}"
        )
    return mode


def _maybe_json_assert(a: Assertion) -> bool:
    return len(a.arguments) >= 3 and (_is_json_assert(a) or ("candidate", JSON_ASSERT) in a.facts)


def _unknown_mode_changed(old: Assertion, new: Assertion) -> bool:
    """A call that may be JSONAssert's whose mode is not a plain boolean or JSONCompareMode
    constant on either side, such as a comparator whose customizations can accept anything,
    and that mode is not the old call's mode with the same assignments behind it."""
    if not _maybe_json_assert(new):
        return False
    mode = _mode(new)
    old_mode = _mode(old) if len(old.arguments) >= 3 else None
    unknown = mode not in _PLAIN_MODES or (
        old_mode is not None and _maybe_json_assert(old) and old_mode not in _PLAIN_MODES
    )
    if not unknown:
        return False
    return mode != old_mode or not _assignments_kept(old, new, new.arguments[-1])


def _strictness_dropped(old: Assertion, new: Assertion) -> bool:
    """The same assertion call whose strict last argument, `true`, STRICT or NON_EXTENSIBLE,
    became anything else, whatever class the call belongs to."""
    if old.signature[0] != new.signature[0] or len(old.arguments) != len(new.arguments):
        return False
    if len(old.arguments) < 3:
        return False
    return _mode(old) in _JSON_STRICT and _mode(new) not in _JSON_STRICT


def _json_lenient(a: Assertion) -> bool:
    """A JSONAssert call whose mode, read through a local, is anything but `true`, STRICT or
    NON_EXTENSIBLE: LENIENT, STRICT_ORDER, false, a comparator, whose customizations can
    accept anything, or a name that does not settle as a mode."""
    if not _is_json_assert(a) or len(a.arguments) < 3:
        return False
    return _mode(a) not in _JSON_STRICT


# Chain and matcher bounds: (side, strict) for each, or both sides for the between forms.
_BOUND_CALLS = {
    "isGreaterThan": (("low", True),), "isGreaterThanOrEqualTo": (("low", False),),
    "isLessThan": (("high", True),), "isLessThanOrEqualTo": (("high", False),),
    "greaterThan": (("low", True),), "greaterThanOrEqualTo": (("low", False),),
    "lessThan": (("high", True),), "lessThanOrEqualTo": (("high", False),),
    "isBetween": (("low", False), ("high", False)),
    "isStrictlyBetween": (("low", True), ("high", True)),
}  # fmt: skip
_SIGN_CALLS = {
    "isPositive": ("low", True), "isNotNegative": ("low", False),
    "isNegative": ("high", True), "isNotPositive": ("high", False),
}  # fmt: skip
_COMPARISONS = ((" >= ", "low", False), (" > ", "low", True), (" <= ", "high", False),
                (" < ", "high", True))  # fmt: skip
_FLIPPED = {"low": "high", "high": "low"}

Bound = tuple[float | None, bool, str]  # value (None when it cannot be read), strict, text


def _bounds(a: Assertion) -> dict[str, dict[str, list[Bound]]]:
    """The lower and upper bounds an assertion places on each subject: AssertJ chain bounds
    and sign checks, Hamcrest bound matchers, and comparisons joined by `&&` in assertTrue."""
    out: dict[str, dict[str, list[Bound]]] = {}

    def add(subject: str, side: str, bound: Bound) -> None:
        out.setdefault(subject, {}).setdefault(side, []).append(bound)

    words = _without_literals(a.text).split()
    args = payload(a)
    root = a.signature[0]
    if root in ("assertThat", "then", "checkThat") and args:
        subject = _strip_parens(args[-2] if len(args) >= 2 else args[0])
        for name, call_args in _first_arguments(words, frozenset(_BOUND_CALLS)):
            for (side, strict), arg in zip(_BOUND_CALLS[name], call_args, strict=False):
                text = _strip_parens(" ".join(arg))
                add(subject, side, (_value(a, text), strict, text))
        for name, _ in _first_arguments(words, frozenset(_SIGN_CALLS)):
            side, strict = _SIGN_CALLS[name]
            add(subject, side, (0.0, strict, "0"))
    if root == "assertTrue" and args:
        for conjunct in _strip_parens(args[-1]).split(" && "):
            for op, side, strict in _COMPARISONS:
                if op not in conjunct:
                    continue
                left, _, right = conjunct.partition(op)
                left, right = _strip_parens(left), _strip_parens(right)
                if _value(a, left) is not None and _value(a, right) is None:
                    left, right, side = right, left, _FLIPPED[side]  # `5 < x` bounds x from below
                add(left, side, (_value(a, right), strict, right))
                break
    return out


def _looser(old: Assertion, new: Assertion) -> bool:
    """Some bound old places on a subject new also bounds has no equal or tighter bound on the
    same side in new: lowered (lower side), raised (upper side), made inclusive, no longer
    readable as a number, or dropped."""
    new_bounds = _bounds(new)
    for subject, sides in _bounds(old).items():
        if subject not in new_bounds:
            continue  # the matcher never pairs these; see _drops_bounded_subject
        for side, bounds in sides.items():
            kept = new_bounds[subject].get(side, [])
            if not all(any(_holds(old, new, bound, k, side) for k in kept) for bound in bounds):
                return True
    return False


def _holds(old_a: Assertion, new_a: Assertion, old: Bound, new: Bound, side: str) -> bool:
    """Whether new bounds its subject at least as tightly as old, on the same side. The same
    text with no readable value holds only while every name in it keeps its assignments."""
    (value, strict, text), (new_value, new_strict, new_text) = old, new
    if new_text == text and new_strict == strict and new_value == value:
        return value is not None or _assignments_kept(old_a, new_a, text)
    if value is None or new_value is None:
        return False
    if new_value != value:
        return new_value > value if side == "low" else new_value < value
    return new_strict or not strict


def _assignments_kept(old: Assertion, new: Assertion, text: str) -> bool:
    """Whether every name text reads has the same assignments on both sides."""
    names = _identifiers((text,))
    return {f for f in _defs(old) if f[1] in names} == {f for f in _defs(new) if f[1] in names}


def _drops_bounded_subject(old: Assertion, new: Assertion) -> bool:
    """Old bounds a subject that new does not bound at all: new checks something else, so the
    two are not one assertion changed, and old is left to be judged removed."""
    return not set(_bounds(old)) <= set(_bounds(new))


def _set_converted(old: Assertion, new: Assertion) -> bool:
    """Both sides newly converted to a set, where a converted name is not declared as a Set."""
    added = [c for c in _SET_CONVERSIONS if c in new.text and c not in old.text]
    if not added:
        return False
    sets = {f[1] for f in new.facts if f[0] == "set"}
    names = _identifiers(payload(new)) - {"new", "HashSet", "TreeSet", "LinkedHashSet", "Set",
                                          "Sets", "ImmutableSet", "Collectors", "copyOf",
                                          "newHashSet", "toSet", "collect", "stream"}  # fmt: skip
    return not names or not names <= sets


def _shares_subject(old: Assertion, new: Assertion) -> bool:
    olds = [a for a in old.arguments if not _constant(a)]
    news = [a for a in new.arguments if not _constant(a)]
    return any(o in n or n in o for o in olds for n in news)


def _weakening(old: Assertion, new: Assertion) -> str | None:
    """The named weakening of Decision 5 that turns old into new, if any."""
    if old.text == new.text and _defs(old) == _defs(new):
        return None
    if _json_lenient(new) and not _json_lenient(old) and ("JSONAssert" in old.text
                                                          or _is_exact(old)):  # fmt: skip
        return "weaken_assertion/assertion_removed"  # not a named weakening, and never a match
    if _strictness_dropped(old, new) or _unknown_mode_changed(old, new):
        return "weaken_assertion/assertion_removed"

    old_types, new_types = _expected_types(old), _expected_types(new)
    if (
        old_types
        and new_types
        and ((new_types & _BROAD and not old_types & _BROAD) or old_types < new_types)
    ):
        return "weaken_assertion/expected_exception_broadened"
    if (
        "isExactlyInstanceOf" in old.signature
        and "isInstanceOf" in new.signature
        and ("isExactlyInstanceOf" not in new.signature)
    ):
        return "weaken_assertion/expected_exception_broadened"
    if old.signature[0].startswith("assertThatThrownBy") and new.signature[0] == "assertThatCode":
        return "weaken_assertion/expected_exception_broadened"
    if _verifies(old) and _verifies(new):
        if _LOOSE_MODES & set(new.signature) and not _LOOSE_MODES & set(old.signature):
            return "weaken_assertion/verification_mode_relaxed"
    equals = old.signature[0] == new.signature[0] == "assertEquals"
    if equals and not _json_mode(new) and len(payload(new)) == 3 and len(payload(old)) == 2:
        return "weaken_assertion/tolerance_widened"
    if old.signature == new.signature:
        old_tolerance, new_tolerance = _tolerance(old), _tolerance(new)
        if old_tolerance and (
            new_tolerance is None or (new_tolerance and max(new_tolerance) > max(old_tolerance))
        ):
            return "weaken_assertion/tolerance_widened"
        text = _tolerance_text(old)
        if old_tolerance is None and new_tolerance is None and text == _tolerance_text(new):
            if not _assignments_kept(old, new, text):
                return "weaken_assertion/tolerance_widened"
    if _looser(old, new):
        return "weaken_assertion/tolerance_widened"
    if not _shares_subject(old, new):
        return None
    if _set_converted(old, new):
        return "weaken_assertion/equality_relaxed"
    if _is_exact(old) and _is_relaxed(new) and not _is_exact(new):
        return "weaken_assertion/equality_relaxed"
    if _is_exact(old) and any(s in new.text and s not in old.text for s in _SUMMARIES):
        return "weaken_assertion/equality_relaxed"
    if old.signature[0] == new.signature[0] == "assertTrue" and old.arguments and new.arguments:
        if " || " in new.arguments[-1] and " || " not in old.arguments[-1]:
            return "weaken_assertion/equality_relaxed"
    return None


def _identifiers(arguments: tuple[str, ...]) -> set[str]:
    return {
        w
        for arg in arguments
        for w in _without_literals(arg).split()
        if (w[:1].isalpha() or w[:1] in "_$") and w not in _CONSTANT_WORDS
    }


def _names(a: Assertion) -> set[str]:
    return set(a.signature) | {w for w in a.text.split() if w[:1].isupper()}


def _guard_ok(old: Assertion, new: Assertion) -> bool:
    guard = tuple(g for g in new.guard if g != "wait")
    return _is_subsequence(guard, old.guard) and new.exits_before <= old.exits_before


def _match(
    before: list[Assertion],
    after: list[Assertion],
    soft: dict[str, Assertion] | None = None,
    rebound: frozenset[str] = frozenset(),
    after_calls: frozenset = frozenset(),
) -> list[tuple[Assertion, str, Assertion | None]]:
    """Each before assertion left without a match, with the code it earns, plus the flags a
    match raises."""
    pool = list(after)
    soft = soft or {}
    results = []

    def rebinds(old: Assertion) -> bool:
        type_names = {w for w in _names(old) if w[:1].isupper()}
        return (
            "*" in rebound
            or bool(_names(old) & rebound)
            or ("*types" in rebound and bool(type_names))
        )

    # Unchanged twins are claimed first, so a changed assertion is never paired with a later
    # assertion that is still there unchanged.
    changed = []
    for old in before:
        same = (
            None
            if rebinds(old)
            else next(
                (
                    new
                    for new in pool
                    if new.text == old.text
                    and _defs(new) == _defs(old)
                    and (not new.scanned_only or (old.scanned_only and new.origin == old.origin))
                    and _guard_ok(old, new)
                ),
                None,
            )
        )
        if same is not None:
            pool.remove(same)
            if old.subject and not same.subject:
                # The text is the same, but what it reads is now defined from literals only.
                results.append((old, "flag/assertion_subject_replaced", same))
        else:
            changed.append(old)
    # An assertion that shares a root with something still in the pool pairs first, so one
    # whose subject is gone cannot take another's twin and leave that one removed.
    changed.sort(key=lambda old: not any(old.subject & n.subject for n in pool))
    for old in changed:
        uses_rebound = rebinds(old)
        usable = [
            n
            for n in pool
            if n.credited
            and not n.scanned_only
            and _guard_ok(old, n)
            and not _drops_bounded_subject(old, n)
        ]
        weakened = next(((n, code) for n in usable if (code := _weakening(old, n))), None)
        if weakened is not None:
            pool.remove(weakened[0])
            results.append((old, weakened[1], weakened[0]))
            continue
        candidates = [
            n
            for n in usable
            if (n.signature == old.signature or _kind_two(old, n))
            and not _trivially_true(n)
            and not (uses_rebound and n.text == old.text)
        ]
        # Pair with the candidate that shares the most of the old subject.
        matched = max(candidates, key=lambda n: len(old.subject & n.subject), default=None)
        if matched is not None:
            pool.remove(matched)
            changed = matched.text != old.text or _defs(matched) != _defs(old)
            if changed and old.subject and not old.subject & matched.subject:
                results.append((old, "flag/assertion_subject_replaced", matched))
            continue

        if old.text in soft:
            results.append((old, "weaken_assertion/soft_assertions_unchecked", soft[old.text]))
            continue
        relaxed = next((n for n in usable if _is_exact(old) and _is_relaxed(n)), None)
        if relaxed is not None:
            pool.remove(relaxed)
            results.append((old, "weaken_assertion/equality_relaxed", relaxed))
            continue
        # Rebinding makes an assertion that is still there, unchanged, count as changed; an
        # assertion that is gone is a removal whatever its names now mean.
        head = old.text.split(" (", 1)[0].split(" . ")
        root = (head[0] if len(head) > 1 else None, head[-1])
        still_there = root in after_calls
        code = (
            "weaken_assertion/assertion_rebound"
            if uses_rebound and still_there
            else "weaken_assertion/assertion_removed"
        )
        results.append((old, code, None))
    return results


def _soft_assertions(program: Program, path: str, method: Method) -> tuple[dict, list]:
    """Soft assertions in the method: unchecked ones keyed by their text without the receiver,
    and checked ones as ordinary assertions."""
    unit = program.units[path]
    if method.body is None:
        return {}, []
    start = method.body.start
    words = [t.text for t in unit.tokens[start : method.body.end]]
    managed = any(
        "SoftAssertionsExtension" in _text(unit, a.start, a.end) or a.name == "InjectSoftAssertions"
        for node in path_to(unit, method.start)
        if isinstance(node, TypeDecl | Method)
        for a in node.annotations
    )
    soft_types = ("SoftAssertions", "JUnitSoftAssertions", "AutoCloseableSoftAssertions")
    receivers = {
        words[i + 1]
        for i, word in enumerate(words[:-1])
        if word in soft_types and unit.tokens[start + i + 1].kind == "ident"
    }
    constructed = {
        words[i] for i in range(len(words) - 2) if words[i + 1] == "=" and words[i + 2] == "new"
    } | {
        words[i + 1]
        for i in range(len(words) - 3)
        if words[i] in soft_types and words[i + 2] == "=" and words[i + 3] == "new"
    }
    all_calls = calls_in(unit, method.body)
    exits = [at for at, _ in _exits(unit, method.body)]
    unchecked, checked = {}, []
    for call in all_calls:
        if call.receiver not in receivers or not call.name.startswith("assert"):
            continue
        if call.name == "assertAll":
            continue
        names, end = _chain(unit, call)
        stripped = _text(unit, call.start, end)
        arguments = _split_arguments(unit, call.start + 1, _close(unit, call.start + 1))
        assertion = Assertion(
            stripped, tuple(names), arguments, _guard(unit, method.body, call.start),
            frozenset(), True, unit.tokens[call.start].line, call.start,
            subject=_followed(roots_of(arguments), _local_definitions(unit, method.body)),
        )  # fmt: skip
        closes = any(
            other.name == "assertAll"
            and other.receiver == call.receiver
            and other.start > call.start
            and not _guard(unit, method.body, other.start)
            and not any(call.start < at < other.start for at in exits)
            for other in all_calls
        )
        if (managed and call.receiver not in constructed) or closes:
            checked.append(assertion)
        else:
            unchecked[stripped] = assertion
    return unchecked, checked


def _owner(name: str) -> str:
    return name.rsplit(".", 1)[0]


def _imports(unit: CompilationUnit) -> tuple[dict, set, dict, set]:
    """Single type imports by simple name, type imports on demand, single static imports by
    simple name, and static imports on demand."""
    types, type_wild, members, member_wild = {}, set(), {}, set()
    for imp in unit.imports:
        simple = imp.name.rsplit(".", 1)[-1]
        if imp.static and imp.wildcard:
            member_wild.add(imp.name)
        elif imp.static:
            members.setdefault(simple, set()).add(imp.name)
        elif imp.wildcard:
            type_wild.add(imp.name)
        else:
            types.setdefault(simple, set()).add(imp.name)
    return types, type_wild, members, member_wild


def _declared_type(program: Program, path: str, simple: str) -> bool:
    """Whether a type in the file or its package, which shadows an on demand import, has the
    simple name."""
    unit = program.units[path]
    return any(
        decl.name == simple
        for other_path, other in program.units.items()
        if other_path == path or other.package == unit.package
        for decl in program.all_types(other_path)
    )


def _import_rebinds(change: FileChange, ctx: Context) -> set[str]:
    """Names an import change binds differently (ADR 0007 Decision 5, amended 2026-10-24).

    Type names: a single import is unchanged where an on demand import of its package supplied
    the name on the other side; a set of type imports on demand that only grows or only shrinks
    rebinds nothing, since both sides must compile; a swap rebinds every type name. Member
    names: any change to the static imports on demand rebinds every called name that no single
    static import or declaration in the file supplies, since overload resolution spans them all.
    """
    b_types, b_wild, b_members, b_member_wild = _imports(change.before)
    a_types, a_wild, a_members, a_member_wild = _imports(change.after)
    names: set[str] = set()
    for simple in set(b_types) | set(a_types):
        old, new = b_types.get(simple, set()), a_types.get(simple, set())
        if old == new:
            continue
        # Without the single import the name came from an on demand import only if nothing in
        # scope (a type in the file or the same package) supplied it, since those shadow one.
        if not old and {_owner(n) for n in new} <= b_wild | {"java.lang"}:
            if not _declared_type(ctx.before, change.path, simple):
                continue
        if not new and {_owner(n) for n in old} <= a_wild | {"java.lang"}:
            if not _declared_type(ctx.after, change.path, simple):
                continue
        names.add(simple)
    if b_wild - a_wild and a_wild - b_wild:
        names.add("*types")
        if _supertype_through_wildcard(change, ctx):
            # A swapped on demand import can rebind an unqualified supertype, and with it every
            # inherited assertion method.
            names.add("*")
    for simple in set(b_members) | set(a_members):
        old, new = b_members.get(simple, set()), a_members.get(simple, set())
        if old == new:
            continue
        owners = {_owner(n) for n in old | new}
        if not old and b_member_wild == owners:
            continue  # the only class an on demand import named supplies it now by name
        if not new and a_member_wild == owners:
            continue
        names.add(simple)
    if b_member_wild != a_member_wild:
        # A class whose public static members are listed rebinds only the names it declares;
        # an unlisted class, or a listed name a read file declares itself, may declare any name.
        changed = b_member_wild ^ a_member_wild
        trusted = all(
            owner in LIBRARY_MEMBERS
            and owner not in ctx.before.types_by_fqn
            and owner not in ctx.after.types_by_fqn
            for owner in changed
        )
        supplied = (
            frozenset().union(*(LIBRARY_MEMBERS[owner] for owner in changed)) if trusted else None
        )
        declared = {
            method.name
            for decl in ctx.after.all_types(change.path)
            for owner in (decl, *(found for _, found in ctx.after.supertypes(change.path, decl)))
            for method in owner.methods
        }
        unit = change.after
        for call in calls_in(unit, _Span(0, len(unit.tokens))):
            if call.receiver is None and call.name not in a_members and call.name not in declared:
                if supplied is None or call.name in supplied:
                    names.add(call.name)
    return names


def _rebound_names(change: FileChange, ctx: Context) -> frozenset[str]:
    """Names whose binding the patch may have changed for this file (ADR 0007 Decision 5).

    A newly declared method or type anywhere in the files read, and a changed import or
    package declaration in this file, rebind the names they could supply; `*` stands for every
    name."""
    if change.before is None:
        return frozenset()
    names: set[str] = set()
    if change.before.package != change.after.package:
        names.add("*")
    names |= _import_rebinds(change, ctx)
    old_methods = {(p, m.name) for m_list in ctx.before.methods_by_name.values() for p, m in m_list}
    for name, found in ctx.after.methods_by_name.items():
        if any((p, name) not in old_methods for p, _ in found):
            names.add(name)
    old_types = set(ctx.before.types_by_fqn)
    for fqn in ctx.after.types_by_fqn:
        if fqn not in old_types:
            names.add(fqn.rsplit(".", 1)[-1])
    return frozenset(names)


def _supertype_through_wildcard(change: FileChange, ctx: Context) -> bool:
    for decl in ctx.after.all_types(change.path):
        for name in [*decl.extends, *decl.implements]:
            if "." in name:
                continue
            binding = ctx.after.resolve_type(change.path, name, decl.start, header=True)
            if binding.kind == "unresolved":
                return True
    return False


def _supertypes_changed(ctx: Context, change: FileChange, method: Method) -> bool:
    owner = ctx.after.method_owner.get(id(method))
    after = ctx.after.types_by_fqn.get(owner) if owner else None
    before = ctx.before.types_by_fqn.get(owner) if owner else None
    if after is None or before is None:
        return False
    return (before[1].extends, before[1].implements) != (after[1].extends, after[1].implements)


def _expected_package(path: str) -> str | None:
    for root in ("src/test/java/", "src/main/java/"):
        if root in path:
            folder = path.split(root, 1)[1].rsplit("/", 1)
            return folder[0].replace("/", ".") if len(folder) == 2 else ""
    return None


def _state_free_equals(unit: CompilationUnit, method: Method, owner_name: str) -> bool:
    """An equals that reads no state of either object: a constant, instanceof or getClass."""
    if method.body is None:
        return False
    param = method.params[0].name if method.params else ""
    allowed = {param, owner_name, "getClass", "Object", "this", "o", "obj", "other"}
    words = [t.text for t in unit.tokens[method.body.start : method.body.end] if t.kind == "ident"]
    return all(w in allowed for w in words)


def _equals_findings(ctx: Context, assertions_changed: bool) -> Iterator[Finding]:
    for change in ctx.changes:
        for decl in ctx.after.all_types(change.path):
            for method in decl.methods:
                if method.name != "equals" or len(method.params) != 1:
                    continue
                fqn = ctx.after.method_owner.get(id(method))
                old = ctx.before.types_by_fqn.get(fqn) if fqn else None
                old_equals = [m for m in (old[1].methods if old else []) if m.name == "equals"]
                old_text = (
                    _text(change.before, old_equals[0].start, old_equals[0].end)
                    if old_equals and change.before is not None
                    else None
                )
                if old_text == _text(change.after, method.start, method.end):
                    continue
                # Any other changed equals is flagged, assertion text changed or not: a
                # weakened equals weakens every comparison that relies on it.
                if _state_free_equals(change.after, method, decl.name):
                    yield Finding("weaken_assertion/tautological_equals", change.path, method.line)
                else:
                    yield Finding("flag/equals_changed", change.path, method.line)


def _methods(program: Program, path: str) -> dict[tuple, Method]:
    out = {}
    for decl in program.all_types(path):
        if decl.kind == "anonymous":
            continue
        fqn = program.fqn[id(decl)]
        for method in decl.methods:
            out.setdefault((fqn, method.name, tuple(p.type for p in method.params)), method)
    return out


_EXPECTED_ATTRIBUTES = ("expected", "expectedExceptions")


def _annotation_expectations(program: Program, path: str, method: Method) -> list[Assertion]:
    """JUnit 4 `@Test(expected = X)` and TestNG `expectedExceptions`, as the assertThrows each
    stands for (Decision 5 kind 1). Credit needs the annotation to resolve to a test library."""
    unit = program.units[path]
    out = []
    for annotation in method.annotations:
        if annotation.name.rsplit(".", 1)[-1] != "Test":
            continue
        credited = program.resolve_type(path, annotation.name, annotation.start).kind == "library"
        for name, value in attributes(unit, annotation).items():
            if name not in _EXPECTED_ATTRIBUTES:
                continue
            types = tuple(_split_array(value))
            out.append(Assertion(
                f"assertThrows ( {' , '.join(types)} )", ("assertThrows",), types, (),
                frozenset(), credited, annotation.line, annotation.start,
            ))  # fmt: skip
    return out


def _split_array(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("{") and value.endswith("}"):
        return [v.strip() for v in value[1:-1].split(",") if v.strip()]
    return [value]


def _file(change: FileChange, ctx: Context) -> Iterator[Finding]:
    before = _methods(ctx.before, change.path)
    after = _methods(ctx.after, change.path)
    after_calls = frozenset(
        (call.receiver if call.receiver not in ("<expr>",) else None, call.name)
        for p in ctx.test_paths
        for call in calls_in(ctx.after.units[p], _Span(0, len(ctx.after.units[p].tokens)))
    )
    for key, old in before.items():
        new = after.get(key)
        if new is None:
            continue  # a vanished method is Decision 6's disable/test_removed, reported once
        old_set = _both_ways(ctx.before, change.path, old, ctx.budget)
        old_set += _annotation_expectations(ctx.before, change.path, old)
        if not old_set:
            continue
        new_set = _both_ways(ctx.after, change.path, new, ctx.budget)
        new_set += _annotation_expectations(ctx.after, change.path, new)
        unchecked, checked = _soft_assertions(ctx.after, change.path, new)
        rebound = _rebound_names(change, ctx)
        if _supertypes_changed(ctx, change, new):
            rebound = rebound | {"*"}
        reported = set()
        matches = _match(old_set, new_set + checked, unchecked, rebound, after_calls)
        for _, code, culprit in matches:
            if code not in reported:
                reported.add(code)
                yield Finding(code, change.path, culprit.line if culprit else new.line)


def findings(ctx: Context) -> Iterator[Finding]:
    out = []
    for change in ctx.changes:
        if change.before is not None and change.path in ctx.test_paths:
            out.extend(_file(change, ctx))
            if change.before.package != change.after.package:
                out.append(Finding("weaken_assertion/assertion_rebound", change.path, 1))
        elif change.before is None and change.path in ctx.test_paths:
            expected = _expected_package(change.path)
            if expected is not None and expected != (change.after.package or ""):
                out.append(Finding("weaken_assertion/assertion_rebound", change.path, 1))
    changed = any(not f.code.startswith("flag/") or f.code == "flag/assertion_subject_replaced"
                  for f in out) or _any_assertion_text_changed(ctx)  # fmt: skip
    out.extend(_equals_findings(ctx, changed))
    yield from out


def _any_assertion_text_changed(ctx: Context) -> bool:
    for change in ctx.changes:
        if change.before is None or change.path not in ctx.test_paths:
            continue
        before = {
            a.text for b in _bodies(change.before) for a in collect(ctx.before, change.path, b)
        }
        after = {a.text for b in _bodies(change.after) for a in collect(ctx.after, change.path, b)}
        if before != after:
            return True
    return False


def _bodies(unit: CompilationUnit) -> list[Block]:
    return [n.body for n in walk(unit) if isinstance(n, Method) and n.body is not None]
