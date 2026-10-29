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
    _declarators,
    _text,
    is_assertion_name,
)
from rootflake.gate.checker.calls import Call, calls_in, follow_for_scan, reach
from rootflake.gate.checker.model import Context, FileChange, Finding
from rootflake.gate.checker.poll import initializer_spans
from rootflake.gate.checker.retries import Site, _library_wildcards, added, member_key, resolve
from rootflake.gate.checker.scope import Program, is_library
from rootflake.gate.checker.survive import loop_key
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
# What a call that runs the test is named in a JUnit 4 rule's Statement and a Jupiter
# Invocation: a swallowing try around it swallows every check the test makes.
_RUNS_THE_TEST = frozenset(["evaluate", "proceed"])
_FUTURE_TYPES = frozenset([
    "Future", "CompletableFuture", "FutureTask", "ForkJoinTask", "ScheduledFuture",
    "RunnableFuture", "RecursiveTask", "ListenableFuture",
])  # fmt: skip
# Calls that hand back a future, so a get or join chained on one waits for it.
_FUTURE_SOURCES = frozenset([
    "submit", "supplyAsync", "runAsync", "schedule", "thenApply", "thenApplyAsync",
    "thenAccept", "thenAcceptAsync", "thenRun", "thenRunAsync", "thenCompose",
    "thenComposeAsync", "thenCombine", "thenCombineAsync", "whenComplete", "whenCompleteAsync",
    "handle", "handleAsync", "exceptionally", "allOf", "anyOf", "toCompletableFuture",
])  # fmt: skip
# Waits that hand back a result: the interrupt idiom holds only when that result is checked.
_VALUE_WAITS = frozenset(["await", "awaitTermination", "tryAcquire", "tryLock", "poll"])
# Calls that store into a container a later assertion can read.
_STORES = frozenset([
    "add", "addAll", "put", "putAll", "putIfAbsent", "offer", "push", "set", "append",
    "addFirst", "addLast", "offerFirst", "offerLast", "compute", "merge", "accept",
])  # fmt: skip
_HANDLERS = frozenset(["TestExecutionExceptionHandler", "LifecycleMethodExecutionExceptionHandler"])
_HANDLER_PACKAGE = "org.junit.jupiter.api.extension"


@dataclass
class _Span:
    start: int
    end: int


# -- what a try block covers ----------------------------------------------------------------


def _checking(
    unit: CompilationUnit, call: Call, prefixes: bool, waits: bool = True, runs: bool = True
) -> bool:
    """Whether the call is a check: an assertion, a verify, an expected exception check, a
    bounded wait, a wait on a future, an Awaitility condition, a call that runs the test from a
    rule or an interceptor, or (where prefixes) any call named like a check. Without waits,
    only the checks that are not waits count."""
    if call.reference:
        return False
    name = call.name
    if not waits and (name in _WAITS or name in WAIT_CONDITIONS or name in AWAIT_ROOTS):
        return False
    if is_assertion_name(name) or name in _CLOSING or name == "assertThrows":
        return True
    if prefixes and name.startswith(_CHECK_PREFIXES):
        return True
    if name in _RUNS_THE_TEST and call.receiver_at is not None and not _arguments(unit, call):
        return runs
    if not waits:
        return False
    if name in _RESULT_WAITS:
        return True
    if name in _WAITS and _argument_bounds(_arguments(unit, call), 0)[0]:
        return True
    if name in ("get", "join") and not _arguments(unit, call) and _on_a_future(unit, call):
        return True
    return name in WAIT_CONDITIONS and _chain_root(unit, call.start) in AWAIT_ROOTS


def _on_a_future(unit: CompilationUnit, call: Call) -> bool:
    """Whether a call is made on a future: a local or parameter declared as one, or the result
    of a call that hands one back."""
    tokens = unit.tokens
    at = call.start
    if at < 2 or tokens[at - 1].text != ".":
        return False
    if tokens[at - 2].text == ")":
        depth, k = 0, at - 2
        while k >= 0:
            if tokens[k].text == ")":
                depth += 1
            elif tokens[k].text == "(":
                depth -= 1
                if depth == 0:
                    break
            k -= 1
        return k > 0 and tokens[k - 1].text in _FUTURE_SOURCES
    return _declared_type(unit, at - 2) in _FUTURE_TYPES


