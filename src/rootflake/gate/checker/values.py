"""Folding constant expressions written as space separated tokens (ADR 0007 Decisions 5 and 7).

Literals, arithmetic, casts and `TimeUnit` conversions such as `TimeUnit.SECONDS.toMillis(10)`
fold; a name, plain or qualified, folds only through the lookup the caller gives, which knows
which names hold a constant.
"""

from collections.abc import Callable

TIME_UNITS = {
    "NANOSECONDS": 1e-6, "MICROSECONDS": 1e-3, "MILLISECONDS": 1.0, "SECONDS": 1e3,
    "MINUTES": 6e4, "HOURS": 3.6e6, "DAYS": 8.64e7,
}  # fmt: skip
CONVERSIONS = {
    "toNanos": 1e-6, "toMicros": 1e-3, "toMillis": 1.0, "toSeconds": 1e3, "toMinutes": 6e4,
    "toHours": 3.6e6, "toDays": 8.64e7,
}  # fmt: skip
_CASTS = frozenset(["int", "long", "short", "byte", "double", "float"])
_UNIT_QUALIFIERS = ([], ["TimeUnit"], ["java", "util", "concurrent", "TimeUnit"])

Lookup = Callable[[str], float | None]


def literal(word: str) -> float | None:
    text = word.replace("_", "")
    if not text or not (text[0].isdigit() or (text[0] == "." and text[1:2].isdigit())):
        return None
    try:
        if text[:2].lower() == "0x":
            return float(int(text[2:].rstrip("lL"), 16))
        return float(text.rstrip("lLdDfF"))
    except ValueError:
        return None


def _name_word(word: str) -> bool:
    return word[:1].isalpha() or word[:1] in ("_", "$")


class _Folder:
    def __init__(self, words: list[str], lookup: Lookup | None):
        self.words, self.i, self.lookup = words, 0, lookup

    def peek(self, k: int = 0) -> str | None:
        j = self.i + k
        return self.words[j] if j < len(self.words) else None

    def take(self, expected: str | None = None) -> str:
        word = self.peek()
        if word is None or (expected is not None and word != expected):
            raise ValueError(word)
        self.i += 1
        return word

    def expression(self) -> float:
        value = self.term()
        while self.peek() in ("+", "-"):
            value = value + self.term() if self.take() == "+" else value - self.term()
        return value

    def term(self) -> float:
        value = self.unary()
        while self.peek() in ("*", "/"):
            if self.take() == "*":
                value *= self.unary()
            else:
                divisor = self.unary()
                if divisor == 0:
                    raise ValueError("division by zero")
                value /= divisor
        return value

    def unary(self) -> float:
        word = self.peek()
        if word in ("-", "+"):
            self.take()
            return -self.unary() if word == "-" else self.unary()
        if word == "(" and self.peek(1) in _CASTS and self.peek(2) == ")":
            self.i += 3
            return self.unary()
        return self.primary()

    def primary(self) -> float:
        word = self.take()
        if word == "(":
            value = self.expression()
            self.take(")")
            return value
        value = literal(word)
        if value is not None:
            return value
        if not _name_word(word):
            raise ValueError(word)
        words = [word]
        while self.peek() == "." and self.peek(1) is not None and _name_word(self.peek(1)):
            self.i += 1
            words.append(self.take())
        if self.peek() == "(":
            return self.conversion(words)
        found = self.lookup(" . ".join(words)) if self.lookup is not None else None
        if found is None:
            raise ValueError(".".join(words))
        return found

    def conversion(self, words: list[str]) -> float:
        if len(words) < 2 or words[-2] not in TIME_UNITS or words[-1] not in CONVERSIONS:
            raise ValueError(".".join(words))
        if words[:-2] not in _UNIT_QUALIFIERS:
            raise ValueError(".".join(words))
        self.take("(")
        value = self.expression()
        self.take(")")
        return value * TIME_UNITS[words[-2]] / CONVERSIONS[words[-1]]


def fold(text: str, lookup: Lookup | None = None) -> float | None:
    """The value of a constant expression, or None when anything in it is not constant."""
    words = text.split()
    if not words:
        return None
    folder = _Folder(words, lookup)
    try:
        value = folder.expression()
    except ValueError, OverflowError, RecursionError:
        return None
    return value if folder.i == len(words) else None


def constant_lookup(constants: dict[str, str]) -> Lookup:
    """A lookup through a file's fields declared once with an initializer, followed through
    each other and stopping at a cycle."""
    seen: set[str] = set()

    def lookup(name: str) -> float | None:
        if name in seen or name not in constants:
            return None
        seen.add(name)
        try:
            return fold(constants[name], lookup)
        finally:
            seen.discard(name)

    return lookup
