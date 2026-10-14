"""Checks the builder against the counts ADR 0006 records for the pinned IDoFT file.

The file is not committed (IDoFT declares no license); fetch it with
`uv run python -m rootflake.corpus fetch-idoft`. Without it these tests skip.
"""

import pytest

from rootflake.corpus import idoft
from rootflake.sandbox.workspace import default_root

PATH = idoft.cache_path(default_root(), idoft.PINNED_COMMIT)

pytestmark = [
    pytest.mark.idoft,
    pytest.mark.skipif(not PATH.exists(), reason="pinned pr-data.csv not cached"),
]


@pytest.fixture(scope="module")
def rows():
    return idoft.parse(PATH.read_bytes())


def test_row_and_accepted_counts(rows):
    assert len(rows) == 8075
    assert sum(row.status == "Accepted" for row in rows) == 2303


def test_every_accepted_test_name_has_a_class(rows):
    for row in rows:
        if row.status == "Accepted":
            assert idoft.derive_test_class(row.test_name)


def test_rules_one_to_five_match_the_adr(rows):
    from collections import Counter

    from rootflake.corpus.funnel import screen

    eligible, reasons, not_accepted = screen(rows)
    assert not_accepted == 5772
    assert Counter(reasons.values()) == {
        "duplicate_test": 18,
        "multiple_labels": 15,
        "category_excluded": 3,
        "stratum_too_thin": 4,
    }
    by_stratum = Counter(e.stratum for e in eligible)
    prs = {s: len({e.row.pr_url for e in eligible if e.stratum == s}) for s in by_stratum}
    assert by_stratum == {"ID": 1754, "OD": 338, "NIO": 143, "NOD": 28}
    assert prs == {"ID": 527, "OD": 70, "NIO": 30, "NOD": 7}
