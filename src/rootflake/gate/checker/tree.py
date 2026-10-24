"""Walking the structure the syntax module finds, without recursion."""

from collections.abc import Iterator

from rootflake.gate.checker.syntax import (
    Block,
    Catch,
    CompilationUnit,
    EnumConstant,
    Field,
    Initializer,
    Lambda,
    Method,
    Statement,
    TypeDecl,
)

Node = (
    CompilationUnit | TypeDecl | EnumConstant | Field | Method | Initializer | Block | Statement
    | Catch | Lambda
)  # fmt: skip


def children(node: Node) -> list[Node]:
    if isinstance(node, CompilationUnit):
        return list(node.types)
    if isinstance(node, TypeDecl):
        bodies = [c for c in node.enum_constants]
        return [*bodies, *node.fields, *node.methods, *node.initializers, *node.types]
    if isinstance(node, EnumConstant):
        return [node.body] if node.body else []
    if isinstance(node, Field):
        return [*node.lambdas, *node.classes, *node.switches]
    if isinstance(node, Method):
        return [node.body] if node.body else []
    if isinstance(node, Initializer):
        return [node.body]
    if isinstance(node, Block):
        return list(node.statements)
    if isinstance(node, Statement):
        out: list[Node] = [*node.blocks, *node.catches]
        if node.finally_block:
            out.append(node.finally_block)
        if node.local_type:
            out.append(node.local_type)
        return [*out, *node.lambdas, *node.classes, *node.switches]
    if isinstance(node, Catch):
        return [node.block]
    if isinstance(node, Lambda):
        out = [node.body] if node.body else []
        return [*out, *node.lambdas, *node.classes, *node.switches]
    return []


def walk(node: Node) -> Iterator[Node]:
    pending = [node]
    while pending:
        current = pending.pop()
        yield current
        pending.extend(reversed(children(current)))


def path_to(unit: CompilationUnit, index: int) -> list[Node]:
    """The nodes whose span holds the token at index, outermost first."""
    path: list[Node] = []
    candidates = children(unit)
    while candidates:
        for child in candidates:
            if child.start <= index < child.end:
                path.append(child)
                candidates = children(child)
                break
        else:
            break
    return path
