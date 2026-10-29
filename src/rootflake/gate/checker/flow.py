"""Timing bounds raised through a value rather than at the call (ADR 0007 Decision 7).

Three forms: a raised numeric literal in a field, constant or local that flows into a timing
argument, a loop bound or a time unit conversion; a raised literal in the header of a loop whose
body sleeps or waits; and a raised literal in an assertion that compares an elapsed time.

A value flows by name. From the declaration that changed, every read of the name is followed:
into the argument of a timing call (a sink), into the parameter of a helper declared in a read
file, into a field or local it initializes or is assigned to, and out of a method through its
`return` to every call of that method's name. A field is followed in every read file and a local
within its block. Names are matched by simple name, so a read of an unrelated member with the
same name is followed too; that fails closed.
"""

from collections import Counter, deque
from collections.abc import Iterator

from rootflake.gate.checker.assertions import _close, _declarators, _text, is_assertion_name
from rootflake.gate.checker.calls import Call, ExpansionBudget, calls_in, follow_for_scan, reach
from rootflake.gate.checker.model import Context, FileChange, Finding
from rootflake.gate.checker.poll import _CLOCK_CALLS, _CLOCK_OWNERS, initializer_spans
from rootflake.gate.checker.scope import Program
from rootflake.gate.checker.syntax import Block, CompilationUnit, Field, Lambda, Method, Statement
from rootflake.gate.checker.timing import (
    _AWAITILITY_BOUNDS,
    _DURATION_FACTORIES,
    _LOOPS,
    _TOLERANCE_FACTORIES,
    _WAIT_WORDS,
    _WAITS,
    CODE,
    _argument_bounds,
    _arguments,
    _file_constants,
    _masked,
    _member_key,
    _mode_spans,
    _site,
    _Whole,
    duration_ms,
    sleep_form,
)
from rootflake.gate.checker.tree import path_to, walk
from rootflake.gate.checker.values import CONVERSIONS, constant_lookup, fold, literal

_NAME_WORDS = (
    "timeout", "wait", "delay", "sleep", "duration", "interval", "millis", "seconds",
    "attempts", "retries", "attempt",
)  # fmt: skip
_TRIES = frozenset(["tries", "maxtries", "numtries", "ntries"])
_UNIT_WORDS = ("nanos", "micros", "millis", "seconds", "minutes", "hours")

TIMING, NAMED, CONVERSION, LOOP = "timing", "named", "conversion", "loop"
_ALL = frozenset([TIMING, NAMED, CONVERSION, LOOP])


def components(name: str) -> list[str]:
    """A name split at underscores and case changes, lower cased: `maxTries` is max, tries."""
    out, current = [], ""
    for k, ch in enumerate(name):
        if ch == "_":
            if current:
                out.append(current)
            current = ""
            continue
        boundary = (
            ch.isupper()
            and current
            and (current[-1].islower() or (k + 1 < len(name) and name[k + 1].islower()))
        )
        if boundary:
            out.append(current)
            current = ""
        current += ch
    if current:
        out.append(current)
    return [c.lower() for c in out]


def timing_name(name: str) -> bool:
    lower = name.lower()
    return any(w in lower for w in _NAME_WORDS) or any(c in _TRIES for c in components(name))


# -- sinks ----------------------------------------------------------------------------------


def _argument_spans(unit: CompilationUnit, call: Call) -> list[tuple[int, int]]:
    if call.reference or call.arity is None:
        return []
    tokens, close = unit.tokens, _close(unit, call.end)
    spans, depth, start = [], 0, call.end + 1
    for i in range(call.end + 1, close):
        text = tokens[i].text if tokens[i].kind == "op" else ""
        if text in ("(", "[", "{"):
            depth += 1
        elif text in (")", "]", "}"):
            depth -= 1
        elif text == "," and depth == 0:
            spans.append((start, i))
            start = i + 1
    if start < close:
        spans.append((start, close))
    return spans


def _open_of(unit: CompilationUnit, close: int) -> int:
    depth = 0
    for i in range(close, -1, -1):
        text = unit.tokens[i].text if unit.tokens[i].kind == "op" else ""
        if text in (")", "]", "}"):
            depth += 1
        elif text in ("(", "[", "{"):
            depth -= 1
            if depth == 0:
                return i
    return 0


