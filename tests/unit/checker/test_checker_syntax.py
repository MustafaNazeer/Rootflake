import time
from pathlib import Path

import pytest

from rootflake.gate.checker.lexer import Unparsable, tokenize
from rootflake.gate.checker.syntax import CompilationUnit, parse


def unit(source: str) -> CompilationUnit:
    return parse(tokenize(source))


def text(u: CompilationUnit, node) -> str:
    return " ".join(t.text for t in u.tokens[node.start : node.end])


def body(source: str):
    u = unit("class A { void f() { " + source + " } }")
    return u, u.types[0].methods[0].body


class TestCompilationUnit:
    def test_package_and_imports(self):
        u = unit(
            "package org.example;\n"
            "import java.util.List;\nimport static org.junit.Assert.*;\n"
            "import static org.junit.Assert.assertEquals;\nimport org.junit.*;\nclass A {}"
        )
        assert u.package == "org.example"
        assert [(i.name, i.static, i.wildcard) for i in u.imports] == [
            ("java.util.List", False, False),
            ("org.junit.Assert", True, True),
            ("org.junit.Assert.assertEquals", True, False),
            ("org.junit", False, True),
        ]
        assert [i.line for i in u.imports] == [2, 3, 4, 5]

    def test_no_package_is_none(self):
        assert unit("class A {}").package is None

    def test_an_annotated_package_declaration(self):
        assert unit("@Deprecated package a.b; class A {}").package == "a.b"

    @pytest.mark.parametrize(
        ("source", "kind"),
        [("class A {}", "class"), ("interface A {}", "interface"), ("enum A { X }", "enum"),
         ("record A(int x) {}", "record"), ("@interface A {}", "annotation")],
    )  # fmt: skip
    def test_records_enums_interfaces_and_annotation_types_are_found(self, source, kind):
        [t] = unit(source).types
        assert (t.kind, t.name) == (kind, "A")


class TestTypeHeader:
    def test_extends_implements_and_annotations(self):
        u = unit(
            '@RunWith(Foo.class) @Tag("x") public final class A<T extends B<T>> '
            "extends Base<T> implements I1, p.I2<String> {}"
        )
        [t] = u.types
        assert [a.name for a in t.annotations] == ["RunWith", "Tag"]
        assert text(u, t.annotations[0].arguments) == "Foo . class"
        assert t.modifiers == ["public", "final"]
        assert t.extends == ["Base"]
        assert t.implements == ["I1", "p.I2"]
        assert t.header_text == "extends Base < T > implements I1 , p . I2 < String >"

    def test_a_qualified_annotation_name(self):
        [t] = unit("@org.junit.Ignore class A {}").types
        assert t.annotations[0].name == "org.junit.Ignore"

    def test_nested_types_are_members(self):
        [t] = unit("class A { static class B { void g() {} } interface C {} }").types
        assert [n.name for n in t.types] == ["B", "C"]
        assert [m.name for m in t.types[0].methods] == ["g"]


