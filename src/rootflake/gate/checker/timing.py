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
from rootflake.gate.checker.assertions import (
    _close,
    _shadowed,
    _split_arguments,
    _text,
    is_assertion_name,
)
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
# Timed waits that report a timeout only through what they return, so a dropped result waits
# out the bound and checks nothing (ruled 2026-10-30).
_IGNORED_WAITS = _RESULT_WAITS | frozenset(["tryAcquire", "tryLock", "poll"])
# Timed waits by the form a bound taken off them leaves, and the calls that wait in that form
# with no bound, by arity, so `f.get(500, MILLISECONDS)` turned into `f.get()` or `f.join()` is
# a bound removed (ruled 2026-10-30).
_TIMED_FORMS = {
    "get": "get", "join": "join", "await": "await", "tryAcquire": "acquire", "tryLock": "lock",
    "poll": "take", "awaitAdvanceInterruptibly": "awaitAdvance",
    "awaitTermination": "awaitTermination",
}  # fmt: skip
_UNBOUNDED_CALLS = {
    "get": ((0,), ("get",)), "join": ((0,), ("join", "get")), "await": ((0,), ("await",)),
    "acquire": ((0, 1), ("acquire",)), "acquireUninterruptibly": ((0, 1), ("acquire",)),
    "lock": ((0,), ("lock",)), "lockInterruptibly": ((0,), ("lock",)), "take": ((0,), ("take",)),
    "awaitAdvance": ((1,), ("awaitAdvance",)), "close": ((0,), ("awaitTermination",)),
}  # fmt: skip
# `close()` is too common to pair with a lost wait on another receiver.
_SAME_RECEIVER_ONLY = frozenset(["awaitTermination"])
# Mockito verification modes: a timeout ends once the interaction is seen, while after always
# waits its full period, so an added or raised after is a fixed sleep (ruled 2026-10-30).
_MOCKITO_WAITS = {"timeout": "<mockito-timeout>", "after": "<mockito-after>"}
# Bounds that hold a test for their full length whatever the code does: judged as sleeps, a
# raised or added one is widened unless the member lost a sleep at least as long.
_SLEEP_GROUPS = frozenset(["<mockito-after>", "<pollDelay>"])
_SPIN_CALLS = frozenset(["onSpinWait", "yield"])
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


def _previous_text(unit: CompilationUnit, statement: Statement) -> str:
    nodes = path_to(unit, statement.start)
    for k in range(len(nodes) - 1, 0, -1):
        if nodes[k] is statement and isinstance(nodes[k - 1], Block):
            statements = nodes[k - 1].statements
            at = next(i for i, s in enumerate(statements) if s is statement)
            if at == 0:
                return "{"
            return _text(unit, statements[at - 1].start, statements[at - 1].end)
    return ""


def _preceded(unit: CompilationUnit | None) -> Counter:
    """Every block level statement with the statement before it, counted per member."""
    out: Counter = Counter()
    if unit is None:
        return out
    for node in walk(unit):
        if isinstance(node, Block):
            for k, statement in enumerate(node.statements):
                prev = node.statements[k - 1] if k else None
                text = _text(unit, prev.start, prev.end) if prev is not None else "{"
                member = _member_key(path_to(unit, statement.start))
                out[(member, text, _text(unit, statement.start, statement.end))] += 1
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
    sleeps: list[tuple[int, str]]  # always true pollDelay and during waits
    ignored: list[tuple[Call, Statement]]
    bounded: Counter  # timed waits by member, unbounded form and receiver
    unbounded: dict[tuple, list[Call]]  # the same waits made with no bound
    aliased: dict  # unbounded waits on a local or parameter, by what they may stand for


def _mockito_mode(unit: CompilationUnit, call: Call) -> bool:
    if call.name not in _MOCKITO_WAITS or call.arity != 1 or call.reference:
        return False
    if call.receiver == "Mockito":
        return True
    if call.receiver is not None:
        return False
    return any(
        imp.static and imp.name.startswith("org.mockito.")
        and (imp.wildcard or imp.name.rsplit(".", 1)[-1] == call.name)
        for imp in unit.imports
    )  # fmt: skip


