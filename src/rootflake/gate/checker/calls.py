"""Calls in a span of code, and following them into the helpers they reach (ADR 0007 Decision 4).

A rejection scan follows every declaration the call could reach, whatever its arity, with no
depth limit: those of the receiver's type when it is a known type, otherwise every declaration
with the name. Credit follows a call only when exactly one declaration matches its name and
arity, at most three levels deep. A visited set breaks cycles, and one budget of 2,000
expansions per check covers every follow; going past it is unparsable.
"""

from dataclasses import dataclass

from rootflake.gate.checker.lexer import Unparsable
from rootflake.gate.checker.scope import Program
from rootflake.gate.checker.syntax import CompilationUnit, Method, TypeDecl
from rootflake.gate.checker.tree import path_to

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
    receiver_at: int | None = None


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


def _receiver(unit: CompilationUnit, dot_at: int) -> tuple[str | None, int | None]:
    tokens = unit.tokens
    if dot_at >= 1 and tokens[dot_at].text in (".", "::"):
        before = tokens[dot_at - 1]
        name = before.text if before.kind in ("ident", "keyword") else "<expr>"
        return name, dot_at - 1
    return None, None


def _type_arguments_start(tokens, close: int) -> int | None:
    """Index of the `<` matching the `>` at close, scanning back over balanced angles."""
    depth, j = 0, close
    while j >= 0:
        text = tokens[j].text if tokens[j].kind == "op" else ""
        if text == ">":
            depth += 1
        elif text == "<":
            depth -= 1
            if depth == 0:
                return j
        elif text in (";", "{", "}", "(", ")"):
            return None
        j -= 1
    return None


def calls_in(unit: CompilationUnit, node) -> list[Call]:
    tokens, out = unit.tokens, []
    for i in range(node.start, min(node.end, len(tokens) - 1)):
        token = tokens[i]
        if token.kind != "ident":
            continue
        before = tokens[i - 1] if i else None
        if tokens[i + 1].text == "(" and tokens[i + 1].kind == "op":
            dot_at = i - 1
            if before is not None and before.text == ">" and before.kind == "op":
                opened = _type_arguments_start(tokens, i - 1)
                if opened is None or opened == 0 or tokens[opened - 1].text != ".":
                    continue
                dot_at = opened - 1
            elif before is not None and (
                before.text in _NOT_CALL_BEFORE
                or before.kind == "ident"
                or before.text in _DECLARATION_BEFORE
                or before.text == "]"
            ):
                continue
            if _is_constructor(tokens, i):
                continue
            receiver, receiver_at = _receiver(unit, dot_at)
            out.append(
                Call(token.text, _arity(unit, i + 1), receiver, False, i, i + 1, receiver_at)
            )
        elif before is not None and before.text == "::" and before.kind == "op":
            receiver, receiver_at = _receiver(unit, i - 1)
            out.append(Call(token.text, None, receiver, True, i, i + 1, receiver_at))
    return out


def _is_constructor(tokens, i: int) -> bool:
    j = i - 1
    while j >= 1 and tokens[j].text == "." and tokens[j - 1].kind == "ident":
        j -= 2
    return j >= 0 and tokens[j].text == "new"


def _named(program: Program, owners, name: str) -> list[tuple[str, Method]]:
    return [
        (program.owner[id(owner)], method)
        for owner in owners
        for method in owner.methods
        if method.name == name
    ]


def _owners(program: Program, path: str, decl: TypeDecl) -> list[TypeDecl]:
    return [decl, *(found for _, found in program.supertypes(path, decl))]


def follow_for_scan(program: Program, path: str, call: Call) -> list[tuple[str, Method]]:
    """Every declaration the call could reach, by name and any arity.

    Where the receiver is a type the call can only reach that type's methods, so a call on a
    type the checker did not read is not followed into a same named method elsewhere. Where the
    receiver's type is unknown (a variable or an expression), or an unqualified name has no
    declaration in an enclosing class, every declaration with the name is followed.
    """
    every = list(program.methods_by_name.get(call.name, []))
    if not every:
        return []
    unit = program.units[path]
    if call.receiver in (None, "this", "super"):
        for node in reversed(path_to(unit, call.start)):
            if isinstance(node, TypeDecl):
                found = _named(program, _owners(program, path, node), call.name)
                if found:
                    return found
        return every
    if call.receiver == "<expr>" or call.receiver_at is None:
        return every
    binding = program.resolve_type(path, call.receiver, call.receiver_at)
    if binding.kind == "declared" and isinstance(binding.decl, TypeDecl):
        return _named(program, _owners(program, binding.path, binding.decl), call.name)
    if binding.kind in ("library", "imported", "java_lang", "type_param"):
        return []
    if call.receiver[:1].isupper():
        same_name = [
            decl
            for decl_path in program.units
            for decl in program.all_types(decl_path)
            if decl.name == call.receiver
        ]
        return [
            found
            for decl in same_name
            for found in _named(program, _owners(program, program.owner[id(decl)], decl), call.name)
        ]
    return every


def _matching(program: Program, owners, call: Call) -> list[tuple[str, Method]]:
    return [
        (program.owner[id(owner)], method)
        for owner in owners
        for method in owner.methods
        if method.name == call.name and (call.arity is None or _accepts(method, call.arity))
    ]


def follow_for_credit(program: Program, path: str, call: Call) -> list[tuple[str, Method]]:
    """The one declaration the call resolves to, or nothing: credit is never guessed.

    An unqualified or `this` call resolves in the innermost enclosing class that declares or
    inherits (from a read supertype) a matching method; a call on a type name resolves in that
    type. A call on any other receiver, such as a variable whose type is unknown, gets none.
    """
    unit = program.units[path]
    if call.receiver in (None, "this", "super"):
        for node in reversed(path_to(unit, call.start)):
            if isinstance(node, TypeDecl):
                owners = [node, *(decl for _, decl in program.supertypes(path, node))]
                found = _matching(program, owners, call)
                if found:
                    return found if len(found) == 1 else []
        return []
    if call.receiver == "<expr>" or call.receiver_at is None:
        return []
    binding = program.resolve_type(path, call.receiver, call.receiver_at)
    if binding.kind != "declared" or not isinstance(binding.decl, TypeDecl):
        return []
    owners = [binding.decl, *(decl for _, decl in program.supertypes(binding.path, binding.decl))]
    found = _matching(program, owners, call)
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
            if scan:
                targets = follow_for_scan(program, current.path, call)
            else:
                targets = follow_for_credit(program, current.path, call)
            for path, method in targets:
                if id(method) in seen:
                    continue
                budget.spend(unit.tokens[call.start].line)
                seen.add(id(method))
                reached = Reached(path, method, current.depth + 1)
                out.append(reached)
                pending.append(reached)
    return out