class TestMembers:
    def test_methods_fields_constructors_and_initializers(self):
        u = unit(
            "class A {\n"
            "  @Rule public TestRule r = new Retry(3), s;\n"
            "  private static final long TIMEOUT_MS = 1_000;\n"
            "  A(int x) { this.x = x; }\n"
            "  @Test(timeout = 100) public void testX() throws Exception { run(); }\n"
            "  abstract <T> List<T> make(Class<T> c, int... n);\n"
            "  static { init(); }\n"
            "  { other(); }\n"
            "}"
        )
        [t] = u.types
        assert [(f.names, f.type) for f in t.fields] == [
            (["r", "s"], "TestRule"),
            (["TIMEOUT_MS"], "long"),
        ]
        assert text(u, t.fields[1].initializers["TIMEOUT_MS"]) == "1_000"
        assert t.fields[0].initializers.get("s") is None
        assert [(m.name, m.is_constructor) for m in t.methods] == [
            ("A", True), ("testX", False), ("make", False),
        ]  # fmt: skip
        test = t.methods[1]
        assert [a.name for a in test.annotations] == ["Test"]
        assert test.modifiers == ["public"]
        assert test.return_type == "void"
        assert test.line == 5
        assert [p.name for p in t.methods[2].params] == ["c", "n"]
        assert [p.type for p in t.methods[2].params] == ["Class<T>", "int..."]
        assert t.methods[2].body is None
        assert [i.static for i in t.initializers] == [True, False]

    def test_enum_constants_with_bodies_and_members(self):
        u = unit("enum E { A(1) { void f() {} }, B; int v; E() {} }")
        [t] = u.types
        assert [c.name for c in t.enum_constants] == ["A", "B"]
        assert [m.name for m in t.enum_constants[0].body.methods] == ["f"]
        assert [f.names for f in t.fields] == [["v"]]

    def test_record_components_are_params(self):
        [t] = unit("record P(int x, String y) { P { check(); } }").types
        assert [p.name for p in t.record_components] == ["x", "y"]

    def test_annotation_type_elements_are_methods(self):
        [t] = unit("@interface Retry { int times() default 3; }").types
        assert [m.name for m in t.methods] == ["times"]

    def test_parameter_annotations_and_final(self):
        [t] = unit("class A { void f(@Mock final Foo foo, @TempDir Path p) {} }").types
        assert [(p.type, p.name, [a.name for a in p.annotations]) for p in t.methods[0].params] == [
            ("Foo", "foo", ["Mock"]),
            ("Path", "p", ["TempDir"]),
        ]


class TestStatements:
    def test_statement_kinds(self):
        u, block = body(
            "int x = 1; x++; if (x > 0) { a(); } else b(); while (c) d(); do e(); while (f);"
            "for (int i = 0; i < 3; i++) g(); for (String s : list) h(s); return; "
        )
        assert [s.kind for s in block.statements] == [
            "local_var", "expression", "if", "while", "do", "for", "for", "return",
        ]  # fmt: skip

    def test_if_else_blocks(self):
        u, block = body("if (a) x(); else if (b) { y(); } else z();")
        [s] = block.statements
        assert text(u, s.header) == "( a )"
        then, other = s.blocks
        assert [st.kind for st in then.statements] == ["expression"]
        assert [st.kind for st in other.statements] == ["if"]

    def test_try_catch_finally_and_resources(self):
        u, block = body(
            "try (InputStream in = open()) { read(in); } "
            "catch (IOException | RuntimeException e) { log(e); } "
            "catch (final Throwable t) {} finally { close(); }"
        )
        [s] = block.statements
        assert s.kind == "try"
        assert text(u, s.resources) == "( InputStream in = open ( ) )"
        assert [c.types for c in s.catches] == [["IOException", "RuntimeException"], ["Throwable"]]
        assert [c.name for c in s.catches] == ["e", "t"]
        assert s.catches[1].block.statements == []
        assert [st.kind for st in s.finally_block.statements] == ["expression"]

    def test_simple_statements(self):
        u, block = body(
            'throw new X(); break; continue; assert x : "m"; synchronized (l) { a(); } '
            "label: for (;;) { break label; } ; { inner(); }"
        )
        assert [s.kind for s in block.statements] == [
            "throw", "break", "continue", "assert", "synchronized", "labeled", "empty", "block",
        ]  # fmt: skip

    def test_switch_statement_with_colon_and_arrow_cases(self):
        u, block = body(
            "switch (x) { case 1: a(); break; default: b(); } "
            "switch (y) { case A -> c(); case B -> { d(); } default -> throw new E(); }"
        )
        first, second = block.statements
        assert first.kind == second.kind == "switch"
        assert [st.kind for b in first.blocks for st in b.statements] == [
            "expression", "break", "expression",
        ]  # fmt: skip
        assert [st.kind for b in second.blocks for st in b.statements] == [
            "expression", "expression", "throw",
        ]  # fmt: skip

    def test_yield_inside_a_switch_expression(self):
        u, block = body("int v = switch (x) { case 1: yield 2; default: { yield 3; } };")
        [s] = block.statements
        assert s.kind == "local_var"
        [sw] = s.switches
        assert [st.kind for b in sw.blocks for st in b.statements] == ["yield", "block"]

    def test_local_class_and_record(self):
        u, block = body("class L { void m() {} } record R(int a) {} L l = new L();")
        assert [s.kind for s in block.statements] == ["local_type", "local_type", "local_var"]
        assert block.statements[0].local_type.name == "L"

    def test_an_annotated_local_variable_keeps_its_annotations(self):
        u, block = body('@SuppressWarnings("x") final @A.B int n = 1;')
        [s] = block.statements
        assert s.kind == "local_var"
        assert [a.name for a in s.annotations] == ["SuppressWarnings", "A.B"]

    def test_statement_lines(self):
        u, block = body("a();\n\nb();")
        assert [s.line for s in block.statements] == [1, 3]

    def test_unbraced_nesting(self):
        u, block = body("if (a) if (b) for (;;) while (c) x();")
        inner = block.statements[0].blocks[0].statements[0].blocks[0].statements[0]
        assert inner.kind == "for"