def _declared_type(unit: CompilationUnit, j: int) -> str | None:
    """The simple type, without type arguments, of the local or parameter named at token j."""
    tokens = unit.tokens
    if tokens[j].kind != "ident" or (j and tokens[j - 1].text == "."):
        return None
    name = tokens[j].text
    for node in reversed(path_to(unit, j)):
        if isinstance(node, Lambda):
            if name in node.params:
                return None
        elif isinstance(node, Method):
            for p in node.params:
                if p.name == name:
                    return _simple(p.type)
            return None
        elif isinstance(node, TypeDecl):
            return None
        elif isinstance(node, Block):
            for statement in node.statements:
                if statement.start >= j:
                    break
                if statement.kind != "local_var":
                    continue
                at = [k for k in _declarators(tokens, statement.start, statement.end - 1)
                      if tokens[k].text == name]  # fmt: skip
                if at:
                    words = [t.text for t in tokens[statement.start : at[0]]]
                    while words and words[0] in ("final",):
                        words.pop(0)
                    return words[0] if words and words[0] != "var" else None
    return None


def _asserts(unit: CompilationUnit, span) -> bool:
    nodes = walk(span) if isinstance(span, Block | Statement) else walk(unit)
    return any(
        isinstance(n, Statement) and n.kind == "assert" and span.start <= n.start < span.end
        for n in nodes
    )


def _covers(
    ctx: Context,
    path: str,
    span,
    skip: int | None = None,
    prefixes: bool = True,
    program: Program | None = None,
    waits: bool = True,
    runs: bool = True,
) -> bool:
    """Whether the code in span checks anything, directly or in the code its calls, lambdas
    and method references reach; a reached method counts through a check by name or a wait,
    never through a name prefix. The call at skip, the idiom's closing call, is left out."""
    program = ctx.after if program is None else program
    unit = program.units[path]
    calls = [c for c in calls_in(unit, span) if c.start != skip]
    if any(_checking(unit, c, prefixes, waits, runs) for c in calls) or _asserts(unit, span):
        return True
    roots = [target for c in calls for target in follow_for_scan(program, path, c)]
    for reached in reach(program, roots, True, ctx.budget):
        if reached.method.body is None:
            continue
        inner = program.units[reached.path]
        body = reached.method.body
        if any(_checking(inner, c, False, waits, runs) for c in calls_in(inner, body)):
            return True
        if _asserts(inner, body):
            return True
    return False


# -- the parts of a try statement -----------------------------------------------------------


def _swallows(program: Program, path: str, catch: Catch) -> bool:
    """Whether the catch body does not end in a rethrow, a throw that is not an assumption, or
    a fail that resolves to a test library."""
    unit = program.units[path]
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
            return _aborts(program, path, ".".join(words[2 : k + 1]), last.start + 2)
        return False
    if last.kind == "expression":
        calls = [c for c in calls_in(unit, last) if not c.reference]
        if calls and calls[0].name == "fail":
            if calls[0].receiver_at is None:
                return not _library_name(program, unit, "fail")
            return not _library_receiver(program, path, calls[0])
    return True


def _library_receiver(program: Program, path: str, call: Call) -> bool:
    """Whether a qualified call's receiver, read with any package before it, resolves to a
    test library type, as `Assert.fail` and `org.junit.Assert.fail` do."""
    tokens, j = program.units[path].tokens, call.receiver_at
    parts = [tokens[j].text]
    while j >= 2 and tokens[j - 1].text == "." and tokens[j - 2].kind == "ident":
        parts.insert(0, tokens[j - 2].text)
        j -= 2
    binding = program.resolve_type(path, ".".join(parts), j)
    if binding.kind == "library":
        return True
    unit = program.units[path]
    return (
        binding.kind == "unresolved"
        and len(parts) == 1
        and _library_wildcards(unit)
        and any(imp.wildcard and not imp.static and is_library(imp.name) for imp in unit.imports)
    )  # the stricter reading of Decision 8: only library and JDK packages are on demand


def _aborts(program: Program, path: str, name: str, index: int) -> bool:
    """Whether a thrown type is an assumption or abort type, or a read subclass of one."""
    if _simple(name) in _ASSUMPTION_THROWS:
        return True
    binding = resolve(program, path, name, index)
    if binding.kind != "declared":
        return False
    chain = [binding.decl, *(decl for _, decl in program.supertypes(binding.path, binding.decl))]
    return any(_simple(n) in _ASSUMPTION_THROWS for decl in chain for n in decl.extends)


