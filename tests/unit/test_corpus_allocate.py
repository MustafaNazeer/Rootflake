import pytest

from rootflake.corpus.allocate import largest_remainder, plan, reallocate


def test_the_adr_illustrative_allocation_is_reproduced_exactly():
    assert plan({"ID": 527, "OD": 70, "NIO": 30, "NOD": 7}) == {
        "ID": (35, 70),
        "OD": (8, 15),
        "NIO": (5, 10),
        "NOD": (2, 5),
    }


@pytest.mark.parametrize(
    "prs",
    [
        {"ID": 527, "OD": 70, "NIO": 30, "NOD": 7},
        {"A": 150},
        {"A": 1000},
        {"A": 13, "B": 400, "C": 6, "D": 51},
    ],
)
def test_totals_are_one_fifty_and_fifty(prs):
    result = plan(prs)
    assert sum(d + t for d, t in result.values()) == 150
    assert sum(d for d, _ in result.values()) == 50
    for stratum, (dev, test) in result.items():
        assert 0 <= dev and 0 <= test and dev + test <= prs[stratum]


def test_a_stratum_below_the_floor_gets_all_its_prs():
    result = plan({"A": 500, "B": 7})
    assert sum(result["B"]) == 7


@pytest.mark.parametrize("prs", [{"A": 6, "B": 6}, {"A": 40, "B": 30}, {"A": 149}])
def test_fewer_eligible_prs_than_the_corpus_size_is_refused(prs):
    with pytest.raises(ValueError, match="150"):
        plan(prs)


def test_a_capped_stratum_is_fixed_and_the_rest_recomputed():
    assert largest_remainder(10, {"a": 1, "b": 1}, {"a": 10, "b": 2}) == {"a": 8, "b": 2}


def test_capping_can_cascade():
    assert largest_remainder(10, {"a": 1, "b": 9}, {"a": 5, "b": 3}) == {"a": 5, "b": 3}


def test_leftover_slots_go_to_the_largest_fractions():
    assert largest_remainder(10, {"a": 1, "b": 1, "c": 1}, {s: 10 for s in "abc"}) == {
        "a": 4,
        "b": 3,
        "c": 3,
    }


def test_ties_on_the_fraction_go_to_the_name_first_in_code_point_order():
    assert largest_remainder(1, {"b": 1, "a": 1, "B": 1}, {s: 5 for s in "abB"}) == {
        "B": 1,
        "a": 0,
        "b": 0,
    }


def test_zero_weight_gets_nothing():
    assert largest_remainder(5, {"a": 0, "b": 3}, {"a": 9, "b": 9}) == {"a": 0, "b": 5}


@pytest.mark.parametrize("total", [-1])
def test_a_negative_total_is_refused(total):
    with pytest.raises(ValueError):
        largest_remainder(total, {"a": 1}, {"a": 1})


def test_reallocation_shares_lost_slots_by_remaining_prs_capped_at_them():
    assert reallocate(5, {"X": 3, "Y": 1}) == {"X": 3, "Y": 1}
    assert reallocate(4, {"X": 30, "Y": 10}) == {"X": 3, "Y": 1}


def test_reallocation_with_no_recipient_moves_nothing():
    assert reallocate(3, {}) == {}
