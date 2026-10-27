import ast
import re
import shutil
import subprocess
import sys
import tomllib
from fnmatch import fnmatch
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).parent.parent
PACKAGE_DATA = ["src/skillpose/core/data/skills_seed.yaml", "src/skillpose/core/defaults.yaml",
                *(f"src/skillpose/core/data/prompts_example/{name}.md" for name in ("cv", "job", "relation", "path")),
                "src/skillpose/web/i18n/de.yaml", "src/skillpose/web/i18n/en.yaml",
                "src/skillpose/web/templates/base.html", "src/skillpose/web/static/style.css",
                "src/skillpose/web/static/logo.svg", "src/skillpose/web/static/icon.svg",
                "src/skillpose/web/static/favicon.svg", "src/skillpose/web/static/apple-touch-icon.png",
                "src/skillpose/demo_data/README.md"]
REPO_FILES = ["constraints.txt", "SECURITY.md", ".github/dependabot.yml", "evals/goldset/README.md"]
ENV_KEYS = ["ANTHROPIC_API_KEY", "MONTHLY_LIMIT_USD", "APP_PASSWORD", "SKILLPOSE_ALLOWED_HOSTS", "SKILLPOSE_PROMPTS",
            "SKILLPOSE_LOG"]


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs a git checkout")
@pytest.mark.parametrize("path", PACKAGE_DATA)
def test_package_data_is_not_git_ignored(path):
    ignored = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0
    assert not ignored, f"{path} is git-ignored and would be missing in a fresh clone"


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs a git checkout")
@pytest.mark.parametrize("path", REPO_FILES)
def test_repo_files_are_not_git_ignored(path):
    assert (ROOT / path).is_file()
    assert subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode != 0, f"{path} is git-ignored"


def test_the_example_prompts_are_package_data():
    # a pip install without them could not run any step
    globs = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["skillpose"]
    files = sorted((ROOT / "src/skillpose/core/data/prompts_example").glob("*.md"))
    assert [f.name for f in files] == ["cv.md", "job.md", "path.md", "relation.md"]
    assert [f.name for f in files
            if not any(fnmatch(f.relative_to(ROOT / "src/skillpose").as_posix(), g) for g in globs)] == []


def test_compose_publishes_only_on_loopback():
    ports = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["skillpose"]["ports"]
    assert ports and all(p.startswith("127.0.0.1:") for p in ports)


def test_compose_shows_how_to_mount_a_prompt_folder_without_mounting_one():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "# - ./my-prompts:/prompts:ro" in text and "SKILLPOSE_PROMPTS=/prompts" in text
    # only a comment: a folder of the reader is never mounted by default
    assert yaml.safe_load(text)["services"]["skillpose"]["volumes"] == ["skillpose-data:/data"]
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    assert ("A `SKILLPOSE_PROMPTS` folder that cannot be read ends the app with code 2 in the same way" in readme
            and "commented volume line that mounts a prompt folder read-only" in readme)


def test_ci_uses_no_secrets_and_runs_all_checks():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "secrets." not in ci and all(x in ci for x in ["ruff check", "pytest", "docker build"])


def test_container_runs_as_non_root():
    assert any(line.startswith("USER ") and "root" not in line
               for line in (ROOT / "Dockerfile").read_text().splitlines())


def test_env_example_lists_every_setting():
    lines = (ROOT / ".env.example").read_text().splitlines()
    settings = dict(line.split("=", 1) for line in lines if line.strip() and not line.startswith("#"))
    assert sorted(settings) == sorted(ENV_KEYS)
    assert {key for key, value in settings.items() if value.strip()} == {"MONTHLY_LIMIT_USD"}
    for i, line in enumerate(lines):   # one comment line above each setting says what it is for
        if line.split("=", 1)[0] in ENV_KEYS:
            assert i > 0 and lines[i - 1].startswith("#"), f"{line.split('=')[0]} has no comment line"


def test_project_metadata_is_ready_for_release():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["version"] == "0.2.0"
    assert project["project"]["license"] == "MIT" and project["project"]["license-files"] == ["LICENSE"]
    assert "setuptools>=77" in project["build-system"]["requires"]


def test_dockerignore_keeps_private_and_bulky_files_out_of_the_image():
    ignored = (ROOT / ".dockerignore").read_text().split()
    assert {".git", ".venv", "data", ".env", "*.sqlite", "tests", "evals", "docs"} <= set(ignored)


# --- pinned dependencies (constraints.txt) ---

def _name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _constraint_lines() -> list[str]:
    lines = (ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.strip() and not line.startswith("#")]


def _pins() -> dict[str, str]:
    return {_name(name): version for name, version in (line.split("==") for line in _constraint_lines())}


