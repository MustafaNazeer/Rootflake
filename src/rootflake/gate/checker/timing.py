"""Widened timing bounds in test code (ADR 0007 Decision 7).

Bounds are compared with the before image of the same member: timeouts on test methods and
`Timeout` rules, Awaitility waits, calls taking a number and a `TimeUnit` or a `Duration`, and
numeric literals passed to calls named like a wait. Several such calls of one name in one member
are compared largest first, so a raised bound cannot hide behind a newly added smaller one; the
smallest of any extra calls are the newly added ones, judged as condition waits.

A fixed sleep is added when its statement, with the statement that follows it, is not in the
before image of its member, so a sleep moved within its method counts as added, and so is every
sleep a newly added or moved call reaches through the helpers it may call. A sleep in a loop, or
reached through a loop, is left to the hand written poll loop rule, which is not built yet.
"""

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass

from rootflake.gate.checker.assertions import _close, _split_arguments, _text, is_assertion_name
from rootflake.gate.checker.calls import Call, Reached, calls_in, follow_for_scan, reach
from rootflake.gate.checker.model import Context, FileChange, Finding
from rootflake.gate.checker.scope import Program
from rootflake.gate.checker.survive import attributes
from rootflake.gate.checker.syntax import (
    Block,
    CompilationUnit,
    EnumConstant,
    Field,
    Initializer,
    Method,
    Statement,
    TypeDecl,
)
from rootflake.gate.checker.tree import path_to, walk
from rootflake.gate.checker.values import TIME_UNITS, fold, literal

CODE = "weaken_assertion/timing_bound_widened"
# The gate's wall time limit for one execution, in milliseconds. The gate thresholds ADR sets
# it; until then a newly added condition wait is checked for a bound and a checked result only.
WALL_TIME_LIMIT_MS: float | None = None
AWAITILITY_DEFAULT_MS = 10_000.0

_CHRONO_UNITS = {
    "NANOS": 1e-6, "MICROS": 1e-3, "MILLIS": 1.0, "SECONDS": 1e3, "MINUTES": 6e4,
    "HOURS": 3.6e6, "HALF_DAYS": 4.32e7, "DAYS": 8.64e7,
}  # fmt: skip
_DURATION_FACTORIES = {
    "ofNanos": 1e-6, "ofMillis": 1.0, "ofSeconds": 1e3, "ofMinutes": 6e4, "ofHours": 3.6e6,
    "ofDays": 8.64e7,
}  # fmt: skip
_LOOPS = frozenset(["for", "while", "do"])
_AWAITILITY_ROOTS = frozenset(["await", "waitAtMost", "with", "given"])
_AWAITILITY_BOUNDS = frozenset(["atMost", "timeout", "waitAtMost"])
_WAIT_WORDS = ("wait", "sleep", "timeout", "poll", "within", "until", "atmost", "delay")
_TOLERANCE_FACTORIES = frozenset(["within", "withinPercentage"])
_MODE_CALLS = frozenset(["verify", "should"])  # Mockito modes are judged under Decision 5
_WAITS = frozenset([
    "await", "awaitTermination", "get", "join", "succeedsWithin", "failsWithin", "poll",
    "tryAcquire", "tryLock", "awaitAdvanceInterruptibly",
])  # fmt: skip
_RESULT_WAITS = frozenset(["await", "awaitTermination"])
_AFTER_LIFECYCLE = frozenset([
    "After", "AfterEach", "AfterClass", "AfterAll", "AfterMethod", "AfterTest", "AfterSuite",
    "AfterGroups",
])  # fmt: skip
_TRUE_CONDITIONS = frozenset([
    "( ) -> true", "( ) -> { }", "( ) -> { return true ; }", "new AtomicBoolean ( true )",
])  # fmt: skip
_JOIN_WORDS = ("timeout", "millis", "wait", "delay", "seconds")


