"""Widened timing bounds in test code (ADR 0007 Decision 7).

Bounds are compared with the before image of the same member: timeouts on test methods and
`Timeout` rules, Awaitility waits, calls taking a number and a `TimeUnit` or a `Duration`, and
numeric literals passed to calls named like a wait. Several such calls of one name in one member
are compared largest first, so a raised bound cannot hide behind a newly added smaller one; the
smallest of any extra calls are the newly added ones, judged as condition waits.

A fixed sleep is added when its statement, with the statement that follows it, is not in the
before image of its member, so a sleep moved within its method counts as added, and so is every
sleep a newly added or moved call reaches through the helpers it may call. A sleep in a loop, or
reached through one, is a fixed sleep unless the poll module finds the loop is a poll loop; a
raised interval in an existing loop is widened either way.
"""

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass

from rootflake.gate.checker import poll
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
from rootflake.gate.checker.values import TIME_UNITS, constant_lookup, fold, literal

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
_LOOPS = poll.LOOPS
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


def _value(text: str, line: int) -> Bound:
    return Bound(None, False, text, line)


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


def _fails(unit: CompilationUnit, branch) -> bool:
    """Whether a branch throws, fails or asserts."""
    for node in walk(branch):
        if isinstance(node, Statement) and node.kind == "throw":
            return True
    return any(
        not c.reference and (c.name == "fail" or is_assertion_name(c.name))
        for c in calls_in(unit, branch)
    )


def _checks(unit: CompilationUnit, statement: Statement, start: int, end: int) -> bool:
    """Whether the statement checks what tokens[start:end] hold: an assertion of it, or an `if`
    on it that throws, fails or asserts."""
    if statement.kind == "if" and statement.header is not None:
        if statement.header.start <= start and end <= statement.header.end:
            return any(_fails(unit, b) for b in statement.blocks)
    for call in calls_in(unit, statement):
        if call.reference or not (is_assertion_name(call.name) or call.name == "fail"):
            continue
        if call.end < start and end <= _close(unit, call.end):
            return True
    return False


def _result_ignored(unit: CompilationUnit, call: Call, statement: Statement) -> bool:
    """Whether a bounded wait's boolean goes unchecked (Decision 7): not asserted, not tested by
    an `if` that fails, and not held in a local that a later statement checks. A result that
    leaves through `return` or a loop condition is taken as checked."""
    close = _close(unit, call.end)
    if _checks(unit, statement, call.start, close + 1):
        return False
    tokens = unit.tokens
    name = None
    if statement.kind == "local_var":
        name = next(
            (n for n, s, e in poll.initializer_spans(tokens, statement.start, statement.end)
             if s <= call.start < e), None)  # fmt: skip
    elif statement.kind == "expression":
        for k in range(statement.start, call.start):
            if tokens[k].kind == "op" and tokens[k].text == "=" and tokens[k - 1].kind == "ident":
                name = tokens[k - 1].text
                break
        else:
            return True
    elif statement.kind == "if":
        return True
    else:
        return False
    if name is None:
        return True
    method = _method_at(unit, call.start)
    if method is None or method.body is None:
        return True
    for node in walk(method.body):
        if not isinstance(node, Statement) or node.start < statement.end:
            continue
        if node.kind == "return" and any(
            t.text == name for t in tokens[node.start : node.end] if t.kind == "ident"
        ):
            return False
        for k in range(node.start, node.end):
            if tokens[k].kind == "ident" and tokens[k].text == name:
                if _checks(unit, node, k, k + 1):
                    return False
    return True


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
    """Each method's timeout. Within one kind of annotation, a method's own overrides its
    enclosing classes', the nearest class first: Jupiter documents this for `@Timeout`, and a
    TestNG method's `@Test` is taken the same way, the stricter reading. Kinds that differ all
    apply, so the tightest of them counts."""
    out = {}
    for node in walk(unit):
        if not isinstance(node, Method):
            continue
        nodes = path_to(unit, node.start)
        owners = [node, *(d for d in reversed(nodes) if isinstance(d, TypeDecl))]
        bounds = []
        for kind in ("Timeout", "Test"):
            for owner in owners:
                found = [
                    b
                    for a in owner.annotations
                    if a.name.rsplit(".", 1)[-1] == kind
                    for b in _annotation_bound(unit, a, node.line)
                ]
                if found:
                    bounds += found
                    break
        out[_member_key(nodes)] = _tightest(bounds)
    return out


# A Timeout rule is often declared by its interface type.
_RULE_TYPES = frozenset(["Timeout", "TestRule", "MethodRule"])


