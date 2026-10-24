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


BACKSLASH = chr(0x5C)
ESCAPED_BACKSLASH = BACKSLASH + "u005c"


class TestEscapedBackslash:
    # After an escape that yields a backslash (u005c), JDK 8 and 11 reset the count of
    # preceding backslashes and JDK 17 counts the escaped one, so a raw backslash right after it
    # starts an escape on one and not the other. The checker refuses that case.
    @pytest.mark.parametrize(
        "source",
        [
            "// " + ESCAPED_BACKSLASH + BACKSLASH + "u000a int hidden = 1;",
            "// " + ESCAPED_BACKSLASH + BACKSLASH + BACKSLASH + "u000a x",
            's = "' + ESCAPED_BACKSLASH + BACKSLASH + 'u0041";',
        ],
        ids=["comment_line_break", "two_raw_after", "in_a_string"],
    )
    def test_an_escaped_backslash_followed_by_a_raw_backslash_is_unparsable(self, source):
        with pytest.raises(Unparsable):
            tokenize(source)

    def test_an_escaped_backslash_followed_by_other_text_is_fine(self):
        source = 's = "' + ESCAPED_BACKSLASH + 'n";'
        assert texts(source) == ["s", "=", '"' + BACKSLASH + 'n"', ";"]


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
        start = time.process_time()
        tokenize(source)
        return time.process_time() - start

    seconds(small)
    assert seconds(large) < 8 * seconds(small) + 0.2


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "patches"


def test_every_fixture_pre_image_tokenizes():
    files = sorted(FIXTURES.glob("*/before/**/*.java"))
    assert len(files) > 150
    for path in files:
        tokenize(path.read_bytes().decode("utf-8"))


@pytest.mark.parametrize(
    "ignorable",
    ["\u200b", "\u00ad", "\u2060", "\ufeff", "\u0085", "\u009f"],
    ids=["zero_width_space", "soft_hyphen", "word_joiner", "bom", "c1_next_line", "c1_last"],
)
def test_an_identifier_with_an_ignorable_character_is_unparsable(ignorable):
    # javac drops these when it compares identifiers (JLS 3.8), so @Dis<ZWSP>abled would be
    # @Disabled to it; the checker never normalizes names, so it refuses the file instead.
    with pytest.raises(Unparsable):
        tokenize(f"@Dis{ignorable}abled void t() {{}}")


def test_an_escaped_ignorable_character_in_an_identifier_is_unparsable():
    with pytest.raises(Unparsable):
        tokenize("assert\\u200bEquals(1, 2);")


def _sides(fixture_id: str, tmp_path: Path) -> tuple[list[str], list[str]]:
    from test_checker_fixtures import checked_paths, trees

    fixture = FIXTURES / fixture_id
    pristine, workspace = trees(fixture, tmp_path)
    [path] = [p for p in checked_paths((fixture / "patch.diff").read_text()) if p.endswith(".java")]
    before = [t.text for t in tokenize((pristine / path).read_text(encoding="utf-8"))]
    after = [t.text for t in tokenize((workspace / path).read_text(encoding="utf-8"))]
    return before, after


@pytest.mark.parametrize(
    "fixture_id",
    [
        "reject-unicode-escape-line-break-hides-annotation",
        "reject-unicode-escape-closes-comment-early",
    ],
)
def test_an_escape_that_ends_a_comment_exposes_the_annotation_as_javac_does(tmp_path, fixture_id):
    before, after = _sides(fixture_id, tmp_path)
    count = sum(1 for a, b in zip(after, after[1:], strict=False) if (a, b) == ("@", "Ignore"))
    assert (
        count
        == sum(1 for a, b in zip(before, before[1:], strict=False) if (a, b) == ("@", "Ignore")) + 1
    )


@pytest.mark.parametrize(
    "fixture_id",
    ["reject-unicode-escape-opens-hidden-comment", "reject-line-separator-in-line-comment"],
)
def test_a_hidden_comment_removes_the_assertion_as_javac_does(tmp_path, fixture_id):
    before, after = _sides(fixture_id, tmp_path)
    assert after.count("assertEquals") == before.count("assertEquals") - 1
