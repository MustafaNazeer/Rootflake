"""Flags for what the test code rules do not judge (ADR 0007 Decision 9).

Production code ships, and the gate's reruns test a fix there like any other, so a change to
it is flagged for a person to read and never rejected here. Each flag counts per site, the
construct together with the member it sits in, and only when the patched file has more of it
than the pristine file. Test code that changes a JVM wide default, a changed test resource,
and a patch admitted through the override list are flagged the same way.
"""

from collections import Counter
from collections.abc import Iterator

from rootflake.gate.checker import flow, retries, timing
from rootflake.gate.checker.calls import Call, ExpansionBudget, calls_in, follow_for_scan, reach
from rootflake.gate.checker.lexer import Unparsable
from rootflake.gate.checker.model import Context, FileChange, Finding
from rootflake.gate.checker.poll import LOOPS
from rootflake.gate.checker.scope import Program
from rootflake.gate.checker.survive import loop_key
from rootflake.gate.checker.syntax import Catch, CompilationUnit, Method, Statement, TypeDecl
from rootflake.gate.checker.tree import walk

RETRY = "flag/production_retry"
TIMING = "flag/production_timing_raised"
SLEEP = "flag/production_sleep_added"
SWALLOW = "flag/production_swallow"
DETECTION = "flag/production_test_detection"
CLASS = "flag/new_production_class"
ZONE = "flag/default_zone_or_locale_set"
RESOURCE = "flag/test_resource_changed"
OVERRIDE = "flag/override"

_LOG_CALLS = frozenset(
    ["trace", "debug", "info", "warn", "warning", "error", "fatal", "severe", "fine", "finer",
     "finest", "log", "logf", "printStackTrace"]
)  # fmt: skip
_TEST_SIGNALS = ("surefire", "failsafe", "junit", "testng", "jupiter")
_DEFAULT_TYPES = frozenset(["TimeZone", "Locale"])
_DEFAULT_PROPERTIES = frozenset(
    ['"user.timezone"', '"user.language"', '"user.country"', '"user.region"']
)
_PROPERTY_SETTERS = frozenset(["setProperty", "put"])
_RESOURCES = ["src", "test", "resources"]
_MAIN = ["src", "main", "java"]


def _under(path: str, root: list[str]) -> bool:
    parts = path.split("/")
    return any(parts[i : i + 3] == root for i in range(len(parts) - 3))


def _text(unit: CompilationUnit, start: int, end: int) -> str:
    return " ".join(t.text for t in unit.tokens[start:end])


def _key(program: Program, unit: CompilationUnit, index: int) -> tuple:
    return retries.member_key(program, unit, index)


def _excess(old: Counter, new: list[tuple[tuple, int]]) -> list[int]:
    """The lines of the new sites the pristine file has fewer of, in source order."""
    left, out = Counter(old), []
    for key, line in new:
        if left[key]:
            left[key] -= 1
        else:
            out.append(line)
    return out


def _catches(unit: CompilationUnit) -> list[Catch]:
    return [n for n in walk(unit) if isinstance(n, Catch)]


def _rethrows(unit: CompilationUnit, catch: Catch) -> bool:
    return any(
        t.kind == "keyword" and t.text == "throw"
        for t in unit.tokens[catch.block.start : catch.block.end]
    )


def _logs(unit: CompilationUnit, catch: Catch) -> bool:
    return any(c.name in _LOG_CALLS for c in calls_in(unit, catch.block) if not c.reference)


# -- retries ----------------------------------------------------------------------------------


def _keeps_going(unit: CompilationUnit, node) -> bool:
    """Whether a span holds a catch that does not rethrow."""
    return any(isinstance(n, Catch) and not _rethrows(unit, n) for n in walk(node))


def _retry_loops(program: Program, path: str) -> list[tuple[tuple, int]]:
    unit = program.units[path]
    return [
        ((_key(program, unit, n.start), loop_key(unit, n)), n.line)
        for n in walk(unit)
        if isinstance(n, Statement) and n.kind in LOOPS and _keeps_going(unit, n)
    ]


def _reschedules(program: Program, path: str, method: Method, budget: ExpansionBudget) -> bool:
    """Whether a method's body, or a helper it reaches, calls it again or hands on a reference
    to it, as a task that schedules itself from its `finally` block does."""
    if method.body is None:
        return False
    for reached in reach(program, [(path, method)], True, budget):
        if reached.method.body is None:
            continue
        for call in calls_in(program.units[reached.path], reached.method.body):
            if call.name != method.name or call.receiver not in (None, "this"):
                continue
            if any(m is method for _, m in follow_for_scan(program, reached.path, call)):
                return True
    return False


def _rescheduling(program: Program, path: str) -> list[tuple[tuple, int]]:
    unit = program.units[path]
    out = []
    budget = ExpansionBudget()
    for node in walk(unit):
        if isinstance(node, Method) and node.body is not None and _keeps_going(unit, node.body):
            try:
                again = _reschedules(program, path, node, budget)
            except Unparsable:
                again = True  # past the cap: flagged for a person to read
            if again:
                out.append(((_key(program, unit, node.start), "reschedules"), node.line))
    return out


def _retry_findings(ctx: Context, change: FileChange) -> Iterator[Finding]:
    path = change.path
    for kind in (_retry_loops, _rescheduling):
        old = Counter(k for k, _ in kind(ctx.before, path)) if change.before else Counter()
        for line in _excess(old, kind(ctx.after, path)):
            yield Finding(RETRY, path, line)
    old_sites = retries._sites(ctx.before, path, retries._client_sites)
    for site in retries.added(old_sites, retries._sites(ctx.after, path, retries._client_sites)):
        yield Finding(RETRY, path, site.line)