def _rule_timeouts(unit: CompilationUnit) -> dict[tuple, Bound | None]:
    out = {}
    for node in walk(unit):
        if not isinstance(node, TypeDecl):
            continue
        bounds = []
        for field in node.fields:
            kind = field.type.split("<")[0].rsplit(".", 1)[-1]
            if kind not in _RULE_TYPES:
                continue
            names = {a.name.rsplit(".", 1)[-1] for a in field.annotations}
            if not names & {"Rule", "ClassRule"}:
                continue
            for span in field.initializers.values():
                text = _text(unit, span.start, span.end)
                if kind == "Timeout" or "Timeout" in text.split():
                    bounds.append(_rule_bound(text, field.line))
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


def _innermost_loop(unit: CompilationUnit, index: int, method: Method) -> Statement | None:
    for node in reversed(path_to(unit, index)):
        if node is method:
            return None
        if isinstance(node, Statement) and node.kind in _LOOPS:
            return node
    return None


def _method_at(unit: CompilationUnit, index: int) -> Method | None:
    return next((n for n in reversed(path_to(unit, index)) if isinstance(n, Method)), None)


def _following(unit: CompilationUnit, statement: Statement) -> Statement | None:
    nodes = path_to(unit, statement.start)
    for k in range(len(nodes) - 1, 0, -1):
        if nodes[k] is statement and isinstance(nodes[k - 1], Block):
            statements = nodes[k - 1].statements
            at = next(i for i, s in enumerate(statements) if s is statement)
            return statements[at + 1] if at + 1 < len(statements) else None
    return None


def _asserted_result(unit: CompilationUnit, call: Call) -> bool:
    """Whether the call is an argument of an assertion, which checks what it returns."""
    statement, _ = _statement(path_to(unit, call.start))
    if statement is None:
        return False
    for outer in calls_in(unit, statement):
        if outer.reference or not (is_assertion_name(outer.name) or outer.name == "fail"):
            continue
        if outer.end < call.start < _close(unit, outer.end):
            return True
    return False


def _fixed_loop(
    program: Program,
    path: str,
    method: Method,
    loop: Statement,
    sleep_at: int,
    scope: poll.Scope,
    check: tuple[CompilationUnit, Statement | None, Call | None],
) -> bool:
    """Whether a loop around a sleep is a fixed sleep rather than a poll loop. check is the
    caller's file, the statement whose follower must assert a silent loop's exit condition,
    and the call whose result may be asserted instead."""
    found = poll.read_loop(program, path, method, loop, sleep_at, scope)
    if not poll.qualifies(found):
        return True
    if found.asserts:
        return False
    unit, statement, call = check
    if call is not None and _asserted_result(unit, call):
        return False
    if statement is None:
        return False  # a silent loop in a helper is judged at each new call of it
    after = _following(unit, statement)
    exits = [poll.substitute(e, scope.mapping) for e in found.exits]
    return after is None or not poll.asserts_condition(unit, after, exits)


_ENTRY_POINTS = frozenset([
    "Test", "ParameterizedTest", "RepeatedTest", "TestFactory", "TestTemplate", "Before",
    "BeforeEach", "BeforeClass", "BeforeAll", "BeforeMethod", "BeforeTest", "BeforeSuite",
    "BeforeGroups", *_AFTER_LIFECYCLE,
])  # fmt: skip


def _called(program: Program, method: Method) -> bool:
    """Whether a read file calls a method of this name, so its loop is judged at each call. A
    test or lifecycle method is run by its engine, so its loop is judged where it is."""
    if any(a.name.rsplit(".", 1)[-1] in _ENTRY_POINTS for a in method.annotations):
        return False
    return any(
        not c.reference and c.name == method.name
        for unit in program.units.values()
        for c in calls_in(unit, _Whole(unit))
    )