@dataclass(frozen=True)
class Bound:
    ms: float | None  # None when the value cannot be folded to a constant
    unbounded: bool
    text: str
    line: int


# -- folding ------------------------------------------------------------------------------


def _unit(text: str) -> float | None:
    words = text.split()
    if len(words) == 1:
        return TIME_UNITS.get(words[0], _CHRONO_UNITS.get(words[0]))
    if words[:-1] in (
        ["TimeUnit", "."],
        ["java", ".", "util", ".", "concurrent", ".", "TimeUnit", "."],
    ):
        return TIME_UNITS.get(words[-1])
    if words[:-1] in (
        ["ChronoUnit", "."],
        ["java", ".", "time", ".", "temporal", ".", "ChronoUnit", "."],
    ):
        return _CHRONO_UNITS.get(words[-1])
    return None


def _strip_qualifier(words: list[str]) -> list[str]:
    for prefix in (["java", ".", "time", "."], ["org", ".", "awaitility", "."]):
        if words[: len(prefix)] == prefix:
            return words[len(prefix) :]
    return words


def duration_ms(text: str) -> tuple[bool, float | None]:
    """Whether the text is a `Duration` value, and its length in milliseconds when it folds."""
    words = _strip_qualifier(text.split())
    if words[:1] == ["new"] and words[1:3] == ["Duration", "("] and words[-1:] == [")"]:
        return True, _pair_value(" ".join(words[3:-1]))
    if words[:2] != ["Duration", "."] or len(words) < 5 or words[3] != "(" or words[-1] != ")":
        return False, None
    factory, inner = words[2], " ".join(words[4:-1])
    if factory in _DURATION_FACTORIES:
        parts = _split_words(inner)
        if factory == "ofSeconds" and len(parts) == 2:
            seconds, nanos = fold(parts[0]), fold(parts[1])
            ok = seconds is not None and nanos is not None
            return True, seconds * 1e3 + nanos * 1e-6 if ok else None
        value = fold(inner)
        return True, None if value is None else value * _DURATION_FACTORIES[factory]
    if factory == "of":
        return True, _pair_value(inner)
    return True, None


def _pair_value(inner: str) -> float | None:
    parts = _split_words(inner)
    if len(parts) != 2:
        return None
    number, unit = fold(parts[0]), _unit(parts[1])
    return None if number is None or unit is None else number * unit


def _split_words(text: str) -> list[str]:
    parts, depth, current = [], 0, []
    for word in text.split():
        if word in ("(", "[", "{"):
            depth += 1
        elif word in (")", "]", "}"):
            depth -= 1
        if word == "," and depth == 0:
            parts.append(" ".join(current))
            current = []
        else:
            current.append(word)
    parts.append(" ".join(current))
    return parts


# -- where a call sits --------------------------------------------------------------------


def _member_key(nodes: list) -> tuple:
    key = []
    for node in nodes:
        if isinstance(node, TypeDecl):
            key.append(("T", node.name or "new " + ",".join(node.extends)))
        elif isinstance(node, Method):
            key.append(("M", node.name, tuple(p.type for p in node.params)))
        elif isinstance(node, Field):
            key.append(("F", tuple(node.names)))
        elif isinstance(node, Initializer):
            key.append(("I", node.static))
        elif isinstance(node, EnumConstant):
            key.append(("E", node.name))
    return tuple(key)


def _statement(nodes: list) -> tuple[Statement | None, Block | None]:
    """The innermost statement that sits directly in a block, and that block."""
    for k in range(len(nodes) - 1, 0, -1):
        if isinstance(nodes[k], Statement) and isinstance(nodes[k - 1], Block):
            return nodes[k], nodes[k - 1]
    return None, None


def _in_loop(nodes: list) -> bool:
    return any(isinstance(n, Statement) and n.kind in _LOOPS for n in nodes)


@dataclass(frozen=True)
class _Site:
    member: tuple
    statement: str
    following: str


