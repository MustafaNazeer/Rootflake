"""Tests must survive and still run (ADR 0007 Decision 6).

Only what the patch adds or changes relative to the pristine file is judged. Annotations are
matched by simple name, which is the stricter reading: a project's own `@Disabled` is treated
like Jupiter's.
"""

from collections import Counter
from collections.abc import Iterator

from rootflake.gate.checker.calls import calls_in, follow_for_scan, reach
from rootflake.gate.checker.model import Context, FileChange, Finding
from rootflake.gate.checker.scope import Program
from rootflake.gate.checker.syntax import (
    Annotation,
    Block,
    Catch,
    CompilationUnit,
    Lambda,
    Method,
    Statement,
    TypeDecl,
)
from rootflake.gate.checker.tree import children, path_to, walk

TEST_ANNOTATIONS = frozenset(
    ["Test", "ParameterizedTest", "RepeatedTest", "TestFactory", "TestTemplate"]
)
RETRY_ANNOTATIONS = frozenset(
    ["RetryingTest", "RepeatedIfExceptionsTest", "ParameterizedRepeatedIfExceptionsTest"]
)
_DISABLING = frozenset(["Ignore", "Disabled", "AwaitsFix", "Nightly", "BadApple"])
_ORDER = frozenset(["FixMethodOrder", "TestMethodOrder", "TestClassOrder", "Order"])
_PARALLEL = frozenset(["Isolated", "ResourceLock", "ResourceLocks"])
_TAGS = frozenset(["Tag", "Tags", "Category"])
_ASSUMPTION_OWNERS = frozenset(["Assume", "Assumptions", "org.junit.Assume"])
SKIP_EXCEPTIONS = frozenset(
    ["SkipException", "TestAbortedException", "AssumptionViolatedException"]
)


def simple(annotation: Annotation) -> str:
    return annotation.name.rsplit(".", 1)[-1]


def text(unit: CompilationUnit, node) -> str:
    return " ".join(t.text for t in unit.tokens[node.start : node.end])


def attributes(unit: CompilationUnit, annotation: Annotation) -> dict[str, str]:
    if annotation.arguments is None:
        return {}
    tokens = unit.tokens[annotation.arguments.start : annotation.arguments.end]
    pieces, depth, current = [], 0, []
    for token in tokens:
        if token.kind == "op" and token.text in "([{":
            depth += 1
        elif token.kind == "op" and token.text in ")]}":
            depth -= 1
        if token.text == "," and token.kind == "op" and depth == 0:
            pieces.append(current)
            current = []
        else:
            current.append(token)
    pieces.append(current)
    out = {}
    for piece in pieces:
        if len(piece) >= 2 and piece[0].kind == "ident" and piece[1].text == "=":
            out[piece[0].text] = " ".join(t.text for t in piece[2:])
        elif piece:
            out["value"] = " ".join(t.text for t in piece)
    return out


def annotation_names(annotations: list[Annotation], program: Program | None) -> set[str]:
    """Simple names of the annotations, plus those a composed annotation declared in a read
    file carries, followed through further composed annotations."""
    names = {simple(a) for a in annotations}
    if program is None:
        return names
    declared = {
        t.name: t
        for path in program.units
        for t in program.all_types(path)
        if t.kind == "annotation"
    }
    pending, seen = list(names), set()
    while pending:
        name = pending.pop()
        if name in seen or name not in declared:
            continue
        seen.add(name)
        # A type the files read declare stands only for what it carries, so a local
        # `@interface Test {}` does not keep a method a test.
        names.discard(name)
        for carried in declared[name].annotations:
            names.add(simple(carried))
            pending.append(simple(carried))
    return names


def is_test_method(method: Method, owner: TypeDecl, program: Program | None = None) -> bool:
    if annotation_names(method.annotations, program) & TEST_ANNOTATIONS:
        return True
    return (
        method.name.startswith("test")
        and "public" in method.modifiers
        and method.return_type == "void"
        and not method.params
        and bool(owner.extends)
    )


def _types(program: Program, path: str) -> dict[str, TypeDecl]:
    return {program.fqn[id(t)]: t for t in program.all_types(path) if t.kind != "anonymous"}


def _methods(program: Program, path: str) -> dict[tuple, tuple[Method, TypeDecl]]:
    out = {}
    for fqn, decl in _types(program, path).items():
        for method in decl.methods:
            key = (fqn, method.name, tuple(p.type for p in method.params))
            out.setdefault(key, (method, decl))
    return out


def _outside_nested(body) -> Iterator:
    pending = list(reversed(body.statements)) if body else []
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, Lambda | TypeDecl):
            continue
        pending.extend(
            reversed([c for c in children(node) if not isinstance(c, Lambda | TypeDecl)])
        )


def runs(program: Program, path: str, body: Block | None) -> dict[tuple, list[Statement]]:
    """The simple statements of a body, by where each runs: its text, the statements around it
    in the body (a statement's kind, its header text and which of its parts holds it), and any
    lambda or class it sits in. So a statement moved into a `try`, rewritten, or copied under
    `if (false)` or into a lambda no longer counts as the one that ran before. Computed once
    per body."""
    if body is None:
        return {}
    memo = ("survive/runs", path, body.start)
    if memo not in program.memo:
        unit = program.units[path]
        out: dict[tuple, list[Statement]] = {}
        for node in walk(body):
            if isinstance(node, Statement) and not node.blocks and node.kind != "local_type":
                key = (text(unit, node), _run_context(unit, body, node))
                out.setdefault(key, []).append(node)
        program.memo[memo] = out
    return program.memo[memo]


