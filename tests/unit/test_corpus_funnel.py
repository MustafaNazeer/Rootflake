import pytest

from rootflake.corpus.funnel import FOLD, INCLUDED, REASONS, screen
from rootflake.corpus.idoft import Row

SHA = "a" * 40


def row(n, *, project="o/r", test="p.C.m", category="ID", status="Accepted", pr=None, module="."):
    pr = pr if pr is not None else n
    return Row(
        idoft_row=n,
        project_url=f"https://github.com/{project}",
        sha_detected=SHA,
        module_path=module,
        test_name=test,
        category=category,
        status=status,
        pr_url=f"https://github.com/{project}/pull/{pr}",
    )


def many(category, prs, start=100, project="o/r"):
    return [
        row(start + i, project=project, test=f"p.C{start + i}.m", category=category, pr=start + i)
        for i in range(prs)
    ]


def test_reasons_are_the_fourteen_codes_in_order():
    assert REASONS == (
        "status_not_accepted", "duplicate_test", "multiple_labels", "category_excluded",
        "stratum_too_thin", "same_pr_already_drawn", "project_cap_reached",
        "class_in_other_split", "fix_diff_unavailable", "java_out_of_range", "build_failed",
        "test_not_runnable", "not_examined", "drawn",
    )  # fmt: skip


def test_the_od_family_folds_into_one_stratum():
    assert INCLUDED == {"ID", "OD", "OD-Vic", "OD-Brit", "NIO", "NOD", "TZD"}
    assert FOLD == {"ID": "ID", "OD": "OD", "OD-Vic": "OD", "OD-Brit": "OD", "NIO": "NIO",
                    "NOD": "NOD", "TZD": "TZD"}  # fmt: skip


@pytest.mark.parametrize("status", ["Opened", "accepted", "Accepted ", "", "DeveloperFixed"])
def test_rule_1_only_exact_accepted_survives(status):
    eligible, reasons, not_accepted = screen([row(1, status=status), *many("ID", 6)])
    assert not_accepted == 1
    assert 1 not in reasons
    assert 1 not in {e.row.idoft_row for e in eligible}


def test_rule_2_removes_every_copy_of_a_duplicate_test_across_spellings():
    rows = [row(1, project="O/R", pr=1), row(2, project="o/r", pr=2), *many("ID", 6)]
    _, reasons, _ = screen(rows)
    assert reasons[1] == reasons[2] == "duplicate_test"


def test_the_same_test_in_another_module_is_not_a_duplicate():
    rows = [row(1, module="a"), row(2, module="b"), *many("ID", 6)]
    _, reasons, _ = screen(rows)
    assert 1 not in reasons and 2 not in reasons


@pytest.mark.parametrize("category", ["NIO;OD", "OD;ID", "NDOD;UD;NOD"])
def test_rule_3_removes_any_multi_label_row(category):
    _, reasons, _ = screen([row(1, category=category), *many("ID", 6)])
    assert reasons[1] == "multiple_labels"


@pytest.mark.parametrize("category", ["UD", "OSD", "TD", "ID-HtF", "NDOD", "NDOI", "id"])
def test_rule_4_removes_excluded_labels(category):
    _, reasons, _ = screen([row(1, category=category), *many("ID", 6)])
    assert reasons[1] == "category_excluded"


def test_rule_5_needs_six_eligible_prs_in_the_folded_stratum():
    thin = many("NOD", 5, start=100)
    folded = [*many("OD-Vic", 3, start=200), *many("OD-Brit", 2, start=300), *many("OD", 1, 400)]
    eligible, reasons, _ = screen([*thin, *folded])
    assert {reasons[r.idoft_row] for r in thin} == {"stratum_too_thin"}
    assert {e.stratum for e in eligible} == {"OD"}
    assert len(eligible) == 6


def test_rule_5_counts_prs_not_rows():
    rows = [row(100 + i, test=f"p.C{i}.m", category="NIO", pr=1) for i in range(10)]
    _, reasons, _ = screen(rows)
    assert set(reasons.values()) == {"stratum_too_thin"}


def test_eligible_rows_carry_stratum_and_a_case_folded_class_key():
    eligible, _, _ = screen(many("OD-Vic", 6, project="Apache/Struts"))
    first = eligible[0]
    assert first.stratum == "OD"
    assert first.class_key == ("https://github.com/apache/struts", "p.C100")
    assert first.project == "https://github.com/apache/struts"


@pytest.mark.parametrize(
    "change",
    [
        {"sha_detected": "A" * 40},
        {"sha_detected": "a" * 39},
        {"pr_url": "https://github.com/other/r/pull/1"},
        {"pr_url": "https://github.com/o/r/pull/x"},
        {"pr_url": "http://github.com/o/r/pull/1"},
        {"project_url": "https://gitlab.com/o/r"},
        {"category": ""},
        {"test_name": "m"},
    ],
)
def test_a_malformed_accepted_row_stops_the_screen_naming_it(change):
    from dataclasses import replace

    bad = replace(row(7), **change)
    with pytest.raises(ValueError, match="row 7"):
        screen([bad, *many("ID", 6)])


def test_a_malformed_row_that_is_not_accepted_is_only_counted():
    from dataclasses import replace

    bad = replace(row(7, status="Opened"), sha_detected="x")
    _, _, not_accepted = screen([bad, *many("ID", 6)])
    assert not_accepted == 1


def test_one_pr_spelled_two_ways_stops_the_screen():
    rows = [*many("ID", 6), row(1, pr=100, project="O/R", test="p.X.m")]
    with pytest.raises(ValueError, match="spelled"):
        screen(rows)