def _single_bound(args: tuple[str, ...], line: int) -> Bound:
    found, _ = _argument_bounds(args, line)
    if found:
        return found[0]
    text = " , ".join(args)
    if _strip_qualifier(text.split()) == ["Duration", ".", "ZERO"]:
        return Bound(0.0, False, text, line)
    return Bound(fold(args[0]) if len(args) == 1 else None, False, text, line)


# The types that make a wait of each form, for a parameter the waits may have been moved into.
_FORM_TYPES = {
    "get": frozenset(["Future", "FutureTask", "CompletableFuture", "ForkJoinTask", "RunnableFuture",
                      "ScheduledFuture", "ListenableFuture"]),
    "join": frozenset(["Thread", "ForkJoinTask"]),
    "await": frozenset(["CountDownLatch", "CyclicBarrier", "Condition"]),
    "acquire": frozenset(["Semaphore"]),
    "lock": frozenset(["Lock", "ReentrantLock", "ReadLock", "WriteLock"]),
    "take": frozenset(["BlockingQueue", "BlockingDeque", "TransferQueue", "LinkedBlockingQueue",
                       "ArrayBlockingQueue", "LinkedBlockingDeque", "LinkedTransferQueue",
                       "PriorityBlockingQueue", "DelayQueue", "SynchronousQueue",
                       "CompletionService", "ExecutorCompletionService"]),
    "awaitAdvance": frozenset(["Phaser"]),
}  # fmt: skip


def _simple_type(text: str) -> str:
    return text.split("<", 1)[0].strip().rsplit(".", 1)[-1].strip()


def _cast_receiver(unit: CompilationUnit, call: Call) -> str | None:
    """The name a bracketed receiver holds, as `f` in `((Future<?>) f).get()`."""
    tokens = unit.tokens
    if call.start < 2 or tokens[call.start - 1].text != "." or tokens[call.start - 2].text != ")":
        return None
    depth, k = 0, call.start - 2
    while k >= 0:
        depth += tokens[k].text == ")"
        depth -= tokens[k].text == "("
        if depth == 0:
            break
        k -= 1
    words = [t.text for t in tokens[k + 1 : call.start - 2]]
    if len(words) == 1 and words[0].isidentifier():
        return words[0]
    if words[:1] == ["("] and len(words) >= 3 and words[-2] == ")" and words[-1].isidentifier():
        return words[-1]
    return None


@dataclass(frozen=True)
class _Open:
    traced: frozenset  # the names its definitions hold, followed through every alias
    forms: frozenset  # the forms its declared type makes, for a parameter


def _alias_targets(value: str) -> set[str]:
    """The names a definition holds as it is, through brackets, casts and a conditional's
    branches, as `f` in `f`, `(Future<?>) f` or `ok ? f : g`. Anything built from a name, such
    as `Optional.of(f)`, holds none."""
    words = value.split()
    branches = [words]
    if "?" in words and ":" in words:
        q = words.index("?")
        c = next((k for k in range(len(words) - 1, q, -1) if words[k] == ":"), None)
        if c is not None:
            branches = [words[q + 1 : c], words[c + 1 :]]
    out = set()
    for branch in branches:
        while branch[:1] == ["("]:
            close = _matching_close(branch, 0)
            if close is None:
                break
            if close == len(branch) - 1:
                branch = branch[1:-1]
            else:
                branch = branch[close + 1 :]  # a cast
        if len(branch) == 1 and branch[0].isidentifier():
            out.add(branch[0])
    return out