def lost_calls(ctx: Context, path: str, old: Block | None, new: Block | None) -> set[str]:
    """The names of the calls made by the statements the old body ran that the new body no
    longer runs in the same place, with the calls of every method those calls reach in the
    files the checker reads, under the expansion budget: what a rewritten, wrapped or inlined
    statement may still call."""
    before = ctx.before
    memo = ("survive/lost_calls", path, None if old is None else old.start,
            None if new is None else new.start)  # fmt: skip
    if memo in before.memo:
        return before.memo[memo]
    was, now = runs(before, path, old), runs(ctx.after, path, new)
    unit = before.units[path]
    calls = [
        call
        for key, nodes in was.items()
        if len(now.get(key, ())) < len(nodes)
        for node in nodes
        for call in calls_in(unit, node)
    ]
    names = {c.name for c in calls}
    roots = list(
        {id(m): (p, m) for c in calls for p, m in follow_for_scan(before, path, c)}.values()
    )
    for reached in reach(before, roots, True, ctx.budget):
        if reached.method.body is not None:
            inner = before.units[reached.path]
            names |= {c.name for c in calls_in(inner, reached.method.body)}
    before.memo[memo] = names
    return names


def _run_context(unit: CompilationUnit, body: Block, node: Statement) -> tuple:
    nodes = path_to(unit, node.start)
    inside = nodes[next((k for k, n in enumerate(nodes) if n is body), 0) + 1 :]
    out = []
    for k, n in enumerate(inside[:-1]):
        nxt = inside[k + 1]
        if isinstance(n, Lambda):
            out.append(("lambda",))
        elif isinstance(n, TypeDecl):
            out.append(("class", n.name))
        elif isinstance(n, Statement) and n is not node:
            out.append((n.kind, _header(unit, n), _part(n, nxt)))
    return tuple(out)


def _header(unit: CompilationUnit, n: Statement) -> str:
    if n.header is None:
        return ""
    return " ".join(t.text for t in unit.tokens[n.header.start : n.header.end])


def _part(n: Statement, inner) -> str:
    at = inner.start
    for i, b in enumerate(n.blocks):
        if b.start <= at < b.end:
            return f"block{i}"
    for i, c in enumerate(n.catches):
        if c.start <= at < c.end:
            return f"catch{i}"
    if n.finally_block is not None and n.finally_block.start <= at < n.finally_block.end:
        return "finally"
    return "other"


def _returns(method: Method) -> int:
    """Returns outside nested lambdas and classes. A void `return;` always counts; a value
    return does not in a non void @TestFactory, which must return its tests."""
    factory = any(simple(a) == "TestFactory" for a in method.annotations)
    value_ok = factory and method.return_type not in (None, "void")
    return sum(
        1
        for n in _outside_nested(method.body)
        if isinstance(n, Statement)
        and n.kind == "return"
        and (n.end - n.start == 2 or not value_ok)
    )


def _loop_exits(unit: CompilationUnit, method: Method, old_loops: tuple | None = None) -> int:
    """`continue` statements, and `break` statements whose nearest breakable is a loop, outside
    nested lambdas and classes; each one can skip the cases after it. With old_loops, the loops
    of the before method, an exit that is the last statement of a loop the patch added skips no
    case: the passes after it never ran before, and nothing of the loop body follows it. Nor
    does an exit of an added loop that holds no statement of the before method: all it can
    skip is code the patch wrote."""
    count = 0
    for node in _outside_nested(method.body):
        if not isinstance(node, Statement) or node.kind not in ("continue", "break"):
            continue
        if node.end - node.start > 2:
            count += 1  # every labelled break or continue can skip cases
            continue
        around = [n for n in path_to(unit, node.start) if isinstance(n, Statement | Block)]
        kinds = (
            ("for", "while", "do") if node.kind == "continue" else ("for", "while", "do", "switch")
        )
        at = next(
            (k for k in range(len(around) - 1, -1, -1)
             if isinstance(around[k], Statement) and around[k].kind in kinds),
            None,
        )  # fmt: skip
        if at is None or around[at].kind == "switch":
            continue
        if old_loops is not None and (
            _added_and_trailing(unit, around[at:], node, old_loops)
            or _added_and_new(unit, around[at], node, old_loops)
        ):
            continue
        count += 1
    return count


def loop_key(unit: CompilationUnit, loop: Statement) -> tuple[str, str]:
    header = loop.header
    return loop.kind, "" if header is None else " ".join(
        t.text for t in unit.tokens[header.start : header.end])  # fmt: skip


def loops_of(unit: CompilationUnit, method: Method) -> tuple[Counter, set[str], set[str]]:
    """The loops of a method by kind and header text, the text of every statement that sits
    inside one of them, and the text of every simple statement of the method."""
    keys: Counter = Counter()
    inside: set[str] = set()
    everywhere: set[str] = set()
    for n in _outside_nested(method.body):
        if isinstance(n, Statement) and n.kind in ("for", "while", "do"):
            keys[loop_key(unit, n)] += 1
            inside |= _statement_texts(unit, n)
        if isinstance(n, Statement) and not n.blocks:
            everywhere.add(text(unit, n))
    return keys, inside, everywhere


def _statement_texts(unit: CompilationUnit, loop: Statement) -> set[str]:
    return {
        text(unit, n)
        for block in loop.blocks
        for n in walk(block)
        if isinstance(n, Statement) and not n.blocks
    }


