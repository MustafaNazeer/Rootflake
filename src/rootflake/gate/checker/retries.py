"""Retries in test code and newly referenced extensions (ADR 0007 Decision 8).

Every rule here works on sites: a retrying construct together with the member it sits in. A
site counts only when the patched file has more of it than the pristine file, so a retry the
project already had is not the patch's, while one moved onto another test is.

A newly referenced rule, runner, extension, listener or annotation type is judged by the four
steps of Decision 8. A type the patch creates is judged by its code alone; the name rule of
step 1 applies to types that existed before the patch or that the checker cannot read.
"""

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass

from rootflake.gate.checker.calls import calls_in
from rootflake.gate.checker.model import Context, Finding
from rootflake.gate.checker.poll import LOOPS
from rootflake.gate.checker.scope import Binding, Program, is_library
from rootflake.gate.checker.survive import attributes
from rootflake.gate.checker.syntax import (
    Annotation,
    Catch,
    CompilationUnit,
    EnumConstant,
    Field,
    Initializer,
    Method,
    Statement,
    TypeDecl,
)
from rootflake.gate.checker.tree import path_to, walk
from rootflake.gate.checker.values import fold

RETRY_RULE = "retry/retry_rule"
RETRY_ANNOTATION = "retry/retry_annotation"
ANALYZER = "retry/testng_retry_analyzer"
PERCENTAGE = "retry/success_percentage"
CLIENT = "retry/client_retry"
INTERCEPTOR = "retry/invocation_interceptor"
EXISTING = "retry/existing_retry_extension"
UNKNOWN = "flag/unknown_test_extension"
UNDER_TEST = "flag/retry_library_under_test"

RETRY_ANNOTATIONS = frozenset(
    [
        "org.junitpioneer.jupiter.RetryingTest",
        "io.github.artsok.RepeatedIfExceptionsTest",
        "io.github.artsok.ParameterizedRepeatedIfExceptionsTest",
    ]
)
_INTERCEPTORS = frozenset(
    ["org.junit.jupiter.api.extension.InvocationInterceptor", "org.testng.IHookable"]
)
_INTERCEPTOR_NAMES = {
    "org.junit.jupiter.api.extension": "InvocationInterceptor",
    "org.testng": "IHookable",
}
_TESTNG_TEST = "org.testng.annotations.Test"
_STATEMENT = "org.junit.runners.model.Statement"
_REFERENCING = {
    "org.junit.runner.RunWith": "class",
    "org.junit.jupiter.api.extension.ExtendWith": "class",
    "org.testng.annotations.Listeners": "class",
    "org.junit.Rule": "field",
    "org.junit.ClassRule": "field",
    "org.junit.jupiter.api.extension.RegisterExtension": "field",
}
_NAME_WORDS = ("retry", "rerun", "repeat", "flaky", "ignore")
# Known framework types a test may newly use, by fully qualified name (Decision 8 step 2).
ALLOWLIST = frozenset(
    [
        "org.mockito.junit.jupiter.MockitoExtension",
        "org.mockito.junit.MockitoJUnitRunner",
        "org.mockito.junit.MockitoRule",
        "org.junit.rules.TemporaryFolder",
        "org.junit.rules.TestName",
        "org.junit.rules.ErrorCollector",
        "org.junit.rules.RuleChain",
        "org.junit.runners.JUnit4",
        "org.junit.runners.Parameterized",
        "org.junit.runners.BlockJUnit4ClassRunner",
        "org.junit.experimental.runners.Enclosed",
        "org.springframework.test.context.junit4.SpringRunner",
        "org.springframework.test.context.junit4.SpringJUnit4ClassRunner",
        "org.springframework.test.context.junit.jupiter.SpringExtension",
    ]
)
# Retry libraries, matched on whole package components. In a package whose own name is about
# retrying any type counts; in a general one only a type or member whose name says Retry.
CLIENT_PACKAGES = (
    "com.linecorp.armeria.client.retry",
    "org.springframework.retry",
    "org.springframework.core.retry",
    "org.springframework.resilience.annotation",
    "io.github.resilience4j.retry",
    "dev.failsafe",
    "net.jodah.failsafe",
    "okhttp3",
    "org.apache.http",
    "org.apache.hc.client5",
)
_HTTP = ("org.apache.http", "org.apache.hc.client5")
_RETRY_SETTERS = frozenset(
    ["setRetryHandler", "setRetryStrategy", "setServiceUnavailableRetryStrategy"]
)
_STATUS_KEPT = frozenset(
    ["FAILURE", "ITestResult . FAILURE", "org . testng . ITestResult . FAILURE"]
)


