import time
from pathlib import Path

import pytest

from rootflake.gate.checker.lexer import Token, Unparsable, tokenize


def texts(source: str) -> list[str]:
    return [token.text for token in tokenize(source)]


def kinds(source: str) -> list[str]:
    return [token.kind for token in tokenize(source)]


class TestTokens:
    def test_a_small_class_tokenizes(self):
        assert texts("class A { void f() { x = 1; } }") == [
            "class", "A", "{", "void", "f", "(", ")", "{", "x", "=", "1", ";", "}", "}",
        ]  # fmt: skip

    def test_keywords_identifiers_and_literals_have_their_kinds(self):
        assert kinds('if (x) return "s" + \'c\' + 1L;') == [
            "keyword", "op", "ident", "op", "keyword", "string", "op", "char", "op", "number",
            "op",
        ]  # fmt: skip

    def test_true_false_and_null_are_keywords(self):
        assert kinds("true false null") == ["keyword"] * 3

    def test_contextual_keywords_are_identifiers(self):
        assert kinds("var record yield sealed permits") == ["ident"] * 5

    def test_dollar_underscore_and_unicode_letters_form_identifiers(self):
        assert texts("$x _y caf\u00e9 \u0394t") == ["$x", "_y", "caf\u00e9", "\u0394t"]

    @pytest.mark.parametrize(
        "number",
        ["0", "123", "1_000", "0x1F", "0X1fL", "0b1010", "017", "1.5", "1.5e10", "1e-3",
         "2.0f", "3d", "0x1.8p3", ".5", "100L", "1_000.000_1E+1_0"],
    )  # fmt: skip
    def test_numbers_are_one_token(self, number):
        assert texts(f"x = {number};") == ["x", "=", number, ";"]

    @pytest.mark.parametrize(
        "op",
        ["->", "::", "...", "==", "!=", "<=", ">=", "&&", "||", "++", "--", "+=", "<<", "<<=",
         "@", "?", ":"],
    )  # fmt: skip
    def test_operators_use_the_longest_match(self, op):
        assert texts(f"a {op} b") == ["a", op, "b"]

    def test_every_greater_than_is_its_own_token_so_generics_close(self):
        assert texts("Map<K, List<V>> m; a >>> b; c >>= 1;") == [
            "Map", "<", "K", ",", "List", "<", "V", ">", ">", "m", ";",
            "a", ">", ">", ">", "b", ";", "c", ">", ">=", "1", ";",
        ]  # fmt: skip

    def test_string_escapes_do_not_end_the_string(self):
        assert texts(r'"a\"b\\" x') == [r'"a\"b\\"', "x"]

    def test_a_char_literal_with_an_escaped_quote(self):
        assert texts(r"c = '\'';") == ["c", "=", r"'\''", ";"]

    def test_a_text_block_is_one_token(self):
        source = 'String s = """\n    a "quoted" ""\\""" b\n    """;'
        tokens = tokenize(source)
        assert [t.kind for t in tokens] == ["ident", "ident", "op", "text_block", "op"]

    def test_tokens_carry_their_line(self):
        assert [t.line for t in tokenize("a\nb\r\nc\rd")] == [1, 2, 3, 4]

    def test_token_is_a_value(self):
        assert tokenize("x")[0] == Token("ident", "x", 1)


class TestComments:
    def test_comments_are_removed(self):
        assert texts("a // b\nc /* d */ e /** f */ g") == ["a", "c", "e", "g"]

    def test_comment_markers_inside_strings_are_text(self):
        assert texts('s = "// not /* a comment */";') == ["s", "=", '"// not /* a comment */"', ";"]

    def test_assertion_text_inside_a_text_block_is_not_an_assertion(self):
        source = 's = """\n    @Test assertEquals(1, 2); // x /* y\n    """;'
        assert "assertEquals" not in texts(source)
        assert "@" not in texts(source)

    @pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085", "\x0b", "\x0c"])
    def test_only_cr_lf_and_crlf_end_a_line_comment(self, separator):
        assert texts(f"a // b{separator}assertEquals(1, 2);\nc") == ["a", "c"]

    @pytest.mark.parametrize("end", ["\n", "\r", "\r\n"])
    def test_cr_lf_and_crlf_each_end_a_line_comment(self, end):
        assert texts(f"a // b{end}c") == ["a", "c"]


