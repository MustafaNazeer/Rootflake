"""The Java level a project declares in its POMs, for rule 10 of the corpus funnel.

Reads `maven.compiler.release`, `target`, and `source` from properties and from the
maven-compiler-plugin configuration (plugin configuration overriding the property for the
same parameter, a release beating a target, a target beating a source), merging the module's
POM with the parents Maven would read from the checkout: a parent is followed only through
the default `../pom.xml` and only when its groupId, artifactId, and version match the
declared parent. Anything undeclared, unresolvable, or unreadable gives None: rule 10 then
lets the row through and the sandbox build decides. Profiles, executions, test levels, and
parents outside the repository are not read.
"""

import re
from dataclasses import dataclass, field
from xml.parsers import expat

from rootflake.sandbox.xmlsafe import RefusedXml, new_parser

PARAMETERS = ("release", "target", "source")
PLUGIN = "maven-compiler-plugin"
COORDINATES = ("groupId", "artifactId", "version")
MAX_DEPTH = 10
MAX_VALUE = 256
MAX_XML_DEPTH = 64
DEFAULT_PARENT_PATHS = ("..", "../pom.xml")

_LEVEL_RE = re.compile(r"1\.([1-9])|([1-9][0-9]?)")
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


@dataclass
class _Pom:
    properties: dict[str, str] = field(default_factory=dict)
    managed: dict[str, str] = field(default_factory=dict)
    plugins: dict[str, str] = field(default_factory=dict)
    coords: dict[str, str] = field(default_factory=dict)
    parent: dict[str, str] | None = None

    def inherited(self, name: str) -> str | None:
        return self.coords.get(name) or (self.parent or {}).get(name)


def _read(pom: bytes) -> _Pom:
    result = _Pom()
    properties = result.properties
    sections = {"plugins": result.plugins, "managed": result.managed}
    stack: list[str] = []
    text: list[str] = []
    plugin: dict[str, str] = {}

    def start(name: str, _attrs: dict[str, str]) -> None:
        if len(stack) >= MAX_XML_DEPTH:
            raise ValueError(f"POM nested deeper than {MAX_XML_DEPTH} elements")
        stack.append(name)
        if tuple(stack) == ("project", "parent"):
            result.parent = {}
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
        elif len(path) == 2 and path[0] == "project" and path[1] in COORDINATES:
            result.coords[path[1]] = value
        elif len(path) == 3 and path[:2] == ("project", "parent") and result.parent is not None:
            result.parent[path[2]] = value
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
    return result


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


def _parent_is(child: _Pom, candidate: _Pom) -> bool:
    declared = child.parent
    if declared is None:
        return False
    path = declared.get("relativePath")
    if path is not None and path.rstrip("/") not in DEFAULT_PARENT_PATHS:
        return False
    return all(declared.get(name) == candidate.inherited(name) for name in COORDINATES)


def java_level(poms: list[bytes | None]) -> int | None:
    """`poms` runs from the module's own POM up the directory tree; None marks a missing file."""
    chain: list[_Pom] = []
    try:
        for pom in poms:
            if pom is None:
                break
            current = _read(pom)
            if chain and not _parent_is(chain[-1], current):
                break
            chain.append(current)
    except RefusedXml, expat.ExpatError, ValueError:
        return None
    if not chain:
        return None
    properties: dict[str, str] = {}
    config: dict[str, str] = {}
    for current in reversed(chain):
        properties.update(current.properties)
        config.update(current.managed)
        config.update(current.plugins)
    for parameter in PARAMETERS:
        raw = config.get(parameter, properties.get(f"maven.compiler.{parameter}"))
        if raw is not None:
            resolved = _resolve(raw, properties)
            return None if resolved is None else parse_level(resolved)
    return None
