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


_HANDS_ON = frozenset(
    ["schedule", "scheduleAtFixedRate", "scheduleWithFixedDelay", "submit", "execute",
     "newTimeout", "post", "postDelayed"]
)  # fmt: skip
_TASK_METHODS = frozenset(["run", "call"])


def _arguments(unit: CompilationUnit, call: Call) -> list[list[str]]:
    tokens, out, current, depth = unit.tokens, [], [], 0
    for token in tokens[call.end + 1 :]:
        text = token.text if token.kind == "op" else None
        if text in ("(", "[", "{"):
            depth += 1
        elif text in (")", "]", "}"):
            if depth == 0:
                break
            depth -= 1
        elif text == "," and depth == 0:
            out.append(current)
            current = []
            continue
        current.append(token.text)
    return [*out, current] if current else out


def _self_fields(unit: CompilationUnit) -> set[str]:
    """Names the file sets to `this`, as `Runnable self = this;` does."""
    tokens = unit.tokens
    return {
        tokens[k - 2].text
        for k in range(2, len(tokens) - 1)
        if tokens[k].text == "this" and tokens[k - 1].text == "=" and tokens[k + 1].text == ";"
        and tokens[k - 2].kind == "ident"
    }  # fmt: skip


def _hands_on_itself(unit: CompilationUnit, call: Call, selves: set[str]) -> bool:
    """Whether a call hands the running task to an executor, as `schedule(this, 1, SECONDS)`
    does in a Runnable's run()."""
    if call.name not in _HANDS_ON:
        return False
    return any(arg == ["this"] or (len(arg) == 1 and arg[0] in selves)
               for arg in _arguments(unit, call))  # fmt: skip


def _edges(program: Program, path: str, method: Method, budget: ExpansionBudget | None) -> list:
    """Each call in a method's body with the methods a scan follows it to, built once per
    method for the whole check, so the self rescheduling test costs one walk per file. Only
    a helper reached from another method spends the budget."""
    memo = program.memo.setdefault(("production", "edges"), {})
    if id(method) not in memo:
        if budget is not None:
            budget.spend(method.line)
        unit, out = program.units[path], []
        if method.body is not None:
            selves = _self_fields(unit)
            for call in calls_in(unit, method.body):
                hands = not call.reference and _hands_on_itself(unit, call, selves)
                plain = call.receiver in (None, "this")
                out.append((call.name, plain, hands, follow_for_scan(program, path, call)))
        memo[id(method)] = out
    return memo[id(method)]


def _reschedules(program: Program, path: str, method: Method, budget: ExpansionBudget) -> bool:
    """Whether a method's body, or a helper it reaches, calls it again or hands on a reference
    to it, as a task that schedules itself from its `finally` block does. A task's run() or
    call() that hands `this` to an executor counts too."""
    if method.body is None:
        return False
    task = method.name in _TASK_METHODS
    seen, pending = {id(method)}, [(path, method)]
    while pending:
        at, current = pending.pop()
        spend = None if current is method else budget
        for name, plain, hands, targets in _edges(program, at, current, spend):
            if task and hands:
                return True
            if name == method.name and plain and any(m is method for _, m in targets):
                return True
            for target in targets:
                if id(target[1]) not in seen:
                    seen.add(id(target[1]))
                    pending.append(target)
    return False


def _rescheduling(program: Program, path: str) -> list[tuple[tuple, int]]:
    unit = program.units[path]
    out = []
    budget = program.memo.setdefault(("production", "budget"), ExpansionBudget())
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


def _judged_from_tests(ctx: Context) -> set[int]:
    """The production methods a test call reaches when the test code rules reject that call
    for reaching a fixed sleep (Decision 4): a call on a statement the patch added to a test
    file. A sleep only an unchanged test reaches is judged by no rule, so it is flagged."""
    memo = ctx.after.memo
    if ("production", "judged") not in memo:
        found: set[int] = set()
        try:
            for change in ctx.changes:
                if change.path not in ctx.test_paths:
                    continue
                unit, path = change.after, change.path
                old_sites, new_sites = timing._sites(change.before), timing._sites(unit)
                for call in calls_in(unit, retries.unit_span(unit)):
                    if call.reference:
                        continue
                    site = timing._site(unit, call.start)
                    if new_sites[site] <= old_sites[site]:
                        continue
                    if not timing._reaches_sleep(ctx, path, call):
                        continue
                    roots = follow_for_scan(ctx.after, path, call)
                    budget = ExpansionBudget()
                    found.update(id(r.method) for r in reach(ctx.after, roots, True, budget))
        except Unparsable:
            found = set()  # past the cap: every added sleep is flagged
        memo[("production", "judged")] = found
    return memo[("production", "judged")]


def _sleep_findings(ctx: Context, change: FileChange) -> Iterator[Finding]:
    path = change.path
    old = Counter(k for k, _, _ in _sleeps(ctx.before, path)) if change.before else Counter()
    new = _sleeps(ctx.after, path)
    lines = set(_excess(old, [(k, line) for k, line, _ in new]))
    if not lines:
        return
    judged = _judged_from_tests(ctx)
    for _, line, call in new:
        if line not in lines:
            continue
        method = timing._method_at(change.after, call.start)
        if method is not None and id(method) in judged:
            continue  # rejected by the test code rules
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
