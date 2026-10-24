"""Which code is test code, and what a name means (ADR 0007 Decisions 4, 5 and 8).

Names are resolved only from what the files the checker reads declare and import, following
the JLS scope rules ADR 0007 lists: local and member types, type parameters, the same
compilation unit, single type and single static imports, the same package, and finally the
five annotation types every supported `java.lang` has. An on demand import never shadows
anything and never resolves a type, so a name it might supply stays unresolved.
"""

from dataclasses import dataclass

from rootflake.gate.checker.syntax import Block, CompilationUnit, Method, TypeDecl
from rootflake.gate.checker.tree import path_to, walk

TEST_LIBRARIES = (
    "org.junit",
    "junit.framework",
    "org.testng",
    "org.assertj",
    "org.hamcrest",
    "org.awaitility",
    "org.mockito",
    "org.skyscreamer.jsonassert",
    "com.google.common.truth",
)
JAVA_LANG_ANNOTATIONS = frozenset(
    ["Deprecated", "FunctionalInterface", "Override", "SafeVarargs", "SuppressWarnings"]
)
_TEST_COMPONENTS = frozenset(["test", "tests", "testing", "test-infra"])


def is_library(name: str) -> bool:
    return any(name == lib or name.startswith(lib + ".") for lib in TEST_LIBRARIES)


def _qualified_uses(unit: CompilationUnit) -> list[str]:
    tokens, out, i = unit.tokens, [], 0
    while i < len(tokens):
        if tokens[i].kind == "ident" and not (i and tokens[i - 1].text == "."):
            parts, j = [tokens[i].text], i + 1
            while j + 1 < len(tokens) and tokens[j].text == "." and tokens[j + 1].kind == "ident":
                parts.append(tokens[j + 1].text)
                j += 2
            if len(parts) > 1:
                out.append(".".join(parts))
            i = j
        else:
            i += 1
    return out


def is_test_code(path: str, unit: CompilationUnit) -> bool:
    for component in path.split("/")[:-1]:
        lower = component.lower()
        if lower in _TEST_COMPONENTS or lower.endswith(("-test", "-tests", "-testing")):
            return True
    if any(is_library(imp.name) for imp in unit.imports):
        return True
    return any(is_library(name) for name in _qualified_uses(unit))


@dataclass(frozen=True)
class Binding:
    # library: a test library name; declared: declared in a file read; imported: named by a
    # single import but not read; java_lang; type_param; unresolved.
    kind: str
    fqn: str | None = None
    path: str | None = None
    decl: TypeDecl | Method | None = None


def _type_param_names(text: str) -> set[str]:
    names, depth, expect = set(), 0, False
    for token in text.replace("<", " < ").replace(">", " > ").replace(",", " , ").split():
        if token == "<":
            depth += 1
            expect = depth == 1
        elif token == ">":
            depth -= 1
        elif token == "," and depth == 1:
            expect = True
        elif expect and depth == 1:
            names.add(token)
            expect = False
    return names


