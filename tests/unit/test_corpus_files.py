import hashlib
import json

import pytest

from rootflake.corpus.files import dumps_line, funnel_counts, funnel_rows, write_version
from rootflake.corpus.idoft import Row
from rootflake.corpus.walk import Drawn, FixDiff, WalkResult

FIX = FixDiff("b" * 40, "c" * 40, "2020-01-01T00:00:00Z", "d" * 64)


def drawn(n, split, name="p.C.m"):
    row = Row(n, "https://github.com/o/r", "a" * 40, ".", name, "OD-Vic", "Accepted",
              "https://github.com/o/r/pull/1")  # fmt: skip
    return Drawn(row, "OD", FIX, "11", "e" * 64, split)


def result():
    walk = WalkResult()
    walk.drawn = [drawn(9, "test", 'p.Ü.m[1, "x"]'), drawn(3, "test"), drawn(5, "dev")]
    walk.reasons = {
        9: ("drawn", "11"),
        3: ("drawn", "11"),
        5: ("drawn", "11"),
        7: ("build_failed", None),
    }
    walk.strata = {9: "OD", 3: "OD", 5: "OD", 7: "OD"}
    return walk


def test_a_line_is_sorted_compact_and_keeps_non_ascii():
    assert dumps_line({"b": 1, "a": 'Ü,"'}) == '{"a":"Ü,\\"","b":1}'


def test_split_files_are_sorted_by_row_with_a_final_newline(tmp_path):
    out = tmp_path / "v1"
    funnel = funnel_rows({2: "duplicate_test"}, {}, result())
    write_version(out, result().drawn, funnel, {"corpus_version": "corpus-v1"})
    lines = (out / "test.jsonl").read_bytes().split(b"\n")
    assert lines[-1] == b""
    assert [json.loads(line)["idoft_row"] for line in lines[:-1]] == [3, 9]
    assert json.loads(lines[1])["test_name"] == 'p.Ü.m[1, "x"]'
    assert (out / "dev.jsonl").read_text().count("\n") == 1
    assert b"\r" not in (out / "test.jsonl").read_bytes()
    assert not (out / "test.jsonl").read_bytes().startswith(b"\xef\xbb\xbf")


def test_the_exact_bytes_of_one_split_row(tmp_path):
    out = tmp_path / "v1"
    write_version(out, [drawn(5, "dev")], [], {})
    assert (out / "dev.jsonl").read_text() == (
        '{"category":"OD-Vic","fix_diff_sha256":"' + "d" * 64 + '","idoft_row":5,'
        '"jdk":"11","module_path":".","pr_head_sha":"' + "b" * 40 + '",'
        '"pr_merge_commit_sha":"' + "c" * 40 + '","pr_merged_at":"2020-01-01T00:00:00Z",'
        '"pr_url":"https://github.com/o/r/pull/1","project_url":"https://github.com/o/r",'
        '"rank_key":"' + "e" * 64 + '","sha_detected":"' + "a" * 40 + '","split":"dev",'
        '"stratum":"OD","test_class":"p.C","test_name":"p.C.m"}\n'
    )


def test_funnel_rows_join_screen_and_walk_reasons_by_row():
    rows = funnel_rows({2: "duplicate_test", 4: "stratum_too_thin"}, {4: "TZD"}, result())
    assert rows == [
        {"idoft_row": 2, "reason": "duplicate_test", "stratum": None, "detail": None},
        {"idoft_row": 3, "reason": "drawn", "stratum": "OD", "detail": "11"},
        {"idoft_row": 4, "reason": "stratum_too_thin", "stratum": "TZD", "detail": None},
        {"idoft_row": 5, "reason": "drawn", "stratum": "OD", "detail": "11"},
        {"idoft_row": 7, "reason": "build_failed", "stratum": "OD", "detail": None},
        {"idoft_row": 9, "reason": "drawn", "stratum": "OD", "detail": "11"},
    ]


def test_a_row_with_two_reasons_is_refused():
    with pytest.raises(ValueError):
        funnel_rows({3: "duplicate_test"}, {}, result())


def test_funnel_counts_overall_and_per_stratum():
    rows = funnel_rows({2: "duplicate_test"}, {}, result())
    assert funnel_counts(rows, not_accepted=10) == {
        "overall": {"status_not_accepted": 10, "duplicate_test": 1, "drawn": 3, "build_failed": 1},
        "per_stratum": {"OD": {"drawn": 3, "build_failed": 1}},
    }


def test_the_manifest_records_each_file_hash(tmp_path):
    out = tmp_path / "v1"
    hashes = write_version(out, result().drawn, [], {"corpus_version": "corpus-v1"})
    manifest = json.loads((out / "manifest.json").read_text())
    for name in ("dev.jsonl", "test.jsonl", "funnel.jsonl"):
        digest = hashlib.sha256((out / name).read_bytes()).hexdigest()
        assert hashes[name] == digest == manifest["files"][name]
    assert (out / "manifest.json").read_text().startswith('{\n  "corpus_version"')


def test_an_existing_version_is_never_overwritten(tmp_path):
    out = tmp_path / "v1"
    out.mkdir()
    (out / "test.jsonl").write_text("frozen\n")
    with pytest.raises(FileExistsError):
        write_version(out, result().drawn, [], {})
    assert (out / "test.jsonl").read_text() == "frozen\n"


def test_two_writes_of_the_same_result_are_byte_identical(tmp_path):
    funnel = funnel_rows({2: "duplicate_test"}, {}, result())
    for name in ("a", "b"):
        write_version(tmp_path / name, result().drawn, funnel, {"x": [1, "Ü"]})
    for file in ("dev.jsonl", "test.jsonl", "funnel.jsonl", "manifest.json"):
        assert (tmp_path / "a" / file).read_bytes() == (tmp_path / "b" / file).read_bytes()
