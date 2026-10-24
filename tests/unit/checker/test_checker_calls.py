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
        assert names(reached) == ["h1", "h2", "h2", "h3", "h4", "t"]

    def test_credit_follows_only_unique_declarations_three_levels_deep(self):
        p = program(
            "class T { void t() { h1(); o(); } void h1() { h2(); } void h2() { h3(); } "
            "void h3() { h4(); } void h4() {} void o() {} void o(int a) {} }"
        )
        reached = reach(p, [(PATH, method(p, "t"))], scan=False, budget=ExpansionBudget())
        assert names(reached) == ["h1", "h2", "h3", "o", "t"]

    def test_credit_refuses_an_ambiguous_name_and_arity(self):
        p = program(
            "class T { void t() { h(); } void h() {} }",
            src__test__java__a__U="class U { void h() {} }",
        )
        call = calls_in(p.units[PATH], method(p, "t").body)[0]
        assert follow_for_credit(p, call) == []

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
