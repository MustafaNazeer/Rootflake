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


def _loop_exits(unit: CompilationUnit, method: Method) -> int:
    """`continue` statements, and `break` statements whose nearest breakable is a loop, outside
    nested lambdas and classes; each one can skip the cases after it."""
    count = 0
    for node in _outside_nested(method.body):
        if not isinstance(node, Statement) or node.kind not in ("continue", "break"):
            continue
        if node.kind == "continue" or node.end - node.start > 2:
            count += 1  # every continue, and every labelled break, can skip cases
            continue
        around = [n for n in path_to(unit, node.start) if isinstance(n, Statement)]
        breakable = next(
            (n for n in reversed(around) if n.kind in ("for", "while", "do", "switch")), None
        )
        if breakable is not None and breakable.kind != "switch":
            count += 1
    return count


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
        elif _loop_exits(change.after, new) > _loop_exits(change.before, method):
            yield Finding("disable/early_return", change.path, new.line)
        else:
            old_exits = _exit_positions(change.before, method)
            new_exits = _exit_positions(change.after, new)
            if [t for t, _ in old_exits] == [t for t, _ in new_exits] and any(
                n < o for (_, o), (_, n) in zip(old_exits, new_exits, strict=True)
            ):
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