def _site(unit: CompilationUnit, index: int) -> _Site:
    nodes = path_to(unit, index)
    member = _member_key(nodes)
    statement, block = _statement(nodes)
    if statement is None:
        holder = nodes[-1] if nodes else None
        text = _text(unit, holder.start, holder.end) if holder is not None else ""
        return _Site(member, text, "")
    k = next(i for i, s in enumerate(block.statements) if s is statement)
    nxt = block.statements[k + 1] if k + 1 < len(block.statements) else None
    following = _text(unit, nxt.start, nxt.end) if nxt is not None else "}"
    return _Site(member, _text(unit, statement.start, statement.end), following)


def _sites(unit: CompilationUnit | None) -> Counter:
    """Every block level statement with the statement after it, counted per member."""
    out: Counter = Counter()
    if unit is None:
        return out
    for node in walk(unit):
        if isinstance(node, Block):
            for k, statement in enumerate(node.statements):
                nxt = node.statements[k + 1] if k + 1 < len(node.statements) else None
                following = _text(unit, nxt.start, nxt.end) if nxt is not None else "}"
                member = _member_key(path_to(unit, statement.start))
                out[_Site(member, _text(unit, statement.start, statement.end), following)] += 1
    return out


def _mode_spans(unit: CompilationUnit, calls: list[Call]) -> list[tuple[int, int]]:
    return [(c.end, _close(unit, c.end)) for c in calls if c.name in _MODE_CALLS and c.arity]


def _inside(spans: list[tuple[int, int]], index: int) -> bool:
    return any(start < index < end for start, end in spans)


def _arguments(unit: CompilationUnit, call: Call) -> tuple[str, ...]:
    if call.reference or call.arity is None:
        return ()
    return _split_arguments(unit, call.end, _close(unit, call.end))


def _chain(unit: CompilationUnit, call: Call) -> list[tuple[str, int, tuple[str, ...]]]:
    """The calls of a fluent chain from the root call at call onwards: name, index, arguments."""
    tokens, out, at = unit.tokens, [], call.start
    while True:
        close = _close(unit, at + 1)
        out.append((tokens[at].text, at, _split_arguments(unit, at + 1, close)))
        j = close + 1
        if j + 2 < len(tokens) and tokens[j].text == "." and tokens[j + 1].kind == "ident":
            if tokens[j + 2].text == "(":
                at = j + 1
                continue
        return out


def _awaitility_root(unit: CompilationUnit, call: Call) -> bool:
    if call.name not in _AWAITILITY_ROOTS or call.reference:
        return False
    if call.receiver == "Awaitility":
        return True
    if call.receiver is not None:
        return False
    for imp in unit.imports:
        if imp.static and imp.name.startswith("org.awaitility."):
            if imp.wildcard or imp.name.rsplit(".", 1)[-1] == call.name:
                return True
    return False


# -- the bounds of one file ---------------------------------------------------------------


def _value(text: str, line: int, unbounded: bool = False) -> Bound:
    return Bound(None, unbounded, text, line)


def _argument_bounds(args: tuple[str, ...], line: int) -> tuple[list[Bound], set[int]]:
    """The `Duration` and number plus unit bounds among a call's arguments, and the argument
    positions they use."""
    out, used = [], set()
    for k, arg in enumerate(args):
        is_duration, ms = duration_ms(arg)
        if is_duration:
            out.append(Bound(ms, False, arg, line))
            used.add(k)
        elif k + 1 < len(args) and _unit(args[k + 1]) is not None:
            number = fold(arg)
            ms = None if number is None else number * _unit(args[k + 1])
            out.append(Bound(ms, False, f"{arg} , {args[k + 1]}", line))
            used |= {k, k + 1}
    return out, used


