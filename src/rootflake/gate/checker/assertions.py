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
from rootflake.gate.checker.survive import SKIP_EXCEPTIONS
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


def _guard(unit: CompilationUnit, body: Block, index: int) -> tuple[str, ...]:
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
            guard.append("lambda")
        elif isinstance(node, TypeDecl):
            guard.append("class")
    return tuple(guard)


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


def _exits(unit: CompilationUnit, body: Block) -> list[tuple[int, str]]:
    out = []
    for node in walk(body):
        if isinstance(node, Statement) and node.kind in _EXITS:
            words = {t.text for t in unit.tokens[node.start : node.end]}
            if node.kind == "throw" and words & SKIP_EXCEPTIONS:
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


def collect(program: Program, path: str, body: Block) -> list[Assertion]:
    unit = program.units[path]
    exits = _exits(unit, body)
    out: list[Assertion] = []
    consumed: set[int] = set()
    for call in calls_in(unit, body):
        if call.start in consumed or call.reference or not is_assertion_name(call.name):
            continue
        if program.methods_by_name.get(call.name):
            continue  # a helper the checker reads: its assertions come in by following it
        if call.receiver not in (None, "<expr>") and not call.receiver[:1].isupper():
            continue
        names, end = _chain(unit, call)
        start = call.receiver_at if call.receiver not in (None, "<expr>") else call.start
        for inner in calls_in(unit, _Span(call.start + 1, end)):
            consumed.add(inner.start)
        matchers = tuple(
            inner.name
            for inner in calls_in(unit, _Span(call.start + 1, end))
            if inner.receiver is None
            and inner.name in MATCHER_FACTORIES
            and _library_call(program, path, inner.name, inner.start)
        )
        credited = _library_call(program, path, call.name, call.start) or (
            call.receiver not in (None, "<expr>")
            and program.resolve_type(path, call.receiver, call.receiver_at).kind == "library"
            and not program.methods_by_name.get(call.name)
        )
        out.append(
            Assertion(
                text=_text(unit, start, end),
                signature=(*names, *matchers),
                arguments=_split_arguments(unit, call.start + 1, _close(unit, call.start + 1)),
                guard=_guard(unit, body, call.start),
                exits_before=frozenset(text for at, text in exits if at < call.start),
                credited=credited,
                line=unit.tokens[call.start].line,
                start=call.start,
            )
        )
    for node in walk(body):
        if isinstance(node, Statement) and node.kind == "assert":
            out.append(
                Assertion(_text(unit, node.start, node.end), ("assert",), (), _guard(unit, body,
                          node.start), frozenset(t for at, t in exits if at < node.start), True,
                          node.line, node.start)
            )  # fmt: skip
    return out


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
        guard = parent_guard + _guard(unit, body, site)
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
                    False, origin,
                )
            )  # fmt: skip
    return out


def _as_scanned(assertion: Assertion) -> Assertion:
    return Assertion(
        assertion.text, assertion.signature, assertion.arguments, assertion.guard,
        assertion.exits_before, False, assertion.line, assertion.start, True, assertion.origin,
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


def _same_on_both_sides(arg: str) -> bool:
    for operator in (" == ", " != "):
        if operator in arg:
            left, _, right = arg.partition(operator)
            if _strip_parens(left) == _strip_parens(right):
                return True
    return False


def _trivially_true(assertion: Assertion) -> bool:
    name = assertion.signature[0]
    args = tuple(_strip_parens(a) for a in assertion.arguments)
    if name in ("assertEquals", "assertSame") and len(args) >= 2:
        actual, expected = args[-1], args[-2]
        return actual == expected or (_constant(actual) and _constant(expected))
    if name in ("assertTrue", "assertFalse") and args:
        return _constant(args[-1]) or _same_on_both_sides(args[-1])
    if name == "assertNotNull" and args:
        return _constant(args[-1]) or args[-1].startswith("new ")
    return False


def _match(before: list[Assertion], after: list[Assertion]) -> list[Assertion]:
    """The before assertions left without a match."""
    pool = list(after)
    unmatched = []
    for old in before:
        same = next(
            (
                new
                for new in pool
                if new.text == old.text
                and (not new.scanned_only or (old.scanned_only and new.origin == old.origin))
                and _is_subsequence(new.guard, old.guard)
                and new.exits_before <= old.exits_before
            ),
            None,
        )
        if same is not None:
            pool.remove(same)
            continue
        kind_one = next(
            (
                new
                for new in pool
                if new.credited
                and not new.scanned_only
                and new.signature == old.signature
                and _is_subsequence(new.guard, old.guard)
                and new.exits_before <= old.exits_before
                and not _trivially_true(new)
            ),
            None,
        )
        if kind_one is not None:
            pool.remove(kind_one)
            continue
        unmatched.append(old)
    return unmatched


def _methods(program: Program, path: str) -> dict[tuple, Method]:
    out = {}
    for decl in program.all_types(path):
        if decl.kind == "anonymous":
            continue
        fqn = program.fqn[id(decl)]
        for method in decl.methods:
            out.setdefault((fqn, method.name, tuple(p.type for p in method.params)), method)
    return out


def _file(change: FileChange, ctx: Context) -> Iterator[Finding]:
    before = _methods(ctx.before, change.path)
    after = _methods(ctx.after, change.path)
    for key, old in before.items():
        new = after.get(key)
        if new is None:
            continue  # a vanished method is Decision 6's disable/test_removed, reported once
        old_set = _both_ways(ctx.before, change.path, old, ctx.budget)
        if not old_set:
            continue
        new_set = _both_ways(ctx.after, change.path, new, ctx.budget) if new else []
        for missing in _match(old_set, new_set):
            line = new.line if new is not None else missing.line
            yield Finding("weaken_assertion/assertion_removed", change.path, line)
            break


def findings(ctx: Context) -> Iterator[Finding]:
    for change in ctx.changes:
        if change.before is not None and change.path in ctx.test_paths:
            yield from _file(change, ctx)
