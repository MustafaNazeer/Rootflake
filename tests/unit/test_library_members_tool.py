import importlib.util
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "library_members.py"
TABLE = ROOT / "src" / "rootflake" / "gate" / "checker" / "library_members.json"


def load_tool():
    spec = importlib.util.spec_from_file_location("library_members_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SOURCES = {
    "lib/Base.java": (
        "package lib; public class Base { public static void inherited() {} "
        "public static int BASE_FIELD = 1; static void packagePrivate() {} "
        "public void instance() {} }"
    ),
    "lib/Constants.java": (
        "package lib; public interface Constants { int CONSTANT = 1; "
        "static void notInherited() {} }"
    ),
    "lib/Top.java": (
        "package lib; public class Top extends Base implements Constants { "
        "public static void own() {} private static void hidden() {} "
        'public static final String OWN_FIELD = "x"; }'
    ),
}


@pytest.mark.skipif(shutil.which("javac") is None, reason="needs a JDK to compile class files")
def test_members_follow_the_static_import_on_demand_rules(tmp_path):
    for rel, text in SOURCES.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    out = tmp_path / "classes"
    subprocess.run(
        ["javac", "-d", str(out), *(str(tmp_path / rel) for rel in SOURCES)],
        check=True, capture_output=True,
    )  # fmt: skip
    jar_path = tmp_path / "lib.jar"
    with zipfile.ZipFile(jar_path, "w") as jar:
        for file in out.rglob("*.class"):
            jar.write(file, file.relative_to(out).as_posix())
    with zipfile.ZipFile(jar_path) as jar:
        methods, fields = load_tool().members_of("lib.Top", jar)
    assert methods == ["inherited", "own"]
    assert fields == ["BASE_FIELD", "CONSTANT", "OWN_FIELD"]


def test_every_table_entry_names_a_pinned_maven_central_jar():
    table = json.loads(TABLE.read_text(encoding="utf-8"))
    for fqn, entry in table.items():
        assert entry["source"].startswith("https://repo1.maven.org/maven2/"), fqn
        assert f"/{entry['version']}/" in entry["source"], fqn
        assert entry["methods"] == sorted(set(entry["methods"])), fqn
        assert entry["fields"] == sorted(set(entry["fields"])), fqn