def _chain_bound(chain, line: int) -> Bound:
    bound = Bound(AWAITILITY_DEFAULT_MS, False, "<default>", line)
    for name, _, args in chain:
        if name == "forever":
            bound = Bound(None, True, "forever", line)
        elif name in _AWAITILITY_BOUNDS and args:
            found, _ = _argument_bounds(args, line)
            text = " , ".join(args)
            bound = Bound(found[0].ms, False, text, line) if found else _value(text, line)
    return bound


@dataclass
class _Bounds:
    groups: dict[tuple, list[Bound]]
    sleeps: list[tuple[int, str]]  # direct sleep forms and always true pollDelay waits
    ignored: list[tuple[Call, Statement]]


def _bounds(program: Program, path: str) -> _Bounds:
    unit = program.units[path]
    calls = calls_in(unit, _Whole(unit))
    modes = _mode_spans(unit, calls)
    groups: dict[tuple, list[Bound]] = {}
    sleeps, ignored = [], []
    tokens = unit.tokens
    for call in calls:
        if call.reference:
            continue
        nodes = path_to(unit, call.start)
        member = _member_key(nodes)
        line = tokens[call.start].line
        args = _arguments(unit, call)
        if _awaitility_root(unit, call):
            chain = _chain(unit, call)
            groups.setdefault((member, "<awaitility>"), []).append(_chain_bound(chain, line))
            names = [name for name, _, _ in chain]
            if "pollDelay" in names and chain[-1][0].startswith("until"):
                if " , ".join(chain[-1][2]) in _TRUE_CONDITIONS:
                    sleeps.append((call.start, "pollDelay"))
        if call.name == "setDefaultTimeout":
            groups.setdefault((member, "<setDefaultTimeout>"), []).append(_value("", line))
        if _inside(modes, call.start):
            continue
        found, used = _argument_bounds(args, line)
        for bound in found:
            groups.setdefault((member, call.name, "pair"), []).append(bound)
        lower = call.name.lower()
        if call.name not in _TOLERANCE_FACTORIES and any(w in lower for w in _WAIT_WORDS):
            for k, arg in enumerate(args):
                number = fold(arg)
                if k not in used and number is not None:
                    group = (member, call.name, k)
                    groups.setdefault(group, []).append(Bound(number, False, arg, line))
        if call.name in _RESULT_WAITS and found:
            statement, _ = _statement(nodes)
            if statement is not None and _result_ignored(unit, call, statement):
                ignored.append((call, statement))
    return _Bounds(groups, sleeps, ignored)


class _Whole:
    def __init__(self, unit: CompilationUnit):
        self.start, self.end = 0, len(unit.tokens)


def _result_ignored(unit: CompilationUnit, call: Call, statement: Statement) -> bool:
    if statement.kind != "expression":
        return False
    tokens = unit.tokens
    close = _close(unit, call.end)
    if close + 1 >= len(tokens) or tokens[close + 1].text != ";" or close + 2 != statement.end:
        return False
    return not any(t.text == "=" and t.kind == "op" for t in tokens[statement.start : call.start])


def sleep_form(program: Program, path: str, call: Call) -> bool:
    """Whether a call is a fixed sleep by its own form (Decision 7)."""
    unit = program.units[path]
    lower = call.name.lower()
    if call.name == "join" and call.receiver != "String" and call.arity in (1, 2):
        first = _arguments(unit, call)[:1]
        if first and (
            fold(first[0]) is not None or any(w in first[0].lower() for w in _JOIN_WORDS)
        ):
            return True
    named = lower.endswith(("sleep", "pause")) or call.name in (
        "sleepUninterruptibly",
        "parkNanos",
        "parkUntil",
    )
    if not named and "sleep" not in lower:
        return False
    if follow_for_scan(program, path, call):
        return False  # a read declaration is followed into its body instead
    return named or "sleep" in lower


# -- timeouts on test methods and rules ---------------------------------------------------


