"""Swallowed failures in test code (ADR 0007 Decision 8).

A new or widened `catch` whose body does not end in a rethrow, a throw that is not an
assumption, or a `fail` is rejected when its `try` covers a check, assigns a value a later
assertion reads, or is the last thing its method or lambda does. The try and fail idiom and the
interrupt idiom are allowed on the terms the ADR sets. Exits from a new `catch` or `finally`,
retry loops, added expected exception declarations and exception handler extensions are
rejected too.
"""

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass

from rootflake.gate.checker.assertions import (
    AWAIT_ROOTS,
    WAIT_CONDITIONS,
    _chain_root,
    _close,
    _text,
    is_assertion_name,
)
from rootflake.gate.checker.calls import Call, calls_in, follow_for_scan, reach
from rootflake.gate.checker.model import Context, FileChange, Finding
from rootflake.gate.checker.poll import initializer_spans
from rootflake.gate.checker.retries import Site, added, member_key, resolve
from rootflake.gate.checker.scope import Program
from rootflake.gate.checker.syntax import (
    Block,
    Catch,
    CompilationUnit,
    Field,
    Lambda,
    Method,
    Statement,
    TypeDecl,
)
from rootflake.gate.checker.timing import _RESULT_WAITS, _WAITS, _argument_bounds, _arguments
from rootflake.gate.checker.tree import path_to, walk

CATCH = "swallow_failure/catch_swallows"
EXIT = "swallow_failure/exit_in_catch"
RETRY_LOOP = "swallow_failure/retry_loop"
EXPECTED = "swallow_failure/expected_exception_added"
HANDLER = "swallow_failure/exception_handler"

_LOOPS = frozenset(["for", "while", "do"])
_CHECK_PREFIXES = ("assert", "verify", "check", "expect")
_CLOSING = frozenset(["fail", "failBecauseExceptionWasNotThrown", "shouldHaveThrown"])
_ASSUMPTION_THROWS = frozenset(
    ["AssumptionViolatedException", "TestAbortedException", "SkipException"]
)
# AssertionError and its subclasses in the libraries of section 15 of the semantics note, with
# the classes above it, by simple name: a catch of any of them catches a real failure.
_FAILURE_TYPES = frozenset([
    "Throwable", "Error", "AssertionError", "AssertionFailedError", "MultipleFailuresError",
    "ComparisonFailure", "ArrayComparisonFailure", "MockitoAssertionError", "WantedButNotInvoked",
    "TooFewActualInvocations", "TooManyActualInvocations", "NeverWantedButInvoked",
    "NoInteractionsWanted", "VerificationInOrderFailure", "ArgumentsAreDifferent",
    "AssertionErrorWithFacts", "ComparisonFailureWithFacts",
])  # fmt: skip
_HANDLERS = frozenset(["TestExecutionExceptionHandler", "LifecycleMethodExecutionExceptionHandler"])
_HANDLER_PACKAGE = "org.junit.jupiter.api.extension"


@dataclass
class _Span:
    start: int
    end: int


# -- what a try block covers ----------------------------------------------------------------


def _checking(unit: CompilationUnit, call: Call, prefixes: bool) -> bool:
    """Whether the call is a check: an assertion, a verify, an expected exception check, a
    bounded wait, an Awaitility condition, or (where prefixes) any call named like a check."""
    if call.reference:
        return False
    name = call.name
    if is_assertion_name(name) or name in _CLOSING or name == "assertThrows":
        return True
    if prefixes and name.startswith(_CHECK_PREFIXES):
        return True
    if name in _RESULT_WAITS:
        return True
    if name in _WAITS and _argument_bounds(_arguments(unit, call), 0)[0]:
        return True
    return name in WAIT_CONDITIONS and _chain_root(unit, call.start) in AWAIT_ROOTS


def _asserts(unit: CompilationUnit, span) -> bool:
    return any(
        isinstance(n, Statement) and n.kind == "assert" and span.start <= n.start < span.end
        for n in walk(unit)
    )


