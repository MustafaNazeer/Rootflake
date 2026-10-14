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
