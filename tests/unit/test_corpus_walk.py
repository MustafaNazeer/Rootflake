import hashlib

import pytest

from rootflake.corpus.funnel import Eligible
from rootflake.corpus.idoft import Row
from rootflake.corpus.walk import FixDiff, TransientError, Unavailable, rank_key, row_key, walk

SEED = "rootflake-corpus-v1"
SHA = "a" * 40


def item(n, stratum="ID", *, project="o/r", cls=None, pr=None, category=None):
    cls = cls or f"p.C{n}"
    pr = n if pr is None else pr
    row = Row(
        idoft_row=n,
        project_url=f"https://github.com/{project}",
        sha_detected=SHA,
        module_path=".",
        test_name=f"{cls}.m{n}",
        category=category or stratum,
        status="Accepted",
        pr_url=f"https://github.com/{project}/pull/{pr}",
    )
    return Eligible(row, stratum)


class Checks:
    def __init__(self, *, unavailable=(), levels=None, broken=(), unrunnable=(), transient=()):
        self.unavailable = set(unavailable)
        self.levels = levels or {}
        self.broken = set(broken)
        self.unrunnable = set(unrunnable)
        self.transient = set(transient)
        self.calls = []

    def fix_diff(self, row):
        self.calls.append(("fix_diff", row.idoft_row))
        if row.idoft_row in self.transient:
            raise TransientError("rate limited")
        if row.idoft_row in self.unavailable:
            return Unavailable("not merged")
        return FixDiff("b" * 40, "c" * 40, "2020-01-01T00:00:00Z", "d" * 64)

    def java_level(self, row):
        self.calls.append(("java_level", row.idoft_row))
        return self.levels.get(row.idoft_row)

    def build(self, row):
        self.calls.append(("build", row.idoft_row))
        return None if row.idoft_row in self.broken else 11

    def runnable(self, row, jdk):
        self.calls.append(("runnable", row.idoft_row))
        return row.idoft_row not in self.unrunnable


def ranked(items, stratum="ID"):
    return sorted(items, key=lambda e: rank_key(SEED, stratum, e.row.pr_url))


def checked(checks):
    return {n for _, n in checks.calls}


def test_rank_and_row_keys_follow_the_adr_formula():
    url = "https://github.com/o/r/pull/1"
    assert rank_key(SEED, "OD", url) == hashlib.sha256(f"{SEED}\nOD\n{url}".encode()).hexdigest()
    assert row_key(SEED, 42) == hashlib.sha256(f"{SEED}\n42".encode()).hexdigest()


def test_units_are_drawn_in_rank_order_and_test_fills_before_dev():
    items = [item(n, project=f"o/r{n}") for n in range(1, 7)]
    result = walk(items, {"ID": (1, 2)}, SEED, Checks())
    order = [e.row.idoft_row for e in ranked(items)]
    assert [(d.row.idoft_row, d.split) for d in result.drawn] == [
        (order[0], "test"),
        (order[1], "test"),
        (order[2], "dev"),
    ]
    assert {result.reasons[n][0] for n in order[3:]} == {"not_examined"}
    assert result.drawn[0].rank_key == rank_key(SEED, "ID", result.drawn[0].row.pr_url)


def test_rows_failing_rules_six_to_eight_never_reach_a_check():
    items = [item(n) for n in range(1, 7)]
    checks = Checks()
    result = walk(items, {"ID": (3, 3)}, SEED, checks)
    capped = [n for n, (reason, _) in result.reasons.items() if reason == "project_cap_reached"]
    assert len(capped) == 4
    assert not checked(checks) & set(capped)


def test_one_pr_gives_one_test_and_its_other_rows_are_rule_six():
    items = [item(n, pr=1) for n in range(1, 4)]
    items += [item(n, project=f"o/r{n}") for n in range(4, 9)]
    result = walk(items, {"ID": (0, 6)}, SEED, Checks())
    reasons = [result.reasons[n][0] for n in (1, 2, 3)]
    assert sorted(reasons) == ["drawn", "same_pr_already_drawn", "same_pr_already_drawn"]
    first = min((1, 2, 3), key=lambda n: row_key(SEED, n))
    assert result.reasons[first][0] == "drawn"


def test_a_pr_shared_by_two_strata_goes_to_the_rarer_one():
    rare = [item(n, "NOD", project=f"n/r{n}") for n in range(1, 7)]
    common = [item(n, "ID", project=f"i/r{n}") for n in range(10, 20)]
    shared = item(99, "ID", project="n/r1", pr=1)
    result = walk([*rare, *common, shared], {"NOD": (0, 6), "ID": (0, 10)}, SEED, Checks())
    assert result.reasons[1][0] == "drawn"
    assert result.reasons[99][0] == "same_pr_already_drawn"