def _annotation_bound(unit: CompilationUnit, annotation, line: int) -> list[Bound]:
    simple = annotation.name.rsplit(".", 1)[-1]
    found = attributes(unit, annotation)
    out = []
    if simple == "Timeout" and "value" in found:
        unit_ms = _unit(found["unit"]) if "unit" in found else 1e3
        number = fold(found["value"])
        ms = None if number is None or unit_ms is None else number * unit_ms
        out.append(Bound(ms, False, f"{found['value']} {found.get('unit', '')}", line))
    elif simple == "Test":
        for name in ("timeout", "timeOut", "invocationTimeOut"):
            if name in found:
                out.append(Bound(fold(found[name]), False, found[name], line))
    return out


def _method_timeouts(unit: CompilationUnit) -> dict[tuple, Bound | None]:
    """Each method's tightest timeout from its own and its enclosing classes' annotations."""
    out = {}
    for node in walk(unit):
        if not isinstance(node, Method):
            continue
        nodes = path_to(unit, node.start)
        bounds = [b for a in node.annotations for b in _annotation_bound(unit, a, node.line)]
        for decl in nodes:
            if isinstance(decl, TypeDecl):
                bounds += [
                    b for a in decl.annotations for b in _annotation_bound(unit, a, node.line)
                ]
        out[_member_key(nodes)] = _tightest(bounds)
    return out


def _rule_timeouts(unit: CompilationUnit) -> dict[tuple, Bound | None]:
    out = {}
    for node in walk(unit):
        if not isinstance(node, TypeDecl):
            continue
        bounds = []
        for field in node.fields:
            if field.type.split("<")[0].rsplit(".", 1)[-1] != "Timeout":
                continue
            names = {a.name.rsplit(".", 1)[-1] for a in field.annotations}
            if not names & {"Rule", "ClassRule"}:
                continue
            for span in field.initializers.values():
                bounds.append(_rule_bound(_text(unit, span.start, span.end), field.line))
        out[_member_key(path_to(unit, node.start))] = _tightest(bounds)
    return out


def _rule_bound(text: str, line: int) -> Bound:
    words = text.split()
    if words[:3] == ["new", "Timeout", "("] and words[-1] == ")":
        inner = " ".join(words[3:-1])
        parts = _split_words(inner)
        ms = fold(inner) if len(parts) == 1 else _pair_value(inner)
        return Bound(ms, False, text, line)
    for factory, scale in (("millis", 1.0), ("seconds", 1e3)):
        if words[:4] == ["Timeout", ".", factory, "("] and words[-1] == ")":
            number = fold(" ".join(words[4:-1]))
            return Bound(None if number is None else number * scale, False, text, line)
    if "withTimeout" in words:
        at = words.index("withTimeout")
        close = _matching_close(words, at + 1)
        if close is not None:
            return Bound(_pair_value(" ".join(words[at + 2 : close])), False, text, line)
    return _value(text, line)


def _matching_close(words: list[str], open_at: int) -> int | None:
    depth = 0
    for k in range(open_at, len(words)):
        if words[k] == "(":
            depth += 1
        elif words[k] == ")":
            depth -= 1
            if depth == 0:
                return k
    return None


def _tightest(bounds: list[Bound]) -> Bound | None:
    if not bounds:
        return None
    known = [b for b in bounds if b.ms is not None]
    return min(known, key=lambda b: b.ms) if len(known) == len(bounds) else bounds[0]


# -- comparing ----------------------------------------------------------------------------


def _raised(old: Bound | None, new: Bound | None) -> bool:
    if old is None:
        return False  # a bound added where there was none tightens
    if new is None or new.unbounded:
        return not old.unbounded
    if old.unbounded:
        return False
    if old.text == new.text:
        return False
    if old.ms is None or new.ms is None:
        return True  # a changed bound that cannot be folded fails closed
    return new.ms > old.ms


