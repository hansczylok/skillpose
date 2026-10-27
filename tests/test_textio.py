import pytest
from conftest import make_docx, make_pdf

from skillpose.core.textio import CvTextError, read_cv, read_postings


def test_docx_paragraphs_and_tables(tmp_path):
    text = read_cv(make_docx(tmp_path, para="Senior QA Engineer at Acme " * 10, cell="Selenium"))
    assert "Senior QA Engineer" in text and "Selenium" in text


def test_pdf_text(tmp_path):
    assert "Testautomatisierung" in read_cv(make_pdf(tmp_path, "Testautomatisierung mit Selenium " * 10))


def test_scanned_pdf_rejected(tmp_path):
    with pytest.raises(CvTextError, match="no readable text"):
        read_cv(make_pdf(tmp_path, ""))


def test_size_and_type_limits(tmp_path):
    big = tmp_path / "cv.txt"
    big.write_bytes(b"a" * (5 * 1024 * 1024 + 1))
    with pytest.raises(CvTextError, match="5 MB"):
        read_cv(big)
    odd = tmp_path / "cv.odt"
    odd.write_text("x" * 300)
    with pytest.raises(CvTextError, match="unsupported"):
        read_cv(odd)


def test_cp1252_text_file(tmp_path):
    p = tmp_path / "cv.txt"
    p.write_bytes(("Prüfung und Qualitätssicherung " * 10).encode("cp1252"))
    assert "Qualitätssicherung" in read_cv(p)


def test_read_postings_dir_and_json(tmp_path):
    (tmp_path / "b.md").write_text("Job B")
    (tmp_path / "a.txt").write_text("Job A")
    j = tmp_path / "jobs.json"
    j.write_text('[{"text": "Job C", "ref": "c-1"}]')
    assert read_postings([tmp_path / "a.txt", tmp_path / "b.md", j]) == [("a.txt", "Job A"), ("b.md", "Job B"),
                                                                         ("c-1", "Job C")]
    assert read_postings([tmp_path]) == [("a.txt", "Job A"), ("b.md", "Job B")]


def test_json_postings_without_ref(tmp_path):
    j = tmp_path / "jobs.json"
    j.write_text('[{"text": "Job A"}, {"text": "Job B", "ref": "b-1"}, {"text": "Job C", "ref": null}]')
    assert read_postings([j]) == [("jobs.json#1", "Job A"), ("b-1", "Job B"), ("jobs.json#3", "Job C")]


def test_csv_postings(tmp_path):
    a = tmp_path / "a.csv"
    a.write_text("ref,text\nr1,Job A\n", encoding="utf-8")
    b = tmp_path / "b.csv"
    b.write_text("text;ref\nJob B;r2\n", encoding="utf-8-sig")
    c = tmp_path / "c.csv"
    c.write_text("text\nJob C\n", encoding="utf-8")
    assert read_postings([a, b, c]) == [("r1", "Job A"), ("r2", "Job B"), ("c.csv#1", "Job C")]
    d = tmp_path / "d.csv"
    d.write_text("title\nX\n")
    with pytest.raises(ValueError, match="text"):
        read_postings([d])


def test_csv_tab_quotes_and_unquoted_delimiters_in_the_last_column(tmp_path):
    t = tmp_path / "t.tsv.csv"
    t.write_text('ref\ttext\nr1\t"Job A\nsecond line"\n\tJob B\n', encoding="utf-8")
    assert read_postings([t]) == [("r1", "Job A\nsecond line"), ("t.tsv.csv#2", "Job B")]
    one = tmp_path / "one.csv"
    one.write_text("text\nJob A, Berlin, remote\n", encoding="utf-8")
    assert read_postings([one]) == [("one.csv#1", "Job A, Berlin, remote")]
    first = tmp_path / "first.csv"
    first.write_text("text;ref\nJob; B;r2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        read_postings([first])