def _chain_start(unit: CompilationUnit, at: int) -> int:
    """Where the receiver chain of the call named at `at` starts, as in `a.b(x).c()`."""
    tokens = unit.tokens
    while at >= 2 and tokens[at - 1].text == "." and tokens[at - 1].kind == "op":
        k = at - 2
        if tokens[k].text == ")" and tokens[k].kind == "op":
            k = _open_of(unit, k) - 1
            if k < 0 or tokens[k].kind != "ident":
                return at
        elif tokens[k].kind != "ident":
            return at
        at = k
    return at


def _unit_call(call: Call) -> bool:
    lower = call.name.lower()
    return call.name in CONVERSIONS or any(w in lower for w in _UNIT_WORDS)


def sinks(program: Program, path: str) -> list[tuple[int, int, str]]:
    """The token spans of one file a value can flow into to set a timing bound, by kind."""
    key = ("timing_sinks", path)
    if key in program.memo:
        return program.memo[key]
    unit = program.units[path]
    calls = [c for c in calls_in(unit, _Whole(unit)) if not c.reference]
    modes = _mode_spans(unit, calls)
    out: list[tuple[int, int, str]] = []
    for call in calls:
        if any(start < call.start < end for start, end in modes):
            continue
        spans = _argument_spans(unit, call)
        args = _arguments(unit, call)
        if sleep_form(program, path, call):
            out += [(s, e, TIMING) for s, e in spans]
        _, used = _argument_bounds(args, 0)
        out += [spans[k] + (TIMING,) for k in used if k < len(spans)]
        if call.receiver == "Duration" and call.name in (*_DURATION_FACTORIES, "of"):
            out += [(s, e, TIMING) for s, e in spans]
        if call.name in _AWAITILITY_BOUNDS:
            out += [(s, e, TIMING) for s, e in spans]
        lower = call.name.lower()
        if call.name not in _TOLERANCE_FACTORIES and any(w in lower for w in _WAIT_WORDS):
            out += [(s, e, NAMED) for s, e in spans]
        if _unit_call(call):
            out += [(s, e, CONVERSION) for s, e in spans]
            out.append((_chain_start(unit, call.start), call.start, CONVERSION))
    tokens = unit.tokens
    for i in range(len(tokens) - 2):
        if tokens[i].text == "new" and tokens[i + 1].text in ("Timeout", "Duration"):
            if tokens[i + 2].text == "(":
                out.append((i + 3, _close(unit, i + 2), TIMING))
    for node in walk(unit):
        for annotation in getattr(node, "annotations", ()):
            if annotation.name.rsplit(".", 1)[-1] in ("Timeout", "Test") and annotation.arguments:
                out.append((annotation.arguments.start, annotation.arguments.end, TIMING))
        if isinstance(node, Statement) and node.kind in _LOOPS and node.header is not None:
            if waits_inside(program, path, node):
                out += [(s, e, LOOP) for s, e in bound_spans(unit, node)]
                out += [(s, e, LOOP) for s, e in _checked_in_helpers(program, path, node)]
    program.memo[key] = out
    return out


def waits_inside(program: Program, path: str, loop: Statement) -> bool:
    """Whether a loop's body sleeps or makes a timed wait, directly or through a read helper."""
    key = ("waits_inside", path, loop.start)
    if key not in program.memo:
        program.memo[key] = _waits_inside(program, path, loop)
    return program.memo[key]


def _waits_inside(program: Program, path: str, loop: Statement) -> bool:
    unit = program.units[path]
    budget = ExpansionBudget()
    for body in loop.blocks:
        for call in calls_in(unit, body):
            if call.reference:
                continue
            if sleep_form(program, path, call):
                return True
            if call.name in _WAITS and _argument_bounds(_arguments(unit, call), 0)[0]:
                return True
            roots = follow_for_scan(program, path, call)
            if not roots:
                continue
            for reached in reach(program, roots, True, budget):
                if reached.method.body is None:
                    continue
                inner_unit = program.units[reached.path]
                for inner in calls_in(inner_unit, reached.method.body):
                    if not inner.reference and sleep_form(program, reached.path, inner):
                        return True
    return False


def _leaves(unit: CompilationUnit, branch: Statement) -> bool:
    """Whether a branch ends the loop or the test: a break, return or throw, or a failing or
    asserting call."""
    for node in walk(branch):
        if isinstance(node, Statement) and node.kind in ("break", "return", "throw"):
            return True
    return any(
        not c.reference and (c.name == "fail" or is_assertion_name(c.name))
        for c in calls_in(unit, branch)
    )