class TestLambdasAndAnonymousClasses:
    def test_block_and_expression_lambdas(self):
        u, block = body("run(() -> { a(); }); map(x -> x + 1); bi((p, q) -> p.equals(q));")
        first, second, third = (s.lambdas for s in block.statements)
        assert [st.kind for st in first[0].body.statements] == ["expression"]
        assert first[0].params == []
        assert text(u, second[0].expression) == "x + 1"
        assert second[0].params == ["x"]
        assert third[0].params == ["p", "q"]
        assert text(u, third[0].expression) == "p . equals ( q )"

    def test_typed_lambda_parameters(self):
        u, block = body("f((String s, int n) -> s.length() + n);")
        assert block.statements[0].lambdas[0].params == ["s", "n"]

    def test_nested_lambdas_are_found_inside_lambda_bodies(self):
        u, block = body("outer(() -> { inner(() -> x()); });")
        [lam] = block.statements[0].lambdas
        assert lam.body.statements[0].lambdas[0].params == []

    def test_anonymous_class_bodies(self):
        u, block = body("Runnable r = new Runnable() { public void run() { go(); } };")
        [anon] = block.statements[0].classes
        assert [m.name for m in anon.methods] == ["run"]
        assert anon.extends == ["Runnable"]

    def test_switch_expression_arrows_are_not_lambdas(self):
        u, block = body("int v = switch (k) { case 1 -> 2; default -> f(() -> 3); };")
        [s] = block.statements
        assert s.lambdas == []
        [sw] = s.switches
        assert sum(len(st.lambdas) for b in sw.blocks for st in b.statements) == 1

    def test_lambda_in_a_field_initializer(self):
        u = unit("class A { Runnable r = () -> { go(); }; }")
        [lam] = u.types[0].fields[0].lambdas
        assert [st.kind for st in lam.body.statements] == ["expression"]

    def test_array_initializers_are_not_blocks(self):
        u, block = body("int[] a = {1, 2}; Object[][] b = new Object[][] {{1}, {2}};")
        assert [s.kind for s in block.statements] == ["local_var", "local_var"]


class TestGenericsAndEdges:
    def test_generic_shift_tokens_do_not_close_blocks(self):
        u = unit(
            "class A { Map<String, List<Map<K, V>>> m; void f() { a = b >> 2; c = d >>> 1; } "
            "void g() {} }"
        )
        [t] = u.types
        assert [m.name for m in t.methods] == ["f", "g"]
        assert t.fields[0].type == "Map<String,List<Map<K,V>>>"

    def test_method_references_and_casts(self):
        u, block = body("list.forEach(System.out::println); Object o = (Runnable) () -> {};")
        assert [len(s.lambdas) for s in block.statements] == [0, 1]

    def test_unbalanced_brackets_are_unparsable(self):
        with pytest.raises(Unparsable):
            unit("class A { void f() { ( } }")

    def test_bracket_depth_257_is_unparsable(self):
        with pytest.raises(Unparsable):
            unit("class A { int x = " + "(" * 257 + "1" + ")" * 257 + "; }")

    def test_bracket_depth_250_parses(self):
        unit("class A { int x = " + "(" * 250 + "1" + ")" * 250 + "; }")

    def test_deep_unbraced_nesting_is_unparsable_not_a_crash(self):
        with pytest.raises(Unparsable):
            unit("class A { void f() { " + "if (a) " * 5000 + "x(); } }")

    def test_a_missing_semicolon_is_unparsable(self):
        with pytest.raises(Unparsable):
            unit("class A { void f() { a() } }")

    def test_a_one_mebibyte_class_checks_in_under_five_seconds(self):
        method = "  @Test public void t{n}() {{ assertEquals({n}, f(x -> x + {n})); }}\n"
        source = "class Big {\n" + "".join(method.format(n=n) for n in range(15000)) + "}\n"
        assert len(source) > 1_000_000
        start = time.perf_counter()
        [t] = unit(source).types
        assert len(t.methods) == 15000
        assert time.perf_counter() - start < 5


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "patches"


