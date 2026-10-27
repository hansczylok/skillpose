from skillpose.core.models import Skill, SkillProposal
from skillpose.core.vocabulary import Vocabulary, normalize_name, slug

REQUIRED_IDS = [
    "python", "sql", "java", "csharp", "cpp", "javascript", "r", "selenium", "testng", "pytest", "cucumber", "jenkins",
    "git", "jira", "confluence", "test_automation", "test_management", "manual_testing", "istqb_foundation", "power_bi",
    "excel", "tableau", "power_automate", "uipath", "rpa", "sap_mm", "sap_s4hana", "sap_testing", "aws", "azure",
    "docker", "kubernetes", "airflow", "dbt", "spark", "etl", "data_modeling", "machine_learning", "llm_applications",
    "rest_api", "linux", "agile_scrum", "requirements_engineering", "business_analysis", "english", "german",
    "bachelor_computer_science", "masters_engineering", "stakeholder_management", "customer_support",
]


def test_variants_resolve_to_same_id():
    v = Vocabulary.load()
    assert {v.resolve(x) for x in ["PowerBI", "power-bi", "Power BI", "power_bi"]} == {"power_bi"}
    assert v.resolve("Englisch") == "english"
    assert v.resolve("Quantenkochen") is None


def test_snapshot_is_deterministic():
    a = Vocabulary.load(extra=[Skill(id="zeta", name_en="Zeta", name_de="Zeta", category="tool"),
                               Skill(id="alpha_x", name_en="Alpha", name_de="Alpha", category="tool")])
    b = Vocabulary.load(extra=[Skill(id="alpha_x", name_en="Alpha", name_de="Alpha", category="tool"),
                               Skill(id="zeta", name_en="Zeta", name_de="Zeta", category="tool")])
    assert a.snapshot() == b.snapshot()
    lines = a.snapshot().splitlines()[1:]
    assert lines == sorted(lines)


def test_merge_skips_known_and_duplicates():
    v = Vocabulary.load()
    new = v.merge([SkillProposal(id="power_bi_x", name="Power BI", category="tool"),
                   SkillProposal(id="n8n", name="n8n", category="tool"),
                   SkillProposal(id="n8n_2", name="N8N", category="tool")])
    assert [s.id for s in new] == ["n8n"] and v.resolve("n8n") == "n8n"


def test_seed_contains_required_ids_and_covers_all_categories():
    v = Vocabulary.load()
    assert set(REQUIRED_IDS) <= v.ids()
    assert "n8n" not in v.ids()
    assert len(v.ids()) >= 280
    assert {v.get(i).category for i in v.ids()} == {"tool", "programming_language", "method", "domain", "certificate",
                                                     "degree", "spoken_language", "soft"}


def test_seed_names_do_not_collide():
    v = Vocabulary.load()
    for skill_id in v.ids():
        assert v.resolve(skill_id) == skill_id


def test_normalize_and_slug():
    assert normalize_name("  Power-BI ") == "power bi"
    assert slug("C# / .NET") == "c_net"