def bound_spans(unit: CompilationUnit, loop: Statement) -> list[tuple[int, int]]:
    """Where a loop's bound is written: its header, and the condition of every `if` in its body
    that leaves the loop, outside nested loops and lambdas."""
    spans = [(loop.header.start, loop.header.end)] if loop.header is not None else []
    for body in loop.blocks:
        for node in walk(body):
            if not isinstance(node, Statement) or node.kind != "if" or node.header is None:
                continue
            between = path_to(unit, node.start)
            inner = between[between.index(loop) + 1 : -1] if loop in between else between
            if any(isinstance(n, Lambda) or (isinstance(n, Statement) and n.kind in _LOOPS)
                   for n in inner):  # fmt: skip
                continue
            if any(_leaves(unit, block) for block in node.blocks):
                spans.append((node.header.start, node.header.end))  # either branch may leave
    return spans


def _checked_in_helpers(program: Program, path: str, loop: Statement) -> list[tuple[int, int]]:
    """The arguments of each call in a waiting loop's body to a read helper that throws, fails
    or asserts, since such a helper can hold the loop's bound, as `check(deadline)` does. Only
    the helper the loop calls is read, not the ones it calls in turn."""
    unit = program.units[path]
    out = []
    for body in loop.blocks:
        for call in calls_in(unit, body):
            if call.reference:
                continue
            for target_path, method in follow_for_scan(program, path, call):
                if method.body is None:
                    continue
                inner = program.units[target_path]
                if any(
                    isinstance(n, Statement) and n.kind == "throw" for n in walk(method.body)
                ) or any(
                    not c.reference and (c.name == "fail" or is_assertion_name(c.name))
                    for c in calls_in(inner, method.body)
                ):
                    out += _argument_spans(unit, call)
                    break
    return out


# -- following a value ----------------------------------------------------------------------


def _calls(program: Program, path: str) -> list[tuple[Call, list[tuple[int, int]]]]:
    key = ("flow_calls", path)
    if key not in program.memo:
        unit = program.units[path]
        program.memo[key] = [
            (c, _argument_spans(unit, c)) for c in calls_in(unit, _Whole(unit)) if not c.reference
        ]
    return program.memo[key]


def _block_end(nodes: list, statement: Statement) -> int:
    for k in range(len(nodes) - 1, 0, -1):
        if nodes[k] is statement and isinstance(nodes[k - 1], Block):
            return nodes[k - 1].end
    return statement.end


def _local_declared(unit: CompilationUnit, method: Method | None, name: str) -> bool:
    if method is None or method.body is None:
        return False
    if any(p.name == name for p in method.params):
        return True
    for node in walk(method.body):
        if isinstance(node, Statement) and node.kind == "local_var":
            names = [unit.tokens[k].text for k in _declarators(unit.tokens, node.start, node.end)]
            if name in names:
                return True
    return False


def _contexts(program: Program, path: str, i: int, kinds: frozenset) -> Iterator[tuple]:
    """What the read at token i reaches: True for a sink, else the names and calls it sets."""
    unit = program.units[path]
    tokens = unit.tokens
    if any(s <= i < e and kind in kinds for s, e, kind in sinks(program, path)):
        yield True
        return
    for call, spans in _calls(program, path):
        for k, (s, e) in enumerate(spans):
            if s <= i < e:
                for target, method in follow_for_scan(program, path, call):
                    if method.body is None or not method.params:
                        continue
                    param = method.params[min(k, len(method.params) - 1)]
                    yield ("name", param.name, target, method.body.start, method.body.end)
    nodes = path_to(unit, i)
    method = next((n for n in reversed(nodes) if isinstance(n, Method)), None)
    for node in reversed(nodes):
        if isinstance(node, Lambda):
            return  # a value inside a lambda leaves through the lambda, which is not followed
        if isinstance(node, Field):
            for name, span in node.initializers.items():
                if span.start <= i < span.end:
                    yield ("name", name, None, 0, 0)
            return
        if not isinstance(node, Statement):
            continue
        if node.kind == "local_var":
            for name, s, e in initializer_spans(tokens, node.start, node.end):
                if s <= i < e:
                    yield ("name", name, path, node.end, _block_end(nodes, node))
            return
        if node.kind == "return" and method is not None:
            yield ("call", method.name)
            return
        if node.kind == "expression":
            for k in range(node.start, i):
                if tokens[k].kind == "op" and tokens[k].text.endswith("=") and k > node.start:
                    if tokens[k].text in ("==", "!=", "<=", ">="):
                        continue
                    target = tokens[k - 1].text
                    if tokens[k - 1].kind != "ident":
                        break
                    if _local_declared(unit, method, target):
                        yield ("name", target, path, method.body.start, method.body.end)
                    else:
                        yield ("name", target, None, 0, 0)
                    break
            return
        if node.kind in ("if", "while", "for", "do", "switch", "synchronized"):
            return