@dataclass(frozen=True)
class Site:
    key: tuple
    label: str
    code: str
    line: int
    index: int


def _under(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


def client_package(fqn: str) -> str | None:
    """The retry library a fully qualified name lies in, or None."""
    for package in CLIENT_PACKAGES:
        if _under(fqn, package) and fqn != package:
            rest = fqn[len(package) + 1 :]
            if "retry" in package.split(".")[-1] or "Retry" in rest:
                return package
    return None


def retry_named(name: str) -> bool:
    lower = name.rsplit(".", 1)[-1].lower()
    return any(word in lower for word in _NAME_WORDS)


def member_key(program: Program, unit: CompilationUnit, index: int) -> tuple:
    key = []
    for node in path_to(unit, index):
        if isinstance(node, TypeDecl):
            key.append(("T", program.fqn.get(id(node), node.name)))
        elif isinstance(node, Method):
            key.append(("M", node.name, tuple(p.type for p in node.params)))
        elif isinstance(node, Field):
            key.append(("F", tuple(node.names)))
        elif isinstance(node, EnumConstant):
            key.append(("E", node.name))
        elif isinstance(node, Initializer):
            key.append(("I", node.static))
    return tuple(key)


def resolve(program: Program, path: str, name: str, index: int, header: bool = False) -> Binding:
    """resolve_type, plus a read type of that simple name in a package the file imports on
    demand: the stricter reading, since the checker cannot see a wildcard package's contents."""
    binding = program.resolve_type(path, name, index, header)
    if binding.kind != "unresolved" or "." in name:
        return binding
    for imp in program.units[path].imports:
        if imp.wildcard and not imp.static and f"{imp.name}.{name}" in program.types_by_fqn:
            return program._named(f"{imp.name}.{name}")
    return binding


def _library_wildcards(unit: CompilationUnit) -> bool:
    """Whether the file imports on demand, and only from test library or JDK packages, neither
    of which holds a retrying extension."""
    wildcards = [imp.name for imp in unit.imports if imp.wildcard and not imp.static]
    return bool(wildcards) and all(is_library(w) or _under(w, "java") for w in wildcards)


def _wildcard_named(unit: CompilationUnit, name: str, table: dict[str, str]) -> bool:
    return any(
        imp.wildcard and not imp.static and table.get(imp.name) == name for imp in unit.imports
    )


def _wildcard_role(unit: CompilationUnit, name: str) -> str | None:
    """The role of a referencing annotation an on demand import could supply, so that
    `import org.junit.*;` with `@Rule` still names its field type."""
    for imp in unit.imports:
        if imp.wildcard and not imp.static and f"{imp.name}.{name}" in _REFERENCING:
            return _REFERENCING[f"{imp.name}.{name}"]
    return None


def _annotations(unit: CompilationUnit) -> Iterator[tuple[Annotation, object]]:
    for node in walk(unit):
        if isinstance(node, TypeDecl):
            yield from ((a, node) for a in node.annotations)
            for component in node.record_components:
                yield from ((a, component) for a in component.annotations)
        elif isinstance(node, Method):
            yield from ((a, node) for a in node.annotations)
            for param in node.params:
                yield from ((a, param) for a in param.annotations)
        elif isinstance(node, Field | EnumConstant | Statement):
            yield from ((a, node) for a in node.annotations)


def _class_literals(unit: CompilationUnit, annotation: Annotation) -> list[tuple[str, int]]:
    if annotation.arguments is None:
        return []
    tokens, out = unit.tokens, []
    for i in range(annotation.arguments.start, annotation.arguments.end - 1):
        if tokens[i].text == "class" and tokens[i - 1].text == ".":
            j = i - 2
            parts = []
            while j >= annotation.arguments.start and tokens[j].kind == "ident":
                parts.append(tokens[j].text)
                if j - 1 >= annotation.arguments.start and tokens[j - 1].text == ".":
                    j -= 2
                else:
                    break
            if parts:
                out.append((".".join(reversed(parts)), j))
    return out


def _created_types(unit: CompilationUnit, start: int, end: int) -> list[tuple[str, int]]:
    tokens, out = unit.tokens, []
    for i in range(start, min(end, len(tokens) - 1)):
        if tokens[i].text == "new" and tokens[i + 1].kind == "ident":
            j, parts = i + 1, [tokens[i + 1].text]
            while j + 2 < end and tokens[j + 1].text == "." and tokens[j + 2].kind == "ident":
                parts.append(tokens[j + 2].text)
                j += 2
            out.append((".".join(parts), i + 1))
    return out


def _base_type(type_text: str) -> str:
    return type_text.split("<", 1)[0].rstrip("[]").strip()


def references(program: Program, path: str) -> list[tuple[Site, str, int]]:
    """Every rule, runner, extension, listener or annotation type the file names, as a site with
    the referenced name and where it is written."""
    unit = program.units[path]
    out = []
    for annotation, owner in _annotations(unit):
        binding = resolve(program, path, annotation.name, annotation.start + 1)
        key = member_key(program, unit, annotation.start)
        names: list[tuple[str, int]] = []
        role = _REFERENCING.get(binding.fqn or "") if binding.kind == "library" else None
        if binding.kind == "unresolved":
            role = _wildcard_role(unit, annotation.name)
        if role == "class":
            names = _class_literals(unit, annotation)
        elif role == "field" and isinstance(owner, Field):
            names = [(_base_type(owner.type), owner.start)]
            for span in owner.initializers.values():
                names.extend(_created_types(unit, span.start, span.end))
        elif role == "field" and isinstance(owner, Method) and owner.return_type:
            names = [(_base_type(owner.return_type), owner.start)]
        elif binding.kind not in ("library", "java_lang", "type_param"):
            names = [(annotation.name, annotation.start + 1)]
        for name, at in names:
            site = Site(key, f"ref {annotation.name} {name}", "", annotation.line, at)
            out.append((site, name, at))
    return out


def _method_of(unit: CompilationUnit, index: int) -> tuple[Method | None, bool, bool]:
    method, looped, caught = None, False, False
    for node in path_to(unit, index):
        if isinstance(node, Method | TypeDecl):
            method, looped, caught = (node if isinstance(node, Method) else None), False, False
        elif isinstance(node, Statement) and node.kind in LOOPS:
            looped = True
        elif isinstance(node, Catch):
            caught = True
    return method, looped, caught


def _imports(unit: CompilationUnit, name: str) -> bool:
    return any(
        imp.name == name or (imp.wildcard and _under(name, imp.name)) for imp in unit.imports
    ) or name.replace(".", " . ") in " ".join(t.text for t in unit.tokens)


def _evaluate_sites(program: Program, path: str) -> Iterator[Site]:
    unit = program.units[path]
    if not _imports(unit, _STATEMENT):
        return
    per_method: dict[int, list] = {}
    for call in calls_in(unit, unit_span(unit)):
        if call.name != "evaluate" or call.arity != 0 or call.receiver is None:
            continue
        method, looped, caught = _method_of(unit, call.start)
        if method is not None:
            per_method.setdefault(id(method), [method, []])[1].append((call, looped or caught))
    for method, found in per_method.values():
        if len(found) > 1 or any(retried for _, retried in found):
            key = member_key(program, unit, method.start)
            yield Site(key, "evaluate", RETRY_RULE, method.line, method.start)


@dataclass
class _Whole:
    start: int
    end: int


def unit_span(unit: CompilationUnit) -> _Whole:
    return _Whole(0, len(unit.tokens))


def _supertype_sites(program: Program, path: str) -> Iterator[Site]:
    unit = program.units[path]
    for decl in program.all_types(path):
        key = member_key(program, unit, decl.start)
        for name in (*decl.extends, *decl.implements):
            binding = resolve(program, path, name, decl.start, header=True)
            fqn = binding.fqn or ""
            simple = name.rsplit(".", 1)[-1].split("<", 1)[0]
            if fqn in _INTERCEPTORS or (
                binding.kind == "unresolved" and _wildcard_named(unit, simple, _INTERCEPTOR_NAMES)
            ):
                yield Site(key, f"implements {simple}", INTERCEPTOR, decl.line, decl.start)
            elif (
                binding.kind == "library" and _under(fqn, "org.testng") and "Retry" in simple
            ) or (
                binding.kind == "unresolved"
                and "Retry" in simple
                and any(imp.wildcard and _under(imp.name, "org.testng") for imp in unit.imports)
            ):
                yield Site(key, f"implements {simple}", ANALYZER, decl.line, decl.start)


def _testng(unit: CompilationUnit) -> bool:
    return any(_under(imp.name, "org.testng") for imp in unit.imports)


def _call_sites(program: Program, path: str) -> Iterator[Site]:
    unit = program.units[path]
    tokens = unit.tokens
    testng = _testng(unit)
    for call in calls_in(unit, unit_span(unit)):
        key = member_key(program, unit, call.start)
        line = tokens[call.start].line
        if call.name == "setRetryAnalyzer":
            yield Site(key, "setRetryAnalyzer", ANALYZER, line, call.start)
        elif call.name == "setStatus" and call.arity == 1 and testng:
            argument = " ".join(
                t.text for t in tokens[call.start + 2 : _close(tokens, call.start + 1)]
            )
            if argument not in _STATUS_KEPT:
                yield Site(key, f"setStatus {argument}", INTERCEPTOR, line, call.start)


def _close(tokens, open_at: int) -> int:
    depth = 0
    for k in range(open_at, len(tokens)):
        if tokens[k].text == "(":
            depth += 1
        elif tokens[k].text == ")":
            depth -= 1
            if depth == 0:
                return k
    return len(tokens)


def _annotation_sites(program: Program, path: str) -> Iterator[Site]:
    unit = program.units[path]
    for annotation, _ in _annotations(unit):
        binding = resolve(program, path, annotation.name, annotation.start + 1)
        fqn = binding.fqn or ""
        key = member_key(program, unit, annotation.start)
        if fqn in RETRY_ANNOTATIONS:
            yield Site(key, f"@{fqn}", RETRY_ANNOTATION, annotation.line, annotation.start)
        elif fqn == _TESTNG_TEST:
            values = attributes(unit, annotation)
            if "retryAnalyzer" in values:
                label = f"retryAnalyzer {values['retryAnalyzer']}"
                yield Site(key, label, ANALYZER, annotation.line, annotation.start)
            if "successPercentage" in values:
                percentage = fold(values["successPercentage"])
                if percentage is None or percentage < 100:
                    label = f"successPercentage {values['successPercentage']}"
                    yield Site(key, label, PERCENTAGE, annotation.line, annotation.start)


def _imported_names(unit: CompilationUnit) -> dict[str, str]:
    return {imp.name.rsplit(".", 1)[-1]: imp.name for imp in unit.imports if not imp.wildcard}


def _client_sites(program: Program, path: str) -> Iterator[Site]:
    unit = program.units[path]
    tokens = unit.tokens
    names = {n: f for n, f in _imported_names(unit).items() if client_package(f)}
    wildcards = [imp.name for imp in unit.imports if imp.wildcard and not imp.static]
    starred = [w for w in wildcards if any(_under(w, p) for p in CLIENT_PACKAGES)]
    okhttp = any(_under(imp.name, "okhttp3") for imp in unit.imports)
    body = [i for decl in unit.types for i in range(decl.start, decl.end)]
    for i in body:
        token = tokens[i]
        if token.kind != "ident" or (i and tokens[i - 1].text == "."):
            continue
        parts, j = [token.text], i
        while j + 2 < len(tokens) and tokens[j + 1].text == "." and tokens[j + 2].kind == "ident":
            parts.append(tokens[j + 2].text)
            j += 2
        fqn = None
        if token.text in names:
            fqn = names[token.text]
        elif len(parts) > 1:
            for n in range(len(parts), 1, -1):
                if client_package(".".join(parts[:n])):
                    fqn = ".".join(parts[:n])
                    break
        elif (
            starred
            and "Retry" in token.text
            and resolve(program, path, token.text, i).kind == "unresolved"
        ):
            fqn = f"{starred[0]}.{token.text}"
        if fqn is not None:
            key = member_key(program, unit, i)
            yield Site(key, f"uses {fqn}", CLIENT, token.line, i)
    for call in calls_in(unit, unit_span(unit)):
        key = member_key(program, unit, call.start)
        line = tokens[call.start].line
        argument = " ".join(t.text for t in tokens[call.start + 2 : _close(tokens, call.start + 1)])
        if call.name == "retryOnConnectionFailure" and okhttp and argument == "true":
            yield Site(key, "retryOnConnectionFailure true", CLIENT, line, call.start)
        elif call.name in _RETRY_SETTERS and any(_imports_under(unit, p) for p in _HTTP):
            yield Site(key, call.name, CLIENT, line, call.start)


def _imports_under(unit: CompilationUnit, package: str) -> bool:
    return any(_under(imp.name, package) for imp in unit.imports)


def _disabled_retries(program: Program, path: str) -> Iterator[Site]:
    """Calls that turn a client's default retries off; removing one turns them on."""
    unit = program.units[path]
    tokens = unit.tokens
    okhttp = _imports_under(unit, "okhttp3")
    http = any(_imports_under(unit, p) for p in _HTTP)
    for call in calls_in(unit, unit_span(unit)):
        argument = " ".join(t.text for t in tokens[call.start + 2 : _close(tokens, call.start + 1)])
        key = member_key(program, unit, call.start)
        line = tokens[call.start].line
        if call.name == "retryOnConnectionFailure" and okhttp and argument == "false":
            yield Site(key, "retryOnConnectionFailure false", CLIENT, line, call.start)
        elif call.name == "disableAutomaticRetries" and http and call.arity == 0:
            yield Site(key, "disableAutomaticRetries", CLIENT, line, call.start)


def code_sites(program: Program, path: str) -> list[Site]:
    """The retrying constructs a file holds, whatever the patch changed."""
    return [
        *_annotation_sites(program, path),
        *_evaluate_sites(program, path),
        *_supertype_sites(program, path),
        *_call_sites(program, path),
        *_client_sites(program, path),
    ]


def added(old: list[Site], new: list[Site]) -> list[Site]:
    """The sites the new side has more of, by member and label."""
    left = Counter((s.key, s.label) for s in old)
    out = []
    for site in new:
        if left[(site.key, site.label)]:
            left[(site.key, site.label)] -= 1
        else:
            out.append(site)
    return out


def _sites(program: Program | None, path: str, kind) -> list:
    if program is None or path not in program.units:
        return []
    memo = program.memo.setdefault(("retries", kind.__name__), {})
    if path not in memo:
        memo[path] = list(kind(program, path))
    return memo[path]


def _under_test(ctx: Context, path: str, site: Site) -> bool:
    """A file that lies inside the library it uses, and existed there before the patch."""
    package = ctx.after.units[path].package or ""
    library = (
        client_package(site.label.removeprefix("uses ")) if site.label.startswith("uses ") else None
    )
    before = ctx.before.units.get(path)
    return (
        library is not None
        and _under(package, library)
        and before is not None
        and before.package == package
    )


def _top_level(program: Program, path: str, name: str, index: int) -> tuple[str, list[str]] | None:
    """The top level type a reference names and the member names under it, when it can name
    one: a single import, a type of the same package, or a qualified name."""
    unit = program.units[path]
    parts = name.split(".")
    head = parts[0]
    if head[:1].islower():
        for n, part in enumerate(parts):
            if part[:1].isupper():
                return ".".join(parts[: n + 1]), parts[n + 1 :]
        return None
    binding = resolve(program, path, head, index)
    if binding.kind == "imported" and binding.fqn:
        return binding.fqn, parts[1:]
    if binding.kind == "unresolved":
        prefix = f"{unit.package}." if unit.package else ""
        return prefix + head, parts[1:]
    return None


def requests(ctx: Context) -> list[tuple[str, str]]:
    """The newly referenced top level types no read file declares, with the referencing path,
    so the caller can read the one pristine file that may declare each (ADR 0007 Decision 2)."""
    out = []
    for path in sorted(ctx.test_paths):
        old = [s for s, _, _ in _references(ctx.before, path)]
        fresh = {id(s) for s in added(old, [s for s, _, _ in _references(ctx.after, path)])}
        for site, name, at in _references(ctx.after, path):
            if id(site) not in fresh:
                continue
            binding = resolve(ctx.after, path, name.split(".", 1)[0], at)
            if binding.kind in ("declared", "library", "java_lang", "type_param"):
                continue
            if (binding.fqn or "").startswith(("java.", "javax.")):
                continue
            top = _top_level(ctx.after, path, name, at)
            if top is not None and top[0] not in ctx.after.types_by_fqn:
                out.append((path, top[0]))
    return sorted(set(out))


def _references(program: Program | None, path: str) -> list[tuple[Site, str, int]]:
    if program is None or path not in program.units:
        return []
    memo = program.memo.setdefault(("retries", "references"), {})
    if path not in memo:
        memo[path] = references(program, path)
    return memo[path]


class _Judge:
    """The four steps of Decision 8 for one newly referenced type, followed through what a
    read type extends, carries and delegates to."""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.program = ctx.extended
        self.seen: set[int] = set()

    def created(self, fqn: str | None) -> bool:
        return (
            fqn is not None
            and fqn not in self.ctx.before.types_by_fqn
            and fqn not in self.ctx.pristine_types
        )

    def reference(self, path: str, name: str, index: int, line: int) -> set[str]:
        binding = resolve(self.program, path, name, index)
        if binding.kind in ("java_lang", "type_param"):
            return set()
        if binding.fqn in RETRY_ANNOTATIONS:
            return {RETRY_ANNOTATION}
        if binding.kind == "declared":
            return self.declared(binding)
        fqn = binding.fqn
        top = _top_level(self.program, path, name, index)
        if top is not None and top[0] in self.ctx.ambiguous:
            return {UNKNOWN}
        if binding.kind == "unresolved" and _library_wildcards(self.program.units[path]):
            return set()  # no file of the package declares it, so a test library supplies it
        if retry_named(name):
            return {EXISTING}
        if fqn in ALLOWLIST or binding.kind == "library" or (fqn or "").startswith("java."):
            return set()
        return {UNKNOWN}

    def declared(self, binding: Binding) -> set[str]:
        decl = binding.decl
        if not isinstance(decl, TypeDecl) or id(decl) in self.seen:
            return set()
        self.seen.add(id(decl))
        self.ctx.budget.spend(decl.line)
        if not self.created(binding.fqn) and retry_named(decl.name):
            return {EXISTING}
        if binding.fqn in ALLOWLIST:
            return set()
        path, unit = binding.path, self.program.units[binding.path]
        found = {s.code for s in code_sites(self.program, path) if decl.start <= s.index < decl.end}
        for name in (*decl.extends, *decl.implements):
            found |= self.supertype(path, name, decl)
        for site, name, at in references(self.program, path):
            if decl.start <= at < decl.end:
                found |= self.reference(path, name, at, site.line)
        for name, at in _delegates(unit, decl):
            found |= self.delegate(path, name, at)
        return found

    def supertype(self, path: str, name: str, decl: TypeDecl) -> set[str]:
        base = _base_type(name)
        binding = resolve(self.program, path, base, decl.start, header=True)
        if binding.kind == "declared":
            return self.declared(binding)
        if binding.kind == "library" or (binding.fqn or "").startswith("java."):
            return set()
        return {EXISTING} if retry_named(base) else {UNKNOWN}

    def delegate(self, path: str, name: str, index: int) -> set[str]:
        binding = resolve(self.program, path, name, index)
        if binding.kind == "declared":
            return self.declared(binding)
        if binding.kind in ("library", "java_lang", "type_param"):
            return set()
        return {EXISTING} if retry_named(name) else set()


def _delegates(unit: CompilationUnit, decl: TypeDecl) -> list[tuple[str, int]]:
    """The types a class visibly hands work to: field types, constructed types and static call
    receivers."""
    out = [(_base_type(f.type), f.start) for f in decl.fields]
    out.extend(_created_types(unit, decl.start, decl.end))
    tokens = unit.tokens
    for i in range(decl.start, min(decl.end, len(tokens) - 2)):
        token = tokens[i]
        if (
            token.kind == "ident"
            and token.text[:1].isupper()
            and tokens[i + 1].text == "."
            and tokens[i + 2].kind == "ident"
            and not (i and tokens[i - 1].text in (".", "new"))
        ):
            out.append((token.text, i))
    return out


def findings(ctx: Context) -> Iterator[Finding]:
    seen: set[Finding] = set()
    for finding in _findings(ctx):
        if finding not in seen:
            seen.add(finding)
            yield finding


def _findings(ctx: Context) -> Iterator[Finding]:
    judge = _Judge(ctx)
    for path in sorted(ctx.test_paths):
        for kind in (_annotation_sites, _evaluate_sites, _supertype_sites, _call_sites):
            old = _sites(ctx.before, path, kind)
            for site in added(old, _sites(ctx.after, path, kind)):
                yield Finding(site.code, path, site.line)
        old = _sites(ctx.before, path, _client_sites)
        for site in added(old, _sites(ctx.after, path, _client_sites)):
            yield Finding(UNDER_TEST if _under_test(ctx, path, site) else CLIENT, path, site.line)
        new = _sites(ctx.after, path, _disabled_retries)
        for _ in added(new, _sites(ctx.before, path, _disabled_retries)):
            yield Finding(CLIENT, path, None)  # the call is gone, so it has no line here
        old_refs = [s for s, _, _ in _references(ctx.before, path)]
        new_refs = _references(ctx.after, path)
        fresh = {id(s) for s in added(old_refs, [s for s, _, _ in new_refs])}
        for site, name, at in new_refs:
            if id(site) in fresh:
                for code in sorted(judge.reference(path, name, at, site.line)):
                    yield Finding(code, path, site.line)