def _added_and_trailing(unit: CompilationUnit, around: list, node: Statement, old: tuple) -> bool:
    """Whether the loop an exit leaves is one the patch added, and on the way down to the exit
    every block's statement on that way is the block's last. A loop is added when no loop of
    its kind and header text was in the before method and it holds no statement that sat
    inside a loop there, so editing a loop's header does not make it new."""
    keys, inside = old[:2]
    loop = around[0]
    if keys[loop_key(unit, loop)] or (_statement_texts(unit, loop) - {text(unit, node)}) & inside:
        return False
    chain = [*around[1:], node]
    for k, block in enumerate(chain[:-1]):
        if isinstance(block, Block) and (
            not block.statements or block.statements[-1] is not chain[k + 1]
        ):
            return False
    return True


def _old_loops(ctx: Context, path: str, method: Method, new: Method):
    """The before method's loops for `_loop_exits`, with the calls a lost statement may still
    make (`lost_calls`), so a rewritten or inlined statement cannot make an added loop read as
    holding only new code."""
    keys, inside, everywhere = loops_of(ctx.before.units[path], method)
    gone = lost_calls(ctx, path, method.body, new.body)
    return keys, inside, everywhere, gone


def _added_and_new(unit: CompilationUnit, loop: Statement, node: Statement, old: tuple) -> bool:
    keys, _, everywhere, gone = old
    return (
        not gone & {c.name for c in calls_in(unit, loop)}
        and not keys[loop_key(unit, loop)]
        and not ((_statement_texts(unit, loop) - {text(unit, node)}) & everywhere)
    )


def _exit_positions(unit: CompilationUnit, method: Method) -> list[tuple[str, int]]:
    """Each return with the number of assertion like calls before it, so a return moved ahead
    of an assertion shows up even when the number of returns is unchanged."""
    if method.body is None:
        return []
    calls = [
        c.start
        for c in calls_in(unit, method.body)
        if c.name.startswith(("assert", "fail", "verify"))
    ]
    out = []
    for node in _outside_nested(method.body):
        if isinstance(node, Statement) and node.kind == "return":
            out.append((text(unit, node), sum(1 for at in calls if at < node.start)))
    return sorted(out)


def _around_returns(unit: CompilationUnit, method: Method) -> list[tuple[str, set, set]]:
    """Each return outside nested code, in order, with the texts of the statements that end
    before it and of those that start after it, leaving out the ones that hold it."""
    if method.body is None:
        return []
    statements = [n for n in _outside_nested(method.body) if isinstance(n, Statement)]
    out = []
    for node in statements:
        if node.kind != "return":
            continue
        before = {text(unit, s) for s in statements if s.end <= node.start}
        after = {text(unit, s) for s in statements if s.start >= node.end}
        out.append((text(unit, node), before, after))
    return out


def _return_moved_earlier(old_unit, old: Method, new_unit, new: Method) -> bool:
    """Whether an existing return now comes ahead of a statement that ran before it."""
    olds, news = _around_returns(old_unit, old), _around_returns(new_unit, new)
    if [t for t, _, _ in olds] != [t for t, _, _ in news]:
        return False
    return any((o[1] & n[2]) - o[2] for o, n in zip(olds, news, strict=True))


def _undiscoverable(old: Method, new: Method) -> bool:
    """Jupiter skips a private or static test method, which only warns."""
    became = {"private", "static"} & set(new.modifiers)
    return bool(became - set(old.modifiers))


def _is_empty(method: Method) -> bool:
    return method.body is not None and all(s.kind == "empty" for s in method.body.statements)


def _counterpart(key: tuple, before: dict, after: dict):
    """The after method for a before key: the same signature, or else the same name when that
    name is unique in its class on both sides, so a changed parameter type keeps the test."""
    if key in after:
        return after[key]

    def same_name(side):
        return [k for k in side if k[0] == key[0] and k[1] == key[1]]

    if len(same_name(before)) == 1 and len(same_name(after)) == 1:
        return after[same_name(after)[0]]
    return None


def _removals(change: FileChange, ctx: Context) -> Iterator[Finding]:
    before = _methods(ctx.before, change.path)
    after = _methods(ctx.after, change.path)
    matched = {id(found[0]) for k in before if (found := _counterpart(k, before, after))}
    target = ctx.target
    for method, owner in after.values():
        is_new = id(method) not in matched
        if is_new and is_test_method(method, owner, ctx.after) and _returns(method):
            yield Finding("disable/early_return", change.path, method.line)
    for key, (method, owner) in before.items():
        if not is_test_method(method, owner, ctx.before):
            continue
        found = _counterpart(key, before, after)
        if found is None:
            yield Finding("disable/test_removed", change.path, method.line)
            continue
        new, new_owner = found
        retried = bool(annotation_names(new.annotations, ctx.after) & RETRY_ANNOTATIONS)
        if not is_test_method(new, new_owner, ctx.after):
            if not retried:
                yield Finding("disable/test_removed", change.path, new.line)
            continue
        if method.return_type == "void" and new.return_type != "void":
            # Jupiter does not run a non void @Test, and becoming a factory changes what runs.
            yield Finding("disable/test_removed", change.path, new.line)
        is_target = target is not None and key[0] == target.dotted and key[1] == target.method
        old_names = annotation_names(method.annotations, ctx.before) & TEST_ANNOTATIONS
        new_names = annotation_names(new.annotations, ctx.after) & TEST_ANNOTATIONS
        if is_target and old_names != new_names and not retried:
            yield Finding("disable/test_removed", change.path, new.line)
        if method.body and method.body.statements and _is_empty(new):
            yield Finding("disable/test_body_emptied", change.path, new.line)
        if _returns(new) > _returns(method):
            yield Finding("disable/early_return", change.path, new.line)
        elif _loop_exits(
            change.after, new, _old_loops(ctx, change.path, method, new)
        ) > _loop_exits(change.before, method):
            yield Finding("disable/early_return", change.path, new.line)
        else:
            old_exits = _exit_positions(change.before, method)
            new_exits = _exit_positions(change.after, new)
            if [t for t, _ in old_exits] == [t for t, _ in new_exits] and any(
                n < o for (_, o), (_, n) in zip(old_exits, new_exits, strict=True)
            ):
                yield Finding("disable/early_return", change.path, new.line)
            elif _return_moved_earlier(change.before, method, change.after, new):
                yield Finding("disable/early_return", change.path, new.line)
        if _undiscoverable(method, new) and (
            annotation_names(new.annotations, ctx.after) & TEST_ANNOTATIONS
        ):
            yield Finding("disable/test_removed", change.path, new.line)