def test_every_fixture_pre_image_parses():
    files = sorted(FIXTURES.glob("*/before/**/*.java"))
    assert len(files) > 150
    for path in files:
        try:
            parse(tokenize(path.read_bytes().decode("utf-8")))
        except Unparsable as error:
            pytest.fail(f"{path.relative_to(FIXTURES)}: {error}")


def test_every_fixture_patched_file_parses(tmp_path):
    from test_checker_fixtures import checker_fixtures, trees

    parsed = 0
    for fixture_id in checker_fixtures():
        _, workspace = trees(FIXTURES / fixture_id, tmp_path / fixture_id)
        for path in sorted(workspace.rglob("*.java")):
            try:
                parse(tokenize(path.read_bytes().decode("utf-8")))
            except Unparsable as error:
                pytest.fail(f"{fixture_id}: {path.relative_to(workspace)}: {error}")
            parsed += 1
    assert parsed > 150


def annotation_starts(u: CompilationUnit) -> set[int]:
    found = _Found()
    types = list(u.types)

    def statements(block):
        pending = [block] if block else []
        while pending:
            current = pending.pop()
            for st in current.statements:
                found.update(a for a in st.annotations)
                pending.extend(st.blocks)
                pending.extend(c.block for c in st.catches)
                if st.finally_block:
                    pending.append(st.finally_block)
                if st.local_type:
                    types.append(st.local_type)
                expressions(st)

    def expressions(holder):
        lambdas = list(holder.lambdas)
        types.extend(holder.classes)
        for sw in holder.switches:
            expressions(sw)
            for b in sw.blocks:
                statements(b)
        while lambdas:
            lam = lambdas.pop()
            lambdas.extend(lam.lambdas)
            types.extend(lam.classes)
            for sw in lam.switches:
                expressions(sw)
                for b in sw.blocks:
                    statements(b)
            statements(lam.body)

    while types:
        t = types.pop()
        found.update(a for a in t.annotations)
        types.extend(t.types)
        for c in t.enum_constants:
            found.update(a for a in c.annotations)
            if c.body:
                types.append(c.body)
        for p in t.record_components:
            found.update(a for a in p.annotations)
        for f in t.fields:
            found.update(a for a in f.annotations)
            expressions(f)
        for m in t.methods:
            found.update(a for a in m.annotations)
            for p in m.params:
                found.update(a for a in p.annotations)
            statements(m.body)
        for init in t.initializers:
            statements(init.body)
    return found.covered()


class _Found:
    def __init__(self):
        self.annotations = []

    def update(self, items):
        self.annotations.extend(items)

    def covered(self) -> set[int]:
        out = set()
        for a in self.annotations:
            out.add(a.start)
            if a.arguments:
                out.update(range(a.arguments.start, a.arguments.end))
        return out


def test_every_declaration_annotation_in_the_fixtures_is_attached():
    missing = []
    for path in sorted(FIXTURES.glob("*/before/**/*.java")):
        u = parse(tokenize(path.read_bytes().decode("utf-8")))
        marks = {
            i for i, t in enumerate(u.tokens)
            if t.text == "@" and t.kind == "op" and u.tokens[i + 1].text != "interface"
        }  # fmt: skip
        for i in sorted(marks - annotation_starts(u)):
            missing.append(f"{path.relative_to(FIXTURES)}:{u.tokens[i].line}")
    assert missing == []


def test_repeated_unclosed_generics_stay_linear():
    statement = "x = " + "new A<b && " * 60000 + "c;"
    start = time.perf_counter()
    try:
        unit("class A { void f() { " + statement + " } }")
    except Unparsable:
        pass
    assert time.perf_counter() - start < 3
