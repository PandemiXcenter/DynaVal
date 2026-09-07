import hashlib
from pathlib import Path

import pytest

from dynaval.domain.errors import ImportFailure
from dynaval.domain.models import ParserOptions
from dynaval.importers import base, parse_dataset


def write_source(tmp_path: Path, text: str, suffix: str = "csv", encoding: str = "utf-8") -> Path:
    path = tmp_path / f"source.{suffix}"
    path.write_bytes(text.encode(encoding))
    return path


def test_dollar_csv_preserves_exact_text_quotes_newlines_and_source(tmp_path: Path) -> None:
    text = (
        "image$fp.name$code$flag$space$empty\r\n"
        '\\\\host\\share\\1.jpg$"Søren $ \'quoted\' ""value""\r\nnext"$001$FALSE$  $\r\n'
        '\r\nhttp://example.test/x.png$NA$002$true$ x $""\r\n'
    )
    path = write_source(tmp_path, text, encoding="utf-8-sig")
    before = path.read_bytes()
    result = parse_dataset(path)
    assert result.columns == ["image", "fp.name", "code", "flag", "space", "empty"]
    assert result.parser.delimiter == "$"
    assert len(result.rows) == 2
    assert [cell.text for cell in result.rows[0]] == [
        "\\\\host\\share\\1.jpg",
        "Søren $ 'quoted' \"value\"\r\nnext",
        "001",
        "FALSE",
        "  ",
        "",
    ]
    assert all(cell.kind == "string" for row in result.rows for cell in row)
    assert result.rows[1][1].text == "NA"
    assert result.sha256 == hashlib.sha256(before).hexdigest()
    assert path.read_bytes() == before


@pytest.mark.parametrize("delimiter", [",", "\t", ";", "|", "$", "^"])
def test_explicit_delimiters(tmp_path: Path, delimiter: str) -> None:
    path = write_source(tmp_path, f"image{delimiter}value\na.png{delimiter}004\n")
    result = parse_dataset(path, ParserOptions(delimiter=delimiter))
    assert result.rows[0][1].text == "004"


def test_tsv_and_single_column_csv(tmp_path: Path) -> None:
    path = write_source(tmp_path, "a\tb\nx\tNA\n", "tsv")
    assert parse_dataset(path).parser.delimiter == "\t"
    path = write_source(tmp_path, 'one\n""\n')
    assert parse_dataset(path, ParserOptions(delimiter=",")).rows[0][0].text == ""


def test_ambiguous_delimiter_requires_explicit_choice(tmp_path: Path) -> None:
    path = write_source(tmp_path, "first,second;third\none,two;three\n")
    with pytest.raises(ImportFailure, match="ambiguous"):
        parse_dataset(path)
    assert parse_dataset(path, ParserOptions(delimiter=";")).columns == ["first,second", "third"]


@pytest.mark.parametrize(
    "text,match",
    [
        ("a,a\n1,2\n", "Duplicate"),
        ("a, \n1,2\n", "blank"),
        ("a,b\n1,2,3\n", "Record 1.*line 2"),
        ('a,b\n1,"broken\n', "Malformed CSV"),
        ("a,b\n", "at least one"),
        ("", "at least one"),
    ],
)
def test_bad_csv_never_silently_drops_data(tmp_path: Path, text: str, match: str) -> None:
    path = write_source(tmp_path, text)
    with pytest.raises(ImportFailure, match=match):
        parse_dataset(path, ParserOptions(delimiter=","))


@pytest.mark.parametrize("delimiter", ["", "xx", "\n", '"', "\0"])
def test_invalid_delimiter(tmp_path: Path, delimiter: str) -> None:
    path = write_source(tmp_path, "a,b\nx,y")
    with pytest.raises(ImportFailure, match="one character"):
        parse_dataset(path, ParserOptions(delimiter=delimiter))


def test_encoding_requires_explicit_override(tmp_path: Path) -> None:
    path = write_source(tmp_path, "a,b\nSøren,001", encoding="cp1252")
    with pytest.raises(ImportFailure, match="encoding"):
        parse_dataset(path)
    assert parse_dataset(path, ParserOptions(encoding="cp1252")).rows[0][0].text == "Søren"
    with pytest.raises(ImportFailure, match="encoding"):
        parse_dataset(path, ParserOptions(encoding="not-an-encoding"))