def _type_bound(unit: CompilationUnit, method: Method, name: str) -> frozenset[str]:
    """A parameter's declared simple type, with a type variable of the method or a class around
    it read as its bounds."""
    tokens = unit.tokens
    holders = [method] + [
        d for d in reversed(path_to(unit, method.start)) if isinstance(d, TypeDecl)
    ]
    for holder in holders:
        words, k = [], holder.start
        while k < holder.end and tokens[k].text not in ("(", "{"):
            if tokens[k].text == "@":  # an annotation, with any arguments it has
                k += 1
                while k + 2 < holder.end and tokens[k + 1].text == ".":
                    k += 2
                if k + 1 < holder.end and tokens[k + 1].text == "(":
                    depth, k = 0, k + 1
                    while k < holder.end:
                        depth += tokens[k].text == "("
                        depth -= tokens[k].text == ")"
                        if depth == 0:
                            break
                        k += 1
                k += 1
                continue
            words.append(tokens[k].text)
            k += 1
        for k in range(1, len(words) - 2):
            if words[k] == name and words[k + 1] == "extends" and words[k - 1] in ("<", ","):
                bounds, parts, depth, j = set(), [], 0, k + 2
                while j < len(words):
                    word = words[j]
                    if word == "<":
                        depth += 1
                    elif word == ">" and depth:
                        depth -= 1
                    elif depth == 0 and word in ("&", ",", ">"):
                        bounds.add(_simple_type("".join(parts)))
                        parts = []
                        if word != "&":
                            break
                    elif depth == 0:
                        parts.append(word)
                    j += 1
                return frozenset(bounds)
    return frozenset([name])


def _field_definitions(unit: CompilationUnit, memo: dict) -> dict[str, list[str]]:
    """Every value the file gives each field: its initializers, and each `name = value` or
    `this.name = value` anywhere in the file, field initializer lambdas included, where no
    local or parameter of that name is in scope."""
    if "fields" not in memo:
        out: dict[str, list[str]] = {}
        tokens = unit.tokens
        for node in walk(unit):
            if isinstance(node, Field):
                for name, span in node.initializers.items():
                    out.setdefault(name, []).append(_text(unit, span.start, span.end))
            if not isinstance(node, Statement) or node.kind != "expression":
                continue
            for k in range(node.start + 1, node.end):
                token = tokens[k]
                if token.kind == "op" and token.text == "=" and tokens[k - 1].kind == "ident":
                    name = tokens[k - 1].text
                    qualified = (
                        k >= 3 and tokens[k - 2].text == "." and tokens[k - 3].text == "this"
                    )
                    if qualified or not _shadowed(unit, k - 1, name):
                        out.setdefault(name, []).append(_text(unit, k + 1, node.end - 1))
                    break
        memo["fields"] = out
    return memo["fields"]


def _receiver(unit: CompilationUnit, call: Call, memo: dict) -> tuple[str | None, _Open | None]:
    """A wait's receiver, read through a cast and through every local or field alias that only
    ever holds another name, transitively. For a name with other definitions, also the names
    they hold; for a parameter, the forms its declared type makes."""
    receiver = call.receiver
    if receiver == "<expr>":
        receiver = _cast_receiver(unit, call) or receiver
    method = _method_at(unit, call.start)
    if method is None or method.body is None or receiver is None:
        return receiver, None
    if method.start not in memo:
        local = poll._local_definitions(unit, method.body)
        params = {p.name: _simple_type(p.type) for p in method.params}
        memo[method.start] = (local, params)
    local, params = memo[method.start]
    fields = _field_definitions(unit, memo)

    def definitions(name: str) -> list[str]:
        if name in local:
            return local[name]
        if name in params:
            return []
        return fields.get(name, [])

    current, seen = receiver, {receiver}
    while True:
        values = definitions(current)
        targets = [_alias_targets(v) for v in values]
        if not values or any(len(t) != 1 for t in targets) or len(set().union(*targets)) != 1:
            break
        (nxt,) = set().union(*targets)
        if nxt in seen:
            break
        seen.add(nxt)
        current = nxt
    frontier = [current]
    while frontier:
        for value in definitions(frontier.pop()):
            for target in _alias_targets(value) - seen:
                seen.add(target)
                frontier.append(target)
    if current in params:
        type_names = _type_bound(unit, method, params[current])
        forms = frozenset(f for f, types in _FORM_TYPES.items() if type_names & types)
        return current, _Open(frozenset(seen), forms)
    if len(seen) > 1 or definitions(current):
        return current, _Open(frozenset(seen), frozenset())
    return current, None


