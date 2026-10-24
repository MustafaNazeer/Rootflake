"""A Java lexer for the shortcut checker (ADR 0007 Decision 3).

Unicode escapes are translated first, as JLS 3.3 requires, so `// \\u000d @Disabled` is code
here as it is to javac. Only CR, LF and CRLF end a line (JLS 3.4); whitespace is the JLS 3.6
set and anything else outside a token is unparsable. Comments are dropped. Every `>` is its own
token so nested generics close without the parser splitting `>>`. Lines are reported against
the raw file, so an escaped line break does not shift them. An identifier holding a character
javac ignores in names (JLS 3.8) is unparsable, since names are never normalized. The scanner
is a single pass with no backtracking.
"""

import re
import unicodedata
from dataclasses import dataclass

KEYWORDS = frozenset(
    "abstract assert boolean break byte case catch char class const continue default do double "
    "else enum extends final finally float for goto if implements import instanceof int "
    "interface long native new package private protected public return short static strictfp "
    "super switch synchronized this throw throws transient try void volatile while "
    "true false null".split()
)
_OPERATORS = sorted(
    "( ) { } [ ] ; , . ... @ :: = < ! ~ ? : -> == <= != && || ++ -- + - * / & | ^ % << "
    "+= -= *= /= &= |= ^= %= <<=".split(),
    key=len,
    reverse=True,
)
_HEX = frozenset("0123456789abcdefABCDEF")
_SPACE = frozenset(" \t\x0c\r\n")
_IDENT_START = frozenset(["Lu", "Ll", "Lt", "Lm", "Lo", "Nl", "Sc", "Pc"])
_IDENT_PART = _IDENT_START | {"Nd", "Mn", "Mc"}
_ASCII_IDENT = re.compile(r"[A-Za-z0-9_$]+")
_NUMBER_BODY = re.compile(r"[0-9A-Za-z_.]*")


class Unparsable(Exception):
    def __init__(self, reason: str, line: int):
        super().__init__(f"line {line}: {reason}")
        self.reason = reason
        self.line = line


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    line: int


def _raw_lines(raw: str) -> list[int]:
    lines, line, i = [], 1, 0
    while i < len(raw):
        lines.append(line)
        if raw[i] == "\r" and i + 1 < len(raw) and raw[i + 1] == "\n":
            lines.append(line)
            i += 1
            line += 1
        elif raw[i] in "\r\n":
            line += 1
        i += 1
    lines.append(line)
    return lines


def _translate(raw: str) -> tuple[str, list[int]]:
    """JLS 3.3: returns the translated text and, per character, the raw offset it came from."""
    out: list[str] = []
    origin: list[int] = []
    i, backslashes = 0, 0
    lines = None
    while i < len(raw):
        char = raw[i]
        if char == "\\" and backslashes % 2 == 0 and i + 1 < len(raw) and raw[i + 1] == "u":
            j = i + 1
            while j < len(raw) and raw[j] == "u":
                j += 1
            digits = raw[j : j + 4]
            if len(digits) != 4 or not set(digits) <= _HEX:
                lines = lines or _raw_lines(raw)
                raise Unparsable("malformed unicode escape", lines[i])
            out.append(chr(int(digits, 16)))
            origin.append(i)
            i, backslashes = j + 4, 0
            continue
        backslashes = backslashes + 1 if char == "\\" else 0
        out.append(char)
        origin.append(i)
        i += 1
    return "".join(out), origin


def _is_start(char: str) -> bool:
    return char == "$" or char == "_" or unicodedata.category(char) in _IDENT_START


def _is_ignorable(char: str) -> bool:
    """Character.isIdentifierIgnorable: javac drops these when comparing identifiers."""
    code = ord(char)
    return (code <= 0x08 or 0x0E <= code <= 0x1B or 0x7F <= code <= 0x9F) or unicodedata.category(
        char
    ) == "Cf"


def _is_part(char: str) -> bool:
    return _is_start(char) or unicodedata.category(char) in _IDENT_PART or _is_ignorable(char)