class TestUnicodeEscapes:
    def test_unicode_escapes_are_translated_before_lexing(self):
        assert texts("// \\u000d @Disabled\nx") == ["@", "Disabled", "x"]

    def test_an_escaped_star_slash_closes_a_comment_early(self):
        assert texts('/* \\u002a/ @Ignore("r") /* */') == ["@", "Ignore", "(", '"r"', ")"]

    def test_an_escaped_star_opens_a_hidden_comment(self):
        assert texts("/\\u002a assertEquals(0, n); */ x") == ["x"]

    def test_an_escaped_backslash_does_not_start_an_escape(self):
        assert texts('s = "\\\\u0041";') == ["s", "=", '"\\\\u0041"', ";"]

    def test_three_backslashes_start_an_escape(self):
        assert texts('s = "\\\\\\u0041";') == ["s", "=", '"\\\\A"', ";"]

    def test_any_number_of_u_is_allowed(self):
        assert texts("\\uuu0041b") == ["Ab"]

    def test_an_escape_can_form_an_identifier(self):
        assert texts("\\u0061ssertTrue") == ["assertTrue"]

    @pytest.mark.parametrize("bad", ["\\u00", "\\u00g1", "\\uZZZZ", "\\u"])
    def test_a_malformed_escape_is_unparsable(self, bad):
        with pytest.raises(Unparsable):
            tokenize(f"x {bad} y")

    def test_an_escaped_newline_does_not_change_reported_lines(self):
        tokens = tokenize("a // \\u000a b\nc")
        assert [(t.text, t.line) for t in tokens] == [("a", 1), ("b", 1), ("c", 2)]


class TestUnparsable:
    @pytest.mark.parametrize(
        "source",
        ["/* never closed", '"never closed', 's = "line\nbreak";', "c = 'ab", '"""\n never',
         's = """ no line break """;', "x # y", "x ` y"],
        ids=["comment", "string", "string_line_break", "char", "text_block",
             "text_block_opening", "hash", "backtick"],
    )  # fmt: skip
    def test_unterminated_or_foreign_input_is_unparsable(self, source):
        with pytest.raises(Unparsable):
            tokenize(source)

    @pytest.mark.parametrize("end", ["\n", "\r"])
    def test_a_backslash_before_a_line_break_in_a_string_is_unparsable(self, end):
        with pytest.raises(Unparsable):
            tokenize(f's = "a\\{end}b";')

    def test_a_no_break_space_is_not_whitespace(self):
        with pytest.raises(Unparsable):
            tokenize("a\u00a0b")

    def test_a_trailing_ctrl_z_is_allowed(self):
        assert texts("a\x1a") == ["a"]


def test_crlf_and_lf_files_report_the_same_lines():
    lf = "class A {\n    // c\n    void f() {\n        x();\n    }\n}\n"
    crlf = lf.replace("\n", "\r\n")
    assert tokenize(lf) == tokenize(crlf)


def test_lexing_is_linear():
    unit = '    void f() { assertEquals(1, g("s")); } // note\n'
    small, large = unit * 5000, unit * 20000

    def seconds(source):
        start = time.perf_counter()
        tokenize(source)
        return time.perf_counter() - start

    seconds(small)
    assert seconds(large) < 8 * seconds(small) + 0.2


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "patches"


def test_every_fixture_pre_image_tokenizes():
    files = sorted(FIXTURES.glob("*/before/**/*.java"))
    assert len(files) > 150
    for path in files:
        tokenize(path.read_bytes().decode("utf-8"))
