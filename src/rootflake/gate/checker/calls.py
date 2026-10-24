"""Calls in a span of code, and following them into the helpers they reach (ADR 0007 Decision 4).

A rejection scan follows every declaration with the call's name, whatever its arity, with no
depth limit. Credit follows a call only when exactly one declaration matches its name and
arity, at most three levels deep. A visited set breaks cycles, and one budget of 2,000
expansions per check covers every follow; going past it is unparsable.
"""

from dataclasses import dataclass

from rootflake.gate.checker.lexer import Unparsable
from rootflake.gate.checker.scope import Program
from rootflake.gate.checker.syntax import CompilationUnit, Method

MAX_EXPANSIONS = 2000
CREDIT_DEPTH = 3
_NOT_CALL_BEFORE = frozenset(["new", "@"])
_DECLARATION_BEFORE = frozenset("boolean byte char short int long float double void".split())


@dataclass(frozen=True)
class Call:
    name: str
    arity: int | None
    receiver: str | None
    reference: bool
    start: int
    end: int


@dataclass(frozen=True)
class Reached:
    path: str
    method: Method
    depth: int


class ExpansionBudget:
    def __init__(self, limit: int = MAX_EXPANSIONS):
        self.left = limit

    def spend(self, line: int) -> None:
        self.left -= 1
        if self.left < 0:
            raise Unparsable("helper expansion cap reached", line)


def _arity(unit: CompilationUnit, open_paren: int) -> int:
    tokens, depth, commas, i = unit.tokens, 0, 0, open_paren + 1
    if tokens[i].text == ")":
        return 0
    while True:
        text = tokens[i].text if tokens[i].kind == "op" else ""
        if text in ("(", "[", "{"):
            depth += 1
        elif text in (")", "]", "}"):
            if depth == 0:
                return commas + 1
            depth -= 1
        elif text == "," and depth == 0:
            commas += 1
        i += 1


def _receiver(unit: CompilationUnit, name_at: int) -> str | None:
    tokens = unit.tokens
    if name_at >= 2 and tokens[name_at - 1].text in (".", "::"):
        before = tokens[name_at - 2]
        return before.text if before.kind in ("ident", "keyword") else "<expr>"
    return None


def calls_in(unit: CompilationUnit, node) -> list[Call]:
    tokens, out = unit.tokens, []
    for i in range(node.start, min(node.end, len(tokens) - 1)):
        token = tokens[i]
        if token.kind != "ident":
            continue
        before = tokens[i - 1] if i else None
        if tokens[i + 1].text == "(" and tokens[i + 1].kind == "op":
            if before is not None and (
                before.text in _NOT_CALL_BEFORE
                or before.kind == "ident"
                or before.text in _DECLARATION_BEFORE
                or before.text in (">", "]")
            ):
                continue
            if _is_constructor(tokens, i):
                continue
            close = _arity(unit, i + 1)
            out.append(Call(token.text, close, _receiver(unit, i), False, i, i + 1))
        elif before is not None and before.text == "::" and before.kind == "op":
            out.append(Call(token.text, None, _receiver(unit, i), True, i, i + 1))
    return out


def _is_constructor(tokens, i: int) -> bool:
    j = i - 1
    while j >= 1 and tokens[j].text == "." and tokens[j - 1].kind == "ident":
        j -= 2
    return j >= 0 and tokens[j].text == "new"


def follow_for_scan(program: Program, call: Call) -> list[tuple[str, Method]]:
    return list(program.methods_by_name.get(call.name, []))


def follow_for_credit(program: Program, call: Call) -> list[tuple[str, Method]]:
    found = [
        (path, method)
        for path, method in program.methods_by_name.get(call.name, [])
        if call.arity is None or _accepts(method, call.arity)
    ]
    return found if len(found) == 1 else []


def _accepts(method: Method, arity: int) -> bool:
    if method.params and method.params[-1].type.endswith("..."):
        return arity >= len(method.params) - 1
    return len(method.params) == arity


def reach(
    program: Program, roots: list[tuple[str, Method]], scan: bool, budget: ExpansionBudget
) -> list[Reached]:
    out = [Reached(path, method, 0) for path, method in roots]
    seen = {id(method) for _, method in roots}
    pending = list(out)
    while pending:
        current = pending.pop(0)
        if current.method.body is None or (not scan and current.depth >= CREDIT_DEPTH):
            continue
        unit = program.units[current.path]
        for call in calls_in(unit, current.method.body):
            targets = follow_for_scan(program, call) if scan else follow_for_credit(program, call)
            for path, method in targets:
                if id(method) in seen:
                    continue
                budget.spend(unit.tokens[call.start].line)
                seen.add(id(method))
                reached = Reached(path, method, current.depth + 1)
                out.append(reached)
                pending.append(reached)
    return out