class _Scanner:
    def __init__(self, raw: str):
        self.text, self.origin = _translate(raw)
        self.lines = _raw_lines(raw)
        self.tokens: list[Token] = []

    def line(self, at: int) -> int:
        return self.lines[self.origin[at]] if at < len(self.origin) else self.lines[-1]

    def fail(self, reason: str, at: int) -> Unparsable:
        return Unparsable(reason, self.line(at))

    def run(self) -> list[Token]:
        text, i, n = self.text, 0, len(self.text)
        while i < n:
            char = text[i]
            if char in _SPACE:
                i += 1
            elif char == "\x1a" and i == n - 1:
                i += 1
            elif text.startswith("//", i):
                i = self.line_comment(i)
            elif text.startswith("/*", i):
                end = text.find("*/", i + 2)
                if end < 0:
                    raise self.fail("unterminated comment", i)
                i = end + 2
            elif text.startswith('"""', i):
                i = self.text_block(i)
            elif char == '"' or char == "'":
                i = self.quoted(i, char)
            elif (
                char.isdigit()
                and char.isascii()
                or (char == "." and i + 1 < n and text[i + 1].isascii() and text[i + 1].isdigit())
            ):
                i = self.number(i)
            elif _is_start(char):
                i = self.identifier(i)
            elif char == ">":
                op = ">=" if text.startswith(">=", i) and not text.startswith(">>", i) else ">"
                self.add("op", op, i)
                i += len(op)
            else:
                for op in _OPERATORS:
                    if text.startswith(op, i):
                        self.add("op", op, i)
                        i += len(op)
                        break
                else:
                    raise self.fail(f"unexpected character U+{ord(char):04X}", i)
        return self.tokens

    def add(self, kind: str, text: str, at: int) -> None:
        self.tokens.append(Token(kind, text, self.line(at)))

    def line_comment(self, i: int) -> int:
        n = len(self.text)
        while i < n and self.text[i] not in "\r\n":
            i += 1
        return i

    def quoted(self, i: int, quote: str) -> int:
        text, j = self.text, i + 1
        while j < len(text):
            char = text[j]
            if char == "\\" and j + 1 < len(text) and text[j + 1] not in "\r\n":
                j += 2
                continue
            if char in "\r\n" or char == "\\":
                break
            if char == quote:
                self.add("string" if quote == '"' else "char", text[i : j + 1], i)
                return j + 1
            j += 1
        raise self.fail("unterminated literal", i)

    def text_block(self, i: int) -> int:
        text, j = self.text, i + 3
        while j < len(text) and text[j] in " \t\x0c":
            j += 1
        if j >= len(text) or text[j] not in "\r\n":
            raise self.fail("text block opening must end its line", i)
        while j < len(text):
            if text[j] == "\\":
                j += 2
                continue
            if text.startswith('"""', j):
                self.add("text_block", text[i : j + 3], i)
                return j + 3
            j += 1
        raise self.fail("unterminated text block", i)

    def number(self, i: int) -> int:
        text, j = self.text, i
        while True:
            j = _NUMBER_BODY.match(text, j).end()
            exponent = text[j - 1 : j] in ("e", "E", "p", "P")
            hex_literal = text[i : i + 2].lower() == "0x"
            if (
                j < len(text)
                and text[j] in "+-"
                and exponent
                and (text[j - 1] in "pP" or not hex_literal)
            ):
                j += 1
                continue
            break
        self.add("number", text[i:j], i)
        return j

    def identifier(self, i: int) -> int:
        text, j = self.text, i
        while j < len(text):
            match = _ASCII_IDENT.match(text, j)
            if match:
                j = match.end()
            elif not text[j].isascii() and _is_part(text[j]):
                if _is_ignorable(text[j]):
                    raise self.fail("identifier holds a character javac ignores", j)
                j += 1
            else:
                break
        word = text[i:j]
        self.add("keyword" if word in KEYWORDS else "ident", word, i)
        return j


def tokenize(source: str) -> list[Token]:
    return _Scanner(source).run()