class Program:
    """Every file the checker read, indexed for name resolution and helper following."""

    def __init__(self, units: dict[str, CompilationUnit]):
        self.units = units
        self.fqn: dict[int, str] = {}
        self.owner: dict[int, str] = {}
        self.types_by_fqn: dict[str, tuple[str, TypeDecl]] = {}
        self.methods_by_name: dict[str, list[tuple[str, Method]]] = {}
        for path, unit in units.items():
            prefix = f"{unit.package}." if unit.package else ""
            pending = [(decl, prefix + decl.name) for decl in unit.types]
            while pending:
                decl, fqn = pending.pop()
                self.fqn[id(decl)] = fqn
                self.owner[id(decl)] = path
                self.types_by_fqn.setdefault(fqn, (path, decl))
                pending.extend((member, f"{fqn}.{member.name}") for member in decl.types)
            for node in walk(unit):
                if isinstance(node, TypeDecl) and id(node) not in self.fqn:
                    self.fqn[id(node)] = f"<local>{node.name}"
                    self.owner[id(node)] = path
                if isinstance(node, Method):
                    self.methods_by_name.setdefault(node.name, []).append((path, node))

    def all_types(self, path: str) -> list[TypeDecl]:
        return [n for n in walk(self.units[path]) if isinstance(n, TypeDecl)]

    def _declared(self, path: str, decl: TypeDecl) -> Binding:
        return Binding("declared", self.fqn[id(decl)], path, decl)

    def supertypes(self, path: str, decl: TypeDecl, seen: set[int] | None = None):
        """Read supertypes of decl, nearest first; unread ones are skipped."""
        seen = set() if seen is None else seen
        out = []
        for name in [*decl.extends, *decl.implements]:
            binding = self.resolve_type(path, name, decl.start, header=True)
            if binding.kind == "declared" and id(binding.decl) not in seen:
                seen.add(id(binding.decl))
                out.append((binding.path, binding.decl))
                out.extend(self.supertypes(binding.path, binding.decl, seen))
        return out

    def _member_type(self, path: str, decl: TypeDecl, name: str) -> Binding | None:
        for member in decl.types:
            if member.name == name:
                return self._declared(path, member)
        for super_path, super_decl in self.supertypes(path, decl):
            for member in super_decl.types:
                if member.name == name:
                    return self._declared(super_path, member)
        return None

    def resolve_type(self, path: str, name: str, index: int, header: bool = False) -> Binding:
        if "." in name:
            return self._resolve_qualified(path, name, index)
        unit = self.units[path]
        if not header:
            for node in reversed(path_to(unit, index)):
                found = self._in_scope(path, node, name, index)
                if found:
                    return found
        for decl in unit.types:
            if decl.name == name:
                return self._declared(path, decl)
        for imp in unit.imports:
            if not imp.wildcard and imp.name.rsplit(".", 1)[-1] == name:
                return self._named(imp.name)
        package = unit.package
        for other_path, other in self.units.items():
            if other_path != path and other.package == package:
                for decl in other.types:
                    if decl.name == name:
                        return self._declared(other_path, decl)
        if name in JAVA_LANG_ANNOTATIONS:
            return Binding("java_lang", f"java.lang.{name}")
        return Binding("unresolved", None)

    def _in_scope(self, path: str, node, name: str, index: int) -> Binding | None:
        if isinstance(node, Block):
            for statement in node.statements:
                if statement.start >= index:
                    break
                local = statement.local_type
                if local is not None and local.name == name:
                    return self._declared(path, local)
        elif isinstance(node, Method):
            if name in _type_param_names(node.type_params):
                return Binding("type_param", name)
        elif isinstance(node, TypeDecl):
            on_header = any(a.start <= index < a.end for a in node.annotations)
            if on_header:
                return None
            if name in _type_param_names(node.type_params):
                return Binding("type_param", name)
            if index >= node.body_start:
                return self._member_type(path, node, name)
        return None

    def _named(self, fqn: str) -> Binding:
        if fqn in self.types_by_fqn:
            other_path, decl = self.types_by_fqn[fqn]
            return self._declared(other_path, decl)
        if is_library(fqn):
            return Binding("library", fqn)
        return Binding("imported", fqn)

    def _resolve_qualified(self, path: str, name: str, index: int) -> Binding:
        head, rest = name.split(".", 1)
        outer = self.resolve_type(path, head, index)
        if outer.kind in ("declared", "library") and outer.fqn:
            return self._named(f"{outer.fqn}.{rest}")
        return self._named(name)

    def resolve_method(self, path: str, name: str, index: int) -> Binding:
        unit = self.units[path]
        tokens = unit.tokens
        if index >= 2 and tokens[index - 1].text == ".":
            receiver = tokens[index - 2]
            if receiver.kind == "ident":
                owner = self.resolve_type(path, receiver.text, index - 2)
                if owner.kind in ("library", "declared") and owner.fqn:
                    return Binding(owner.kind, f"{owner.fqn}.{name}")
            return Binding("unresolved", None)
        for node in reversed(path_to(unit, index)):
            if isinstance(node, TypeDecl):
                for decl_path, decl in [(path, node), *self.supertypes(path, node)]:
                    for method in decl.methods:
                        if method.name == name:
                            return Binding("declared", f"{self.fqn[id(decl)]}.{name}",
                                           decl_path, method)  # fmt: skip
        for imp in unit.imports:
            if imp.static and not imp.wildcard and imp.name.rsplit(".", 1)[-1] == name:
                return Binding("library" if is_library(imp.name) else "imported", imp.name)
        wildcards = [imp.name for imp in unit.imports if imp.static and imp.wildcard]
        if wildcards and all(is_library(w) for w in wildcards):
            return Binding("library", f"{wildcards[0]}.{name}")
        return Binding("unresolved", None)
