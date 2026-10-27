# Translations of the gold-set postings

The demo shows each posting in the language of the interface. A posting written in German has its English translation
in this folder, a posting written in English its German one. Claude made them in a Claude Code session, not in an API
step. The demo marks them as translations by Claude (AI) and shows the original one click away. Like the postings,
they are invented texts.

## File name

`<posting file stem>.<lang>.md`. `<lang>` is the language of the translation, `de` or `en`. Example:
`01_test_automation_linz.en.md` is the English translation of `../postings/01_test_automation_linz.md`.

A posting has a translation only into the other language. `skillpose demo build` refuses a file in the language that
the posting is written in (`src/skillpose/core/language.py` decides which language that is).

## Content

UTF-8 (a BOM is ignored):

```markdown
# <translated title>

<translated posting>
```

- First line: `# ` and the translated job title. Translate the title that the demo lists show: the `title` of the
  `job_profile` of the posting in the bundles in `src/skillpose/demo_data`.
- Below it, after an empty line: the whole posting, translated, from its first line to its last. Keep the line
  breaks, the lists and the headings of the original. Do not add, leave out or shorten anything.
- Keep the names of companies, products, tools and certificates. Write the gender note of a job title in the form of
  the target language, for example (m/w/d) becomes (m/f/d). Keep every amount and number.
  In a German translation, (all genders) and (m/f/d) both become (m/w/d).
- Empty lines directly below the title and white space at the end are dropped. Everything else stays as written.

## How the build uses them

`skillpose demo build` reads every file here before its first API call. It stores each translation for the posting
as the posting reads now (by the hash of its text) and writes it into the demo bundles. The files cost nothing; the
build makes no API call for them.

- A broken file stops the build with a message that names the file: a wrong name, no `# <title>` on the first line,
  no text below the title, not UTF-8, or a translation into the language of the posting.
- A file for a posting that the gold set does not have is skipped with a warning.
- A posting without a translation gets a warning. The demo then shows it in its own language only.
- A deleted file removes its translation from the next bundles.
- If you change a posting, change its translation too: the build cannot see that a translation is out of date.

The requirement quotes are never translated. The demo shows them verbatim, in the language of the posting.