# -- timing -----------------------------------------------------------------------------------


def _timing_findings(change: FileChange) -> Iterator[Finding]:
    if change.before is None:
        return
    constants = timing._file_constants(change.before), timing._file_constants(change.after)
    old = flow._declarations(change.before)
    for key, (text, line, _, _) in flow._declarations(change.after).items():
        if key in old and flow.timing_name(key[1]):
            if flow._raised(old[key][0], text, *constants):
                yield Finding(TIMING, change.path, line)


def _sleeps(program: Program, path: str) -> list[tuple[tuple, int, Call]]:
    unit = program.units[path]
    out = []
    for call in calls_in(unit, retries.unit_span(unit)):
        if not call.reference and timing.sleep_form(program, path, call):
            close = timing._close(unit, call.end)
            masked = timing._masked(_text(unit, call.start, close + 1))
            out.append(((_key(program, unit, call.start), masked), unit.tokens[call.start].line,
                        call))  # fmt: skip
    return out


def _reached_from_tests(ctx: Context) -> set[int] | None:
    """The methods a scan from test code reaches, which the test code rules judge (Decision 4),
    or None when the scan goes past its cap."""
    memo = ctx.after.memo
    if ("production", "reached") not in memo:
        roots = []
        for path in sorted(ctx.test_paths):
            unit = ctx.after.units[path]
            for call in calls_in(unit, retries.unit_span(unit)):
                roots.extend(follow_for_scan(ctx.after, path, call))
        try:
            found = {id(r.method) for r in reach(ctx.after, roots, True, ExpansionBudget())}
        except Unparsable:
            found = None
        memo[("production", "reached")] = found
    return memo[("production", "reached")]


def _method_at(unit: CompilationUnit, index: int) -> Method | None:
    holder = None
    for node in walk(unit):
        if isinstance(node, Method) and node.start <= index < node.end:
            if holder is None or node.start >= holder.start:
                holder = node
    return holder


def _sleep_findings(ctx: Context, change: FileChange) -> Iterator[Finding]:
    path = change.path
    old = Counter(k for k, _, _ in _sleeps(ctx.before, path)) if change.before else Counter()
    new = _sleeps(ctx.after, path)
    lines = set(_excess(old, [(k, line) for k, line, _ in new]))
    if not lines:
        return
    reached = _reached_from_tests(ctx)
    for _, line, call in new:
        if line not in lines:
            continue
        method = _method_at(change.after, call.start)
        if reached is not None and method is not None and id(method) in reached:
            continue  # judged by the test code rules
        yield Finding(SLEEP, path, line)


# -- catches, signals and classes -------------------------------------------------------------


def _quiet_catches(program: Program, path: str) -> list[tuple[tuple, int]]:
    unit = program.units[path]
    return [
        ((_key(program, unit, c.start), tuple(c.types)), c.line)
        for c in _catches(unit)
        if not _rethrows(unit, c) and not _logs(unit, c)
    ]


def _signals(program: Program, path: str) -> list[tuple[tuple, int]]:
    unit = program.units[path]
    return [
        ((_key(program, unit, i), t.text), t.line)
        for i, t in enumerate(unit.tokens)
        if t.kind in ("string", "text_block") and any(s in t.text.lower() for s in _TEST_SIGNALS)
    ]


def _classes(program: Program, path: str) -> list[tuple[str, int]]:
    return [
        (program.fqn[id(n)], n.line)
        for n in walk(program.units[path])
        if isinstance(n, TypeDecl) and n.kind != "anonymous"
    ]


def _counted(ctx: Context, change: FileChange, kind, code: str) -> Iterator[Finding]:
    old = Counter(k for k, _ in kind(ctx.before, change.path)) if change.before else Counter()
    for line in _excess(old, kind(ctx.after, change.path)):
        yield Finding(code, change.path, line)


def _production(ctx: Context, change: FileChange) -> Iterator[Finding]:
    yield from _retry_findings(ctx, change)
    yield from _timing_findings(change)
    yield from _sleep_findings(ctx, change)
    yield from _counted(ctx, change, _quiet_catches, SWALLOW)
    yield from _counted(ctx, change, _signals, DETECTION)
    if _under(change.path, _MAIN):
        yield from _counted(ctx, change, _classes, CLASS)


# -- JVM wide defaults in test code -----------------------------------------------------------


def _defaults(program: Program, path: str) -> list[tuple[tuple, int]]:
    unit = program.units[path]
    tokens, out = unit.tokens, []
    for call in calls_in(unit, retries.unit_span(unit)):
        if call.reference:
            continue
        if call.name == "setDefault" and call.receiver in _DEFAULT_TYPES:
            label = f"{call.receiver}.setDefault"
        elif call.name in _PROPERTY_SETTERS and tokens[call.end + 1].text in _DEFAULT_PROPERTIES:
            label = tokens[call.end + 1].text
        else:
            continue
        out.append(((_key(program, unit, call.start), label), tokens[call.start].line))
    return out


def findings(ctx: Context) -> Iterator[Finding]:
    for change in ctx.changes:
        if change.path in ctx.test_paths:
            yield from _counted(ctx, change, _defaults, ZONE)
        else:
            yield from _production(ctx, change)
    for path in ctx.paths:
        if _under(path, _RESOURCES):
            yield Finding(RESOURCE, path)
    if ctx.overridden:
        read = {c.path for c in ctx.changes}
        unread = [p for p in ctx.overridden if p not in read]
        for path in unread or [None]:
            yield Finding(OVERRIDE, path)