def _bounds(program: Program, path: str) -> _Bounds:
    unit = program.units[path]
    calls = calls_in(unit, _Whole(unit))
    modes = _mode_spans(unit, calls)
    groups: dict[tuple, list[Bound]] = {}
    sleeps, ignored = [], []
    bounded: Counter = Counter()
    unbounded: dict[tuple, list[Call]] = {}
    aliased: dict[int, _Open] = {}
    receivers: dict = {}
    tokens = unit.tokens
    for call in calls:
        if call.reference:
            continue
        nodes = path_to(unit, call.start)
        member = _member_key(nodes)
        line = tokens[call.start].line
        args = _arguments(unit, call)
        root = _awaitility_root(unit, call)
        if root:
            chain = _chain(unit, call)
            groups.setdefault((member, "<awaitility>"), []).append(_chain_bound(chain, line))
            for name, _, chain_args in chain:
                if name == "pollDelay" and chain_args:
                    bound = _single_bound(chain_args, line)
                    groups.setdefault((member, "<pollDelay>"), []).append(bound)
            names = [name for name, _, _ in chain]
            if ("pollDelay" in names or "during" in names) and chain[-1][0].startswith("until"):
                if " , ".join(chain[-1][2]) in _TRUE_CONDITIONS:
                    sleeps.append((call.start, "pollDelay"))
        if call.name == "setDefaultTimeout":
            groups.setdefault((member, "<setDefaultTimeout>"), []).append(_value("", line))
        if _mockito_mode(unit, call):
            group = (member, _MOCKITO_WAITS[call.name])
            groups.setdefault(group, []).append(_single_bound(args, line))
            continue
        if _inside(modes, call.start):
            continue
        found, used = _argument_bounds(args, line)
        if not root and (call.name in _TIMED_FORMS or call.name in _UNBOUNDED_CALLS):
            timed = found or (call.name == "join" and sleep_form(program, path, call))
            receiver, open_name = _receiver(unit, call, receivers)
            if timed and call.name in _TIMED_FORMS:
                bounded[(member, _TIMED_FORMS[call.name], receiver)] += 1
            elif not timed and call.name in _UNBOUNDED_CALLS:
                arities, forms = _UNBOUNDED_CALLS[call.name]
                if call.arity in arities:
                    for form in forms:
                        unbounded.setdefault((member, form, receiver), []).append(call)
                    if open_name is not None:
                        aliased[call.start] = open_name
        for bound in found:
            groups.setdefault((member, call.name, "pair"), []).append(bound)
        lower = call.name.lower()
        if call.name not in _TOLERANCE_FACTORIES and any(w in lower for w in _WAIT_WORDS):
            for k, arg in enumerate(args):
                number = fold(arg)
                if k not in used and number is not None:
                    group = (member, call.name, k)
                    groups.setdefault(group, []).append(Bound(number, False, arg, line))
        if call.name in _IGNORED_WAITS and found:
            statement, _ = _statement(nodes)
            if statement is not None and _result_ignored(unit, call, statement):
                ignored.append((call, statement))
    return _Bounds(groups, sleeps, ignored, bounded, unbounded, aliased)


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
    return True


def _object_wait(program: Program, path: str, call: Call) -> bool:
    """`Object.wait`, which outside a loop that rechecks its condition waits out its timeout,
    or until any notify, and checks nothing: judged as a sleep (ruled 2026-10-30)."""
    if call.name != "wait" or call.reference or call.arity not in (0, 1, 2):
        return False
    return not follow_for_scan(program, path, call)


def _sleeps_like(program: Program, path: str, call: Call) -> bool:
    """A test side sleep: a sleep form or an `Object.wait`. Production code keeps reading only
    sleep forms."""
    return sleep_form(program, path, call) or _object_wait(program, path, call)


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
                    bounds.append(_rule_bound(_timeout_part(text), field.line))
        for method in node.methods:  # JUnit 4 also reads a rule a method returns
            names = {a.name.rsplit(".", 1)[-1] for a in method.annotations}
            kind = (method.return_type or "").split("<")[0].rsplit(".", 1)[-1]
            if not names & {"Rule", "ClassRule"} or method.body is None:
                continue
            for statement in walk(method.body):
                if not isinstance(statement, Statement) or statement.kind != "return":
                    continue
                text = _text(unit, statement.start + 1, statement.end - 1)
                if text and (kind == "Timeout" or "Timeout" in text.split()):
                    bounds.append(_rule_bound(_timeout_part(text), statement.line))
        out[_member_key(path_to(unit, node.start))] = _tightest(bounds)
    return out