def _covers(ctx: Context, path: str, span, skip: int | None = None, prefixes: bool = True) -> bool:
    """Whether the code in span checks anything, directly or in the test code its calls, lambdas
    and method references reach. The call at skip, the idiom's closing call, is left out."""
    program = ctx.after
    unit = program.units[path]
    calls = [c for c in calls_in(unit, span) if c.start != skip]
    if any(_checking(unit, c, prefixes) for c in calls) or _asserts(unit, span):
        return True
    roots = [target for c in calls for target in follow_for_scan(program, path, c)]
    for reached in reach(program, roots, True, ctx.budget):
        if reached.path not in ctx.test_paths or reached.method.body is None:
            continue
        inner = program.units[reached.path]
        body = reached.method.body
        if any(_checking(inner, c, False) for c in calls_in(inner, body)):
            return True
        if _asserts(inner, body):
            return True
    return False


# -- the parts of a try statement -----------------------------------------------------------


def _swallows(unit: CompilationUnit, catch: Catch) -> bool:
    """Whether the catch body does not end in a rethrow, a throw that is not an assumption, or
    a fail."""
    statements = catch.block.statements
    if not statements:
        return True
    last = statements[-1]
    if last.kind == "throw":
        words = [t.text for t in unit.tokens[last.start : last.end]]
        if len(words) > 2 and words[1] == "new":
            k = 2
            while k + 2 < len(words) and words[k + 1] == ".":
                k += 2
            return words[k] in _ASSUMPTION_THROWS
        return False
    if last.kind == "expression":
        calls = [c for c in calls_in(unit, last) if not c.reference]
        if calls and calls[0].name == "fail":
            return False
    return True


def _simple(type_name: str) -> str:
    return type_name.split("<", 1)[0].rsplit(".", 1)[-1]


def _closing_call(ctx: Context, unit: CompilationUnit, block: Block) -> Call | None:
    """The idiom's closing call, when the try block's last statement is one."""
    if not block.statements or block.statements[-1].kind != "expression":
        return None
    last = block.statements[-1]
    calls = [c for c in calls_in(unit, last) if not c.reference]
    if not calls or calls[0].name not in _CLOSING:
        return None
    if ctx.after.methods_by_name.get(calls[0].name):
        return None  # a closing name a read file declares voids the idiom
    return calls[0]


def _leaves_before(unit: CompilationUnit, block: Block, at: int) -> bool:
    """Whether a return, break, continue or throw before at can leave the try block."""
    for node in walk(block):
        if not isinstance(node, Statement) or node.start >= at:
            continue
        if node.kind not in ("return", "throw", "break", "continue"):
            continue
        inner = path_to(unit, node.start)
        below = inner[next(k for k, n in enumerate(inner) if n is block) + 1 :]
        if any(isinstance(n, (Lambda, TypeDecl)) for n in below):
            continue
        if node.kind in ("break", "continue") and any(
            isinstance(n, Statement) and (n.kind in _LOOPS or n.kind == "switch")
            for n in below[:-1]
        ):
            continue
        return True
    return False


def _statement_texts(unit: CompilationUnit, block: Block) -> list[str]:
    return [_text(unit, n.start, n.end) for n in _simple_statements(block)]


def _outside_tries(unit: CompilationUnit | None, key: tuple) -> set[str]:
    """The texts of the simple statements a pristine member runs outside any try block."""
    if unit is None:
        return set()
    out = set()
    for node in walk(unit):
        if not isinstance(node, Statement) or node.blocks or node.kind == "local_type":
            continue
        nodes = path_to(unit, node.start)
        if _owner_key(unit, nodes) != key:
            continue
        guarded = any(
            isinstance(n, Statement) and n.kind == "try" and n.blocks
            and nodes[k + 1] is n.blocks[0]
            for k, n in enumerate(nodes[:-1])
        )  # fmt: skip
        if not guarded:
            out.add(_text(unit, node.start, node.end))
    return out


def _owner_key(unit: CompilationUnit, nodes: list) -> tuple:
    return tuple(
        (type(n).__name__, getattr(n, "name", None), getattr(n, "line", None))
        for n in nodes
        if isinstance(n, (TypeDecl, Method))
    )


def _body_of(nodes: list) -> Block | None:
    """The body of the innermost method or block lambda around a statement."""
    for node in reversed(nodes):
        if isinstance(node, Lambda):
            return node.body
        if isinstance(node, Method):
            return node.body
    return None