def _reads(program: Program, item: tuple) -> Iterator[tuple[str, int]]:
    if item[0] == "call":
        for path in program.units:
            for call, _ in _calls(program, path):
                if call.name == item[1]:
                    yield path, call.start
        return
    _, name, scope, start, end = item
    for path in [scope] if scope is not None else list(program.units):
        tokens = program.units[path].tokens
        lo, hi = (start, end) if scope is not None else (0, len(tokens))
        for i in range(lo, min(hi, len(tokens) - 1)):
            if tokens[i].kind != "ident" or tokens[i].text != name:
                continue
            following = tokens[i + 1]
            if following.kind == "op" and following.text in ("(", "="):
                continue  # a call of that name, or the name being written
            yield path, i


def flows(ctx: Context, item: tuple, kinds: frozenset) -> bool:
    program = ctx.after
    seen, pending = {item}, deque([item])
    while pending:
        current = pending.popleft()
        ctx.budget.spend(0)
        for path, i in _reads(program, current):
            for found in _contexts(program, path, i, kinds):
                if found is True:
                    return True
                if found not in seen:
                    seen.add(found)
                    pending.append(found)
    return False


# -- raised declarations --------------------------------------------------------------------


def _numeric(text: str, constants: dict[str, str]) -> float | None:
    value = fold(text, constant_lookup(constants))
    if value is None:
        if text.endswith(" . toMillis ( )"):
            text = text[: -len(" . toMillis ( )")]  # a duration read back in milliseconds
        is_duration, ms = duration_ms(text)
        value = ms if is_duration else None
    return value


def _declarations(unit: CompilationUnit) -> dict[tuple, tuple[str, int, int, bool]]:
    """Each field and local initializer by where it sits: its text, line, token, whether a
    field. Several locals of one name in one member are told apart by their order."""
    out = {}
    order: Counter = Counter()
    for node in walk(unit):
        if isinstance(node, Field):
            member = _member_key(path_to(unit, node.start))
            for name, span in node.initializers.items():
                text = _text(unit, span.start, span.end)
                out[(member, name)] = (text, unit.tokens[span.start].line, span.start, True)
        elif isinstance(node, Statement) and node.kind == "local_var":
            member = _member_key(path_to(unit, node.start))
            for name, s, e in initializer_spans(unit.tokens, node.start, node.end):
                order[(member, name)] += 1
                key = (member, name, order[(member, name)])
                out[key] = (_text(unit, s, e), unit.tokens[s].line, s, False)
    return out


def _raised(old: str, new: str, old_constants: dict, new_constants: dict) -> bool:
    if old == new:
        return False
    before, after = _numeric(old, old_constants), _numeric(new, new_constants)
    if before is None or after is None:
        if _masked(old) == _masked(new):
            return _directed_raised(old, new)  # only its literals moved
        return True  # a changed value that cannot be read fails closed
    return after > before


def _raised_values(ctx: Context, change: FileChange) -> Iterator[Finding]:
    unit = change.after
    old_constants, new_constants = _file_constants(change.before), _file_constants(unit)
    old = _declarations(change.before)
    for key, (text, line, at, is_field) in _declarations(unit).items():
        if key not in old or not _raised(old[key][0], text, old_constants, new_constants):
            continue
        name = key[1]
        if timing_name(name):
            kinds = _ALL
        else:  # a raised literal whatever its name reaches the bound of a loop that waits
            kinds = frozenset([TIMING, CONVERSION, LOOP] if is_field else [TIMING, LOOP])
        if is_field:
            item = ("name", name, None, 0, 0)
        else:
            nodes = path_to(unit, at)
            statement = next(n for n in reversed(nodes) if isinstance(n, Statement))
            item = ("name", name, change.path, statement.end, _block_end(nodes, statement))
        if flows(ctx, item, kinds):
            yield Finding(CODE, change.path, line)


# -- values changed after their declaration ------------------------------------------------

_NUMERIC_TYPES = frozenset(["int", "long", "short", "byte", "double", "float", "Integer", "Long",
                            "Short", "Byte", "Double", "Float"])  # fmt: skip


