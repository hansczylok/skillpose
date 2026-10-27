"""System texts and fixed prompt parts per step.

The system texts come from a folder with four files, one per step. The package ships simplified example prompts; the
author's installation uses private prompts that are not part of the repository, chosen with SKILLPOSE_PROMPTS or
--prompts. A folder that is chosen but cannot be read is an error, never a silent fallback to the examples: results
and costs would change without notice.

The fixed part of a step depends only on the prompt set, the vocabulary snapshot, the role families and the output
language, never on the CV or posting, so every call of a run hits the same cached prefix.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

LANGUAGE_NAMES = {"de": "German", "en": "English"}
EXAMPLE_DIR = Path(__file__).parent / "data" / "prompts_example"
PROMPT_FILES = ("cv", "job", "relation", "path")  # steps 1, 2, 3b and 5


class PromptError(ValueError):
    """A prompt folder that cannot be loaded. The message names the folder, never its full path."""


@dataclass(frozen=True)
class PromptSet:
    cv: str
    job: str
    relation: str
    path: str
    label: str  # "example", or "private:" and the first 8 hex digits of a SHA-256 over the four texts


def load_prompts(folder: Path) -> PromptSet:
    """The four texts exactly as written; only the line ends of a Windows checkout are read as plain line ends and a
    byte order mark (saved by many Windows editors) is dropped, so that a set gives the same texts and label
    everywhere. All four files are required."""
    folder = Path(folder)
    name = folder.resolve().name or str(folder)
    texts = {}
    for step in PROMPT_FILES:
        file = folder / f"{step}.md"
        if not file.is_file():
            raise PromptError(f"prompt folder {name}: missing {step}.md")
        try:
            texts[step] = file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            raise PromptError(f"prompt folder {name}: {step}.md cannot be read as UTF-8 text") from None
        if not texts[step].strip():
            raise PromptError(f"prompt folder {name}: {step}.md is empty")
    if folder.resolve() == EXAMPLE_DIR.resolve():
        label = "example"
    else:
        digest = hashlib.sha256("\0".join(texts[step] for step in PROMPT_FILES).encode("utf-8")).hexdigest()
        label = f"private:{digest[:8]}"
    return PromptSet(**texts, label=label)


_active: PromptSet | None = None  # None: the examples, read on first use


def use_prompts(folder: Path | str | None) -> PromptSet:
    """Choose the prompt set of this process: a folder, or the examples for None or an empty value. A folder that
    cannot be loaded raises PromptError and leaves the active set as it was."""
    global _active
    _active = load_prompts(EXAMPLE_DIR if folder is None or not str(folder).strip() else Path(folder))
    return _active


def active_prompts() -> PromptSet:
    global _active
    if _active is None:
        _active = load_prompts(EXAMPLE_DIR)
    return _active


def wrap(tag: str, text: str) -> str:
    """Mark untrusted text as data. A closing tag inside the text is defused so it cannot end the section early."""
    safe = re.sub(rf"</\s*{re.escape(tag)}\s*>", f"[/{tag}]", text, flags=re.IGNORECASE)
    return f"<{tag}>\n{safe}\n</{tag}>"


def _language_line(lang: str, fields: str = "context, name, reason") -> str:
    return f"Write all free-text fields ({fields}) in {LANGUAGE_NAMES[lang]}."


def cv_fixed(snapshot: str, lang: str) -> list[str]:
    return [f"{active_prompts().cv}\n\n{_language_line(lang)}", wrap("vocabulary", snapshot)]


def job_fixed(snapshot: str, role_families: list[str], lang: str) -> list[str]:
    families = wrap("role_families", "\n".join(role_families))
    return [f"{active_prompts().job}\n\n{_language_line(lang)}\n\n{families}", wrap("vocabulary", snapshot)]


def relation_fixed(snapshot: str) -> list[str]:
    return [active_prompts().relation, wrap("vocabulary", snapshot)]


def path_fixed(profile_summary: str, lang: str) -> list[str]:
    """Fixed part of step 5: it depends on the profile only, so every target role of one person shares the prefix."""
    system = f"{active_prompts().path}\n\n{_language_line(lang, 'activity, proof, rationale')}"
    return [system, wrap("profile", profile_summary)]