def compare(old: list[Bound], new: list[Bound]) -> tuple[list[Bound], list[Bound]]:
    """The raised bounds and the newly added ones among the calls of one name in one member.

    Bounds equal in text pair up first. The rest are paired largest first, an unknown new value
    counting as the largest and an unknown old one as the smallest, so a raised bound is found
    whichever call it sits in; the smallest leftover new bounds are the newly added calls.
    """
    left = Counter(b.text for b in old)
    rest_new = []
    for bound in new:
        if left[bound.text] > 0:
            left[bound.text] -= 1
        else:
            rest_new.append(bound)
    rest_old = []
    for bound in old:
        if left[bound.text] > 0:
            left[bound.text] -= 1
            rest_old.append(bound)

    def size(b: Bound, unknown: float) -> float:
        return float("inf") if b.unbounded else (unknown if b.ms is None else b.ms)

    rest_new.sort(key=lambda b: size(b, float("inf")), reverse=True)
    rest_old.sort(key=lambda b: size(b, float("-inf")), reverse=True)
    raised = [n for n, o in zip(rest_new, rest_old, strict=False) if _raised(o, n)]
    return raised, rest_new[len(rest_old) :]


def _over_limit(bound: Bound) -> bool:
    if bound.unbounded:
        return True
    if WALL_TIME_LIMIT_MS is None:
        return False
    return bound.ms is None or bound.ms > WALL_TIME_LIMIT_MS


# -- sleeps -------------------------------------------------------------------------------


def _reaches_sleep(ctx: Context, path: str, call: Call) -> bool:
    """Whether a call reaches a fixed sleep through the helpers it may call, outside any loop."""
    program = ctx.after
    roots = follow_for_scan(program, path, call)
    if not roots:
        return False
    for reached in reach(program, roots, True, ctx.budget):
        if reached.method.body is None or _through_loop(program, reached):
            continue
        unit = program.units[reached.path]
        for inner in calls_in(unit, reached.method.body):
            if _in_loop(path_to(unit, inner.start)):
                continue
            if sleep_form(program, reached.path, inner):
                return True
    return False


def _through_loop(program: Program, reached: Reached) -> bool:
    while reached.parent is not None:
        unit = program.units[reached.parent.path]
        if reached.call is not None and _in_loop(path_to(unit, reached.call.start)):
            return True
        reached = reached.parent
    return False


def _sleep_findings(ctx: Context, change: FileChange, found: _Bounds) -> Iterator[Finding]:
    program, unit = ctx.after, change.after
    old_sites = _sites(change.before)
    new_sites = _sites(unit)
    lowered = _lowered_sleeps(ctx, change)
    seen: set[int] = set()
    direct = {at for at, _ in found.sleeps}
    for call in calls_in(unit, _Whole(unit)):
        if call.reference or call.start in seen:
            continue
        nodes = path_to(unit, call.start)
        if _in_loop(nodes):
            continue  # a poll interval or a fixed sleep: the poll loop rule decides
        site = _site(unit, call.start)
        if new_sites[site] <= old_sites[site]:
            continue
        line = unit.tokens[call.start].line
        if call.start in direct or sleep_form(program, change.path, call):
            if (site.member, site.following, line) in lowered:
                continue
            seen.add(call.start)
            yield Finding(CODE, change.path, line)
        elif _reaches_sleep(ctx, change.path, call):
            seen.add(call.start)
            yield Finding(CODE, change.path, line)