def _simple_names(names: list[str]) -> set[str]:
    return {name.rsplit(".", 1)[-1] for name in names}


def _condition_fields(decl: TypeDecl) -> set[str]:
    return {
        name
        for field in decl.fields
        if field.type.split("<", 1)[0].rsplit(".", 1)[-1] == "ExecutionCondition"
        for name in field.names
    }


def _supertype_tests(program: Program, path: str, decl: TypeDecl) -> tuple[bool, bool, bool]:
    """Whether a read supertype declares test methods, whether the extended class is unread,
    and whether an implemented interface is unread."""
    read = program.supertypes(path, decl)
    with_tests = any(is_test_method(m, d, program) for _, d in read for m in d.methods)
    read_names = {d.name for _, d in read}
    unread_base = bool(_simple_names(decl.extends) - read_names)
    unread_interface = bool(_simple_names(decl.implements) - read_names)
    return with_tests, unread_base, unread_interface


def _class_shapes(change: FileChange, ctx: Context) -> Iterator[Finding]:
    before = _types(ctx.before, change.path) if change.before else {}
    after = _types(ctx.after, change.path)
    for fqn, decl in after.items():
        old = before.get(fqn)
        old_supers = _simple_names([*old.extends, *old.implements]) if old else set()
        if "ExecutionCondition" in _simple_names([*decl.extends, *decl.implements]) and (
            "ExecutionCondition" not in old_supers
        ):
            yield Finding("disable/execution_condition", change.path, decl.line)
        if _condition_fields(decl) - (_condition_fields(old) if old else set()):
            yield Finding("disable/execution_condition", change.path, decl.line)
        if old is None or (old.extends, old.implements) == (decl.extends, decl.implements):
            continue
        has_tests = any(
            is_test_method(m, d, program)
            for d, program in ((old, ctx.before), (decl, ctx.after))
            for m in d.methods
        )
        old_tests, old_base, old_interface = _supertype_tests(ctx.before, change.path, old)
        new_tests, new_base, new_interface = _supertype_tests(ctx.after, change.path, decl)
        base_changed = old.extends != decl.extends
        if has_tests or old_tests or new_tests or (base_changed and (old_base or new_base)):
            yield Finding("weaken_assertion/assertion_rebound", change.path, decl.line)
        elif old_base or new_base or old_interface or new_interface:
            yield Finding("flag/unknown_test_extension", change.path, decl.line)


def _declarations(program: Program, path: str) -> dict[tuple, object]:
    out: dict[tuple, object] = {("type", fqn): decl for fqn, decl in _types(program, path).items()}
    for key, (method, _) in _methods(program, path).items():
        out[("method", *key)] = method
    return out


_FALSE = frozenset(["false", "Boolean . FALSE", "java . lang . Boolean . FALSE"])


def _number(value: str | None) -> float:
    """An attribute's integer value; an absent one is 1, and one that is not a literal counts
    as 0, so a change to it reads as fewer cases."""
    if value is None:
        return 1
    literal = value.replace("_", "").rstrip("lL")
    for prefix, base in (("0x", 16), ("0X", 16), ("0b", 2), ("0B", 2)):
        if literal.startswith(prefix):
            try:
                return int(literal[2:], base)
            except ValueError:
                return 0
    if literal.isdigit():
        return int(literal, 8) if len(literal) > 1 and literal.startswith("0") else int(literal)
    return 0


_DISABLING_ATTRIBUTES = (
    ("ParameterizedTest", "allowZeroInvocations", "true"),
    ("Testcontainers", "disabledWithoutDocker", "true"),
)


def _test_attribute_findings(old: dict, new: dict, path: str, line: int) -> Iterator[Finding]:
    # Any enabled value other than true, a constant included, may turn the test off.
    if new.get("enabled", "true") != "true" and new.get("enabled") != old.get("enabled"):
        yield Finding("disable/test_disabled", path, line)
    if _number(new.get("invocationCount")) < _number(old.get("invocationCount")):
        yield Finding("disable/parameter_cases_dropped", path, line)
    for key in ("priority", "dependsOnMethods", "dependsOnGroups"):
        if new.get(key) != old.get(key) and key in new:
            yield Finding("disable/order_pinned", path, line)
    if new.get("groups") != old.get("groups"):
        yield Finding("disable/tag_changed", path, line)
    if new.get("singleThreaded") == "true" and old.get("singleThreaded") != "true":
        yield Finding("disable/parallelism_disabled", path, line)


def _classify(
    annotation: Annotation, unit: CompilationUnit, path: str, program: Program
) -> Iterator[Finding]:
    name = simple(annotation)
    line = annotation.line
    if name == "Order":
        binding = program.resolve_type(path, annotation.name, annotation.start)
        if (
            binding.kind in ("imported", "declared")
            and binding.fqn != "org.junit.jupiter.api.Order"
        ):
            return
    if name in _DISABLING or name.startswith(("Disabled", "Enabled", "DisableIf")):
        yield Finding("disable/test_disabled", path, line)
    elif name in _ORDER:
        yield Finding("disable/order_pinned", path, line)
    elif name in _PARALLEL or (name == "Execution" and "SAME_THREAD" in text(unit, annotation)):
        yield Finding("disable/parallelism_disabled", path, line)
    elif name in _TAGS:
        yield Finding("disable/tag_changed", path, line)