def _assignments(unit: CompilationUnit) -> list[tuple[tuple, str, str, str, int, int]]:
    """Each statement that writes a plain name, `this.name` or `Type.name`: the member it sits
    in, read without parameter types, the name, the operator, the right hand text, its line and
    token."""
    out = []
    tokens = unit.tokens
    for node in walk(unit):
        if not isinstance(node, Statement) or node.kind != "expression":
            continue
        at = node.start
        if (
            node.end - at >= 6
            and tokens[at].kind in ("ident", "keyword")
            and (tokens[at + 1].text == "." and tokens[at + 2].kind == "ident")
            and (tokens[at].text == "this" or tokens[at].kind == "ident")
        ):
            at += 2  # this.name = ... and Type.name = ...
        if node.end - at < 4 or tokens[at].kind != "ident":
            continue
        op = tokens[at + 1]
        if op.kind != "op" or not op.text.endswith("=") or op.text in ("==", "!=", "<=", ">="):
            continue
        end = node.end - 1 if tokens[node.end - 1].text == ";" else node.end
        member = tuple(k[:2] for k in _member_key(path_to(unit, node.start)))
        out.append(
            (member, tokens[at].text, op.text, _text(unit, at + 2, end), op.line, node.start)
        )
    return out


def _timed(text: str, constants: dict) -> bool:
    """Whether a value is a number, a duration, or reads a clock or a time unit, the values a
    timing bound is made of; a server, a flag or a list written again is not."""
    if _numeric(text, constants) is not None or duration_ms(text)[0]:
        return True
    words = text.split()
    return any(w in _CLOCK_CALLS or w in _CLOCK_OWNERS or w in CONVERSIONS or w == "TimeUnit"
               for w in words)  # fmt: skip


def _field_types(program: Program) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for unit in program.units.values():
        for node in walk(unit):
            if isinstance(node, Field):
                for name in node.names:
                    out.setdefault(name, set()).add(node.type)
    return out


def _initializer(declarations: dict, member: tuple, name: str) -> str | None:
    for key, found in declarations.items():
        if len(key) == 3 and key[1] == name and tuple(k[:2] for k in key[0]) == member:
            return found[0] if key[2] == 1 else None
    fields = [d for k, d in declarations.items() if len(k) == 2 and k[1] == name and d[3]]
    return fields[0][0] if len(fields) == 1 else None


_GROWING = frozenset(["+=", "*=", "<<="])
_SHRINKING = frozenset(["-=", "/=", ">>=", ">>>="])


def _write_raised(op: str, old: str | None, new: str, constants: tuple[dict, dict]) -> bool:
    """Whether a write makes the value it holds larger than the one it replaces: for `=` the
    values are compared, for a compound write its step, a new step against no step at all."""
    if op == "=":
        return old is None or _raised(old, new, *constants)
    step = _numeric(new, constants[1])
    if step is None:
        return True
    if old is None:
        neutral = {"+=": 0, "-=": 0, "*=": 1, "/=": 1, "<<=": 0, ">>=": 0, ">>>=": 0}
        old_step = neutral.get(op)
    else:
        old_step = _numeric(old, constants[0])
    if old_step is None:
        return True
    if op in _GROWING:
        return step > old_step
    if op in _SHRINKING:
        return step < old_step
    return True