def _lowered_sleeps(ctx: Context, change: FileChange) -> set[tuple]:
    """Sleeps whose statement changed only by a duration that went down."""
    out = set()
    if change.before is None:
        return out

    def durations(program: Program, unit: CompilationUnit) -> dict:
        found = {}
        for call in calls_in(unit, _Whole(unit)):
            if call.reference or not sleep_form(program, change.path, call):
                continue
            args = _arguments(unit, call)
            values, _ = _argument_bounds(args, 0)
            ms = values[0].ms if values else (fold(args[0]) if args else None)
            site = _site(unit, call.start)
            found.setdefault((site.member, site.following, _masked(site.statement)), []).append(
                (ms, unit.tokens[call.start].line)
            )
        return found

    old = durations(ctx.before, change.before)
    for key, items in durations(ctx.after, change.after).items():
        olds = old.get(key, [])
        if len(items) > len(olds) or not olds:
            continue
        known = [ms for ms, _ in olds if ms is not None]
        if len(known) != len(olds):
            continue
        ceiling = sorted(known, reverse=True)
        for (ms, line), limit in zip(
            sorted(items, key=lambda x: -(x[0] or 0)), ceiling, strict=False
        ):
            if ms is not None and ms <= limit:
                out.add((key[0], key[1], line))
    return out


def _masked(text: str) -> str:
    return " ".join("#" if literal(w) is not None else w for w in text.split())


# -- ignored bounded waits ----------------------------------------------------------------


def _cleanup(unit: CompilationUnit, call: Call, statement: Statement) -> bool:
    nodes = path_to(unit, call.start)
    method = next((n for n in reversed(nodes) if isinstance(n, Method)), None)
    if method is None or method.body is None:
        return False
    names = {a.name.rsplit(".", 1)[-1] for a in method.annotations}
    in_finally = any(
        isinstance(n, Statement) and n.finally_block is not None
        and n.finally_block.start <= call.start < n.finally_block.end
        for n in nodes
    )  # fmt: skip
    if not (names & _AFTER_LIFECYCLE) and not in_finally:
        return False
    later = [c for c in calls_in(unit, method.body) if c.start >= statement.end]
    return not any(is_assertion_name(c.name) or c.name in ("verify", "fail") for c in later)


# -- the rule -----------------------------------------------------------------------------


def _file(ctx: Context, change: FileChange) -> Iterator[Finding]:
    path = change.path
    after = _bounds(ctx.after, path)
    before = _bounds(ctx.before, path) if change.before is not None else _Bounds({}, [], [])
    for group, new in after.groups.items():
        raised, added = compare(before.groups.get(group, []), new)
        for bound in raised:
            yield Finding(CODE, path, bound.line)
        if group[1] == "<setDefaultTimeout>":
            for bound in added:
                yield Finding(CODE, path, bound.line)
            continue
        if group[1] == "<awaitility>" or (group[1] in _WAITS and group[-1] == "pair"):
            for bound in added:
                if _over_limit(bound):
                    yield Finding(CODE, path, bound.line)
    old_timeouts = _method_timeouts(change.before) if change.before is not None else {}
    for member, new in _method_timeouts(change.after).items():
        if member in old_timeouts and _raised(old_timeouts[member], new):
            line = new.line if new is not None else old_timeouts[member].line
            yield Finding(CODE, path, _line_of(change.after, member, line))
    old_rules = _rule_timeouts(change.before) if change.before is not None else {}
    for member, new in _rule_timeouts(change.after).items():
        if member in old_rules and _raised(old_rules[member], new):
            yield Finding(CODE, path, new.line if new is not None else old_rules[member].line)
    old_sites, new_sites = _sites(change.before), _sites(change.after)
    for call, statement in after.ignored:
        site = _site(change.after, call.start)
        if new_sites[site] <= old_sites[site]:
            continue
        if not _cleanup(change.after, call, statement):
            yield Finding(CODE, path, change.after.tokens[call.start].line)
    yield from _sleep_findings(ctx, change, after)


def _line_of(unit: CompilationUnit, member: tuple, fallback: int) -> int:
    for node in walk(unit):
        if isinstance(node, Method) and _member_key(path_to(unit, node.start)) == member:
            return node.line
    return fallback


def findings(ctx: Context) -> Iterator[Finding]:
    seen = set()
    for change in ctx.changes:
        if change.path not in ctx.test_paths:
            continue
        for finding in _file(ctx, change):
            if (finding.path, finding.line) not in seen:
                seen.add((finding.path, finding.line))
                yield finding
