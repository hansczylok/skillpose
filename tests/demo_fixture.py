"""A demo bundle for tests: one person "Maria Demo", five postings of one role family, one path in German and in
English, two cost rows; with bundle_with_answers also the answers to the conditions of postings."""

from pathlib import Path

from skillpose.core.models import (
    CandidateProfile,
    CandidateSkill,
    Conditions,
    DevelopmentPath,
    Evidence,
    JobMatch,
    JobProfile,
    PathStep,
    Requirement,
    RequirementMatch,
    SkillProposal,
    Station,
    text_hash,
)
from skillpose.demo import COSTS_FILE, DemoBundle, DemoCosts, DemoPosting, DemoTranslation
from skillpose.store.db import CostRow

KEY = "maria_demo"
NAME = "Maria Demo"
CV_TEXT = ("Maria Demo - QA Engineer. 2016-2026 Acme Testing GmbH: Built a Selenium test automation framework in Java "
           "that cut regression time from 5 days to 4 hours.")
TIERS = [1, 1, 2, 3, 1]
COSTS = [
    CostRow(ts="2026-10-20T09:00:00", step="cv", model="claude-opus-5-5", input=3000, output=4000, cache_write=5000,
            cache_read=0, usd=0.17, batch=False),
    CostRow(ts="2026-10-20T09:05:00", step="path", model="claude-opus-5-5", input=1500, output=3000,
            cache_write=2500, cache_read=0, usd=0.09, batch=False),
]


def _evidence(quote: str) -> Evidence:
    return Evidence(quote=quote, context="Acme Testing GmbH", period_from=2016, period_to=None, strength="outcome",
                    verified=True)


EVIDENCE = _evidence("Built a Selenium test automation framework in Java")


def _profile() -> CandidateProfile:
    skills = [CandidateSkill(skill_id="selenium", level="expert", years=10, last_used=2026, evidence=[EVIDENCE]),
              CandidateSkill(skill_id="java", level="strong", years=10, last_used=2026, evidence=[EVIDENCE])]
    station = Station(title="QA Engineer", employer="Acme Testing GmbH", period_from=2016, period_to=None)
    return CandidateProfile(language="de", skills=skills, stations=[station])


def _posting_and_match(i: int, tier: int) -> tuple[DemoPosting, JobMatch]:
    text = f"Test Automation Engineer {i}\nSelenium required, Kubernetes {'required' if tier > 1 else 'a plus'}."
    h = text_hash(text)
    selenium = Requirement(skill_id="selenium", importance="must", min_level=None, min_years=None, kind="tool",
                           quote="Selenium required")
    kubernetes = Requirement(skill_id="kubernetes", importance="nice" if tier == 1 else "must", min_level=None,
                             min_years=5 if tier == 3 else None, kind="experience" if tier == 3 else "tool",
                             quote="Kubernetes")
    job = JobProfile(text_hash=h, title=f"Test Automation Engineer {i}", role_family="test_automation",
                     seniority="mid", requirements=[selenium, kubernetes])
    met = RequirementMatch(requirement=selenium, status="met", reason="evidenced", evidence=EVIDENCE, gap_type=None,
                           effort_weeks=None)
    gap = RequirementMatch(requirement=kubernetes, status="gap", reason="not in the CV", evidence=None,
                           gap_type="years" if tier == 3 else "weeks", effort_weeks=None if tier == 3 else 4)
    match = JobMatch(text_hash=h, matches=[met, gap], must_coverage=1.0 if tier == 1 else 0.5,
                     nice_coverage=0.0 if tier == 1 else 1.0, tier=tier)
    return DemoPosting(hash=h, refs=[f"job_{i}.md"], text=text, job_profile=job), match


def fixture_bundle() -> DemoBundle:
    pairs = [_posting_and_match(i, tier) for i, tier in enumerate(TIERS)]
    step = PathStep(activity="Einen kleinen Kubernetes-Cluster aufsetzen und Tests darin laufen lassen",
                    closes=["kubernetes"], effort_weeks=4, proof="öffentliches Repository mit Pipeline")
    german = DevelopmentPath(role_family="test_automation", language="de", rationale="Zuerst die häufigste Lücke.",
                             steps=[step], total_weeks=4, input_hash="0123456789abcdef")
    step = PathStep(activity="Set up a small Kubernetes cluster and run the tests in it", closes=["kubernetes"],
                    effort_weeks=4, proof="public repository with a pipeline")
    english = DevelopmentPath(role_family="test_automation", language="en", rationale="The most frequent gap first.",
                              steps=[step], total_weeks=4, input_hash="fedcba9876543210")
    return DemoBundle(key=KEY, cv_name=NAME, cv_text=CV_TEXT, profile=_profile(),
                      postings=sorted((p for p, _ in pairs), key=lambda p: p.hash),
                      matches=sorted((m for _, m in pairs), key=lambda m: m.text_hash), paths=[german, english])