def _changed_after_declaration(ctx: Context, change: FileChange) -> Iterator[Finding]:
    """Writes the patch made to a value that already existed. Each changed write is paired with
    the pristine write it replaces, by member, name and operator in order; a write left over is
    compared with the declared value, or with zero for a numeric field declared without one. A
    write that only moved, with the same text, between members is not judged."""
    unit = change.after
    old_constants, new_constants = _file_constants(change.before), _file_constants(unit)
    old_writes, new_writes = _assignments(change.before), _assignments(unit)
    old_groups: dict[tuple, list[str]] = {}
    for member, name, op, text, _, _ in old_writes:
        old_groups.setdefault((member, name, op), []).append(text)
    new_groups: dict[tuple, list[tuple]] = {}
    for member, name, op, text, line, at in new_writes:
        new_groups.setdefault((member, name, op), []).append((text, line, at))
    left_old: Counter = Counter()
    pending = []
    for key, writes in new_groups.items():
        olds = list(old_groups.get(key, ()))
        fresh = []
        for write in writes:
            if write[0] in olds:
                olds.remove(write[0])
            else:
                fresh.append(write)
        for k, write in enumerate(fresh):
            pending.append((key, olds[k] if k < len(olds) else None, write))
        for text in olds[len(fresh) :]:
            left_old[(key[1], key[2], text)] += 1
    for key, olds in old_groups.items():
        if key not in new_groups:
            for text in olds:
                left_old[(key[1], key[2], text)] += 1
    old_declarations, declarations = _declarations(change.before), _declarations(unit)
    old_fields = _field_types(ctx.before)
    candidates = []
    for (member, name, op), old, (text, line, at) in pending:
        if old is None and left_old[(name, op, text)] > 0:
            left_old[(name, op, text)] -= 1
            continue  # moved to another member unchanged
        local = any(len(k) == 3 and k[1] == name and tuple(m[:2] for m in k[0]) == member
                    for k in old_declarations)  # fmt: skip
        if (
            old is None
            and not local
            and name not in old_fields
            and not _old_local(change.before, member, name)
        ):
            continue  # a value the patch declares is new code, judged where it is read
        if old is None and op == "=":
            old = _initializer(old_declarations, member, name)
            if old is None and not local and old_fields.get(name, set()) & _NUMERIC_TYPES:
                old = "0"
        if not (_timed(text, new_constants) or (old is not None and _timed(old, old_constants))):
            continue
        if _write_raised(op, old, text, (old_constants, new_constants)):
            candidates.append((name, line, at, None))
        elif op != "=":
            # In a count up or a countdown a smaller step waits longer, while on a deadline a
            # larger one does, so a compound step that reaches a loop bound fails closed.
            candidates.append((name, line, at, frozenset([LOOP])))
    for key, (text, line, at, is_field) in declarations.items():
        if is_field or key[2] != 1 or key in old_declarations:
            continue
        shadowed = _initializer({k: d for k, d in declarations.items() if len(k) == 2}, (), key[1])
        if (
            shadowed is not None
            and _timed(shadowed, new_constants)
            and _raised(shadowed, text, new_constants, new_constants)
        ):
            candidates.append((key[1], line, at, None))
    for name, line, at, only in candidates:
        kinds = _ALL if timing_name(name) else frozenset([TIMING, LOOP])
        kinds = kinds if only is None else kinds & only
        nodes = path_to(unit, at)
        method = next((n for n in reversed(nodes) if isinstance(n, Method)), None)
        if method is not None and method.body is not None and _local_declared(unit, method, name):
            item = ("name", name, change.path, method.body.start, method.body.end)
        else:
            item = ("name", name, None, 0, 0)
        if flows(ctx, item, kinds):
            yield Finding(CODE, change.path, line)


def _old_local(unit: CompilationUnit, member: tuple, name: str) -> bool:
    """Whether the pristine member declares a local or parameter of that name, initialized or
    not."""
    for node in walk(unit):
        if (
            isinstance(node, Method)
            and tuple(k[:2] for k in _member_key(path_to(unit, node.start)))
            == member[: len(member)]
            and _local_declared(unit, node, name)
        ):
            return True
    return False


# -- raised literals in a loop header or an elapsed time assertion --------------------------


def _literals(text: str) -> list[float]:
    return [v for v in (literal(w) for w in text.split()) if v is not None]


_SEPARATORS = frozenset(["&&", "||", ";", ",", "?", ":"])
_SIDES = {"<": 1, "<=": 1, ">": -1, ">=": -1, "==": 0, "!=": 0}