def _follows(unit: CompilationUnit, nodes: list, statement: Statement) -> bool:
    """Whether anything runs after the statement in its method or lambda body."""
    body = _body_of(nodes)
    if body is None:
        return True
    for node in reversed(nodes):
        if node is statement:
            continue
        if isinstance(node, Statement) and node.kind in _LOOPS:
            return True  # the loop runs its body again
        if node is body:
            break
    return any(isinstance(n, Statement) and n.start >= statement.end for n in walk(body))


def _assigned(unit: CompilationUnit, block: Block) -> set[str]:
    out = set()
    tokens = unit.tokens
    for node in walk(block):
        if not isinstance(node, Statement):
            continue
        if node.kind == "local_var":
            out.update(name for name, _, _ in initializer_spans(tokens, node.start, node.end))
        for k in range(node.start + 1, min(node.end, len(tokens))):
            t = tokens[k]
            if t.kind == "op" and t.text.endswith("=") and t.text not in ("==", "!=", "<=", ">="):
                if tokens[k - 1].kind == "ident":
                    out.add(tokens[k - 1].text)
        for k in range(node.start, min(node.end, len(tokens)) - 1):
            if tokens[k].kind == "ident" and tokens[k + 1].text in ("++", "--"):
                out.add(tokens[k].text)
    return out


def _read_later(ctx: Context, path: str, nodes: list, statement: Statement, names: set) -> bool:
    """Whether a later assertion of the method reads a name, directly or through later
    assignments."""
    unit = ctx.after.units[path]
    body = _body_of(nodes)
    if body is None or not names:
        return False
    tokens = unit.tokens
    tracked = set(names)
    later = sorted(
        (n for n in walk(body) if isinstance(n, Statement) and n.start >= statement.end
         and not n.blocks),
        key=lambda n: n.start,
    )  # fmt: skip
    for node in later:
        words = {tokens[k].text for k in range(node.start, node.end) if tokens[k].kind == "ident"}
        if not words & tracked:
            continue
        span = _Span(node.start, node.end)
        if node.kind == "assert" or any(_checking(unit, c, True) for c in calls_in(unit, span)):
            return True
        tracked |= _assigned(unit, Block([node], node.start, node.end))
    return False


def _idiom_holds(ctx: Context, unit: CompilationUnit, statement: Statement) -> Call | None:
    block = statement.blocks[0]
    closing = _closing_call(ctx, unit, block)
    if closing is None or _leaves_before(unit, block, closing.start):
        return None
    if any(_simple(t) in _FAILURE_TYPES for c in statement.catches for t in c.types):
        return None
    return closing


def _interrupt_idiom(unit: CompilationUnit, statement: Statement) -> bool:
    catches = statement.catches
    if len(catches) != 1 or [_simple(t) for t in catches[0].types] != ["InterruptedException"]:
        return False
    return any(c.name == "interrupt" for c in calls_in(unit, catches[0].block))


# -- tries the patch adds or widens ---------------------------------------------------------


def _tries(unit: CompilationUnit) -> list[tuple[tuple, Statement]]:
    out = []
    for node in walk(unit):
        if isinstance(node, Statement) and node.kind == "try" and node.catches and node.blocks:
            nodes = path_to(unit, node.start)
            types = tuple(sorted(_simple(t) for c in node.catches for t in c.types))
            out.append(((_owner_key(unit, nodes), types), node))
    return out


def _judged(change: FileChange) -> list[tuple[Statement, list[Statement] | None]]:
    """The tries of the patched file that are new, with None, and those that now hold simple
    statements the pristine try of the same member and catch types did not, with those."""
    old: dict[tuple, list[Statement]] = {}
    if change.before is not None:
        for key, node in _tries(change.before):
            old.setdefault(_rekey(change.before, key), []).append(node)
    out = []
    for key, node in _tries(change.after):
        twins = old.get(_rekey(change.after, key), [])
        if not twins:
            out.append((node, None))
            continue
        twin = twins.pop(0)
        left = Counter(_statement_texts(change.before, twin.blocks[0]))
        fresh = []
        for inner in _simple_statements(node.blocks[0]):
            text = _text(change.after, inner.start, inner.end)
            if left[text]:
                left[text] -= 1
            else:
                fresh.append(inner)
        if fresh:
            out.append((node, fresh))
    return out


