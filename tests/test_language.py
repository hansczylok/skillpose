from pathlib import Path

import pytest

from skillpose.core.language import detect_language, other_language

POSTINGS = Path(__file__).parent.parent / "evals" / "goldset" / "postings"
# read by hand: the gold postings written in English (all others are German)
ENGLISH = {"03", "05", "07", "09", "12", "15", "17", "20", "22", "24", "25", "27", "30"}


def test_every_gold_posting_gets_its_language():
    files = sorted(POSTINGS.glob("*.md"))
    assert len(files) == 30
    found = {p.name[:2]: detect_language(p.read_text(encoding="utf-8")) for p in files}
    assert {number for number, lang in found.items() if lang == "en"} == ENGLISH
    assert sum(lang == "de" for lang in found.values()) == 17


@pytest.mark.parametrize("text, lang", [
    ("We are looking for a tester with Selenium and Java. You will work in our team.", "en"),
    ("Wir suchen eine Testerin mit Selenium und Java. Du arbeitest in unserem Team.", "de"),
    ("QA Engineer (m/w/d)\nEnd-to-End-Tests mit Playwright und Python für unsere Web-Plattform", "de"),
    ("Data Engineer (m/f/d)\nSpark, Kafka, AWS, CI/CD", "de"),   # no stop word at all: German, the default
    ("", "de"),
])
def test_detect_language_counts_stop_words(text, lang):
    assert detect_language(text) == lang


def test_other_language():
    assert other_language("de") == "en" and other_language("en") == "de"
