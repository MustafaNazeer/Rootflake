"""Tests must survive and still run (ADR 0007 Decision 6).

Only what the patch adds or changes relative to the pristine file is judged. Annotations are
matched by simple name, which is the stricter reading: a project's own `@Disabled` is treated
like Jupiter's.
"""

from collections import Counter
from collections.abc import Iterator

from rootflake.gate.checker.calls import calls_in
from rootflake.gate.checker.model import Context, FileChange, Finding
from rootflake.gate.checker.scope import Program
from rootflake.gate.checker.syntax import (
    Annotation,
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
_ASSUMPTIONS = frozenset(
    ["assumeTrue", "assumeFalse", "assumeThat", "assumeNotNull", "assumingThat"]
)
_ASSUMPTION_OWNERS = frozenset(["Assume", "Assumptions", "org.junit.Assume"])
SKIP_EXCEPTIONS = frozenset(
    ["SkipException", "TestAbortedException", "AssumptionViolatedException"]
)
_CASE_CALLS = frozenset(["of", "arguments"])
_ASSERTION_PREFIXES = ("assert", "fail", "verify", "check", "expect")


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


def _returns(method: Method) -> int:
    return sum(
        1 for n in _outside_nested(method.body) if isinstance(n, Statement) and n.kind == "return"
    )


def _is_empty(method: Method) -> bool:
    return method.body is not None and all(s.kind == "empty" for s in method.body.statements)


def _removals(change: FileChange, ctx: Context) -> Iterator[Finding]:
    before = _methods(ctx.before, change.path)
    after = _methods(ctx.after, change.path)
    target = ctx.target
    for key, (method, owner) in after.items():
        if key not in before and is_test_method(method, owner, ctx.after) and _returns(method):
            yield Finding("disable/early_return", change.path, method.line)
    for key, (method, owner) in before.items():
        if not is_test_method(method, owner, ctx.before):
            continue
        found = after.get(key)
        if found is None:
            yield Finding("disable/test_removed", change.path, method.line)
            continue
        new, new_owner = found
        retried = bool(annotation_names(new.annotations, ctx.after) & RETRY_ANNOTATIONS)
        if not is_test_method(new, new_owner, ctx.after):
            if not retried:
                yield Finding("disable/test_removed", change.path, new.line)
            continue
        is_target = target is not None and key[0] == target.dotted and key[1] == target.method
        old_names = annotation_names(method.annotations, ctx.before) & TEST_ANNOTATIONS
        new_names = annotation_names(new.annotations, ctx.after) & TEST_ANNOTATIONS
        if is_target and old_names != new_names and not retried:
            yield Finding("disable/test_removed", change.path, new.line)
        if method.body and method.body.statements and _is_empty(new):
            yield Finding("disable/test_body_emptied", change.path, new.line)
        if _returns(new) > _returns(method):
            yield Finding("disable/early_return", change.path, new.line)


def _assertion_like(decl: TypeDecl, unit: CompilationUnit) -> bool:
    return any(
        call.name.startswith(_ASSERTION_PREFIXES)
        for method in decl.methods
        if method.body
        for call in calls_in(unit, method.body)
    )


def _class_shapes(change: FileChange, ctx: Context) -> Iterator[Finding]:
    before = _types(ctx.before, change.path) if change.before else {}
    after = _types(ctx.after, change.path)
    for fqn, decl in after.items():
        implements_condition = "ExecutionCondition" in [*decl.extends, *decl.implements]
        old = before.get(fqn)
        if implements_condition and (
            old is None or "ExecutionCondition" not in [*old.extends, *old.implements]
        ):
            yield Finding("disable/execution_condition", change.path, decl.line)
        if old is None:
            continue
        if (old.extends, old.implements) == (decl.extends, decl.implements):
            continue
        has_tests = any(is_test_method(m, d) for d in (old, decl) for m in d.methods)
        if has_tests or old.extends or _assertion_like(decl, change.after):
            yield Finding("weaken_assertion/assertion_rebound", change.path, decl.line)


def _declarations(program: Program, path: str) -> dict[tuple, object]:
    out: dict[tuple, object] = {("type", fqn): decl for fqn, decl in _types(program, path).items()}
    for key, (method, _) in _methods(program, path).items():
        out[("method", *key)] = method
    return out


def _test_attribute_findings(old: dict, new: dict, path: str, line: int) -> Iterator[Finding]:
    if new.get("enabled") == "false" and old.get("enabled") != "false":
        yield Finding("disable/test_disabled", path, line)
    for key in ("priority", "dependsOnMethods", "dependsOnGroups"):
        if new.get(key) != old.get(key) and key in new:
            yield Finding("disable/order_pinned", path, line)
    if new.get("groups") != old.get("groups"):
        yield Finding("disable/tag_changed", path, line)
    if new.get("singleThreaded") == "true" and old.get("singleThreaded") != "true":
        yield Finding("disable/parallelism_disabled", path, line)


def _classify(annotation: Annotation, unit: CompilationUnit, path: str) -> Iterator[Finding]:
    name = simple(annotation)
    line = annotation.line
    if name in _DISABLING or name.startswith(("Disabled", "Enabled")):
        yield Finding("disable/test_disabled", path, line)
    elif name in _ORDER:
        yield Finding("disable/order_pinned", path, line)
    elif name in _PARALLEL or (name == "Execution" and "SAME_THREAD" in text(unit, annotation)):
        yield Finding("disable/parallelism_disabled", path, line)
    elif name in _TAGS:
        yield Finding("disable/tag_changed", path, line)


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
        for annotation in decl.annotations:
            if simple(annotation) != "Test" and text(change.after, annotation) not in old_texts:
                yield from _classify(annotation, change.after, change.path)
        for annotation in old_anns:
            if simple(annotation) in _TAGS and text(change.before, annotation) not in new_texts:
                yield Finding("disable/tag_changed", change.path, decl.line)


def _assumption_sites(unit: CompilationUnit) -> list[tuple[str, bool, int]]:
    sites = []
    for node in walk(unit):
        if not isinstance(node, Method) or node.body is None:
            continue
        for call in calls_in(unit, node.body):
            named = call.name in _ASSUMPTIONS
            aborted = call.name == "abort" and call.receiver in _ASSUMPTION_OWNERS
            if named or aborted:
                sites.append(_site(unit, call.start))
        for inner in walk(node.body):
            if isinstance(inner, Statement) and inner.kind == "throw":
                names = {t.text for t in unit.tokens[inner.start : inner.end]}
                if names & SKIP_EXCEPTIONS:
                    sites.append(_site(unit, inner.start))
    return sites


def _site(unit: CompilationUnit, index: int) -> tuple[str, bool, int]:
    path = path_to(unit, index)
    statement = next((n for n in reversed(path) if isinstance(n, Statement)), None)
    in_catch = any(isinstance(n, Catch) for n in path)
    span = statement if statement is not None else path[-1]
    return text(unit, span), in_catch, unit.tokens[index].line


def _assumptions(change: FileChange, ctx: Context) -> Iterator[Finding]:
    old = Counter(t for t, _, _ in _assumption_sites(change.before)) if change.before else Counter()
    for site_text, in_catch, line in _assumption_sites(change.after):
        if old[site_text] > 0:
            old[site_text] -= 1
            continue
        code = "swallow_failure/assumption_in_catch" if in_catch else "disable/assumption_added"
        yield Finding(code, change.path, line)


def _case_count(unit: CompilationUnit, method: Method, owner: TypeDecl) -> tuple[str, int | None]:
    """The text of the method's parameter source and its literal case count, if one is known."""
    for annotation in method.annotations:
        name = simple(annotation)
        if name in ("ValueSource", "CsvSource"):
            return text(unit, annotation), _array_elements(unit, annotation)
        if name == "MethodSource":
            attrs = attributes(unit, annotation)
            provider = (attrs.get("value") or f'"{method.name}"').strip('"{} ')
            for candidate in owner.methods:
                if candidate.name == provider and candidate.body is not None:
                    return text(unit, candidate.body), _provided(unit, candidate)
            return text(unit, annotation), None
    return "", 0


def _array_elements(unit: CompilationUnit, annotation: Annotation) -> int | None:
    if annotation.arguments is None:
        return None
    tokens = unit.tokens[annotation.arguments.start : annotation.arguments.end]
    opens = [i for i, t in enumerate(tokens) if t.text == "{"]
    if not opens:
        return 1
    depth, count, seen = 0, 0, False
    for token in tokens[opens[0] :]:
        if token.text == "{":
            depth += 1
        elif token.text == "}":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1 and token.text == ",":
            count += 1
        elif depth >= 1:
            seen = True
    return count + 1 if seen else 0


def _provided(unit: CompilationUnit, provider: Method) -> int | None:
    count = sum(
        1
        for call in calls_in(unit, provider.body)
        if call.name in _CASE_CALLS and call.receiver in ("Arguments", None)
        and (call.name == "arguments" or call.receiver == "Arguments")
    )  # fmt: skip
    return count or None


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