def _attribute_switches(change: FileChange, old_anns, new_anns) -> Iterator[Finding]:
    for name, key, value in _DISABLING_ATTRIBUTES:
        new = next((a for a in new_anns if simple(a) == name), None)
        if new is None or attributes(change.after, new).get(key) != value:
            continue
        old = next((a for a in old_anns if simple(a) == name), None)
        if old is None or attributes(change.before, old).get(key) != value:
            yield Finding("disable/test_disabled", change.path, new.line)
    new_repeat = next((a for a in new_anns if simple(a) == "RepeatedTest"), None)
    old_repeat = next((a for a in old_anns if simple(a) == "RepeatedTest"), None)
    if new_repeat is not None and old_repeat is not None:
        old_count = _number(attributes(change.before, old_repeat).get("value"))
        new_count = _number(attributes(change.after, new_repeat).get("value"))
        if new_count < old_count:
            yield Finding("disable/parameter_cases_dropped", change.path, new_repeat.line)


def _hidden_classes(change: FileChange, ctx: Context) -> Iterator[Finding]:
    """Tests a class change stops Jupiter or JUnit 4 from running: a class made abstract, an
    inner class losing @Nested, an outer class losing @RunWith(Enclosed.class)."""
    if change.before is None:
        return
    before = _types(ctx.before, change.path)
    for fqn, decl in _types(ctx.after, change.path).items():
        old = before.get(fqn)
        if old is None:
            continue
        nested = [n for n in walk(old) if isinstance(n, TypeDecl)]
        has_tests = any(is_test_method(m, d, ctx.before) for d in nested for m in d.methods)
        if not has_tests:
            continue
        if "abstract" in decl.modifiers and "abstract" not in old.modifiers:
            yield Finding("disable/test_removed", change.path, decl.line)
        old_names = {simple(a) for a in old.annotations}
        new_names = {simple(a) for a in decl.annotations}
        if "Nested" in old_names and "Nested" not in new_names:
            yield Finding("disable/test_removed", change.path, decl.line)
        old_enclosed = any(
            simple(a) == "RunWith" and "Enclosed" in text(change.before, a) for a in old.annotations
        )
        new_enclosed = any(
            simple(a) == "RunWith" and "Enclosed" in text(change.after, a) for a in decl.annotations
        )
        if old_enclosed and not new_enclosed:
            yield Finding("disable/test_removed", change.path, decl.line)


_ENGINES = {
    "org.junit.Test": "junit4",
    "org.junit.jupiter.api.Test": "jupiter",
    "org.testng.annotations.Test": "testng",
}
# Members only one engine runs: JUnit 4 rules and lifecycle methods, Jupiter lifecycle methods,
# extensions and timeouts, TestNG configuration methods.
_MEMBER_ENGINES = {
    **{f"org.junit.{n}": "junit4"
       for n in ("Rule", "ClassRule", "Before", "After", "BeforeClass", "AfterClass")},
    **{f"org.junit.jupiter.api.{n}": "jupiter"
       for n in ("BeforeEach", "AfterEach", "BeforeAll", "AfterAll", "Timeout")},
    "org.junit.jupiter.api.extension.RegisterExtension": "jupiter",
    "org.junit.jupiter.api.extension.ExtendWith": "jupiter",
    **{f"org.testng.annotations.{w}{n}": "testng"
       for w in ("Before", "After") for n in ("Method", "Class", "Test", "Suite", "Groups")},
}  # fmt: skip
_RULE_SUPPORT = frozenset([
    "org.junit.jupiter.migrationsupport.rules.EnableRuleMigrationSupport",
    "org.junit.jupiter.migrationsupport.EnableJUnit4MigrationSupport",
])  # fmt: skip
# The rule types migration support honours, subclasses included; @ClassRule is never read.
_SUPPORTED_RULES = frozenset(
    f"org.junit.rules.{n}"
    for n in ("Verifier", "ErrorCollector", "ExternalResource", "TemporaryFolder",
              "ExpectedException")
)  # fmt: skip


def _named(program: Program, path: str, name: str, index: int, table, header: bool = False):
    """The entries of table that name can stand for at index: its resolved name, or what a
    single on demand import supplies. A type declared in a read file stands for none."""
    binding = program.resolve_type(path, name, index, header)
    if binding.kind == "declared":
        return set()
    if binding.fqn:
        return {binding.fqn} & set(table)
    last = name.rsplit(".", 1)[-1]
    return {f"{i.name}.{last}" for i in program.units[path].imports
            if i.wildcard and not i.static} & set(table)  # fmt: skip


def _test_engine(program: Program, path: str, annotation: Annotation) -> str | None:
    if program.resolve_type(path, annotation.name, annotation.start).kind == "declared":
        return None  # a local Test type is Decision 6's test_removed, reported there
    found = _named(program, path, annotation.name, annotation.start, _ENGINES)
    return _ENGINES[found.pop()] if len(found) == 1 else "unknown"