def _timeout_part(text: str) -> str:
    """The Timeout inside a rule's text, as in `RuleChain.outerRule(new Timeout(5000))
    .around(cleanup)`, so a rule added around it leaves the bound as it was."""
    words = text.split()
    for k in range(len(words) - 2):
        if words[k : k + 3] == ["new", "Timeout", "("]:
            close = _matching_close(words, k + 2)
            if close is not None:
                return " ".join(words[k : close + 1])
        if words[k : k + 2] == ["Timeout", "."] and k + 3 < len(words) and words[k + 3] == "(":
            if words[k + 2] == "builder":
                at = next((j for j in range(k, len(words)) if words[j] == "withTimeout"), None)
                close = _matching_close(words, at + 1) if at is not None else None
            else:
                close = _matching_close(words, k + 3)
            if close is not None:
                return " ".join(words[k : close + 1])
    return text


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
    """The raised bounds and the newly added ones among the calls of one name in one member."""
    raised, added, _ = _pairing(old, new)
    return raised, added


def _pairing(old: list[Bound], new: list[Bound]) -> tuple[list[Bound], list[Bound], list[Bound]]:
    """The raised bounds, the newly added ones and the lost ones among the calls of one name.

    Bounds equal in text pair up first. The rest are paired largest first, an unknown new value
    counting as the largest and an unknown old one as the smallest, so a raised bound is found
    whichever call it sits in; the smallest leftover new bounds are the newly added calls, and
    the smallest leftover old ones the calls that went.
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
    return raised, rest_new[len(rest_old) :], rest_old[len(rest_new) :]


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


def _follower(unit: CompilationUnit, statement: Statement) -> Statement | None:
    """The statement after this one, looking past the end of a `synchronized` block that it
    closes, as in `synchronized (lock) { while (...) { lock.wait(100); } } assertTrue(...)`."""
    after = _following(unit, statement)
    while after is None:
        nodes = path_to(unit, statement.start)
        outer = next(
            (n for n in reversed(nodes) if isinstance(n, Statement) and n is not statement
             and n.start < statement.start),
            None,
        )  # fmt: skip
        if outer is None or outer.kind != "synchronized" or not outer.blocks:
            return None
        inner = outer.blocks[0].statements
        if not inner or inner[-1] is not statement:
            return None
        statement, after = outer, _following(unit, outer)
    return after


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
    after = _follower(unit, statement)
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
            if inner.reference or not _sleeps_like(program, reached.path, inner):
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


def _reaches_ignored_wait(ctx: Context, path: str, call: Call) -> bool:
    """Whether a call reaches a helper that makes a bounded wait and drops its result, as
    `static void waitFor(CountDownLatch l) { l.await(5, SECONDS); }` does: the call then waits up
    to the bound and checks nothing, a fixed sleep like an unchecked latch wait. A helper that
    returns the result leaves it to its caller and is not judged here, a recorded limit."""
    program = ctx.after
    roots = follow_for_scan(program, path, call)
    if not roots:
        return False
    for reached in reach(program, roots, True, ctx.budget):
        body = reached.method.body
        if body is None:
            continue
        inner = program.units[reached.path]
        for wait in calls_in(inner, body):
            if wait.reference or wait.name not in _IGNORED_WAITS:
                continue
            if not _argument_bounds(_arguments(inner, wait), 0)[0]:
                continue
            statement, _ = _statement(path_to(inner, wait.start))
            if statement is None or not _result_ignored(inner, wait, statement):
                continue
            if not _cleanup(inner, wait, statement):
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
        if call.start in direct or _sleeps_like(program, change.path, call):
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
        elif _reaches_sleep(ctx, change.path, call) or _reaches_ignored_wait(
            ctx, change.path, call
        ):
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
            if call.reference or not _sleeps_like(program, change.path, call):
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
    empty = _Bounds({}, [], [], Counter(), {}, {})
    before = _bounds(ctx.before, path) if change.before is not None else empty
    old_sleeps = _member_sleeps(ctx.before, path, change.before)
    new_sleeps = _member_sleeps(ctx.after, path, change.after)
    lost_by: dict[tuple, list[Bound]] = {}
    added_by: dict[tuple, list[Bound]] = {}
    sleep_added: dict[tuple, list[Bound]] = {}
    sleep_lost: list[Bound] = []
    for group in sorted(set(after.groups) | set(before.groups), key=repr):
        new = after.groups.get(group, [])
        raised, added, lost = _pairing(before.groups.get(group, []), new)
        for bound in raised:
            yield Finding(CODE, path, bound.line)
        if group[1] == "<setDefaultTimeout>":
            for bound in added:
                yield Finding(CODE, path, bound.line)
            continue
        if group[1] in _SLEEP_GROUPS:
            sleep_added.setdefault(group[0], []).extend(b for b in added if b.ms != 0)
            sleep_lost.extend(lost)
            continue
        lost_by.setdefault(group[1:], []).extend(lost)
        added_by.setdefault(group[1:], []).extend(added)
        if group[1] == "<awaitility>" or (group[1] in _WAITS and group[-1] == "pair"):
            for bound in added:
                if _over_limit(bound):
                    yield Finding(CODE, path, bound.line)
    # A wait one member lost is paired with one of its name another member gained, so moving a
    # wait into a helper cannot carry a raised bound (ruled 2026-10-30).
    for tail, lost in lost_by.items():
        for bound in compare(lost, added_by.get(tail, []))[0]:
            yield Finding(CODE, path, bound.line)
    # An added or raised after or pollDelay is first set against the sleeps its member lost,
    # each lost sleep once, then against the after and pollDelay waits other members lost.
    left: list[Bound] = []
    for member, added in sorted(sleep_added.items(), key=repr):
        gone = _pairing(old_sleeps.get(member, []), new_sleeps.get(member, []))[2]
        extra, unpaired = compare(gone, added)
        for bound in extra:
            yield Finding(CODE, path, bound.line)
        left += unpaired
    extra, unpaired = compare(sleep_lost, left)
    for bound in extra + unpaired:
        yield Finding(CODE, path, bound.line)
    yield from _removed_bounds(change, before, after)
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
    old_preceded, new_preceded = _preceded(change.before), _preceded(change.after)
    for call, statement in after.ignored:
        site = _site(change.after, call.start)
        if new_sites[site] <= old_sites[site]:
            continue
        before_it = (site.member, _previous_text(change.after, statement), site.statement)
        if call.name not in _RESULT_WAITS and new_preceded[before_it] <= old_preceded[before_it]:
            continue  # unmoved, with only the code after it new: a ruling still open
        if not _cleanup(change.after, call, statement):
            yield Finding(CODE, path, change.after.tokens[call.start].line)
    yield from _sleep_findings(ctx, change, after)
    yield from _spin_loops(ctx, change)


def _removed_bounds(change: FileChange, before: _Bounds, after: _Bounds) -> Iterator[Finding]:
    """A timed wait whose bound was taken off, as `f.get(500, MILLISECONDS)` turned into
    `f.get()` or `sem.tryAcquire(5, SECONDS)` into `sem.acquire()`. Each unbounded wait of a
    form on a receiver that a member gained, up to the timed ones of that form and receiver it
    lost, is widened, the gained waits on added statements first. What is left is paired across
    the file: a timed wait lost anywhere with an unbounded one of its form gained anywhere on
    the same receiver, on a local or a field assigned there whose definitions name it, or on a
    parameter whose declared type makes that form."""
    old_sites, new_sites = _sites(change.before), _sites(change.after)
    unit = change.after

    def fresh(call: Call) -> bool:
        site = _site(unit, call.start)
        return new_sites[site] > old_sites[site]

    lost_left: Counter = Counter()
    gained_left: dict[str, list[tuple[str | None, Call]]] = {}
    for key in set(before.bounded) | set(after.bounded) | set(after.unbounded):
        _, form, receiver = key
        calls = after.unbounded.get(key, [])
        lost = before.bounded[key] - after.bounded[key]
        gained = len(calls) - len(before.unbounded.get(key, []))
        ordered = sorted(calls, key=lambda c: (not fresh(c), c.start))[: max(gained, 0)]
        taken = min(max(lost, 0), len(ordered))
        for call in ordered[:taken]:
            yield Finding(CODE, change.path, unit.tokens[call.start].line)
        if lost > taken:
            lost_left[(form, receiver)] += lost - taken
        gained_left.setdefault(form, []).extend((receiver, c) for c in ordered[taken:])
    used: set[int] = set()
    for (form, receiver), count in sorted(lost_left.items(), key=repr):
        for other, call in gained_left.get(form, []):
            if count == 0:
                break
            if call.start in used:
                continue
            seen = after.aliased.get(call.start)
            stands_for = (
                seen is not None
                and form not in _SAME_RECEIVER_ONLY
                and (receiver in seen.traced or form in seen.forms)
            )
            if other == receiver or stands_for:
                used.add(call.start)
                count -= 1
                yield Finding(CODE, change.path, unit.tokens[call.start].line)


def _member_sleeps(
    program: Program, path: str, unit: CompilationUnit | None
) -> dict[tuple, list[Bound]]:
    """The test side sleeps of a file by member, each with its length when it folds."""
    out: dict[tuple, list[Bound]] = {}
    if unit is None:
        return out
    lookup = constant_lookup(_file_constants(unit))
    for call in calls_in(unit, _Whole(unit)):
        if call.reference or not _sleeps_like(program, path, call):
            continue
        args = _arguments(unit, call)
        values, _ = _argument_bounds(args, 0)
        ms = values[0].ms if values else (fold(args[0], lookup) if args else None)
        line = unit.tokens[call.start].line
        text = _text(unit, call.start, _close(unit, call.end) + 1) if call.arity is not None else ""
        member = _member_key(path_to(unit, call.start))
        out.setdefault(member, []).append(Bound(ms, False, text, line))
    return out


def _spin_loops(ctx: Context, change: FileChange) -> Iterator[Finding]:
    """An added loop that waits without a sleep (ruled 2026-10-30): a body that is empty or
    only spins (`Thread.onSpinWait()`, `Thread.yield()`) under a condition that reads state, or
    a condition that makes a timed wait, as in `while (!latch.await(1, SECONDS)) {}`. It is
    judged under the poll rules, so it must be bounded by a clock or counter and checked."""
    program, unit = ctx.after, change.after
    old_sites, new_sites = _sites(change.before), _sites(unit)
    for loop in walk(unit):
        if not isinstance(loop, Statement) or loop.kind not in _LOOPS or loop.header is None:
            continue
        site = _site(unit, loop.start)
        if new_sites[site] <= old_sites[site]:
            continue
        method = _method_at(unit, loop.start)
        if method is None or method.body is None:
            continue
        defs = poll._local_definitions(unit, method.body)
        scope = poll.Scope(program, {}, poll._counters(unit, loop), defs, defs)
        if not (_timed_condition(unit, loop) or (
            _only_spins(unit, loop) and "state" in poll.kinds(poll._condition(unit, loop), scope)
        )):  # fmt: skip
            continue
        check = (unit, None if _called(program, method) else loop, None)
        if _fixed_loop(program, change.path, method, loop, loop.end, scope, check):
            yield Finding(CODE, change.path, loop.line)


def _only_spins(unit: CompilationUnit, loop: Statement) -> bool:
    for body in loop.blocks:
        for statement in body.statements:
            if statement.kind == "empty":
                continue
            if statement.kind != "expression":
                return False
            calls = [c for c in calls_in(unit, statement) if not c.reference]
            if len(calls) != 1 or calls[0].name not in _SPIN_CALLS or calls[0].arity != 0:
                return False
    return True


def _timed_condition(unit: CompilationUnit, loop: Statement) -> bool:
    header = loop.header
    for call in calls_in(unit, header):
        if call.reference or call.name not in _WAITS:
            continue
        if _argument_bounds(_arguments(unit, call), 0)[0]:
            return True
    return False


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
