import pytest

from rootflake.gate.checker.calls import ExpansionBudget, calls_in, follow_for_credit, reach
from rootflake.gate.checker.lexer import Unparsable, tokenize
from rootflake.gate.checker.scope import Program
from rootflake.gate.checker.syntax import parse

PATH = "src/test/java/a/T.java"


def program(source: str, **others: str) -> Program:
    units = {PATH: parse(tokenize(source))}
    units.update({k.replace("__", "/") + ".java": parse(tokenize(v)) for k, v in others.items()})
    return Program(units)


def method(p: Program, name: str, path: str = PATH):
    return next(m for t in p.all_types(path) for m in t.methods if m.name == name)


def names(methods) -> list[str]:
    return sorted(m.method.name for m in methods)


class TestCallsIn:
    def test_calls_with_arity_receiver_and_references(self):
        p = program(
            "class T { void t() { a(); b(1, g(2, 3)); x.c(); this.d(4); list.forEach(T::e); "
            "new Foo(1); } }"
        )
        found = calls_in(p.units[PATH], method(p, "t").body)
        assert [(c.name, c.arity, c.receiver, c.reference) for c in found] == [
            ("a", 0, None, False),
            ("b", 2, None, False),
            ("g", 2, None, False),
            ("c", 0, "x", False),
            ("d", 1, "this", False),
            ("forEach", 1, "list", False),
            ("e", None, "T", True),
        ]


class TestFollowing:
    def test_a_scan_follows_every_overload_without_a_depth_limit(self):
        p = program(
            "class T { void t() { h1(); } void h1() { h2(1); } void h2(int a) { h3(); } "
            "void h2() {} void h3() { h4(); } void h4() {} }"
        )
        reached = reach(p, [(PATH, method(p, "t"))], scan=True, budget=ExpansionBudget())
        assert names(reached) == ["h1", "h2", "h3", "h4", "t"]

    def test_a_scan_rules_out_only_overloads_the_counted_arity_makes_impossible(self):
        p = program(
            "class T { void t() { h(new HashMap<String, Integer>()); g(1); } void h() {} "
            "void h(Object o) {} void h(Object a, Object b) {} void h(Object a, Object b, "
            "Object c) {} void g(int... xs) {} void g(int a, int b, int... xs) {} }"
        )
        reached = reach(p, [(PATH, method(p, "t"))], scan=True, budget=ExpansionBudget())
        params = sorted(len(r.method.params) for r in reached if r.method.name in ("h", "g"))
        assert params == [1, 1, 2]  # h(Object), h(Object, Object) and g(int...)

    def test_credit_follows_only_unique_declarations_three_levels_deep(self):
        p = program(
            "class T { void t() { h1(); o(); } void h1() { h2(); } void h2() { h3(); } "
            "void h3() { h4(); } void h4() {} void o() {} void o(int a) {} }"
        )
        reached = reach(p, [(PATH, method(p, "t"))], scan=False, budget=ExpansionBudget())
        assert names(reached) == ["h1", "h2", "h3", "o", "t"]

    def test_credit_refuses_an_ambiguous_name_and_arity(self):
        p = program("class T { void t() { h(x); } void h(int a) {} void h(String s) {} }")
        call = calls_in(p.units[PATH], method(p, "t").body)[0]
        assert follow_for_credit(p, PATH, call) == []

    def test_an_unqualified_call_credits_the_enclosing_declaration_only(self):
        p = program(
            "class T { void t() { h(); } void h() {} }",
            src__test__java__a__U="class U { void h() {} }",
        )
        call = calls_in(p.units[PATH], method(p, "t").body)[0]
        [(path, found)] = follow_for_credit(p, PATH, call)
        assert (path, found) == (PATH, method(p, "h"))

    def test_an_unqualified_call_to_an_unrelated_class_gets_no_credit(self):
        p = program(
            "class T { void t() { check(); } }",
            src__test__java__a__U="class U { void check() {} }",
        )
        call = calls_in(p.units[PATH], method(p, "t").body)[0]
        assert follow_for_credit(p, PATH, call) == []

    def test_an_inherited_helper_from_a_read_supertype_gets_credit(self):
        p = program(
            "package a; class T extends Base { void t() { check(); } }",
            src__test__java__a__Base="package a; class Base { void check() {} }",
        )
        call = calls_in(p.units[PATH], method(p, "t").body)[0]
        [(path, _)] = follow_for_credit(p, PATH, call)
        assert path == "src/test/java/a/Base.java"

    def test_a_call_on_an_unresolved_receiver_gets_no_credit(self):
        p = program(
            "class T { void t() { other.check(); } }",
            src__test__java__a__U="class U { void check() {} }",
        )
        call = calls_in(p.units[PATH], method(p, "t").body)[0]
        assert follow_for_credit(p, PATH, call) == []

    def test_a_static_call_on_a_read_class_gets_credit(self):
        p = program(
            "package a; class T { void t() { Checks.check(); } }",
            src__test__java__a__Checks="package a; class Checks { static void check() {} }",
        )
        call = calls_in(p.units[PATH], method(p, "t").body)[0]
        [(path, _)] = follow_for_credit(p, PATH, call)
        assert path == "src/test/java/a/Checks.java"

    def test_a_call_with_explicit_type_arguments_is_followed(self):
        p = program(
            "class T { void t() { Waits.<String>pause(); this.<Integer>go(); } void go() {} }",
            src__test__java__a__Waits="class Waits { static <X> void pause() {} }",
        )
        found = calls_in(p.units[PATH], method(p, "t").body)
        assert [(c.name, c.receiver) for c in found] == [("pause", "Waits"), ("go", "this")]
        reached = reach(p, [(PATH, method(p, "t"))], scan=True, budget=ExpansionBudget())
        assert names(reached) == ["go", "pause", "t"]

    def test_a_generic_method_declaration_is_not_a_call(self):
        p = program("class T { void t() { class L { <X> List<X> make() { return null; } } } }")
        assert calls_in(p.units[PATH], method(p, "t").body) == []

    def test_lambdas_and_method_references_are_followed(self):
        p = program(
            "class T { void t() { run(() -> h1()); run(T::h2); } void h1() {} void h2() {} }"
        )
        reached = reach(p, [(PATH, method(p, "t"))], scan=True, budget=ExpansionBudget())
        assert names(reached) == ["h1", "h2", "t"]

    def test_helpers_in_another_read_file_are_followed(self):
        p = program(
            "class T { void t() { Waits.pause(); } }",
            src__test__java__a__Waits="class Waits { static void pause() { inner(); } "
            "static void inner() {} }",
        )
        reached = reach(p, [(PATH, method(p, "t"))], scan=True, budget=ExpansionBudget())
        assert names(reached) == ["inner", "pause", "t"]

    def test_mutually_recursive_helpers_terminate(self):
        p = program("class T { void t() { a(); } void a() { b(); } void b() { a(); t(); } }")
        reached = reach(p, [(PATH, method(p, "t"))], scan=True, budget=ExpansionBudget())
        assert names(reached) == ["a", "b", "t"]

    def test_the_expansion_cap_is_unparsable(self):
        helpers = " ".join(f"void h{n}() {{ h{n + 1}(); }}" for n in range(2100))
        p = program(f"class T {{ void t() {{ h0(); }} {helpers} void h2100() {{}} }}")
        with pytest.raises(Unparsable):
            reach(p, [(PATH, method(p, "t"))], scan=True, budget=ExpansionBudget())

    def test_the_budget_is_shared_across_reaches(self):
        helpers = " ".join(f"void h{n}() {{}}" for n in range(1500))
        calls = " ".join(f"h{n}();" for n in range(1500))
        p = program(f"class T {{ void t() {{ {calls} }} {helpers} }}")
        budget = ExpansionBudget()
        reach(p, [(PATH, method(p, "t"))], scan=True, budget=budget)
        with pytest.raises(Unparsable):
            reach(p, [(PATH, method(p, "t"))], scan=True, budget=budget)