def _method_engines(program: Program, path: str, decl: TypeDecl) -> dict[str, str]:
    """The engine each test method of decl runs under, keyed by name and parameter types. A
    TestNG `@Test` on the class makes its public methods tests."""
    whole = None
    for annotation in decl.annotations:
        if simple(annotation) == "Test" and _test_engine(program, path, annotation) == "testng":
            whole = "testng"
    out = {}
    for method in decl.methods:
        key = f"{method.name}({','.join(p.type for p in method.params)})"
        engine = None
        tests = [a for a in method.annotations if simple(a) == "Test"]
        if tests:
            engine = _test_engine(program, path, tests[0])
        elif whole and "public" in method.modifiers and not method.is_constructor:
            configured = {simple(a) for a in method.annotations}
            if not configured & {k.rsplit(".", 1)[-1] for k in _MEMBER_ENGINES}:
                engine = whole
        if engine is not None:
            out[key] = engine
    return out


def _member_engines(program: Program, path: str, annotation: Annotation) -> set[str]:
    """The engines a member annotation belongs to; one that names no type a read file or an
    import supplies counts for every engine with that simple name."""
    found = _named(program, path, annotation.name, annotation.start, _MEMBER_ENGINES)
    binding = program.resolve_type(path, annotation.name, annotation.start)
    if not found and binding.kind == "unresolved":
        found = {k for k in _MEMBER_ENGINES if k.rsplit(".", 1)[-1] == simple(annotation)}
    return {_MEMBER_ENGINES[k] for k in found}


def _supported_rule(program: Program, path: str, type_text: str | None, index: int) -> bool:
    """Whether a rule of this declared type is honoured by Jupiter's migration support."""
    if not type_text:
        return False
    name = type_text.split("<", 1)[0].strip()
    if _named(program, path, name, index, _SUPPORTED_RULES):
        return True
    binding = program.resolve_type(path, name, index)
    if binding.kind != "declared" or not isinstance(binding.decl, TypeDecl):
        return False
    for owner_path, owner in [(binding.path, binding.decl),
                              *program.supertypes(binding.path, binding.decl)]:  # fmt: skip
        for parent in owner.extends:
            if _named(program, owner_path, parent, owner.start, _SUPPORTED_RULES, header=True):
                return True
    return False


def _members(program: Program, path: str, decl: TypeDecl):
    """Each engine bound member annotation of decl and its read supertypes, with whether it is
    a `@Rule` of a type migration support honours."""
    for owner_path, owner in [(path, decl), *program.supertypes(path, decl)]:
        placed = [(a, None, None) for a in owner.annotations]
        placed += [(a, f.type, f.start) for f in owner.fields for a in f.annotations]
        placed += [(a, m.return_type, m.start) for m in owner.methods for a in m.annotations]
        for annotation, type_text, index in placed:
            engines = _member_engines(program, owner_path, annotation)
            if engines:
                rule = _named(program, owner_path, annotation.name, annotation.start,
                              _MEMBER_ENGINES) == {"org.junit.Rule"}  # fmt: skip
                kept = rule and _supported_rule(program, owner_path, type_text, index or 0)
                yield engines, kept


def _unread_supertype(program: Program, path: str, decl: TypeDecl) -> bool:
    """Whether decl or a read supertype extends or implements a type no read file declares;
    a JDK type carries no test engine member."""
    for owner_path, owner in [(path, decl), *program.supertypes(path, decl)]:
        for name in (*owner.extends, *owner.implements):
            binding = program.resolve_type(owner_path, name, owner.start, header=True)
            if binding.kind != "declared" and not (binding.fqn or "").startswith("java."):
                return True
    return False


def _rule_support(program: Program, path: str, decl: TypeDecl) -> bool:
    return any(
        _named(program, owner_path, a.name, a.start, _RULE_SUPPORT)
        for owner_path, owner in [(path, decl), *program.supertypes(path, decl)]
        for a in owner.annotations
    )


def _engine_switches(change: FileChange, ctx: Context) -> Iterator[Finding]:
    """A test method moved to another engine while its class keeps members only the old engine
    runs: a JUnit 4 rule or `@Before` under Jupiter, a Jupiter `@AfterEach` under JUnit 4. The
    checks those members make are then never run for it."""
    if change.before is None:
        return
    before = _types(ctx.before, change.path)
    for fqn, decl in _types(ctx.after, change.path).items():
        old = before.get(fqn)
        if old is None:
            continue
        old_engines = _method_engines(ctx.before, change.path, old)
        new_engines = _method_engines(ctx.after, change.path, decl)
        moves = {
            (was, new_engines[key])
            for key, was in old_engines.items()
            if was != "unknown" and new_engines.get(key, was) != was
        }
        if not moves:
            continue
        if _unread_supertype(ctx.after, change.path, decl):
            yield Finding("weaken_assertion/assertion_rebound", change.path, decl.line)
            continue  # an unread supertype may carry any member the old engine runs
        left = {was for was, _ in moves}
        support = _rule_support(ctx.after, change.path, decl) and all(
            now == "jupiter" for was, now in moves if was == "junit4"
        )
        for engines, kept in _members(ctx.after, change.path, decl):
            if engines & left and not (support and kept):
                yield Finding("weaken_assertion/assertion_rebound", change.path, decl.line)
                break


def _annotations(change: FileChange, ctx: Context) -> Iterator[Finding]:
    before = _declarations(ctx.before, change.path) if change.before else {}
    after = _declarations(ctx.after, change.path)
    for key, decl in after.items():
        old = before.get(key)
        old_anns = old.annotations if old is not None else []
        old_texts = Counter(text(change.before, a) for a in old_anns) if old is not None else {}
        new_texts = Counter(text(change.after, a) for a in decl.annotations)
        old_test = next((a for a in old_anns if simple(a) == "Test"), None)
        new_test = next((a for a in decl.annotations if simple(a) == "Test"), None)
        if new_test is not None:
            yield from _test_attribute_findings(
                attributes(change.before, old_test) if old_test else {},
                attributes(change.after, new_test),
                change.path,
                new_test.line,
            )
        yield from _attribute_switches(change, old_anns, decl.annotations)
        for annotation in decl.annotations:
            if simple(annotation) != "Test" and text(change.after, annotation) not in old_texts:
                yield from _classify(annotation, change.after, change.path, ctx.after)
        for annotation in old_anns:
            if simple(annotation) in _TAGS and text(change.before, annotation) not in new_texts:
                yield Finding("disable/tag_changed", change.path, decl.line)


