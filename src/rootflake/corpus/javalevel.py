"""The Java level a project declares in its POMs, for rule 10 of the corpus funnel.

Reads `maven.compiler.release`, `target`, and `source` from properties and from the
maven-compiler-plugin configuration (plugin configuration overriding the property for the
same parameter, a release beating a target, a target beating a source), merging POMs from
the root down to the module. Anything undeclared, unresolvable, or unreadable gives None:
rule 10 then lets the row through and the sandbox build decides. Profiles and parents
outside the repository are not read.
"""

import re
from xml.parsers import expat

from rootflake.sandbox.xmlsafe import RefusedXml, new_parser

PARAMETERS = ("release", "target", "source")
PLUGIN = "maven-compiler-plugin"
MAX_DEPTH = 10
MAX_VALUE = 256

_LEVEL_RE = re.compile(r"1\.([5-9])|([1-9][0-9]?)")
_REFERENCE_RE = re.compile(r"\$\{([^}]*)\}")
_PLUGINS = {
    ("project", "build", "plugins", "plugin"): "plugins",
    ("project", "build", "pluginManagement", "plugins", "plugin"): "managed",
}


def parse_level(text: str) -> int | None:
    match = _LEVEL_RE.fullmatch(text.strip())
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _read(pom: bytes) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    properties: dict[str, str] = {}
    sections: dict[str, dict[str, str]] = {"plugins": {}, "managed": {}}
    stack: list[str] = []
    text: list[str] = []
    plugin: dict[str, str] = {}

    def start(name: str, _attrs: dict[str, str]) -> None:
        stack.append(name)
        text.clear()
        if tuple(stack) in _PLUGINS:
            plugin.clear()

    def data(chunk: str) -> None:
        text.append(chunk)

    def end(_name: str) -> None:
        path = tuple(stack)
        value = "".join(text).strip()
        if len(path) == 3 and path[:2] == ("project", "properties"):
            properties[path[2]] = value
        elif path[:-1] in _PLUGINS and path[-1] == "artifactId":
            plugin["artifactId"] = value
        elif path[:-2] in _PLUGINS and path[-2] == "configuration" and path[-1] in PARAMETERS:
            plugin[path[-1]] = value
        elif path in _PLUGINS and plugin.get("artifactId") == PLUGIN:
            sections[_PLUGINS[path]].update(
                {key: plugin[key] for key in PARAMETERS if key in plugin}
            )
        stack.pop()
        text.clear()

    parser = new_parser()
    parser.StartElementHandler = start
    parser.CharacterDataHandler = data
    parser.EndElementHandler = end
    parser.Parse(pom, True)
    return properties, sections["managed"], sections["plugins"]


def _resolve(value: str, properties: dict[str, str]) -> str | None:
    """Expands `${name}` references; a hostile POM can nest them to grow exponentially, so
    the value is capped at every step."""
    for _ in range(MAX_DEPTH):
        if len(value) > MAX_VALUE:
            return None
        names = _REFERENCE_RE.findall(value)
        if not names:
            return value
        if any(name not in properties for name in names):
            return None
        value = _REFERENCE_RE.sub(lambda match: properties[match.group(1)][: MAX_VALUE + 1], value)
    return None


def java_level(poms: list[bytes]) -> int | None:
    """`poms` runs from the module's own POM up to the repository root."""
    properties: dict[str, str] = {}
    config: dict[str, str] = {}
    for pom in reversed(poms):
        try:
            found, managed, plugins = _read(pom)
        except RefusedXml, expat.ExpatError, ValueError:
            return None
        properties.update(found)
        config.update(managed)
        config.update(plugins)
    for parameter in PARAMETERS:
        raw = config.get(parameter, properties.get(f"maven.compiler.{parameter}"))
        if raw is not None:
            resolved = _resolve(raw, properties)
            return None if resolved is None else parse_level(resolved)
    return None