# invented answers to the conditions of postings, as the demo has them: one set for all its persons
DEMO_ANSWERS = Conditions(driving_licence_b=True, travel_share_max=20, shift_work=False, commute_km_max=40,
                          remote_only=False, full_time=True, notes="Invented answers of the demo")


def bundle_with_answers(answers: Conditions = DEMO_ANSWERS) -> DemoBundle:
    """fixture_bundle with answers to the conditions of postings (a bundle from before them has none)."""
    return fixture_bundle().model_copy(update={"conditions": answers})


def bundle_with_proposals() -> DemoBundle:
    """fixture_bundle with skills that only Claude's proposals name, as in the real demo: the vocabulary of the demo
    store does not know them. The profile has DAX (a tool), every posting a must requirement Apache Flink that is a
    gap, and the path closes it too."""
    bundle = fixture_bundle()
    bundle.profile.skills.append(CandidateSkill(skill_id="dax", level="working", years=2, last_used=2026,
                                                evidence=[EVIDENCE]))
    bundle.profile.proposed_skills.append(SkillProposal(id="dax", name="DAX", category="tool"))
    flink = Requirement(skill_id="apache_flink", importance="must", min_level=None, min_years=None, kind="tool",
                        quote="Apache Flink")
    for posting in bundle.postings:
        posting.job_profile.requirements.append(flink)
        posting.job_profile.proposed_skills.append(SkillProposal(id="apache_flink", name="Apache Flink",
                                                                 category="tool"))
    for match in bundle.matches:
        match.matches.append(RequirementMatch(requirement=flink, status="gap", reason="not in the CV", evidence=None,
                                              gap_type="weeks", effort_weeks=4))
    for path in bundle.paths:
        path.steps[0].closes.append("apache_flink")
    return bundle


GERMAN_TITLE = "Testautomatisierer Linz"
GERMAN_TEXT = ("Testautomatisierer (m/w/d) – Linz\n"
               "Wir suchen Verstärkung mit Selenium und Java, Kubernetes ist von Vorteil.")
ENGLISH_TITLE = "Test Automation Specialist Linz"
ENGLISH_TEXT = "Test Automation Specialist (m/f/d) – Linz\nWe are looking for support with Selenium and Java."
# the German translation of an English posting, with markup in it: the pages must show it escaped
MARKUP_TITLE = "Testautomatisierung <b>Eins</b>"
MARKUP_TEXT = "Testautomatisierung <script>alert(1)</script>\n  - Selenium ist Voraussetzung"


def bundle_with_translations() -> DemoBundle:
    """fixture_bundle with a German posting (the one of tier 2) and two translations: the German posting into English,
    the English posting of tier 3 into German (with markup in it). The other three postings have none."""
    bundle = fixture_bundle()
    by_tier = {jm.tier: jm for jm in bundle.matches}
    german = next(p for p in bundle.postings if p.hash == by_tier[2].text_hash)
    h = text_hash(GERMAN_TEXT)
    by_tier[2].text_hash = h
    german.hash, german.text, german.refs = h, GERMAN_TEXT, ["job_de.md"]
    german.job_profile.text_hash, german.job_profile.title = h, GERMAN_TITLE
    german.translations = {"en": DemoTranslation(title=ENGLISH_TITLE, text=ENGLISH_TEXT)}
    english = next(p for p in bundle.postings if p.hash == by_tier[3].text_hash)
    english.translations = {"de": DemoTranslation(title=MARKUP_TITLE, text=MARKUP_TEXT)}
    bundle.postings.sort(key=lambda p: p.hash)
    bundle.matches.sort(key=lambda m: m.text_hash)
    return bundle


def write_fixture_bundles(dir: Path, bundle: DemoBundle | None = None) -> None:
    """<dir>/maria_demo.json and <dir>/costs.json, written like export_demo writes them."""
    dir.mkdir(parents=True, exist_ok=True)
    (dir / f"{KEY}.json").write_text((bundle or fixture_bundle()).model_dump_json(indent=1) + "\n", encoding="utf-8",
                                     newline="\n")
    (dir / COSTS_FILE).write_text(DemoCosts(rows=COSTS).model_dump_json(indent=1) + "\n", encoding="utf-8",
                                  newline="\n")