def _simple_statements(block: Block) -> list[Statement]:
    return [
        n for n in walk(block) if isinstance(n, Statement) and not n.blocks
        and n.kind != "local_type"
    ]  # fmt: skip


def _rekey(unit: CompilationUnit, key: tuple) -> tuple:
    owner, types = key
    return tuple((kind, name) for kind, name, _ in owner), types


def _in_loop(nodes: list, statement: Statement) -> bool:
    for node in reversed(nodes):
        if isinstance(node, (Method, Lambda)):
            return False
        if node is not statement and isinstance(node, Statement) and node.kind in _LOOPS:
            return True
    return False


def _try_findings(ctx: Context, change: FileChange) -> Iterator[Finding]:
    unit = change.after
    for statement, fresh in _judged(change):
        if not any(_swallows(unit, c) for c in statement.catches):
            continue
        nodes = path_to(unit, statement.start)
        block = statement.blocks[0]
        key = tuple((kind, name) for kind, name, _ in _owner_key(unit, nodes))
        pristine = _outside_by_name(change.before, key) if change.before is not None else set()
        moved_in = bool(pristine & set(_statement_texts(unit, block)))
        closing = None if moved_in else _idiom_holds(ctx, unit, statement)
        skip = closing.start if closing is not None else None
        if fresh is None:  # a new try is judged whole
            covers = _covers(ctx, change.path, block, skip)
            names = _assigned(unit, block)
            last = closing is None and not _follows(unit, nodes, statement)
        else:  # a widened one by what it newly holds
            covers = any(_covers(ctx, change.path, s, skip) for s in fresh)
            names = set().union(*(_assigned(unit, Block([s], s.start, s.end)) for s in fresh))
            last = False
        if _interrupt_idiom(unit, statement) and not covers:
            continue
        assigned = _read_later(ctx, change.path, nodes, statement, names)
        if covers or assigned or last:
            yield Finding(CATCH, change.path, statement.line)
            if covers and _in_loop(nodes, statement):
                yield Finding(RETRY_LOOP, change.path, statement.line)


def _outside_by_name(unit: CompilationUnit, key: tuple) -> set[str]:
    owners = {
        _owner_key(unit, path_to(unit, n.start))
        for n in walk(unit)
        if isinstance(n, Statement)
        and tuple((k, nm) for k, nm, _ in _owner_key(unit, path_to(unit, n.start))) == key
    }
    out: set[str] = set()
    for owner in owners:
        out |= _outside_tries(unit, owner)
    return out


# -- exits, expected exceptions and handlers ------------------------------------------------


def _exit_sites(program: Program, path: str) -> list[Site]:
    unit = program.units[path]
    sites = []
    for node in walk(unit):
        if not isinstance(node, Statement) or node.kind != "try":
            continue
        handlers = [(c.block, "catch") for c in node.catches]
        if node.finally_block is not None:
            handlers.append((node.finally_block, "finally"))
        for block, where in handlers:
            for inner in walk(block):
                if not isinstance(inner, Statement):
                    continue
                if inner.kind not in ("return", "break", "continue"):
                    continue
                nested = path_to(unit, inner.start)
                below = nested[next(k for k, n in enumerate(nested) if n is block) + 1 :]
                if any(isinstance(n, (Lambda, TypeDecl)) for n in below):
                    continue
                if inner.kind != "return" and any(
                    isinstance(n, Statement) and (n.kind in _LOOPS or n.kind == "switch")
                    for n in below[:-1]
                ):
                    continue
                label = f"{where} {_text(unit, inner.start, inner.end)}"
                key = member_key(program, unit, inner.start)
                sites.append(Site(key, label, EXIT, inner.line, inner.start))
    return sites


