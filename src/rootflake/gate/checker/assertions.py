"""The strict assertion principle (ADR 0007 Decision 5): collection, removal and kind 1 matches.

Every method body in test code that exists before and after the patch has its assertions
collected on both sides: its own, those in lambdas and anonymous classes inside it, and those
in the helpers it calls (followed as rejection scans follow before the patch and as credit
follows after it). An assertion before the patch must reappear unchanged and no more guarded,
or be matched by an added assertion that keeps its strength; otherwise it was removed.
"""

from collections.abc import Iterator
from dataclasses import dataclass

from rootflake.gate.checker.calls import Call, ExpansionBudget, calls_in, reach
from rootflake.gate.checker.model import Context, FileChange, Finding
from rootflake.gate.checker.scope import Program
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
    for i in range(open_paren + 1, close):
        token = unit.tokens[i]
        if token.kind == "op" and token.text in "([{":
            depth += 1
        elif token.kind == "op" and token.text in ")]}":
            depth -= 1
        elif token.kind == "op" and token.text == "," and depth == 0:
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
    """Each local's identifiers on the right of its declarations and assignments."""
    out: dict[str, set[str]] = {}
    for node in walk(body):
        if not isinstance(node, Statement) or node.kind not in ("local_var", "expression"):
            continue
        tokens = unit.tokens[node.start : node.end]
        for i, token in enumerate(tokens[:-1]):
            if token.kind == "ident" and tokens[i + 1].text == "=" and tokens[i + 1].kind == "op":
                rhs = " ".join(t.text for t in tokens[i + 2 :])
                out.setdefault(token.text, set()).update(_identifiers((rhs,)))
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
    rules = _expected_exception_fields(program, path)
    out: list[Assertion] = []
    consumed: set[int] = set()
    for call in calls_in(unit, body):
        if call.name == "expect" and call.receiver in rules and call.arity == 1:
            arguments = _split_arguments(unit, call.start + 1, _close(unit, call.start + 1))
            out.append(Assertion(
                f"expect ( {arguments[0]} )", ("expect",), arguments,
                _guard(unit, body, call.start, declared),
                frozenset(t for at, t in exits if at < call.start), rules[call.receiver],
                unit.tokens[call.start].line, call.start, facts=facts,
            ))  # fmt: skip
            continue
        if call.start in consumed or call.reference or not is_assertion_name(call.name):
            continue
        if program.methods_by_name.get(call.name):
            continue  # a helper the checker reads: its assertions come in by following it
        if call.receiver not in (None, "<expr>") and not call.receiver[:1].isupper():
            continue
        names, end = _chain(unit, call)
        start = call.receiver_at if call.receiver not in (None, "<expr>") else call.start
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
                or _timed_mode(program, path, unit, inner)
            )
        )
        credited = _library_call(program, path, call.name, call.start) or (
            call.receiver not in (None, "<expr>")
            and program.resolve_type(path, call.receiver, call.receiver_at).kind == "library"
            and not program.methods_by_name.get(call.name)
        )
        out.append(
            Assertion(
                text=_text(unit, start, end),
                signature=(*(n for n in names if n not in _DESCRIPTIONS), *matchers),
                arguments=_split_arguments(unit, call.start + 1, _close(unit, call.start + 1)),
                guard=_guard(unit, body, call.start, declared),
                exits_before=frozenset(text for at, text in exits if at < call.start),
                credited=credited,
                line=unit.tokens[call.start].line,
                start=call.start,
                subject=_followed(
                    _identifiers(
                        _split_arguments(unit, call.start + 1, _close(unit, call.start + 1))
                    ),
                    definitions,
                ),
                facts=facts,
            )
        )
    for node in walk(body):
        if (
            isinstance(node, Lambda)
            and node.expression is not None
            and _wait_lambda(unit, node, declared)
        ):
            if unit.tokens[node.start - 2].text != "until":
                continue
            expression = _text(unit, node.expression.start, node.expression.end)
            operands = _equality_operands(unit, node.expression.start, node.expression.end)
            after = unit.tokens[node.end].text if node.end < len(unit.tokens) else ""
            if after == ",":
                expected = _equal_to_argument(program, path, unit, node.end + 1)
                operands = (expected, expression) if expected is not None else None
            elif after != ")":
                operands = None
            elif operands is None:
                out.append(_wait_assertion(unit, body, declared, exits, node.expression.start,
                                           "assertTrue", (expression,), node.start))  # fmt: skip
            if operands:
                out.append(_wait_assertion(unit, body, declared, exits, node.expression.start,
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
            and tokens[call.end].text == ")"
            and (tokens[at].kind == "ident" or tokens[at].text == "this")
            and _chain_root(unit, at - 2) in AWAIT_ROOTS
            and _chain_root(unit, at - 2) not in declared
        ):
            receiver = _text(unit, at, at + 1)
            out.append(_wait_assertion(unit, body, declared, exits, at, "assertTrue",
                                       (f"{receiver} . {call.name} ( )",), at))  # fmt: skip
    for node in walk(body):
        if isinstance(node, Statement) and node.kind == "try" and node.blocks:
            statements = node.blocks[0].statements
            last = statements[-1] if statements else None
            if last is None or last.kind != "expression" or not _ends_in_fail(unit, last):
                continue
            credited = _fail_resolves(program, path, unit, last)
            for catch in node.catches:
                for caught in catch.types:
                    text = f"{' . '.join(caught.split('.'))} . class"
                    out.append(Assertion(
                        f"assertThrows ( {text} )", ("assertThrows",), (text,),
                        _guard(unit, body, node.start, declared),
                        frozenset(t for at, t in exits if at < node.start), credited,
                        catch.line, catch.start, facts=facts,
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
    """The assertion an Awaitility condition stands for under kind 3."""
    return Assertion(
        f"{name} ( {' , '.join(arguments)} )", (name,), arguments,
        _guard(unit, body, start, declared), frozenset(t for at, t in exits if at < before),
        True, unit.tokens[start].line, start,
    )  # fmt: skip


def _equal_to_argument(program: Program, path: str, unit: CompilationUnit, i: int) -> str | None:
    """X in a closing `equalTo(X))` argument that resolves to Hamcrest."""
    tokens = unit.tokens
    if tokens[i].text != "equalTo" or tokens[i + 1].text != "(":
        return None
    close = _close(unit, i + 1)
    if tokens[close + 1].text != ")" or not _library_call(program, path, "equalTo", i):
        return None
    return _text(unit, i + 2, close)


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


def _expected_exception_fields(program: Program, path: str) -> dict[str, bool]:
    """Fields of the ExpectedException rule type, with whether that type resolves to JUnit."""
    key = ("expected_exception_fields", path)
    if key not in program.memo:
        program.memo[key] = _find_expected_exception_fields(program, path)
    return program.memo[key]


def _find_expected_exception_fields(program: Program, path: str) -> dict[str, bool]:
    out = {}
    for decl in program.all_types(path):
        for field in decl.fields:
            if field.type.rsplit(".", 1)[-1] != "ExpectedException":
                continue
            credited = field.type.startswith("org.junit.") or (
                program.resolve_type(path, "ExpectedException", field.start).kind == "library"
            )
            for name in field.names:
                out[name] = credited
    return out


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


def _assignment_counts(tokens, start: int, end: int) -> dict[str, int]:
    """How often each name is the target of an assignment, `++` or `--` in a token range."""
    out: dict[str, int] = {}
    for i in range(start, end):
        token = tokens[i]
        if token.kind != "ident" or (i and tokens[i - 1].text == "."):
            continue
        after = tokens[i + 1].text if i + 1 < end else ""
        before = tokens[i - 1].text if i else ""
        if after in _ASSIGNMENTS or after in ("++", "--") or before in ("++", "--"):
            out[token.text] = out.get(token.text, 0) + 1
    return out


def _field_facts(program: Program, path: str) -> tuple[dict[str, list], dict[str, list]]:
    """Each field name's literal values and whether each declaration has a Set type. A field
    keeps a value only when it is declared once and nothing in the file assigns it again."""
    key = ("field_facts", path)
    if key not in program.memo:
        unit = program.units[path]
        assigned = _assignment_counts(unit.tokens, 0, len(unit.tokens))
        values: dict[str, list] = {}
        sets: dict[str, list] = {}
        for decl in (n for n in walk(unit) if isinstance(n, TypeDecl)):
            for field in decl.fields:
                for name in field.names:
                    span = field.initializers.get(name)
                    text = _text(unit, span.start, span.end) if span else ""
                    reassigned = assigned.get(name, 0) > (1 if span else 0)
                    values.setdefault(name, []).append(None if reassigned else _number(text))
                    sets.setdefault(name, []).append(_set_type(field.type))
        program.memo[key] = (values, sets)
    return program.memo[key]


def _facts(program: Program, path: str, body: Block) -> frozenset:
    """Names bound only to a numeric literal, and names every declaration of which in scope has
    a Set type, from the file's fields and the method's parameters and locals. A name declared
    more than once, or assigned, incremented or decremented after its declaration, has no
    value."""
    unit = program.units[path]
    field_values, field_sets = _field_facts(program, path)
    values = {name: list(found) for name, found in field_values.items()}
    sets = {name: list(found) for name, found in field_sets.items()}
    assigned = _assignment_counts(unit.tokens, body.start, body.end)
    method = next((n for n in reversed(path_to(unit, body.start)) if isinstance(n, Method)), None)
    if method is not None and method.body is body:
        for param in method.params:
            values.setdefault(param.name, []).append(None)
            sets.setdefault(param.name, []).append(_set_type(param.type))
    for node in walk(body):
        if isinstance(node, Catch):
            sets.setdefault(node.name, []).append(False)
        elif isinstance(node, Lambda):
            for name in node.params:
                values.setdefault(name, []).append(None)
                sets.setdefault(name, []).append(False)
        elif isinstance(node, Statement) and node.kind == "local_var":
            tokens = unit.tokens[node.start : node.end]
            for i, token in enumerate(tokens[:-1]):
                follows = tokens[i + 1].text
                if token.kind != "ident" or follows not in ("=", ";", ","):
                    continue
                before = tokens[i - 1] if i else None
                if before is None or not (
                    before.kind in ("ident", "keyword") or before.text in (">", "]", ",")
                ):
                    continue  # not a declared name, such as a name read in an initializer
                end = i + 2
                while end < len(tokens) and tokens[end].text not in (",", ";"):
                    end += 1
                initialized = follows == "="
                value = _number(" ".join(t.text for t in tokens[i + 2 : end]))
                reassigned = assigned.get(token.text, 0) > (1 if initialized else 0)
                values.setdefault(token.text, []).append(
                    value if initialized and not reassigned else None
                )
                sets.setdefault(token.text, []).append(
                    _set_type(" ".join(t.text for t in tokens[:i]))
                )
    out = {("set", name) for name, found in sets.items() if found and all(found)}
    for name, found in values.items():
        if len(found) == 1 and found[0] is not None:
            out.add(("value", name, found[0]))
    return frozenset(out)


def _set_type(type_text: str) -> bool:
    words = [w for w in type_text.replace("<", " < ").split() if w not in ("final",)]
    base = next((w for w in words if not w.startswith("@")), "")
    return base.rsplit(".", 1)[-1].endswith("Set")


def _value(a: Assertion, text: str) -> float | None:
    number = _number(text)
    if number is not None:
        return number
    name = text.strip()
    found = [f[2] for f in a.facts if f[0] == "value" and f[1] == name]
    return found[0] if found else None


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
    return (
        tokens[name_at].text in _TIMED
        and tokens[name_at - 1].text != "."
        and _library_call(program, path, tokens[name_at].text, name_at)
    )


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


def _is_message(arg: str) -> bool:
    """A String valued argument: a lone string literal, a concatenation that starts with one
    (Java evaluates `+` left to right, so the result is a String), or a supplier lambda. A literal
    followed by anything else, such as `"0.5".length()`, is not a message."""
    if arg.startswith("( ) ->"):
        return True
    words = _without_literals(arg).split()
    return arg.startswith('"') and words[:1] == ["0"] and (len(words) == 1 or words[1] == "+")


def payload(assertion: Assertion) -> tuple[str, ...]:
    """The arguments without a message argument."""
    args = assertion.arguments
    least = _PAYLOAD.get(assertion.signature[0])
    if least is None:
        return args
    if len(args) > least and _is_message(args[-1]):
        args = args[:-1]
    if len(args) > least and _is_message(args[0]):
        args = args[1:]
    return args


def _trivially_true(assertion: Assertion) -> bool:
    name = assertion.signature[0]
    args = tuple(_strip_parens(a) for a in payload(assertion))
    if name in ("assertEquals", "assertSame") and len(args) >= 2:
        actual, expected = args[-1], args[-2]
        return actual == expected or (_constant(actual) and _constant(expected))
    if name in ("assertTrue", "assertFalse") and args:
        return (
            _constant(args[-1])
            or _same_on_both_sides(args[-1])
            or _or_true(_without_literals(args[-1]))
        )
    if name == "assertNotNull" and args:
        return _constant(args[-1]) or args[-1].startswith("new ")
    if name == "assertThat" and args:
        return _trivial_chain(assertion, args)
    return False


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
                _constant(subject) or subject.startswith("new ")
            ):
                return True
            if name in _SELF_CHECKS and call_args and call_args[0]:
                other = _strip_parens(" ".join(call_args[0]))
                if other == _strip_parens(_without_literals(subject)) or (
                    _constant(subject) and _constant(other)
                ):
                    return True
        return False
    actual = _strip_parens(_without_literals(args[-2]))
    for name, call_args in calls:
        if name in ("is", "equalTo") and call_args and call_args[0]:
            if call_args[0][0] in ("equalTo", "is"):
                continue  # the inner call is judged on its own
            other = _strip_parens(" ".join(call_args[0]))
            if other == actual or (_constant(actual) and _constant(other)):
                return True
    return False


_KIND_TWO = (
    (" containsExactly (", " containsExactlyInAnyOrder ("),
    (" containsExactlyElementsOf (", " containsExactlyInAnyOrderElementsOf ("),
    (" containsExactlyEntriesOf (", " containsExactlyInAnyOrderEntriesOf ("),
    (" contains (", " containsInAnyOrder ("),
    ("JSONCompareMode . STRICT", "JSONCompareMode . NON_EXTENSIBLE"),
)
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
    if a.signature[0] == "assertEquals" and len(payload(a)) == 3:
        return _resolved(a, payload(a)[-1])
    words = _without_literals(a.text).split()
    for names in _TOLERANCE_ORDER:
        found = _tolerance_call(words, names)
        if found is not None:
            return _resolved(a, found)
    return []


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


def _expected_types(a: Assertion) -> frozenset[str]:
    """Simple names of the exception types an assertion expects, from `X.class` arguments."""
    out = set()
    for _, args in _first_arguments(_without_literals(a.text).split(), _EXCEPTION_CALLS):
        first = args[0] if args else []
        if len(first) >= 3 and first[-2:] == [".", "class"]:
            out.add(first[-3])
    return frozenset(out)


def _json_lenient(a: Assertion) -> bool:
    if "JSONAssert" not in a.text:
        return False
    loose = ("JSONCompareMode . LENIENT", "JSONCompareMode . STRICT_ORDER")
    return any(m in a.text for m in loose) or (bool(a.arguments) and a.arguments[-1] == "false")


def _intervals(a: Assertion) -> dict[str, tuple[float | None, float | None]]:
    """Two sided bounds an assertion places on each subject: isBetween, a lower and an upper
    chain bound, or a conjunction of comparisons in assertTrue."""
    out: dict[str, tuple[float | None, float | None]] = {}
    words = _without_literals(a.text).split()
    subject = " ".join(a.arguments[:1])
    for _, args in _first_arguments(words, frozenset(["isBetween", "isStrictlyBetween"])):
        if len(args) == 2:
            out[subject] = (_value(a, " ".join(args[0])), _value(a, " ".join(args[1])))
    chain = dict(_first_arguments(words, _LOWER | _UPPER))
    lower = next((v for k, v in chain.items() if k in _LOWER), None)
    upper = next((v for k, v in chain.items() if k in _UPPER), None)
    if lower and upper:
        out[subject] = (_value(a, " ".join(lower[0])), _value(a, " ".join(upper[0])))
    if a.signature[0] == "assertTrue" and payload(a):
        bounds: dict[str, list] = {}
        for conjunct in _strip_parens(payload(a)[-1]).split(" && "):
            for op, low_if_left in ((" >= ", True), (" > ", True), (" <= ", False),
                                    (" < ", False)):  # fmt: skip
                if op in conjunct:
                    left, _, right = conjunct.partition(op)
                    left, right = _strip_parens(left), _strip_parens(right)
                    number, other, is_low = _value(a, right), left, low_if_left
                    if number is None:
                        number, other, is_low = _value(a, left), right, not low_if_left
                    if number is not None:
                        bounds.setdefault(other, [None, None])[0 if is_low else 1] = number
                    break
        for name, (low, high) in bounds.items():
            if low is not None and high is not None:
                out[name] = (low, high)
    return out


def _wider(old: dict, new: dict) -> bool:
    for subject, (low, high) in old.items():
        if subject not in new:
            continue
        new_low, new_high = new[subject]
        if None in (low, high, new_low, new_high) or new_low < low or new_high > high:
            return True
    return False


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
    if old.text == new.text:
        return None
    if _json_lenient(new) and not _json_lenient(old) and ("JSONAssert" in old.text
                                                          or _is_exact(old)):  # fmt: skip
        return "weaken_assertion/assertion_removed"  # not a named weakening, and never a match

    old_types, new_types = _expected_types(old), _expected_types(new)
    if old_types and new_types and new_types & _BROAD and not old_types & _BROAD:
        return "weaken_assertion/expected_exception_broadened"
    if (
        "isExactlyInstanceOf" in old.signature
        and "isInstanceOf" in new.signature
        and ("isExactlyInstanceOf" not in new.signature)
    ):
        return "weaken_assertion/expected_exception_broadened"
    if old.signature[0].startswith("assertThatThrownBy") and new.signature[0] == "assertThatCode":
        return "weaken_assertion/expected_exception_broadened"
    if old.signature[0] == "verify" and new.signature[0] == "verify":
        if _LOOSE_MODES & set(new.signature) and not _LOOSE_MODES & set(old.signature):
            return "weaken_assertion/verification_mode_relaxed"
    equals = old.signature[0] == new.signature[0] == "assertEquals"
    if equals and len(payload(new)) == 3 and len(payload(old)) == 2:
        return "weaken_assertion/tolerance_widened"
    if old.signature == new.signature:
        old_tolerance, new_tolerance = _tolerance(old), _tolerance(new)
        if old_tolerance and (
            new_tolerance is None or (new_tolerance and max(new_tolerance) > max(old_tolerance))
        ):
            return "weaken_assertion/tolerance_widened"
        if _wider(_intervals(old), _intervals(new)):
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
                    and (not new.scanned_only or (old.scanned_only and new.origin == old.origin))
                    and _guard_ok(old, new)
                ),
                None,
            )
        )
        if same is not None:
            pool.remove(same)
        else:
            changed.append(old)
    for old in changed:
        uses_rebound = rebinds(old)
        usable = [n for n in pool if n.credited and not n.scanned_only and _guard_ok(old, n)]
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
            if matched.text != old.text and old.subject and not old.subject & matched.subject:
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


def _import_keys(unit: CompilationUnit) -> set[tuple[str, bool, bool]]:
    return {(i.name, i.static, i.wildcard) for i in unit.imports}


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
    for name, static, wildcard in _import_keys(change.before) ^ _import_keys(change.after):
        if wildcard:
            # A type import on demand binds only type names; a static one binds members too.
            names.add("*" if static else "*types")
            if not static and _supertype_through_wildcard(change, ctx):
                # A swapped on demand import can rebind an unqualified supertype, and with it
                # every inherited assertion method.
                names.add("*")
        else:
            names.add(name.rsplit(".", 1)[-1])
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
            for text in _split_array(value):
                out.append(Assertion(
                    f"assertThrows ( {text} )", ("assertThrows",), (text,), (), frozenset(),
                    credited, annotation.line, annotation.start,
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