@pytest.mark.parametrize("suffix", ["jsonl", "ndjson", "json"])
def test_json_exact_numbers_value_kinds_and_schema_union(tmp_path: Path, suffix: str) -> None:
    records = [
        '{"image":"a.png","fp.age":900719925474099312345,"decimal":1.234567890123456789,'
        '"maybe":null,"empty":"","literal":"null","nested":{"x":[1.00000000000000001,true]}}',
        '{"image":"b.png","extra":false,"fp.age":"004"}',
    ]
    text = "[" + ",".join(records) + "]" if suffix == "json" else "\n\n".join(records)
    result = parse_dataset(write_source(tmp_path, text, suffix))
    assert result.columns == [
        "image",
        "fp.age",
        "decimal",
        "maybe",
        "empty",
        "literal",
        "nested",
        "extra",
    ]
    assert result.rows[0][1].text == "900719925474099312345"
    assert result.rows[0][2].text == "1.234567890123456789"
    assert result.rows[0][3].kind == "null"
    assert result.rows[0][3].text == "null"
    assert result.rows[0][4].kind == "string"
    assert result.rows[0][4].text == ""
    assert result.rows[0][5].kind == "string"
    assert result.rows[0][6].text == '{"x":[1.00000000000000001,true]}'
    assert result.rows[0][7].kind == "missing"
    assert result.rows[1][1].text == "004"
    assert result.rows[1][2].kind == "missing"
    assert result.rows[1][7].text == "false"


def test_jsonl_unicode_line_separator_is_not_a_record_separator(tmp_path: Path) -> None:
    result = parse_dataset(write_source(tmp_path, '{"text":"x\u2028y\u0085z"}\n', "jsonl"))
    assert result.rows[0][0].text == "x\u2028y\u0085z"


@pytest.mark.parametrize(
    "text,suffix,match",
    [
        ('{"a":1,"a":2}', "jsonl", "Duplicate"),
        ('{"a":{"b":1,"b":2}}', "jsonl", "Duplicate"),
        ('{"a":NaN}', "jsonl", "Non-finite"),
        ('{"a":Infinity}', "jsonl", "Non-finite"),
        ('{"a":-Infinity}', "jsonl", "Non-finite"),
        ('{"a":1}\n[2]', "jsonl", "line 2"),
        ('{"a":1}\n{"b":}', "jsonl", "line 2"),
        ('{"a":1}', "json", "array of objects"),
        ("[1]", "json", "array of objects"),
        ("[]", "json", "at least one"),
        ("[{}]", "json", "at least one"),
        ('[{" ":1}]', "json", "blank"),
        ('[{"a":"\\ud800"}]', "json", "Unicode"),
    ],
)
def test_invalid_json_is_actionable(tmp_path: Path, text: str, suffix: str, match: str) -> None:
    with pytest.raises(ImportFailure, match=match):
        parse_dataset(write_source(tmp_path, text, suffix))


def test_upload_name_format_override_progress_and_cancel(tmp_path: Path) -> None:
    path = write_source(tmp_path, "a,b\nx,y", "staged")
    progress: list[float] = []
    result = parse_dataset(path, name="original.csv", progress=progress.append)
    assert result.name == "original.csv"
    assert progress[0] == 0
    assert progress[-1] == 1
    assert progress == sorted(progress)
    assert parse_dataset(path, ParserOptions(format="csv")).parser.format == "csv"
    with pytest.raises(ImportFailure, match="format"):
        parse_dataset(path)
    with pytest.raises(ImportFailure, match="canceled"):
        parse_dataset(path, name="original.csv", cancel=lambda: True)


def test_size_limit_and_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_source(tmp_path, "a,b\nx,y")
    monkeypatch.setattr(base, "MAX_DATASET_BYTES", 3)
    with pytest.raises(ImportFailure, match="100 MiB"):
        parse_dataset(path)
    with pytest.raises(ImportFailure, match="Cannot read"):
        parse_dataset(tmp_path / "missing.csv")
