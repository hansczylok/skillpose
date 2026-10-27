from skillpose.core.evidence import apply_verification, normalize_text, verify_quote
from skillpose.core.models import CandidateSkill, Evidence

# Invented CV text with the quirks PDFs produce: hyphenation at a line break, typographic quotes, a dash, an umlaut.
SRC = ("Aufbau der Lager-\nverwaltung mit Python: 12.500 Artikel in die neue Datenbank übernommen (REST-API).\n"
       "„Agile Coach“ im Team – 3 Jahre.")


def test_exact_and_hyphenated():
    assert verify_quote("Aufbau der Lagerverwaltung mit Python", SRC)


def test_typographic_quotes_and_dashes():
    assert verify_quote('"Agile Coach" im Team - 3 Jahre', SRC)


def test_small_typo_passes_large_change_fails():
    assert verify_quote("12.500 Artikel in die neue Datenbank ubernommen", SRC)
    assert not verify_quote("1.250 Artikel nach Excel exportiert", SRC)


def test_quote_from_elsewhere_fails():
    assert not verify_quote("Mehrjährige Erfahrung mit AWS und Terraform", SRC)


def test_empty_quote_fails():
    assert not verify_quote("  ", SRC)


def test_soft_hyphen_and_case():
    assert normalize_text("Daten­bank  ENTWICKLUNG") == "datenbank entwicklung"


def test_apply_verification_downgrades():
    ok = Evidence(quote="12.500 Artikel in die neue Datenbank übernommen", context="Lager", period_from=2020,
                  period_to=2024, strength="outcome")
    bad = ok.model_copy(update={"quote": "leitete ein Team von 20 Entwicklern"})
    [cs] = apply_verification([CandidateSkill(skill_id="python", level="strong", years=3, evidence=[ok, bad])], SRC)
    assert cs.evidence[0].verified and cs.evidence[0].strength == "outcome"
    assert not cs.evidence[1].verified and cs.evidence[1].strength == "claim"