def _library_name(program: Program, unit: CompilationUnit, name: str) -> bool:
    """Whether an unqualified call of name can only reach a test library: no read file
    declares a method of that name, and the file imports no static member of that name, or
    static members on demand, from anywhere else."""
    if program.methods_by_name.get(name):
        return False
    for imp in unit.imports:
        if not imp.static or is_library(imp.name):
            continue
        if imp.wildcard or imp.name.rsplit(".", 1)[-1] == name:
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
    if calls[0].receiver_at is None and not _library_name(ctx.after, unit, calls[0].name):
        return None  # a closing name a read file declares or imports from elsewhere voids it
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


def _owner_key(unit: CompilationUnit, nodes: list) -> tuple:
    """The types and methods around a statement, a method by its name and parameter types."""
    return tuple(
        (
            type(n).__name__,
            f"{n.name}({','.join(p.type for p in n.params)})" if isinstance(n, Method) else n.name,
            n.line,
        )
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
        for k in range(node.start, min(node.end, len(tokens)) - 2):
            if tokens[k].kind != "ident" or (k and tokens[k - 1].text == "."):
                continue
            if tokens[k + 1].text == "[":
                close = _close(unit, k + 1) + 1
                after = tokens[close].text if close < len(tokens) else ""
                if after.endswith("=") and after not in ("==", "!=", "<=", ">="):
                    out.add(tokens[k].text)
                elif after in ("++", "--"):
                    out.add(tokens[k].text)
            elif tokens[k + 1].text == "." and tokens[k + 2].text in _STORES:
                if k + 3 < len(tokens) and tokens[k + 3].text == "(":
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
            lambdas = sum(isinstance(n, Lambda) for n in nodes)
            out.append(((_owner_key(unit, nodes), lambdas, types), node))
    return out


def _judged(ctx: Context, change: FileChange) -> list[tuple[Statement, list | None, Statement]]:
    """Each try of the patched file with what to judge: None for a new try, or for one whose
    pristine twin (same member, lambda depth and catch types) caught nothing it swallowed, so
    it is judged whole; otherwise the simple statements the twin did not hold, with the twin."""
    old: dict[tuple, list[Statement]] = {}
    if change.before is not None:
        for key, node in _tries(change.before):
            old.setdefault(_rekey(change.before, key), []).append(node)
    out = []
    for key, node in _tries(change.after):
        twins = old.get(_rekey(change.after, key), [])
        if not twins:
            out.append((node, None, None))
            continue
        twin = twins.pop(0)
        if not any(_swallows(ctx.before, change.path, c) for c in twin.catches):
            out.append((node, None, twin))
            continue
        left = Counter(_statement_texts(change.before, twin.blocks[0]))
        fresh = []
        for inner in _simple_statements(node.blocks[0]):
            text = _text(change.after, inner.start, inner.end)
            if left[text]:
                left[text] -= 1
            else:
                fresh.append(inner)
        if fresh:
            out.append((node, fresh, twin))
    return out


def _simple_statements(block: Block) -> list[Statement]:
    return [
        n for n in walk(block) if isinstance(n, Statement) and not n.blocks
        and n.kind != "local_type"
    ]  # fmt: skip


def _rekey(unit: CompilationUnit, key: tuple) -> tuple:
    owner, lambdas, types = key
    return tuple((kind, name) for kind, name, _ in owner), lambdas, types


def _in_loop(nodes: list, statement: Statement) -> Statement | None:
    """The nearest loop around the statement in its method or lambda body."""
    for node in reversed(nodes):
        if isinstance(node, (Method, Lambda)):
            return None
        if node is not statement and isinstance(node, Statement) and node.kind in _LOOPS:
            return node
    return None


def _try_findings(ctx: Context, change: FileChange) -> Iterator[Finding]:
    unit = change.after
    for statement, fresh, twin in _judged(ctx, change):
        if not any(_swallows(ctx.after, change.path, c) for c in statement.catches):
            continue
        nodes = path_to(unit, statement.start)
        block = statement.blocks[0]
        key = tuple((kind, name) for kind, name, _ in _owner_key(unit, nodes))
        pristine = (
            _outside_by_name(ctx.before, change.path, key) if change.before is not None else set()
        )
        moved_in = bool(pristine & set(_statement_texts(unit, block)))
        closing = None if moved_in else _idiom_holds(ctx, unit, statement)
        skip = closing.start if closing is not None else None
        if fresh is None:  # a new try is judged whole
            judged = [block]
            names = _assigned(unit, block)
            last = closing is None and not _follows(unit, nodes, statement)
        else:  # a widened one by what it newly holds
            twin_block = twin.blocks[0]
            judged = list(fresh)
            if _covers(ctx, change.path, twin_block, program=ctx.before):
                # the twin already caught a check, so only a check moved in from outside a
                # try widens what it catches; one rewritten in place does not
                judged = [s for s in fresh if _text(unit, s.start, s.end) in pristine]
            names = set().union(*(_assigned(unit, Block([s], s.start, s.end)) for s in fresh))
            names -= _assigned(change.before, twin_block)
            last = False
        covers = any(_covers(ctx, change.path, s, skip) for s in judged)
        if _interrupt_idiom(unit, statement) and not any(
            _covers(ctx, change.path, s, skip, waits=False) for s in judged
        ):
            if _wait_results_checked(ctx, change.path, nodes, statement, False):
                continue
            covers = True  # a wait whose result the idiom drops is a swallowed check
        assigned = _read_later(ctx, change.path, nodes, statement, names)
        loop = _in_loop(nodes, statement)
        if (
            covers
            and _runs_again(unit, statement, loop)
            and not any(_covers(ctx, change.path, s, skip, runs=False) for s in judged)
        ):
            continue  # a rule or interceptor that runs the test again is a retry (Decision 8)
        retried = (
            fresh is None and moved_in and loop is not None
            and loop_key(unit, loop) not in _pristine_loops(ctx, change.path, key)
        )  # fmt: skip
        if covers or assigned or last or retried:
            yield Finding(CATCH, change.path, statement.line)
            if (covers or retried) and loop is not None:
                yield Finding(RETRY_LOOP, change.path, statement.line)


def _runs_again(unit: CompilationUnit, statement: Statement, loop: Statement | None) -> bool:
    """Whether a try that runs the test sits in a loop or runs it again from its catch."""
    if loop is not None:
        return True
    return any(
        c.name in _RUNS_THE_TEST and c.receiver_at is not None
        for catch in statement.catches
        for c in calls_in(unit, catch.block)
    )


def _wait_results_checked(
    ctx: Context, path: str, nodes: list, statement: Statement, returned: bool
) -> bool:
    """Whether every wait in the try that hands back a result has it checked: assigned to a
    name a later assertion reads, or, where returned, handed back to a caller that checks it."""
    unit = ctx.after.units[path]
    block = statement.blocks[0]
    waits = [c for c in calls_in(unit, block) if _value_wait(unit, c)]
    if not waits:
        return True
    returns = [n for n in walk(block) if isinstance(n, Statement) and n.kind == "return"]
    open_waits = [
        c for c in waits if not (returned and any(r.start <= c.start < r.end for r in returns))
    ]
    if not open_waits:
        return True
    return _read_later(ctx, path, nodes, statement, _assigned(unit, block))


def _value_wait(unit: CompilationUnit, call: Call) -> bool:
    if call.reference:
        return False
    if call.name in _VALUE_WAITS:
        return bool(_arguments(unit, call))
    return call.name in ("get", "join") and _on_a_future(unit, call)


def _pristine_loops(ctx: Context, path: str, key: tuple) -> set:
    """The loops of a pristine member, by kind and header text, computed once per file."""
    if path not in ctx.before.units:
        return set()
    memo = ("swallow/loops", path)
    if memo not in ctx.before.memo:
        unit = ctx.before.units[path]
        table: dict[tuple, set] = {}
        for n in walk(unit):
            if isinstance(n, Statement) and n.kind in _LOOPS:
                owner = tuple((k, nm) for k, nm, _ in _owner_key(unit, path_to(unit, n.start)))
                table.setdefault(owner, set()).add(loop_key(unit, n))
        ctx.before.memo[memo] = table
    return ctx.before.memo[memo].get(key, set())


def _outside_by_name(program: Program, path: str, key: tuple) -> set[str]:
    """The texts of the simple statements the pristine member of that key runs outside any
    try, computed once per file."""
    memo = ("swallow/outside", path)
    if memo not in program.memo:
        program.memo[memo] = _outside_table(program.units[path])
    return program.memo[memo].get(key, set())


def _outside_table(unit: CompilationUnit) -> dict[tuple, set[str]]:
    out: dict[tuple, set[str]] = {}
    for node in walk(unit):
        if not isinstance(node, Statement) or node.blocks or node.kind == "local_type":
            continue
        nodes = path_to(unit, node.start)
        guarded = any(
            isinstance(n, Statement) and n.kind == "try" and n.blocks
            and nodes[k + 1] is n.blocks[0]
            for k, n in enumerate(nodes[:-1])
        )  # fmt: skip
        if not guarded:
            key = tuple((k, nm) for k, nm, _ in _owner_key(unit, nodes))
            out.setdefault(key, set()).add(_text(unit, node.start, node.end))
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
    """Expected exception declarations: a JUnit 4 expected attribute and an ExpectedException
    rule."""
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


# Calls that check for an expected exception: a rule's expect call converted from one of these
# keeps the member's expectations as they were, which Decision 5 compares.
_EXPECTING = frozenset(["assertThrows", "assertThatThrownBy", "expectThrows", "assertThatCode"])


def _expectations(program: Program, path: str) -> Counter:
    """Per member, the expect calls made on an ExpectedException rule field of the file, and
    the other expected exception checks they may replace, counted the same on both sides."""
    unit = program.units[path]
    rules = {
        name for node in walk(unit) if isinstance(node, Field)
        and _simple(node.type) == "ExpectedException" for name in node.names
    }  # fmt: skip
    out: Counter = Counter()
    for call in calls_in(unit, unit_span(unit)):
        if call.reference:
            continue
        on_rule = call.receiver in rules and call.name in ("expect", "expectMessage", "expectCause")
        if on_rule or call.name in _EXPECTING:
            out[member_key(program, unit, call.start)] += 1
    return out


def _expect_findings(ctx: Context, path: str) -> Iterator[Finding]:
    """An expect call on an ExpectedException rule a member newly makes, beyond the
    expectations it already had, turns a failure into the expected one."""
    old = _expectations(ctx.before, path) if path in ctx.before.units else Counter()
    new = _expectations(ctx.after, path)
    unit = ctx.after.units[path]
    if not +(new - old):
        return
    for call in calls_in(unit, unit_span(unit)):
        key = member_key(ctx.after, unit, call.start)
        if not call.reference and call.name in ("expect", "expectMessage", "expectCause"):
            if new[key] > old[key]:
                yield Finding(EXPECTED, path, unit.tokens[call.start].line)
                old[key] = new[key]


def _handler_sites(program: Program, path: str) -> list[Site]:
    """Classes, anonymous classes and fields of an exception handler type, the last of which a
    lambda can implement."""
    unit = program.units[path]
    sites = []
    for node in walk(unit):
        if isinstance(node, Field) and _simple(node.type) in _HANDLERS:
            binding = resolve(program, path, node.type.split("<", 1)[0], node.start)
            fqn = binding.fqn or ""
            if binding.kind == "declared":
                continue
            if binding.kind != "unresolved" and not fqn.startswith(_HANDLER_PACKAGE + "."):
                continue
            key = member_key(program, unit, node.start)
            label = f"field {_simple(node.type)} {' '.join(node.names)}"
            sites.append(Site(key, label, HANDLER, node.line, node.start))
    for decl in program.all_types(path):
        anonymous = [] if decl.name else decl.extends
        for name in [*decl.implements, *anonymous]:
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


def _inside_a_check(unit: CompilationUnit, call: Call) -> bool:
    """Whether the call sits in the arguments of a check, which then reads its result."""
    for other in calls_in(unit, unit_span(unit)):
        if other.start >= call.start or other.reference or other.arity is None:
            continue
        if _checking(unit, other, True) and other.end <= call.start <= _close(unit, other.end):
            return True
    return False


def unit_span(unit: CompilationUnit) -> _Span:
    return _Span(0, len(unit.tokens))


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
                if not any(_swallows(program, reached.path, c) for c in node.catches):
                    continue
                block = node.blocks[0]
                closing = _idiom_holds(ctx, inner, node)
                skip = closing.start if closing is not None else None
                covers = _covers(ctx, reached.path, block, skip, prefixes=test_code)
                if _interrupt_idiom(inner, node) and not _covers(
                    ctx, reached.path, block, skip, prefixes=test_code, waits=False
                ):
                    nodes = path_to(inner, node.start)
                    checked = _inside_a_check(unit, call)
                    covers = not _wait_results_checked(ctx, reached.path, nodes, node, checked)
                loop = _in_loop(path_to(inner, node.start), node)
                if (
                    covers
                    and _runs_again(inner, node, loop)
                    and not _covers(ctx, reached.path, block, skip, prefixes=test_code, runs=False)
                ):
                    continue  # a retry, judged by Decision 8's retry rules
                runs_param = any(c.receiver in params for c in calls_in(inner, block))
                if (runs_param and reached.depth == 0 and passes_check) or covers:
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
            *_expect_findings(ctx, change.path),
            *_helper_findings(ctx, change),
        ]
        for finding in found:
            if finding not in seen:
                seen.add(finding)
                yield finding
