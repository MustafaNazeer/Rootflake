import pytest

from rootflake.corpus.javalevel import java_level, parse_level


def pom(properties="", plugins="", managed=""):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <properties>{properties}</properties>
  <build>
    <pluginManagement><plugins>{managed}</plugins></pluginManagement>
    <plugins>{plugins}</plugins>
  </build>
</project>""".encode()


def compiler(config, artifact="maven-compiler-plugin"):
    return (
        f"<plugin><artifactId>{artifact}</artifactId>"
        f"<configuration>{config}</configuration></plugin>"
    )


@pytest.mark.parametrize(
    ("text", "level"),
    [("1.8", 8), ("8", 8), ("1.7", 7), ("1.6", 6), ("11", 11), ("17", 17), ("21", 21),
     (" 11 ", 11), ("1.8.0", None), ("eleven", None), ("", None), ("${x}", None)],
)  # fmt: skip
def test_levels_are_read_as_java_major_versions(text, level):
    assert parse_level(text) == level


def test_nothing_declared_is_none():
    assert java_level([pom()]) is None


@pytest.mark.parametrize("name", ["release", "target", "source"])
def test_each_property_is_read(name):
    assert java_level([pom(f"<maven.compiler.{name}>11</maven.compiler.{name}>")]) == 11


def test_release_beats_target_and_target_beats_source():
    props = (
        "<maven.compiler.source>1.6</maven.compiler.source>"
        "<maven.compiler.target>1.7</maven.compiler.target>"
    )
    assert java_level([pom(props)]) == 7
    assert java_level([pom(props + "<maven.compiler.release>11</maven.compiler.release>")]) == 11


def test_plugin_configuration_overrides_the_property_for_the_same_parameter():
    poms = [
        pom("<maven.compiler.target>1.6</maven.compiler.target>", compiler("<target>11</target>"))
    ]
    assert java_level(poms) == 11


def test_a_release_property_beats_a_plugin_target():
    poms = [
        pom("<maven.compiler.release>17</maven.compiler.release>", compiler("<target>8</target>"))
    ]
    assert java_level(poms) == 17


def test_plugin_management_counts_and_plugins_override_it():
    assert java_level([pom(managed=compiler("<source>1.8</source>"))]) == 8
    both = pom(plugins=compiler("<source>11</source>"), managed=compiler("<source>1.8</source>"))
    assert java_level([both]) == 11


def test_other_plugins_are_ignored():
    assert (
        java_level([pom(plugins=compiler("<target>21</target>", "maven-javadoc-plugin"))]) is None
    )


def test_properties_are_resolved_through_indirection():
    props = (
        "<java.version>1.8</java.version><v>${java.version}</v>"
        "<maven.compiler.target>${v}</maven.compiler.target>"
    )
    assert java_level([pom(props)]) == 8


def test_an_unresolvable_property_is_none():
    assert java_level([pom("<maven.compiler.target>${jdk}</maven.compiler.target>")]) is None


def test_a_property_cycle_is_none():
    props = "<a>${b}</a><b>${a}</b><maven.compiler.target>${a}</maven.compiler.target>"
    assert java_level([pom(props)]) is None


def test_the_module_inherits_and_overrides_its_parents():
    root = pom(
        "<java.version>1.8</java.version>"
        "<maven.compiler.target>${java.version}</maven.compiler.target>"
    )
    module = pom("<java.version>11</java.version>")
    assert java_level([module, root]) == 11
    assert java_level([pom(), root]) == 8


@pytest.mark.parametrize(
    "bad",
    [
        b'<?xml version="1.0"?><!DOCTYPE p [<!ENTITY x "11">]><project/>',
        b"<project><properties>",
        b"\x00\x01",
    ],
)
def test_an_unreadable_pom_anywhere_in_the_chain_is_none(bad):
    assert java_level([pom("<maven.compiler.release>11</maven.compiler.release>"), bad]) is None


def test_exponential_property_expansion_is_cut_off_quickly():
    import time

    props = "<p0>1</p0>" + "".join(
        f"<p{n}>{('${p' + str(n - 1) + '}') * 6}</p{n}>" for n in range(1, 10)
    )
    props += "<maven.compiler.target>${p9}</maven.compiler.target>"
    started = time.monotonic()
    assert java_level([pom(props)]) is None
    assert time.monotonic() - started < 1