def _directed(text: str) -> list[tuple[float, int]]:
    """Each literal with the way it loosens a bound when it grows: 1 when a larger value waits
    longer, as in `i < 10`, -1 when a smaller one does, as a floor in `remaining > 4000` or a
    divisor in `TIMEOUT / 10` do, and 0 when either change can, as in `==` or inside a call's
    arguments, so any change there counts."""
    words = text.split()
    opens: list[int] = []
    group = [-1] * len(words)  # the open parenthesis each word sits in
    for k, w in enumerate(words):
        group[k] = opens[-1] if opens else -1
        if w == "(":
            opens.append(k)
        elif w == ")" and opens:
            opens.pop()
            group[k] = opens[-1] if opens else -1

    depth = [0] * len(words)
    level = 0
    for k, w in enumerate(words):
        if w == ")":
            level -= 1
        depth[k] = level
        if w == "(":
            level += 1

    def segment(k: int) -> tuple[int, int]:
        start, end = k, k
        while (
            start > 0
            and depth[start - 1] >= depth[k]
            and not (depth[start - 1] == depth[k] and words[start - 1] in _SEPARATORS)
        ):
            start -= 1
        while (
            end + 1 < len(words)
            and depth[end + 1] >= depth[k]
            and not (depth[end + 1] == depth[k] and words[end + 1] in _SEPARATORS)
        ):
            end += 1
        return start, end

    def flipped(k: int) -> int:
        before = words[k - 1] if k else ""
        if before == "/":
            return -1
        if before == "-":
            return -1  # a subtrahend, or a negated value
        return 1

    def side(k: int) -> int:
        """The direction for the item at k, a literal or an open parenthesis, in its group."""
        start, end = segment(k)
        for j in range(start, end + 1):
            if depth[j] == depth[k] and words[j] in _SIDES and j != k:
                way = _SIDES[words[j]]
                return way if k > j else -way
        outer = group[k]
        if outer < 0:
            return 1
        if outer >= 1 and (words[outer - 1][:1].isalnum() or words[outer - 1][:1] in "_$>"):
            return 0  # the arguments of a call
        return side(outer) * flipped(outer)

    out = []
    for k, w in enumerate(words):
        value = literal(w)
        if value is not None:
            out.append((value, side(k) * flipped(k)))
    return out


def _directed_raised(old: str, new: str) -> bool:
    before, after = _directed(old), _directed(new)
    if len(before) != len(after):
        return False
    for (b, _), (a, way) in zip(before, after, strict=True):
        if a != b and (way == 0 or (a - b) * way > 0):
            return True
    return False


def _any_raised(old: list[list[float]], new: list[list[float]]) -> bool:
    for before, after in zip(old, new, strict=False):
        if len(before) == len(after) and any(b < a for b, a in zip(before, after, strict=True)):
            return True
    return False


def _loops(program: Program, path: str, unit: CompilationUnit) -> dict[tuple, list]:
    """Each loop that waits, by member in source order: its bound text and line."""
    out: dict[tuple, list] = {}
    for node in walk(unit):
        if not isinstance(node, Statement) or node.kind not in _LOOPS or node.header is None:
            continue
        if not waits_inside(program, path, node):
            continue
        texts = tuple(_text(unit, s, e) for s, e in bound_spans(unit, node))
        out.setdefault(_member_key(path_to(unit, node.start)), []).append((texts, node.line))
    return out


def _names(text: str) -> set[str]:
    """The names a bound reads, less the calls it makes."""
    words = text.split()
    return {
        w
        for k, w in enumerate(words)
        if (w[:1].isalpha() or w[:1] in "_$")
        and literal(w) is None
        and (k + 1 == len(words) or words[k + 1] != "(")
    }


def _loop_widened(old_spans: tuple, new_spans: tuple) -> bool:
    """Whether a waiting loop's bound got looser. Under the same shape its literals are compared
    in place by the way each loosens the bound. A changed shape passes when it only adds
    conditions, which can only end the loop sooner: conjuncts at the top level of its header
    condition, or whole new `if` statements that leave it, each free of writes and waits, with
    every old one kept as it was. Any other change passes only when its literals are the same
    and it reads no name the old bound did not, so a bound moved into a new name fails closed."""
    old, new = " ; ".join(old_spans), " ; ".join(new_spans)
    if _masked(old) == _masked(new):
        return _directed_raised(old, new)
    if _only_conditions_added(old_spans, new_spans):
        return False
    return _literals(old) != _literals(new) or bool(_names(new) - _names(old))


_WRITES = frozenset(["++", "--"])


def _top_split(text: str, separator: str) -> list[str]:
    words = text.split()
    parts, current, depth = [], [], 0
    for w in words:
        if w in ("(", "[", "{"):
            depth += 1
        elif w in (")", "]", "}"):
            depth -= 1
        if depth == 0 and w == separator:
            parts.append(" ".join(current))
            current = []
        else:
            current.append(w)
    parts.append(" ".join(current))
    return parts


def _unwrapped(text: str) -> str:
    words = text.split()
    if len(words) >= 2 and words[0] == "(" and words[-1] == ")":
        return " ".join(words[1:-1])
    return text


def _pure(text: str) -> bool:
    for w in text.split():
        if w in _WRITES or (w.endswith("=") and w not in ("==", "!=", "<=", ">=")):
            return False
        lower = w.lower()
        if "sleep" in lower or any(word in lower for word in _WAIT_WORDS):
            return False
    return True