def _is_abort(program: Program, path: str, call) -> bool:
    if call.name != "abort":
        return False
    if call.receiver in _ASSUMPTION_OWNERS:
        return True
    if call.receiver is None:
        binding = program.resolve_method(path, "abort", call.start)
        return bool(binding.fqn) and binding.fqn.endswith("Assumptions.abort")
    return False


def _top_level_arguments(unit: CompilationUnit, open_paren: int) -> list[tuple[int, int]]:
    pieces, depth, start, i = [], 0, open_paren + 1, open_paren
    while i < len(unit.tokens):
        token = unit.tokens[i]
        if token.kind == "op" and token.text in "([{":
            depth += 1
        elif token.kind == "op" and token.text in ")]}":
            depth -= 1
            if depth == 0:
                if start < i:
                    pieces.append((start, i))
                return pieces
        elif token.kind == "op" and token.text == "," and depth == 1:
            pieces.append((start, i))
            start = i + 1
        i += 1
    return pieces


def _call_site(unit: CompilationUnit, call) -> str:
    """An assumption call without its executable arguments, so a change inside the code that
    assumingThat runs is not read as a changed assumption."""
    arguments = _top_level_arguments(unit, call.start + 1)
    if call.name == "assumingThat" and len(arguments) > 1:
        arguments = arguments[:-1]  # the trailing Executable runs under the assumption
    kept = [" ".join(t.text for t in unit.tokens[start:end]) for start, end in arguments]
    receiver = f"{call.receiver}." if call.receiver not in (None, "<expr>") else ""
    return f"{receiver}{call.name}({', '.join(kept)})"


def _constructs_skip(unit: CompilationUnit, statement: Statement) -> bool:
    tokens = unit.tokens[statement.start : statement.end]
    for i, token in enumerate(tokens):
        if token.text != "new":
            continue
        j = i + 1
        while j + 2 < len(tokens) and tokens[j + 1].text == ".":
            j += 2
        if j < len(tokens) and tokens[j].text in SKIP_EXCEPTIONS:
            return True
    return False


def _assumption_sites(
    unit: CompilationUnit, program: Program, path: str
) -> list[tuple[str, bool, int]]:
    sites = []
    for node in walk(unit):
        if not isinstance(node, Method) or node.body is None:
            continue
        for call in calls_in(unit, node.body):
            if call.name.startswith(("assume", "assuming")) or _is_abort(program, path, call):
                sites.append(_site(unit, call.start, _call_site(unit, call)))
        for inner in walk(node.body):
            simple_statement = isinstance(inner, Statement) and not inner.blocks
            if simple_statement and inner.kind != "local_type" and _constructs_skip(unit, inner):
                sites.append(_site(unit, inner.start, text(unit, inner)))
    return sites


def _site(unit: CompilationUnit, index: int, site_text: str) -> tuple[str, bool, int]:
    path = path_to(unit, index)
    in_catch = any(isinstance(n, Catch) for n in path)
    return site_text, in_catch, unit.tokens[index].line


def _assumptions(change: FileChange, ctx: Context) -> Iterator[Finding]:
    old = Counter()
    if change.before is not None:
        old = Counter(t for t, _, _ in _assumption_sites(change.before, ctx.before, change.path))
    for site_text, in_catch, line in _assumption_sites(change.after, ctx.after, change.path):
        if old[site_text] > 0:
            old[site_text] -= 1
            continue
        code = "swallow_failure/assumption_in_catch" if in_catch else "disable/assumption_added"
        yield Finding(code, change.path, line)


_SOURCES = frozenset(
    ["ValueSource", "CsvSource", "MethodSource", "EnumSource", "CsvFileSource", "ArgumentsSource",
     "FieldSource", "NullSource", "EmptySource", "NullAndEmptySource"]
)  # fmt: skip
_COLLECTIONS = frozenset(["Stream.of", "List.of", "Arrays.asList", "Set.of"])


