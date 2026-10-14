import hashlib

import pytest

from rootflake.corpus import idoft
from rootflake.corpus.idoft import HEADER, Row, derive_test_class, fetch, parse

HEADER_LINE = ",".join(HEADER)


def csv_bytes(*lines):
    return ("\n".join([HEADER_LINE, *lines]) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


ROW = (
    "https://github.com/o/r,"
    + "a" * 40
    + ',core,"p.C.m[1, ""x""]",ID,Accepted,https://github.com/o/r/pull/7,'
)


def test_rows_keep_every_field_verbatim_and_count_from_one():
    data = csv_bytes(ROW)
    (row,) = parse(data, digest(data))
    assert row == Row(
        idoft_row=1,
        project_url="https://github.com/o/r",
        sha_detected="a" * 40,
        module_path="core",
        test_name='p.C.m[1, "x"]',
        category="ID",
        status="Accepted",
        pr_url="https://github.com/o/r/pull/7",
    )


def test_a_hash_mismatch_is_refused():
    data = csv_bytes(ROW)
    with pytest.raises(ValueError, match="SHA256"):
        parse(data, "0" * 64)


def test_a_different_header_is_refused():
    data = csv_bytes(ROW).replace(b"Notes", b"Note", 1)
    with pytest.raises(ValueError, match="header"):
        parse(data, digest(data))


def test_a_row_with_the_wrong_column_count_names_the_row():
    data = csv_bytes(ROW, "a,b,c")
    with pytest.raises(ValueError, match="row 2"):
        parse(data, digest(data))


def test_a_byte_order_mark_is_refused():
    data = b"\xef\xbb\xbf" + csv_bytes(ROW)
    with pytest.raises(ValueError, match="header"):
        parse(data, digest(data))


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.b.C.m", "a.b.C"),
        ("a.b.C.m[1]", "a.b.C"),
        ("a.b.C$Inner.m", "a.b.C$Inner"),
        ("a.b.Feature.Scenario: adds two numbers", "a.b.Feature"),
        ("a.b.C.m(int, String)", "a.b.C"),
        ("C.m", "C"),
        ('a.b.C."doTest[[x, y]]"', "a.b.C"),
    ],
)
def test_the_class_is_the_name_before_the_method(name, expected):
    assert derive_test_class(name) == expected


@pytest.mark.parametrize("name", ["m", "[1]", "", "a..m", "1a.m"])
def test_a_name_without_a_class_is_refused(name):
    with pytest.raises(ValueError):
        derive_test_class(name)


class Opener:
    def __init__(self, body):
        self.body = body
        self.urls = []

    def __call__(self, url, timeout):
        self.urls.append(url)
        return self.body


def test_fetch_writes_only_bytes_with_the_expected_hash(tmp_path):
    data = csv_bytes(ROW)
    opener = Opener(data)
    path = fetch(tmp_path, "c" * 40, digest(data), opener)
    assert path == idoft.cache_path(tmp_path, "c" * 40)
    assert path.read_bytes() == data
    raw = "https://raw.githubusercontent.com/TestingResearchIllinois/idoft/"
    assert opener.urls == [raw + "c" * 40 + "/pr-data.csv"]


def test_fetch_of_the_wrong_bytes_writes_nothing(tmp_path):
    with pytest.raises(ValueError, match="SHA256"):
        fetch(tmp_path, "c" * 40, "0" * 64, Opener(b"x"))
    assert not idoft.cache_path(tmp_path, "c" * 40).exists()
    assert not any(tmp_path.rglob("*.part"))


@pytest.mark.parametrize("commit", ["main", "c" * 39, "../" + "c" * 37, "C" * 40])
def test_fetch_needs_a_full_commit_sha(tmp_path, commit):
    with pytest.raises(ValueError):
        fetch(tmp_path, commit, "0" * 64, Opener(b""))