def _only_conditions_added(old_spans: tuple, new_spans: tuple) -> bool:
    if not old_spans or not new_spans:
        return False
    old_head, new_head = _top_split(_unwrapped(old_spans[0]), ";"), _top_split(
        _unwrapped(new_spans[0]), ";")  # fmt: skip
    if len(old_head) != len(new_head):
        return False
    middle = 1 if len(old_head) == 3 else 0
    if len(old_head) == 3 and (old_head[0], old_head[2]) != (new_head[0], new_head[2]):
        return False
    pairs = [(_top_split(old_head[middle], "&&"), _top_split(new_head[middle], "&&")),
             (list(old_spans[1:]), list(new_spans[1:]))]  # fmt: skip
    for olds, news in pairs:
        left = list(news)
        for item in olds:
            if item not in left:
                return False
            left.remove(item)
        if not all(_pure(item) for item in left):
            return False
    return True


def _widened_loops(old: dict, new: dict, path: str) -> Iterator[Finding]:
    for member, items in new.items():
        olds = [text for text, _ in old.get(member, [])]
        pending = list(range(len(items)))
        pairs = []
        for k in list(pending):  # loops whose bound text kept its shape pair first, in order
            match = next((j for j, t in enumerate(olds) if t is not None
                          and _masked(" ; ".join(t)) == _masked(" ; ".join(items[k][0]))),
                         None)  # fmt: skip
            if match is not None:
                pairs.append((olds[match], k))
                olds[match] = None
                pending.remove(k)
        rest = [t for t in olds if t is not None]
        pairs += list(zip(rest, pending, strict=False))
        for text, k in pairs:
            if _loop_widened(text, items[k][0]):
                yield Finding(CODE, path, items[k][1])


def _clock_read(unit: CompilationUnit, start: int, end: int) -> bool:
    tokens = unit.tokens
    for i in range(start, end):
        text = tokens[i].text
        if tokens[i].kind != "ident":
            continue
        if "elapsed" in text.lower() or "stopwatch" in text.lower():
            return True
        receiver = tokens[i - 2].text if i >= 2 and tokens[i - 1].text == "." else None
        if text in _CLOCK_CALLS and receiver == "System":
            return True
        if text == "now" and receiver in _CLOCK_OWNERS:
            return True
        if text == "between" and receiver == "Duration":
            return True
    return False


def _elapsed(unit: CompilationUnit, call: Call) -> bool:
    close = _close(unit, call.end)
    if _clock_read(unit, call.end, close):
        return True
    method = next((n for n in reversed(path_to(unit, call.start)) if isinstance(n, Method)), None)
    if method is None or method.body is None:
        return False
    names = {t.text for t in unit.tokens[call.end : close] if t.kind == "ident"}
    for node in walk(method.body):
        if isinstance(node, Statement) and node.kind == "local_var":
            for name, s, e in initializer_spans(unit.tokens, node.start, node.end):
                if name in names and _clock_read(unit, s, e):
                    return True
    return False


def _elapsed_assertions(unit: CompilationUnit) -> dict[tuple, list]:
    out: dict[tuple, list] = {}
    for call in calls_in(unit, _Whole(unit)):
        if call.reference or not is_assertion_name(call.name) or not _elapsed(unit, call):
            continue
        site = _site(unit, call.start)
        key = (site.member, _masked(site.statement))
        out.setdefault(key, []).append((_literals(site.statement), unit.tokens[call.start].line))
    return out


def _raised_literals(old: dict, new: dict, path: str) -> Iterator[Finding]:
    for key, items in new.items():
        before = [values for values, _ in old.get(key, [])]
        for (values, line), previous in zip(items, before, strict=False):
            if _any_raised([previous], [values]):
                yield Finding(CODE, path, line)


def _file(ctx: Context, change: FileChange) -> Iterator[Finding]:
    yield from _raised_values(ctx, change)
    yield from _changed_after_declaration(ctx, change)
    yield from _widened_loops(
        _loops(ctx.before, change.path, change.before),
        _loops(ctx.after, change.path, change.after),
        change.path,
    )
    yield from _raised_literals(
        _elapsed_assertions(change.before), _elapsed_assertions(change.after), change.path
    )


def findings(ctx: Context) -> Iterator[Finding]:
    seen = set()
    for change in ctx.changes:
        if change.path not in ctx.test_paths or change.before is None:
            continue
        for finding in _file(ctx, change):
            if (finding.path, finding.line) not in seen:
                seen.add((finding.path, finding.line))
                yield finding