def _blank_literals(value: str) -> str:
    out, quote, i = [], None, 0
    while i < len(value):
        char = value[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
                out.append("0")
        elif char in "\"'":
            quote = char
        else:
            out.append(char)
        i += 1
    return "".join(out)


def _count_elements(value: str) -> int | None:
    """Elements of an annotation array value such as `{1, 2, 3}` or a single value."""
    value = _blank_literals(value).strip()
    if not value.startswith("{"):
        return 1 if value else None
    inner = value[1:-1].strip() if value.endswith("}") else None
    if inner is None:
        return None
    depth, pieces, current = 0, [], []
    for word in inner.split():
        if word in ("{", "(", "["):
            depth += 1
        elif word in ("}", ")", "]"):
            depth -= 1
        if word == "," and depth == 0:
            pieces.append(current)
            current = []
        else:
            current.append(word)
    pieces.append(current)
    return sum(1 for piece in pieces if piece)


def _text_block_rows(value: str) -> int | None:
    if not value.startswith('"""'):
        return None
    lines = value[3:-3].splitlines()
    return sum(1 for line in lines if line.strip() and not line.strip().startswith("#"))


def _case_count(unit: CompilationUnit, method: Method, owner: TypeDecl) -> tuple[str, int | None]:
    """The text of the method's parameter source and its literal case count, None if unknown."""
    for annotation in method.annotations:
        name = simple(annotation)
        if name not in _SOURCES:
            continue
        attrs = attributes(unit, annotation)
        if name == "ValueSource":
            values = list(attrs.values())
            return text(unit, annotation), _count_elements(values[0]) if len(values) == 1 else None
        if name == "CsvSource":
            if "textBlock" in attrs:
                return text(unit, annotation), _text_block_rows(attrs["textBlock"])
            return text(unit, annotation), _count_elements(attrs.get("value", ""))
        if name == "MethodSource":
            value = attrs.get("value", f'"{method.name}"')
            if not (value.startswith('"') and value.endswith('"')):
                return text(unit, annotation), None
            provider = value.strip('"')
            for candidate in owner.methods:
                if (
                    candidate.name == provider
                    and candidate is not method
                    and not candidate.params
                    and candidate.body is not None
                ):
                    return text(unit, candidate.body), _provided(unit, candidate)
            return text(unit, annotation), None
        return text(unit, annotation), None
    return "", 0


_NARROWING = frozenset(
    ["filter", "limit", "skip", "distinct", "takeWhile", "dropWhile", "flatMap", "mapMulti"]
)


def _provided(unit: CompilationUnit, provider: Method) -> int | None:
    """The provider's literal cases: its Arguments.of and arguments calls, each written out once.

    The count is unknown when any case call sits inside a lambda or a loop, where one call can
    stand for any number of cases, or when the provider narrows a stream (filter, limit, skip
    and the like), since then the written calls overstate what runs."""
    calls = calls_in(unit, provider.body)
    if any(call.name in _NARROWING for call in calls):
        return None
    count = 0
    for call in calls:
        is_case = (call.name == "of" and call.receiver == "Arguments") or (
            call.name == "arguments" and call.receiver in (None, "Arguments")
        )
        if not is_case:
            continue
        around = path_to(unit, call.start)
        loops = ("for", "while", "do")
        if any(
            isinstance(n, Lambda) or (isinstance(n, Statement) and n.kind in loops) for n in around
        ):
            return None
        count += 1
    return count or None


_ROW_PROVIDERS = frozenset(["Parameters", "DataProvider", "TestFactory"])


def _rows(unit: CompilationUnit, provider: Method) -> int | None:
    """Rows written out by a JUnit 4 @Parameters, TestNG @DataProvider or @TestFactory method:
    the inner groups of a two dimensional array initializer, `new Object[] {...}` rows, or
    dynamicTest calls. Unknown when any of them sits inside a lambda or loop."""
    if provider.body is None:
        return None
    tokens = unit.tokens[provider.body.start : provider.body.end]
    words = [t.text for t in tokens]
    loops = any(
        isinstance(n, Statement) and n.kind in ("for", "while", "do") for n in walk(provider.body)
    )
    if loops:
        return None
    count = 0
    for i in range(len(words) - 5):
        if words[i : i + 6] == ["[", "]", "[", "]", "{", words[i + 5]]:
            depth = 0
            for word in words[i + 4 :]:
                if word == "{":
                    depth += 1
                    if depth == 2:
                        count += 1
                elif word == "}":
                    depth -= 1
                    if depth == 0:
                        break
    for i in range(len(words) - 3):
        if words[i : i + 4] == ["new", "Object", "[", "]"] and words[i + 4 : i + 5] == ["{"]:
            count += 1
    for call in calls_in(unit, provider.body):
        if call.name in ("dynamicTest", "dynamicContainer"):
            if any(isinstance(n, Lambda) for n in path_to(unit, call.start)):
                return None  # one call in a lambda can stand for any number of tests
            count += 1
    return count or None


def _class_rows(change: FileChange, ctx: Context) -> Iterator[Finding]:
    if change.before is None:
        return
    before = _methods(ctx.before, change.path)
    for key, (method, _) in _methods(ctx.after, change.path).items():
        if not {simple(a) for a in method.annotations} & _ROW_PROVIDERS:
            continue
        old = before.get(key)
        if old is None:
            continue
        old_text, new_text = text(change.before, old[0]), text(change.after, method)
        if old_text == new_text:
            continue
        old_rows, new_rows = _rows(change.before, old[0]), _rows(change.after, method)
        if old_rows is None or new_rows is None or new_rows < old_rows:
            yield Finding("disable/parameter_cases_dropped", change.path, method.line)


def _parameters(change: FileChange, ctx: Context) -> Iterator[Finding]:
    if change.before is None:
        return
    before = _methods(ctx.before, change.path)
    for key, (method, owner) in _methods(ctx.after, change.path).items():
        old = before.get(key)
        if old is None or not is_test_method(method, owner):
            continue
        old_text, old_count = _case_count(change.before, *old)
        new_text, new_count = _case_count(change.after, method, owner)
        if old_text == new_text:
            continue
        if old_count is None or new_count is None or new_count < old_count:
            yield Finding("disable/parameter_cases_dropped", change.path, method.line)


def findings(ctx: Context) -> Iterator[Finding]:
    for change in ctx.changes:
        if change.path not in ctx.test_paths:
            continue
        if change.before is not None:
            yield from _removals(change, ctx)
        yield from _class_shapes(change, ctx)
        yield from _annotations(change, ctx)
        yield from _assumptions(change, ctx)
        yield from _parameters(change, ctx)
        yield from _class_rows(change, ctx)
        yield from _hidden_classes(change, ctx)
        yield from _engine_switches(change, ctx)