def test_a_class_never_lands_in_both_splits():
    items = [item(1, cls="p.Same"), item(2, cls="p.Same")]
    result = walk(items, {"ID": (1, 1)}, SEED, Checks())
    assert [d.split for d in result.drawn] == ["test"]
    assert sorted(reason for reason, _ in result.reasons.values()) == [
        "class_in_other_split",
        "drawn",
    ]


@pytest.mark.parametrize(
    ("checks", "reason"),
    [
        (Checks(unavailable={1}), "fix_diff_unavailable"),
        (Checks(levels={1: 7}), "java_out_of_range"),
        (Checks(levels={1: 21}), "java_out_of_range"),
        (Checks(levels={1: 18}), "java_out_of_range"),
        (Checks(levels={1: 4}), "java_out_of_range"),
        (Checks(broken={1}), "build_failed"),
        (Checks(unrunnable={1}), "test_not_runnable"),
    ],
)
def test_rules_nine_to_twelve_in_order(checks, reason):
    result = walk([item(1)], {"ID": (0, 1)}, SEED, checks)
    assert result.reasons[1][0] == reason
    assert not result.drawn


@pytest.mark.parametrize("level", [None, 8, 11, 17])
def test_levels_in_range_or_undeclared_pass_rule_ten(level):
    result = walk([item(1)], {"ID": (0, 1)}, SEED, Checks(levels={1: level}))
    assert result.reasons[1] == ("drawn", "11")
    assert result.drawn[0].jdk == "11"


def test_the_fix_diff_is_fetched_once_per_pr():
    items = [item(n, pr=1) for n in range(1, 4)]
    checks = Checks(levels={n: 7 for n in range(1, 4)})
    walk(items, {"ID": (0, 1)}, SEED, checks)
    assert sum(call[0] == "fix_diff" for call in checks.calls) == 1


def test_a_short_stratum_loses_dev_first_and_later_strata_receive_the_slots():
    rare = [item(n, "NOD", project=f"n/r{n}") for n in range(1, 7)]
    common = [item(n, "ID", project=f"i/r{n}") for n in range(10, 40)]
    checks = Checks(broken={2, 3, 4, 5, 6})
    result = walk([*rare, *common], {"NOD": (2, 3), "ID": (5, 10)}, SEED, checks)
    assert result.shortfalls == [{"stratum": "NOD", "lost_test": 2, "lost_dev": 2}]
    assert result.reallocations == [{"from": "NOD", "test": {"ID": 2}, "dev": {"ID": 2}}]
    assert sum(d.split == "test" for d in result.drawn) == 13
    assert sum(d.split == "dev" for d in result.drawn) == 7
    assert result.final == {"NOD": (0, 1), "ID": (7, 12)}


def test_a_corpus_short_of_prs_is_published_smaller():
    items = [item(n, project=f"o/r{n}") for n in range(1, 4)]
    result = walk(items, {"ID": (2, 3)}, SEED, Checks())
    assert result.final == {"ID": (0, 3)}
    assert result.shortfalls == [{"stratum": "ID", "lost_test": 0, "lost_dev": 2}]
    assert result.reallocations == []


def test_a_transient_error_aborts_the_walk():
    with pytest.raises(TransientError):
        walk([item(1), item(2, project="x/y")], {"ID": (0, 2)}, SEED, Checks(transient={1, 2}))


def test_the_walk_is_deterministic():
    items = [item(n, project=f"o/r{n % 5}", cls=f"p.C{n % 7}") for n in range(1, 40)]
    first = walk(items, {"ID": (4, 8)}, SEED, Checks(broken={3, 9}))
    second = walk(list(reversed(items)), {"ID": (4, 8)}, SEED, Checks(broken={3, 9}))
    assert first == second


def test_a_stratum_without_a_plan_is_refused():
    with pytest.raises(ValueError):
        walk([item(1, "NIO")], {"ID": (0, 1)}, SEED, Checks())


