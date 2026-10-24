"""The structure of a Java file, found by bracket matching over the lexer's tokens.

This is not a Java parser. It finds what the shortcut checker's rules need: the package,
imports, types and their headers, members, method bodies, statements, lambdas, anonymous
classes and switch bodies. Every node records the token span it covers in the file's token
list, so a rule can ask whether a token sits inside a lambda, a `try` or a `catch`. Brackets are
matched with an explicit stack of depth at most 256 (ADR 0007 Decision 3); syntactic nesting,
including unbraced statements such as `if (a) if (b) x();`, is capped at the same depth.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rootflake.gate.checker.lexer import Token, Unparsable

MAX_DEPTH = 256
_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = {")", "]", "}"}
_MODIFIERS = frozenset(
    "public protected private static final abstract native synchronized transient volatile "
    "strictfp default sealed".split()
)
_PRIMITIVES = frozenset("boolean byte char short int long float double void".split())
_TYPE_KEYWORDS = frozenset(["class", "interface", "enum"])


@dataclass
class Annotation:
    name: str
    arguments: Span | None
    line: int
    start: int
    end: int


@dataclass
class Span:
    start: int
    end: int


@dataclass
class Import:
    name: str
    static: bool
    wildcard: bool
    line: int


@dataclass
class Param:
    type: str
    name: str
    annotations: list[Annotation]
    start: int
    end: int


@dataclass
class Lambda:
    params: list[str]
    body: Block | None
    expression: Span | None
    lambdas: list[Lambda]
    classes: list[TypeDecl]
    switches: list[Statement]
    line: int
    start: int
    end: int


@dataclass
class Catch:
    types: list[str]
    name: str
    block: Block
    line: int
    start: int
    end: int


@dataclass
class Statement:
    kind: str
    line: int
    start: int
    end: int
    header: Span | None = None
    blocks: list[Block] = field(default_factory=list)
    resources: Span | None = None
    catches: list[Catch] = field(default_factory=list)
    finally_block: Block | None = None
    local_type: TypeDecl | None = None
    annotations: list[Annotation] = field(default_factory=list)
    lambdas: list[Lambda] = field(default_factory=list)
    classes: list[TypeDecl] = field(default_factory=list)
    switches: list[Statement] = field(default_factory=list)


@dataclass
class Block:
    statements: list[Statement]
    start: int
    end: int


@dataclass
class Method:
    name: str
    annotations: list[Annotation]
    modifiers: list[str]
    type_params: str
    return_type: str | None
    params: list[Param]
    throws: list[str]
    body: Block | None
    is_constructor: bool
    line: int
    start: int
    end: int


@dataclass
class Field:
    names: list[str]
    type: str
    annotations: list[Annotation]
    modifiers: list[str]
    initializers: dict[str, Span]
    lambdas: list[Lambda]
    classes: list[TypeDecl]
    switches: list[Statement]
    line: int
    start: int
    end: int


@dataclass
class Initializer:
    static: bool
    body: Block
    line: int
    start: int
    end: int


@dataclass
class EnumConstant:
    name: str
    annotations: list[Annotation]
    arguments: Span | None
    body: TypeDecl | None
    line: int
    start: int
    end: int


@dataclass
class TypeDecl:
    kind: str
    name: str
    annotations: list[Annotation]
    modifiers: list[str]
    type_params: str
    extends: list[str]
    implements: list[str]
    permits: list[str]
    header_text: str
    record_components: list[Param]
    enum_constants: list[EnumConstant]
    fields: list[Field]
    methods: list[Method]
    types: list[TypeDecl]
    initializers: list[Initializer]
    line: int
    start: int
    end: int
    body_start: int = 0
    body_end: int = 0


@dataclass
class CompilationUnit:
    tokens: list[Token]
    package: str | None
    package_line: int | None
    imports: list[Import]
    types: list[TypeDecl]


def _match_brackets(tokens: list[Token]) -> list[int]:
    partner = [-1] * len(tokens)
    stack: list[int] = []
    for i, token in enumerate(tokens):
        if token.kind != "op":
            continue
        if token.text in _OPEN:
            stack.append(i)
            if len(stack) > MAX_DEPTH:
                raise Unparsable("brackets nested too deep", token.line)
        elif token.text in _CLOSE:
            if not stack or _OPEN[tokens[stack[-1]].text] != token.text:
                raise Unparsable(f"unbalanced {token.text}", token.line)
            opened = stack.pop()
            partner[opened], partner[i] = i, opened
    if stack:
        raise Unparsable("unclosed bracket", tokens[stack[-1]].line)
    return partner


def _type_text(tokens: list[Token], start: int, end: int) -> str:
    return "".join(t.text for t in tokens[start:end] if t.text not in ("final",))


def _base_name(text: str) -> str:
    depth, out = 0, []
    for char in text:
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
        elif depth == 0 and char not in "[]":
            out.append(char)
    return "".join(out)


class _Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.partner = _match_brackets(tokens)
        self.depth = 0

    # Small helpers.

    def text(self, i: int) -> str:
        return self.tokens[i].text if 0 <= i < len(self.tokens) else ""

    def is_op(self, i: int, text: str) -> bool:
        return (
            0 <= i < len(self.tokens)
            and self.tokens[i].kind == "op"
            and self.tokens[i].text == text
        )

    def is_ident(self, i: int) -> bool:
        return 0 <= i < len(self.tokens) and self.tokens[i].kind == "ident"

    def line(self, i: int) -> int:
        if i < len(self.tokens):
            return self.tokens[i].line
        return self.tokens[-1].line if self.tokens else 1

    def fail(self, reason: str, i: int) -> Unparsable:
        return Unparsable(reason, self.line(i))

    def enter(self, i: int) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise self.fail("nested too deep", i)

    def leave(self) -> None:
        self.depth -= 1

    def after(self, i: int) -> int:
        """Index after the bracket opened at i."""
        return self.partner[i] + 1

    def skip_angles(self, i: int, end: int) -> int:
        depth = 0
        while i < end:
            if self.is_op(i, "<"):
                depth += 1
            elif self.is_op(i, ">"):
                depth -= 1
                if depth == 0:
                    return i + 1
            elif self.text(i) in _OPEN and self.tokens[i].kind == "op":
                i = self.partner[i]
            elif self.text(i) in (";", "{", "}"):
                raise self.fail("unbalanced generic brackets", i)
            i += 1
        raise self.fail("unbalanced generic brackets", i)

    def qualified(self, i: int) -> tuple[str, int]:
        parts = [self.text(i)]
        i += 1
        while self.is_op(i, ".") and (self.is_ident(i + 1) or self.text(i + 1) == "*"):
            parts.append(self.text(i + 1))
            i += 2
        return ".".join(parts), i

    def find_top(self, i: int, end: int, stops: tuple[str, ...]) -> int:
        while i < end:
            token = self.tokens[i]
            if token.kind == "op" and token.text in stops:
                return i
            if token.kind == "op" and token.text in _OPEN:
                i = self.partner[i]
            i += 1
        return end

    # Annotations and modifiers.

    def annotation(self, i: int) -> tuple[Annotation, int]:
        start = i
        name, i = self.qualified(i + 1)
        arguments = None
        if self.is_op(i, "("):
            arguments = Span(i + 1, self.partner[i])
            i = self.after(i)
        return Annotation(name, arguments, self.line(start), start, i), i

    def modifiers(self, i: int, end: int) -> tuple[list[Annotation], list[str], int]:
        annotations, modifiers = [], []
        while i < end:
            if self.is_op(i, "@") and self.text(i + 1) != "interface":
                annotation, i = self.annotation(i)
                annotations.append(annotation)
            elif self.text(i) in _MODIFIERS and not self.is_op(i + 1, "("):
                if self.text(i) == "default" and (
                    self.is_op(i + 1, ":") or self.is_op(i + 1, "->")
                ):
                    break
                modifiers.append(self.text(i))
                i += 1
            elif self.text(i) == "non" and self.is_op(i + 1, "-") and self.text(i + 2) == "sealed":
                modifiers.append("non-sealed")
                i += 3
            else:
                break
        return annotations, modifiers, i

    # Compilation unit and types.

    def unit(self) -> CompilationUnit:
        tokens, i, end = self.tokens, 0, len(self.tokens)
        package = package_line = None
        imports: list[Import] = []
        types: list[TypeDecl] = []
        while i < end:
            if self.is_op(i, ";"):
                i += 1
                continue
            annotations, modifiers, j = self.modifiers(i, end)
            if self.text(j) == "package":
                package, k = self.qualified(j + 1)
                package_line = self.line(j)
                i = self.semicolon(k)
            elif self.text(j) == "import" and not annotations and not modifiers:
                static = self.text(j + 1) == "static"
                name, k = self.qualified(j + 2 if static else j + 1)
                wildcard = name.endswith(".*")
                imports.append(
                    Import(name[:-2] if wildcard else name, static, wildcard, self.line(j))
                )
                i = self.semicolon(k)
            elif self.starts_type(j):
                decl, i = self.type_decl(i, annotations, modifiers, j)
                types.append(decl)
            else:
                raise self.fail("expected a type declaration", j)
        return CompilationUnit(tokens, package, package_line, imports, types)

    def semicolon(self, i: int) -> int:
        if not self.is_op(i, ";"):
            raise self.fail("expected ;", i)
        return i + 1

    def starts_type(self, i: int) -> bool:
        if self.text(i) in _TYPE_KEYWORDS and self.tokens[i].kind == "keyword":
            return True
        if self.is_op(i, "@") and self.text(i + 1) == "interface":
            return True
        return (
            self.text(i) == "record"
            and self.is_ident(i + 1)
            and (self.is_op(i + 2, "(") or self.is_op(i + 2, "<"))
        )

    def type_decl(
        self, start: int, annotations: list[Annotation], modifiers: list[str], i: int
    ) -> tuple[TypeDecl, int]:
        self.enter(i)
        if self.is_op(i, "@"):
            kind, i = "annotation", i + 2
        else:
            kind, i = self.text(i), i + 1
        if not self.is_ident(i):
            raise self.fail("expected a type name", i)
        name, line = self.text(i), self.line(i)
        i += 1
        type_params_start = i
        if self.is_op(i, "<"):
            i = self.skip_angles(i, len(self.tokens))
        type_params = _type_text(self.tokens, type_params_start, i)
        components: list[Param] = []
        if kind == "record":
            if not self.is_op(i, "("):
                raise self.fail("expected record components", i)
            components = self.params(i)
            i = self.after(i)
        header_start = i
        body = self.find_top(i, len(self.tokens), ("{",))
        if body >= len(self.tokens):
            raise self.fail("expected a type body", i)
        extends, implements, permits = self.header_lists(header_start, body, kind)
        decl = TypeDecl(
            kind, name, annotations, modifiers, type_params, extends, implements, permits,
            " ".join(t.text for t in self.tokens[header_start:body]), components,
            [], [], [], [], [], line, start, self.after(body),
        )  # fmt: skip
        self.type_body(decl, body)
        self.leave()
        return decl, self.after(body)

    def header_lists(self, i: int, end: int, kind: str) -> tuple[list[str], list[str], list[str]]:
        lists: dict[str, list[str]] = {"extends": [], "implements": [], "permits": []}
        current = None
        piece_start = i
        while i <= end:
            word = self.text(i) if i < end else None
            if i == end or word in lists or (self.is_op(i, ",") and current):
                if current is not None and piece_start < i:
                    lists[current].append(_base_name(_type_text(self.tokens, piece_start, i)))
                if word in lists:
                    current = word
                piece_start = i + 1
                if i == end:
                    break
            elif self.is_op(i, "<"):
                i = self.skip_angles(i, end) - 1
            i += 1
        if kind == "interface":
            return [], lists["extends"], lists["permits"]
        return lists["extends"], lists["implements"], lists["permits"]

    def type_body(self, decl: TypeDecl, open_brace: int) -> None:
        end = self.partner[open_brace]
        decl.body_start, decl.body_end = open_brace, end + 1
        i = open_brace + 1
        if decl.kind == "enum":
            i = self.enum_constants(decl, i, end)
        while i < end:
            i = self.member(decl, i, end)

    def enum_constants(self, decl: TypeDecl, i: int, end: int) -> int:
        while i < end and not self.is_op(i, ";"):
            if self.is_op(i, ","):
                i += 1
                continue
            start = i
            annotations, _, i = self.modifiers(i, end)
            if not self.is_ident(i):
                raise self.fail("expected an enum constant", i)
            name, line = self.text(i), self.line(i)
            i += 1
            arguments = None
            if self.is_op(i, "("):
                arguments = Span(i + 1, self.partner[i])
                i = self.after(i)
            body = None
            if self.is_op(i, "{"):
                body = self.anonymous(name, i)
                i = self.after(i)
            decl.enum_constants.append(
                EnumConstant(name, annotations, arguments, body, line, start, i)
            )
        return i + 1 if i < end else i

    def anonymous(self, base: str, open_brace: int) -> TypeDecl:
        self.enter(open_brace)
        decl = TypeDecl(
            "anonymous", "", [], [], "", [_base_name(base)] if base else [], [], [], "", [], [],
            [], [], [], [], self.line(open_brace), open_brace, self.after(open_brace),
        )  # fmt: skip
        self.type_body(decl, open_brace)
        self.leave()
        return decl

    def member(self, decl: TypeDecl, i: int, end: int) -> int:
        if self.is_op(i, ";"):
            return i + 1
        start = i
        if self.is_op(i, "{") or (self.text(i) == "static" and self.is_op(i + 1, "{")):
            static = self.text(i) == "static"
            brace = i + 1 if static else i
            decl.initializers.append(
                Initializer(static, self.block(brace), self.line(i), start, self.after(brace))
            )
            return self.after(brace)
        annotations, modifiers, i = self.modifiers(i, end)
        if self.starts_type(i):
            nested, i = self.type_decl(start, annotations, modifiers, i)
            decl.types.append(nested)
            return i
        type_params_start = i
        if self.is_op(i, "<"):
            i = self.skip_angles(i, end)
        type_params = _type_text(self.tokens, type_params_start, i)
        if decl.kind == "record" and self.text(i) == decl.name and self.is_op(i + 1, "{"):
            body = self.block(i + 1)
            decl.methods.append(
                Method(decl.name, annotations, modifiers, type_params, None, [], [], body, True,
                       self.line(i), start, self.after(i + 1))
            )  # fmt: skip
            return self.after(i + 1)
        stop = self.find_top(i, end, ("(", "=", ";"))
        if stop >= end:
            raise self.fail("expected a member", i)
        if self.is_op(stop, "("):
            return self.method(decl, start, annotations, modifiers, type_params, i, stop, end)
        return self.field_decl(decl, start, annotations, modifiers, i, end)

    def method(
        self, decl: TypeDecl, start: int, annotations: list[Annotation], modifiers: list[str],
        type_params: str, i: int, paren: int, end: int,
    ) -> int:  # fmt: skip
        name_at = paren - 1
        if not self.is_ident(name_at):
            raise self.fail("expected a method name", name_at)
        return_type = _type_text(self.tokens, i, name_at) or None
        params = self.params(paren)
        j = self.after(paren)
        while self.is_op(j, "[") and self.is_op(j + 1, "]"):
            j += 2
        throws: list[str] = []
        if self.text(j) == "throws":
            stop = self.find_top(j + 1, end, ("{", ";"))
            for piece in self.split_commas(j + 1, stop):
                throws.append(_base_name(_type_text(self.tokens, piece.start, piece.end)))
            j = stop
        body = None
        if self.is_op(j, "{"):
            body = self.block(j)
            j = self.after(j)
        elif self.text(j) == "default":
            j = self.semicolon(self.find_top(j, end, (";",)))
        else:
            j = self.semicolon(j)
        decl.methods.append(
            Method(self.text(name_at), annotations, modifiers, type_params, return_type, params,
                   throws, body, return_type is None, self.line(name_at), start, j)
        )  # fmt: skip
        return j

    def split_commas(self, i: int, end: int) -> list[Span]:
        pieces, start = [], i
        while i < end:
            if self.is_op(i, ","):
                pieces.append(Span(start, i))
                start = i + 1
            elif self.is_op(i, "<"):
                i = self.skip_angles(i, end) - 1
            elif self.tokens[i].kind == "op" and self.text(i) in _OPEN:
                i = self.partner[i]
            i += 1
        if start < end:
            pieces.append(Span(start, end))
        return pieces

    def params(self, paren: int) -> list[Param]:
        out = []
        for piece in self.split_commas(paren + 1, self.partner[paren]):
            annotations, _, i = self.modifiers(piece.start, piece.end)
            name_at = piece.end - 1
            while self.is_op(name_at, "]") and self.is_op(name_at - 1, "["):
                name_at -= 2
            if not self.is_ident(name_at) and self.text(name_at) != "this":
                raise self.fail("expected a parameter name", name_at)
            type_text = _type_text(self.tokens, i, name_at)
            out.append(Param(type_text, self.text(name_at), annotations, piece.start, piece.end))
        return out

    def type_end(self, i: int, end: int) -> int | None:
        """Index after a type starting at i, or None if no type starts there."""
        if self.text(i) in _PRIMITIVES and self.tokens[i].kind == "keyword":
            i += 1
        elif self.is_ident(i):
            i += 1
            while True:
                if self.is_op(i, "<"):
                    try:
                        i = self.skip_angles(i, end)
                    except Unparsable:
                        return None
                elif self.is_op(i, ".") and self.is_ident(i + 1):
                    i += 2
                else:
                    break
        else:
            return None
        while self.is_op(i, "[") and self.is_op(i + 1, "]"):
            i += 2
        if self.is_op(i, "..."):
            i += 1
        return i

    def field_decl(
        self, decl: TypeDecl, start: int, annotations: list[Annotation], modifiers: list[str],
        i: int, end: int,
    ) -> int:  # fmt: skip
        type_end = self.type_end(i, end)
        if type_end is None:
            raise self.fail("expected a field type", i)
        stop = self.find_top(type_end, end, (";",))
        if stop >= end:
            raise self.fail("expected ;", type_end)
        names, initializers, lambdas, classes, switches = self.declarators(type_end, stop)
        decl.fields.append(
            Field(names, _type_text(self.tokens, i, type_end), annotations, modifiers,
                  initializers, lambdas, classes, switches, self.line(type_end), start, stop + 1)
        )  # fmt: skip
        return stop + 1

    def declarators(self, i: int, stop: int):
        names: list[str] = []
        initializers: dict[str, Span] = {}
        lambdas: list[Lambda] = []
        classes: list[TypeDecl] = []
        switches: list[Statement] = []
        while i < stop:
            if not self.is_ident(i):
                raise self.fail("expected a variable name", i)
            name = self.text(i)
            names.append(name)
            i += 1
            while self.is_op(i, "[") and self.is_op(i + 1, "]"):
                i += 2
            if self.is_op(i, "="):
                value_end = self.declarator_end(i + 1, stop)
                initializers[name] = Span(i + 1, value_end)
                self.scan(i + 1, value_end, lambdas, classes, switches)
                i = value_end
            if self.is_op(i, ","):
                i += 1
            elif i < stop:
                raise self.fail("expected , or ;", i)
        return names, initializers, lambdas, classes, switches

    def declarator_end(self, i: int, stop: int) -> int:
        while i < stop:
            if (
                self.is_op(i, ",")
                and self.is_ident(i + 1)
                and (self.text(i + 2) in ("=", ",", ";", "[") or i + 2 == stop)
            ):
                return i
            if self.tokens[i].kind == "op" and self.text(i) in _OPEN:
                i = self.partner[i]
            i += 1
        return stop

    # Blocks and statements.

    def block(self, open_brace: int) -> Block:
        if not self.is_op(open_brace, "{"):
            raise self.fail("expected {", open_brace)
        self.enter(open_brace)
        end = self.partner[open_brace]
        statements, i = [], open_brace + 1
        while i < end:
            statement, i = self.statement(i, end)
            statements.append(statement)
        self.leave()
        return Block(statements, open_brace, end + 1)

    def wrap(self, i: int, end: int) -> tuple[Block, int]:
        if self.is_op(i, "{"):
            return self.block(i), self.after(i)
        statement, after = self.statement(i, end)
        return Block([statement], i, after), after

    def statement(self, i: int, end: int) -> tuple[Statement, int]:
        self.enter(i)
        try:
            return self.statement_inner(i, end)
        finally:
            self.leave()

    def simple(self, kind: str, i: int, end: int) -> tuple[Statement, int]:
        stop = self.find_top(i, end, (";",))
        if stop >= end:
            raise self.fail("expected ;", i)
        statement = Statement(kind, self.line(i), i, stop + 1)
        self.scan(i, stop, statement.lambdas, statement.classes, statement.switches)
        return statement, stop + 1

    def statement_inner(self, i: int, end: int) -> tuple[Statement, int]:
        word, line = self.text(i), self.line(i)
        if self.is_op(i, ";"):
            return Statement("empty", line, i, i + 1), i + 1
        if self.is_op(i, "{"):
            block = self.block(i)
            return Statement("block", line, i, self.after(i), blocks=[block]), self.after(i)
        if self.is_ident(i) and self.is_op(i + 1, ":"):
            inner, after = self.statement(i + 2, end)
            return Statement(
                "labeled", line, i, after, blocks=[Block([inner], i + 2, after)]
            ), after
        if word in ("if", "while", "synchronized") and self.tokens[i].kind == "keyword":
            return self.conditional(word, i, end)
        if word == "for" and self.tokens[i].kind == "keyword":
            return self.conditional("for", i, end)
        if word == "do" and self.tokens[i].kind == "keyword":
            body, j = self.wrap(i + 1, end)
            if self.text(j) != "while" or not self.is_op(j + 1, "("):
                raise self.fail("expected while", j)
            header = Span(j + 1, self.after(j + 1))
            statement = Statement("do", line, i, self.semicolon(self.after(j + 1)), header, [body])
            self.scan(header.start, header.end, statement.lambdas, statement.classes,
                      statement.switches)  # fmt: skip
            return statement, statement.end
        if word == "try" and self.tokens[i].kind == "keyword":
            return self.try_statement(i, end)
        if word == "switch" and self.tokens[i].kind == "keyword" and self.is_op(i + 1, "("):
            switch = self.switch(i)
            if self.is_op(switch.end, ";"):
                return switch, switch.end + 1
            if self.is_op(switch.end, ".") or self.is_op(switch.end, "?"):
                return self.simple("expression", i, end)
            return switch, switch.end
        if word in ("return", "throw", "break", "continue", "assert"):
            if self.tokens[i].kind == "keyword":
                return self.simple(word, i, end)
        if word == "yield" and self.is_ident(i) and self.text(i + 1) not in (
            "=", ".", "[", "++", "--", "->", ";", ",", ")",
        ):  # fmt: skip
            return self.simple("yield", i, end)
        annotations, modifiers, j = self.modifiers(i, end)
        if self.starts_type(j):
            decl, after = self.type_decl(i, annotations, modifiers, j)
            return Statement("local_type", line, i, after, local_type=decl), after
        if self.is_local_var(j, end):
            statement, after = self.simple("local_var", i, end)
            statement.annotations = annotations
            return statement, after
        return self.simple("expression", i, end)

    def is_local_var(self, i: int, end: int) -> bool:
        type_end = self.type_end(i, end)
        if type_end is None or type_end == i:
            return False
        return self.is_ident(type_end) and self.text(type_end + 1) in ("=", ";", ",", "[", ":")

    def conditional(self, kind: str, i: int, end: int) -> tuple[Statement, int]:
        if not self.is_op(i + 1, "("):
            raise self.fail(f"expected ( after {kind}", i)
        header = Span(i + 1, self.after(i + 1))
        body, j = self.wrap(header.end, end)
        blocks = [body]
        if kind == "if" and self.text(j) == "else":
            other, j = self.wrap(j + 1, end)
            blocks.append(other)
        statement = Statement(kind, self.line(i), i, j, header, blocks)
        self.scan(header.start, header.end, statement.lambdas, statement.classes,
                  statement.switches)  # fmt: skip
        return statement, j

    def try_statement(self, i: int, end: int) -> tuple[Statement, int]:
        j = i + 1
        resources = None
        statement = Statement("try", self.line(i), i, i)
        if self.is_op(j, "("):
            resources = Span(j, self.after(j))
            self.scan(j, resources.end, statement.lambdas, statement.classes, statement.switches)
            j = resources.end
        body = self.block(j)
        j = self.after(j)
        catches = []
        while self.text(j) == "catch" and self.is_op(j + 1, "("):
            paren = j + 1
            close = self.partner[paren]
            _, _, k = self.modifiers(paren + 1, close)
            name_at = close - 1
            types, piece = [], k
            for at in range(k, name_at + 1):
                if at == name_at or self.is_op(at, "|"):
                    types.append(_base_name(_type_text(self.tokens, piece, at)))
                    piece = at + 1
            block = self.block(close + 1)
            catches.append(
                Catch(types, self.text(name_at), block, self.line(j), j, self.after(close + 1))
            )
            j = self.after(close + 1)
        finally_block = None
        if self.text(j) == "finally":
            finally_block = self.block(j + 1)
            j = self.after(j + 1)
        if not catches and finally_block is None and resources is None:
            raise self.fail("try without catch or finally", i)
        statement.end, statement.blocks, statement.resources = j, [body], resources
        statement.catches, statement.finally_block = catches, finally_block
        return statement, j

    def switch(self, i: int) -> Statement:
        header = Span(i + 1, self.after(i + 1))
        brace = header.end
        if not self.is_op(brace, "{"):
            raise self.fail("expected switch body", brace)
        self.enter(brace)
        close = self.partner[brace]
        statement = Statement("switch", self.line(i), i, close + 1, header)
        self.scan(header.start, header.end, statement.lambdas, statement.classes,
                  statement.switches)  # fmt: skip
        j = brace + 1
        while j < close:
            if self.text(j) not in ("case", "default"):
                raise self.fail("expected case or default", j)
            label_end = self.find_top(j + 1, close, (":", "->"))
            if label_end >= close:
                raise self.fail("expected : or ->", j)
            j = label_end + 1
            if self.is_op(label_end, "->"):
                if self.is_op(j, "{"):
                    statement.blocks.append(self.block(j))
                    j = self.after(j)
                else:
                    kind = "throw" if self.text(j) == "throw" else "expression"
                    inner, j = self.simple(kind, j, close)
                    statement.blocks.append(Block([inner], inner.start, j))
                continue
            statements, start = [], j
            while (
                j < close
                and self.text(j) not in ("case", "default")
                or (
                    self.text(j) == "default"
                    and not (self.is_op(j + 1, ":") or self.is_op(j + 1, "->"))
                )
            ):
                inner, j = self.statement(j, close)
                statements.append(inner)
            statement.blocks.append(Block(statements, start, j))
        self.leave()
        return statement

    # Lambdas, anonymous classes and switch expressions inside expressions.

    def scan(
        self, i: int, end: int, lambdas: list[Lambda], classes: list[TypeDecl],
        switches: list[Statement],
    ) -> None:  # fmt: skip
        while i < end:
            token = self.tokens[i]
            if token.kind == "keyword" and token.text == "switch" and self.is_op(i + 1, "("):
                switch = self.switch(i)
                switches.append(switch)
                i = switch.end
            elif token.kind == "keyword" and token.text == "new":
                i = self.new_expression(i, end, classes)
            elif token.kind == "op" and token.text == "->":
                lam = self.lambda_at(i, end)
                lambdas.append(lam)
                i = lam.end
            else:
                i += 1

    def new_expression(self, i: int, end: int, classes: list[TypeDecl]) -> int:
        j = i + 1
        while j < end and self.tokens[j].kind == "op" and self.text(j) == "@":
            _, j = self.annotation(j)
        name_start = j
        type_end = self.type_end(j, end)
        if type_end is None:
            return i + 1
        if self.is_op(type_end, "(") and self.is_op(self.after(type_end), "{"):
            brace = self.after(type_end)
            classes.append(self.anonymous(_type_text(self.tokens, name_start, type_end), brace))
            return self.after(brace)
        return i + 1

    def lambda_params(self, arrow: int) -> tuple[list[str], int]:
        before = arrow - 1
        if self.is_ident(before):
            return [self.text(before)], before
        if self.is_op(before, ")"):
            opened = self.partner[before]
            names = []
            for piece in self.split_commas(opened + 1, before):
                last = piece.end - 1
                if self.is_ident(last):
                    names.append(self.text(last))
            return names, opened
        raise self.fail("expected lambda parameters", arrow)

    def lambda_at(self, arrow: int, end: int) -> Lambda:
        self.enter(arrow)
        params, start = self.lambda_params(arrow)
        lambdas: list[Lambda] = []
        classes: list[TypeDecl] = []
        switches: list[Statement] = []
        if self.is_op(arrow + 1, "{"):
            body = self.block(arrow + 1)
            lam = Lambda(params, body, None, lambdas, classes, switches, self.line(arrow),
                         start, self.after(arrow + 1))  # fmt: skip
        else:
            j = arrow + 1
            while j < end:
                token = self.tokens[j]
                if token.kind == "op" and token.text in (",", ")", "]", "}", ";"):
                    break
                if token.kind == "op" and token.text in _OPEN:
                    j = self.partner[j]
                j += 1
            expression = Span(arrow + 1, j)
            self.scan(arrow + 1, j, lambdas, classes, switches)
            lam = Lambda(params, None, expression, lambdas, classes, switches, self.line(arrow),
                         start, j)  # fmt: skip
        self.leave()
        return lam


def parse(tokens: list[Token]) -> CompilationUnit:
    try:
        return _Parser(tokens).unit()
    except RecursionError:
        line = tokens[0].line if tokens else 1
        raise Unparsable("nested too deep", line) from None