def test_constraints_are_exact_sorted_and_leave_out_the_project_itself():
    lines = _constraint_lines()
    assert len(lines) > 20 and all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*==[0-9][A-Za-z0-9.+!]*", x) for x in lines)
    names = [line.split("==")[0] for line in lines]
    assert names == sorted(names, key=str.lower) and len({_name(n) for n in names}) == len(names)
    assert "skillpose" not in _pins()
    first = (ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("#") and "pip freeze" in first   # the one line that says how to make the file again


def test_constraints_pin_every_dependency_named_in_pyproject():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    named = [Requirement(spec) for spec in project["dependencies"]]
    named += [Requirement(spec) for specs in project["optional-dependencies"].values() for spec in specs]
    assert len(named) >= 10
    pins = _pins()
    assert [r.name for r in named if _name(r.name) not in pins] == []
    # and a pin never contradicts the range that pyproject allows
    assert [r.name for r in named if not r.specifier.contains(Version(pins[_name(r.name)]))] == []


def test_every_package_the_code_imports_is_declared():
    # httpx2 was imported by core/llm.py and cli.py, but came only along with anthropic
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    specs = project["dependencies"] + project["optional-dependencies"]["web"]
    declared = {_name(Requirement(spec).name) for spec in specs}
    distribution = {"yaml": "pyyaml", "docx": "python-docx"}   # where the import name is not the name on PyPI
    imported = set()
    for path in (ROOT / "src/skillpose").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    outside = imported - set(sys.stdlib_module_names) - {"skillpose"}
    assert {"anthropic", "httpx2", "pydantic", "yaml", "docx", "pypdf"} <= outside
    needed = {_name(distribution.get(name, name)) for name in outside}
    assert sorted(needed - declared) == []


def test_the_floors_of_anthropic_and_httpx2_are_tested_versions_that_the_pins_satisfy():
    # the stream events and the retry of a started stream (core/llm.py) were checked against anthropic 2.3; httpx2
    # is a direct dependency. A floor never lies above its pin (a pin is what the tests ran with).
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    named = {_name(r.name): r for r in map(Requirement, project["dependencies"])}
    floors = {name: [Version(s.version) for s in named[name].specifier if s.operator == ">="]
              for name in ("anthropic", "httpx2")}
    assert floors["anthropic"] == [Version("2.3")] and len(floors["httpx2"]) == 1
    assert not named["anthropic"].specifier.contains(Version("2.2.9"))
    assert [name for name, [floor] in floors.items() if floor > Version(_pins()[name])] == []


def test_the_extras_name_a_lower_bound_that_the_pins_satisfy():
    # lower bounds are the versions tested when they were set; constraints.txt may be newer, so a minor bump of a pin
    # must not break this test. Only the bound itself must hold: every extra has one and no pin lies below it.
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]
    pins = _pins()
    lower = {}
    for spec in extras["web"] + extras["dev"]:
        r = Requirement(spec)
        bounds = [Version(s.version) for s in r.specifier if s.operator == ">="]
        assert bounds, f"{r.name} has no lower bound"
        lower[_name(r.name)] = bounds[0]
    assert {"fastapi", "starlette", "jinja2", "uvicorn", "python-multipart", "httpx2", "pytest", "ruff"} <= set(lower)
    assert [name for name, bound in lower.items() if bound > Version(pins[name])] == []


def test_the_web_extra_never_allows_a_starlette_without_the_host_check():
    # a security floor, not a marker of the tested version: the host check against DNS rebinding (TrustedHost) relies
    # on the parse_host_header of Starlette 1.9 and later; fastapi alone allows starlette>=0.46
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]
    starlette = [r for r in map(Requirement, extras["web"]) if _name(r.name) == "starlette"]
    assert len(starlette) == 1
    assert not starlette[0].specifier.contains(Version("1.8.9")) and starlette[0].specifier.contains(Version("1.9"))


def test_dockerfile_installs_with_the_constraints():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY pyproject.toml README.md LICENSE constraints.txt ./" in dockerfile
    assert 'pip install --no-cache-dir -c constraints.txt ".[web]"' in dockerfile


def test_dockerfile_pins_the_base_image_by_digest():
    first = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()[0]
    # the tag is for people, the digest is what the build uses
    assert re.fullmatch(r"FROM python:3\.12-slim@sha256:[0-9a-f]{64}", first), first


def test_dependabot_can_update_the_base_image():
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))
    assert any(u["package-ecosystem"] == "docker" and u["directory"] == "/" for u in config["updates"])
    assert (ROOT / "Dockerfile").is_file()   # Dependabot reads the FROM line of /Dockerfile, tag and digest together


def test_ci_installs_with_the_constraints():
    assert 'pip install -c constraints.txt -e ".[dev,web]"' in (ROOT / ".github/workflows/ci.yml").read_text()


def test_the_build_context_keeps_the_constraints():
    assert "constraints.txt" not in (ROOT / ".dockerignore").read_text().split()


# --- dependabot, security policy, gold-set note ---

def test_dependabot_checks_pip_docker_and_github_actions_monthly():
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))
    assert config["version"] == 2
    assert {u["package-ecosystem"] for u in config["updates"]} == {"pip", "docker", "github-actions"}
    assert len(config["updates"]) == 3
    assert all(u["directory"] == "/" and u["schedule"] == {"interval": "monthly"} for u in config["updates"])


def test_security_policy_uses_private_reporting_and_gives_no_address():
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert text.startswith("# Security policy\n")
    for part in ["private, non-commercial learning project", "no bug bounty", "## Reporting a vulnerability",
                 "GitHub's \"Report a vulnerability\" button", "do not open a public issue", "## Supported versions",
                 "Only the latest commit on `main` gets fixes.", "## Scope", "127.0.0.1", "`APP_PASSWORD`"]:
        assert part in text, part
    assert not re.search(r"@|mailto:|https?://", text)   # no address at all, neither mail nor link
    assert not re.search(r"within \d+|\d+ (hours|days)|SLA", text)   # no promise of an answer time


def test_goldset_note_says_everything_is_invented():
    text = (ROOT / "evals/goldset/README.md").read_text(encoding="utf-8")
    assert text.startswith("# Gold set\n")
    for part in ["invented for skillpose", "none is copied from a real posting", "Universities and certificates",
                 "Johannes Kepler University Linz", "Masaryk University", "ISTQB", "PL-300"]:
        assert part in text, part
    assert not re.search(r"@|https?://", text)