class TestScanReceivers:
    def test_a_call_on_an_unread_type_is_not_followed_into_a_same_named_method(self):
        p = program(
            "import x.RpcStatus; class T { void beginCount() { RpcStatus.beginCount(u); } "
            "void other() { RpcStatus.beginCount(u); } }"
        )
        reached = reach(p, [(PATH, method(p, "other"))], scan=True, budget=ExpansionBudget())
        assert names(reached) == ["other"]

    def test_a_call_on_a_variable_is_followed_to_every_declaration(self):
        p = program(
            "class T { void t() { policy.shouldRetry(1); } }",
            src__test__java__a__P="class P { boolean shouldRetry(int n) { return true; } }",
        )
        reached = reach(p, [(PATH, method(p, "t"))], scan=True, budget=ExpansionBudget())
        assert names(reached) == ["shouldRetry", "t"]

    def test_an_unqualified_call_is_followed_in_the_enclosing_class(self):
        p = program(
            "class T { void t() { h(); } void h() {} }",
            src__test__java__a__U="class U { void h() {} }",
        )
        reached = reach(p, [(PATH, method(p, "t"))], scan=True, budget=ExpansionBudget())
        assert [(r.path, r.method.name) for r in reached] == [(PATH, "t"), (PATH, "h")]

    def test_an_unqualified_call_without_a_local_declaration_follows_every_one(self):
        p = program(
            "class T { void t() { h(); } }",
            src__test__java__a__U="class U { static void h() {} }",
        )
        reached = reach(p, [(PATH, method(p, "t"))], scan=True, budget=ExpansionBudget())
        assert names(reached) == ["h", "t"]

    def test_a_call_on_a_read_type_follows_only_that_type(self):
        p = program(
            "package a; class T { void t() { Waits.pause(); } }",
            src__test__java__a__Waits="package a; class Waits { static void pause() {} }",
            src__test__java__a__Other="package a; class Other { static void pause() {} }",
        )
        reached = reach(p, [(PATH, method(p, "t"))], scan=True, budget=ExpansionBudget())
        assert [r.path for r in reached] == [PATH, "src/test/java/a/Waits.java"]


def test_an_uppercase_receiver_that_is_not_a_type_is_followed_everywhere():
    p = program(
        "class T { void t() { HELPER.sleepy(); } }",
        src__test__java__a__U="class U { void sleepy() {} }",
    )
    reached = reach(p, [(PATH, method(p, "t"))], scan=True, budget=ExpansionBudget())
    assert names(reached) == ["sleepy", "t"]


def test_a_loop_body_reaching_past_the_cap_is_unparsable():
    from rootflake.gate.checker.flow import waits_inside
    from rootflake.gate.checker.syntax import Statement
    from rootflake.gate.checker.tree import walk

    helpers = " ".join(f"void h{n}() {{ h{n + 1}(); }}" for n in range(2100))
    p = program(f"class T {{ void t() {{ while (x) {{ h0(); }} }} {helpers} void h2100() {{}} }}")
    unit = p.units[PATH]
    loop = next(n for n in walk(unit) if isinstance(n, Statement) and n.kind == "while")
    with pytest.raises(Unparsable):
        waits_inside(p, PATH, loop)