def _expected_sites(program: Program, path: str) -> list[Site]:
    unit = program.units[path]
    sites = []
    for node in walk(unit):
        if isinstance(node, Method):
            for annotation in node.annotations:
                if annotation.name.rsplit(".", 1)[-1] != "Test" or annotation.arguments is None:
                    continue
                binding = resolve(program, path, annotation.name, annotation.start + 1)
                if binding.fqn not in (None, "org.junit.Test") and binding.kind != "unresolved":
                    continue
                tokens = unit.tokens
                span = range(annotation.arguments.start, annotation.arguments.end - 1)
                if any(tokens[k].text == "expected" and tokens[k + 1].text == "=" for k in span):
                    key = member_key(program, unit, node.start)
                    sites.append(Site(key, "expected", EXPECTED, annotation.line, node.start))
        elif isinstance(node, Field) and _simple(node.type) == "ExpectedException":
            names = {a.name.rsplit(".", 1)[-1] for a in node.annotations}
            if names & {"Rule", "ClassRule"}:
                key = member_key(program, unit, node.start)
                sites.append(Site(key, "rule", EXPECTED, node.line, node.start))
    return sites


def _handler_sites(program: Program, path: str) -> list[Site]:
    unit = program.units[path]
    sites = []
    for decl in program.all_types(path):
        for name in decl.implements:
            simple = _simple(name)
            if simple not in _HANDLERS:
                continue
            binding = resolve(program, path, name, decl.start, header=True)
            if binding.kind == "declared":
                continue
            fqn = binding.fqn or ""
            if binding.kind != "unresolved" and not fqn.startswith(_HANDLER_PACKAGE + "."):
                continue
            key = member_key(program, unit, decl.start)
            sites.append(Site(key, f"implements {simple}", HANDLER, decl.line, decl.start))
    return sites


def _site_findings(ctx: Context, path: str) -> Iterator[Finding]:
    for kind in (_exit_sites, _expected_sites, _handler_sites):
        old = kind(ctx.before, path) if path in ctx.before.units else []
        for site in added(old, kind(ctx.after, path)):
            yield Finding(site.code, path, site.line)


# -- helpers a new call reaches -------------------------------------------------------------


def _new_calls(change: FileChange) -> list[Call]:
    """The calls in statements the patch adds to a member."""
    unit = change.after

    def statements(u: CompilationUnit) -> list[tuple[tuple, str, Statement]]:
        out = []
        for node in walk(u):
            if isinstance(node, Statement) and not node.blocks and node.kind != "local_type":
                nodes = path_to(u, node.start)
                key = tuple((k, nm) for k, nm, _ in _owner_key(u, nodes))
                out.append((key, _text(u, node.start, node.end), node))
        return out

    old = Counter()
    if change.before is not None:
        old = Counter((k, t) for k, t, _ in statements(change.before))
    calls = []
    for key, text, node in statements(unit):
        if old[(key, text)]:
            old[(key, text)] -= 1
            continue
        calls.extend(c for c in calls_in(unit, node) if not c.reference)
    return calls


def _helper_findings(ctx: Context, change: FileChange) -> Iterator[Finding]:
    program = ctx.after
    unit = program.units[change.path]
    for call in _new_calls(change):
        targets = follow_for_scan(program, change.path, call)
        if not targets:
            continue
        arguments = _Span(call.end, _close(unit, call.end))
        passes_check = _covers(ctx, change.path, arguments)
        for reached in reach(program, targets, True, ctx.budget):
            body = reached.method.body
            if body is None:
                continue
            test_code = reached.path in ctx.test_paths
            inner = program.units[reached.path]
            params = {p.name for p in reached.method.params}
            for node in walk(body):
                if not isinstance(node, Statement) or node.kind != "try" or not node.catches:
                    continue
                if not any(_swallows(inner, c) for c in node.catches):
                    continue
                block = node.blocks[0]
                runs_param = any(c.receiver in params for c in calls_in(inner, block))
                if (runs_param and reached.depth == 0 and passes_check) or _covers(
                    ctx, reached.path, block, prefixes=test_code
                ):
                    yield Finding(CATCH, change.path, unit.tokens[call.start].line)
                    break


def findings(ctx: Context) -> Iterator[Finding]:
    seen = set()
    for change in ctx.changes:
        if change.path not in ctx.test_paths:
            continue
        found = [
            *_try_findings(ctx, change),
            *_site_findings(ctx, change.path),
            *_helper_findings(ctx, change),
        ]
        for finding in found:
            if finding not in seen:
                seen.add(finding)
                yield finding