def _reaches_sleep(ctx: Context, path: str, call: Call) -> bool:
    """Whether a call reaches a fixed sleep through the helpers it may call: a sleep outside any
    loop, or in a loop that is not a poll loop."""
    program = ctx.after
    roots = follow_for_scan(program, path, call)
    if not roots:
        return False
    unit = program.units[path]
    caller = _method_at(unit, call.start)
    caller_defs = poll._local_definitions(unit, caller.body) if caller and caller.body else {}
    statement, _ = _statement(path_to(unit, call.start))
    mappings: dict[int, dict] = {}

    def mapping(reached: Reached) -> dict:
        if id(reached) not in mappings:
            if reached.parent is None:
                found = poll.call_mapping(unit, call, reached.method, {})
            else:
                outer = program.units[reached.parent.path]
                found = poll.call_mapping(
                    outer, reached.call, reached.method, mapping(reached.parent)
                )
            mappings[id(reached)] = found
        return mappings[id(reached)]

    for reached in reach(program, roots, True, ctx.budget):
        if reached.method.body is None:
            continue
        inner_unit = program.units[reached.path]
        for inner in calls_in(inner_unit, reached.method.body):
            if inner.reference or not sleep_form(program, reached.path, inner):
                continue
            where, at, holder = reached, inner.start, reached
            loop = _innermost_loop(inner_unit, at, reached.method)
            while loop is None and where.parent is not None:
                at, where = where.call.start, where.parent
                loop = _innermost_loop(program.units[where.path], at, where.method)
                holder = where
            if loop is not None:
                scope = poll.Scope(
                    program,
                    mapping(holder),
                    poll._counters(program.units[holder.path], loop),
                    poll._local_definitions(program.units[holder.path], holder.method.body),
                    caller_defs,
                )
                if _fixed_loop(program, holder.path, holder.method, loop, at, scope,
                               (unit, statement, call)):  # fmt: skip
                    return True
                continue
            loop = _innermost_loop(unit, call.start, caller) if caller is not None else None
            if loop is None:
                return True
            scope = poll.Scope(program, {}, poll._counters(unit, loop), caller_defs, caller_defs)
            if _fixed_loop(program, path, caller, loop, call.start, scope, (unit, loop, None)):
                return True
    return False


def _sleep_findings(ctx: Context, change: FileChange, found: _Bounds) -> Iterator[Finding]:
    program, unit = ctx.after, change.after
    old_sites = _sites(change.before)
    new_sites = _sites(unit)
    lowered, old_keys = _lowered_sleeps(ctx, change)
    seen: set[int] = set()
    direct = {at for at, _ in found.sleeps}
    for call in calls_in(unit, _Whole(unit)):
        if call.reference or call.start in seen:
            continue
        site = _site(unit, call.start)
        if new_sites[site] <= old_sites[site]:
            continue
        line = unit.tokens[call.start].line
        if call.start in direct or sleep_form(program, change.path, call):
            if (site.member, site.following, line) in lowered:
                continue
            method = _method_at(unit, call.start)
            loop = _innermost_loop(unit, call.start, method) if method is not None else None
            if loop is not None and _sleep_key(unit, call) not in old_keys:
                defs = poll._local_definitions(unit, method.body)
                scope = poll.Scope(program, {}, poll._counters(unit, loop), defs, defs)
                check = (unit, None if _called(program, method) else loop, None)
                if not _fixed_loop(program, change.path, method, loop, call.start, scope, check):
                    continue
            seen.add(call.start)
            yield Finding(CODE, change.path, line)
        elif _reaches_sleep(ctx, change.path, call):
            seen.add(call.start)
            yield Finding(CODE, change.path, line)


def _lowered_sleeps(ctx: Context, change: FileChange) -> tuple[set[tuple], set[tuple]]:
    """Sleeps whose statement changed only by a duration that went down, and the sites of
    every sleep in the before image with its literals masked."""
    out: set[tuple] = set()
    if change.before is None:
        return out, set()

    def durations(program: Program, unit: CompilationUnit) -> dict:
        found = {}
        lookup = constant_lookup(_file_constants(unit))
        for call in calls_in(unit, _Whole(unit)):
            if call.reference or not sleep_form(program, change.path, call):
                continue
            args = _arguments(unit, call)
            values, _ = _argument_bounds(args, 0)
            ms = values[0].ms if values else (fold(args[0], lookup) if args else None)
            found.setdefault(_sleep_key(unit, call), []).append((ms, unit.tokens[call.start].line))
        return found

    old = durations(ctx.before, change.before)
    old_keys = set(old)
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
    return out, old_keys


def _file_constants(unit: CompilationUnit) -> dict[str, str]:
    """The fields of a file declared once, by name, with their initializer text."""
    counts: Counter = Counter()
    texts: dict[str, str] = {}
    for node in walk(unit):
        if isinstance(node, Field):
            for name in node.names:
                counts[name] += 1
            for name, span in node.initializers.items():
                texts[name] = _text(unit, span.start, span.end)
    return {n: t for n, t in texts.items() if counts[n] == 1}


def _sleep_key(unit: CompilationUnit, call: Call) -> tuple:
    """Where a sleep sits, with its own arguments left out, so a changed duration pairs."""
    site = _site(unit, call.start)
    statement, _ = _statement(path_to(unit, call.start))
    if statement is None or call.arity is None:
        return (site.member, site.following, _masked(site.statement))
    close = _close(unit, call.end)
    text = f"{_text(unit, statement.start, call.end + 1)} # {_text(unit, close, statement.end)}"
    return (site.member, site.following, _masked(text))


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