def test_a_claimed_pr_is_rule_six_even_after_the_quota_fills():
    rare = [item(n, "NOD", project=f"n/r{n}") for n in range(1, 7)]
    common = [item(n, "ID", project=f"i/r{n}") for n in range(10, 30)]
    shared = item(99, "ID", project="n/r1", pr=1)
    ids = [*common, shared]
    first = ranked(ids, "ID")[0]
    assert first is not shared, "precondition: the shared PR is not the first ID unit"
    result = walk([*rare, *ids], {"NOD": (0, 6), "ID": (0, 1)}, SEED, Checks())
    assert result.reasons[99][0] == "same_pr_already_drawn"


def test_two_rows_of_one_class_can_both_go_to_test():
    items = [item(1, cls="p.Same"), item(2, cls="p.Same")]
    result = walk(items, {"ID": (0, 2)}, SEED, Checks())
    assert [d.split for d in result.drawn] == ["test", "test"]


def test_rule_eight_is_checked_before_any_network_check():
    items = [item(1, cls="p.Same"), item(2, cls="p.Same")]
    checks = Checks()
    result = walk(items, {"ID": (1, 1)}, SEED, checks)
    (blocked,) = [
        n for n, (reason, _) in result.reasons.items() if reason == "class_in_other_split"
    ]
    assert blocked not in checked(checks)


def test_strata_with_equal_pr_counts_are_walked_in_name_order():
    x = [item(n, "X", project=f"s/r{n}") for n in range(1, 7)]
    y = [item(100 + n, "Y", project=f"y/r{n}") for n in range(1, 6)]
    y.append(item(200, "Y", project="s/r1", pr=1))
    result = walk([*y, *x], {"Y": (0, 6), "X": (0, 6)}, SEED, Checks())
    assert result.reasons[1][0] == "drawn"
    assert result.reasons[200][0] == "same_pr_already_drawn"


def test_lost_test_and_dev_slots_are_reallocated_separately():
    rare = [item(n, "A", project=f"a/r{n}") for n in range(1, 7)]
    later = [item(100 + n, "B", project=f"b/r{n}") for n in range(1, 21)]
    checks = Checks(broken={3, 4, 5, 6})
    result = walk([*rare, *later], {"A": (2, 3), "B": (0, 0)}, SEED, checks)
    assert result.shortfalls[0] == {"stratum": "A", "lost_test": 1, "lost_dev": 2}
    assert result.reallocations == [{"from": "A", "test": {"B": 1}, "dev": {"B": 2}}]
    assert result.final["B"] == (2, 1)


def test_reallocation_weights_count_only_unclaimed_prs():
    a = [item(n, "A", project=f"s/r{n}") for n in range(1, 7)]
    b_shared = [item(100 + n, "B", project=f"s/r{n}", pr=n) for n in range(1, 7)]
    b_own = [item(200 + n, "B", project=f"b/r{n}") for n in range(1, 7)]
    c = [item(300 + n, "C", project=f"c/r{n}") for n in range(1, 8)]
    plan = {"A": (0, 10), "B": (0, 0), "C": (0, 0)}
    result = walk([*a, *b_shared, *b_own, *c], plan, SEED, Checks())
    assert result.reallocations[0] == {"from": "A", "test": {"B": 2, "C": 2}, "dev": {}}


def test_a_short_recipient_passes_on_the_slots_it_received():
    a = [item(n, "A", project=f"a/r{n}") for n in range(1, 7)]
    b = [item(100 + n, "B", project=f"b/r{n}") for n in range(1, 11)]
    c = [item(200 + n, "C", project=f"c/r{n}") for n in range(1, 11)]
    broken = {3, 4, 5, 6, *range(102, 111)}
    plan = {"A": (0, 4), "B": (0, 2), "C": (0, 0)}
    result = walk([*a, *b, *c], plan, SEED, Checks(broken=broken))
    assert result.shortfalls == [
        {"stratum": "A", "lost_test": 2, "lost_dev": 0},
        {"stratum": "B", "lost_test": 2, "lost_dev": 0},
    ]
    assert result.final == {"A": (0, 2), "B": (0, 1), "C": (0, 3)}


def test_a_later_stratum_with_every_pr_claimed_receives_nothing():
    a = [item(n, "A", project=f"s/r{n}") for n in range(1, 7)]
    b = [item(100 + n, "B", project=f"s/r{n}", pr=n) for n in (1, 2)]
    c = [item(200 + n, "C", project=f"c/r{n}") for n in range(1, 11)]
    plan = {"A": (0, 6), "B": (0, 0), "C": (0, 0)}
    result = walk([*a, *b, *c], plan, SEED, Checks(broken={3, 4, 5, 6}))
    assert result.reallocations == [{"from": "A", "test": {"C": 4}, "dev": {}}]
