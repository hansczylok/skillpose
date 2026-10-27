"""The log file: what skillpose did, never what it analyzed.

The commands that may pay (analyze, eval, demo build, web) write skillpose.log next to their database (with Docker
/data/skillpose.log), rotated at 1 MB with three old files kept. It holds the start, end, duration and result of each
task, one line per API call (step, model, tokens, US dollars, duration, single or batch; a batch result names the
duration of its batch), each batch submitted and collected, and errors with their traceback; only the type of the
error where a parser may have quoted a file (a failed command, an upload that cannot be read). The code logs ids,
hashes and counts, never the text of a CV or posting, a quote, a prompt, the API key or APP_PASSWORD. The file
formatter adds three guards: a validation error shows where and why, not the input it rejected; the file an OSError
names (a CV file, a file in an upload folder) shows only its type, as <file>.pdf; and the home and temporary folders
(a user name, the folder of an upload) become ~ and <tmp>, also in the form an error text writes them (doubled
backslashes; on Windows in any case). The console shows warnings and errors only, as before.
"""

import logging
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from functools import cache
from logging.handlers import RotatingFileHandler
from pathlib import Path, PurePath

from pydantic import ValidationError

LOG_NAME = "skillpose.log"
OFF = "off"
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
CONSOLE_FORMAT = "%(levelname)s %(name)s: %(message)s"
MAX_BYTES = 1024 * 1024
BACKUPS = 3


def log_path(choice: str | None, db: Path) -> Path | None:
    """Where the log goes. choice (--log-file, else SKILLPOSE_LOG): a path, or "off" for none; empty: the folder of
    the database."""
    choice = (choice or "").strip()
    if choice.lower() == OFF:
        return None
    return Path(choice) if choice else Path(db).parent / LOG_NAME


def console() -> None:
    """Warnings and errors on the console. Nothing when logging has a handler already (a test runner, a program that
    embeds skillpose)."""
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setLevel(logging.WARNING)  # the INFO lines of skillpose are for the file
    handler.setFormatter(logging.Formatter(CONSOLE_FORMAT))
    root.addHandler(handler)
    root.setLevel(logging.WARNING)


def safe_error(error: BaseException) -> str:
    """The text of an error for the log: a validation error names the fields and the kinds of error, not the input
    (it may be a quote from a CV)."""
    if isinstance(error, ValidationError):
        where = "; ".join(f"{'.'.join(str(part) for part in e['loc']) or '(root)'}: {e['type']}"
                          for e in error.errors(include_url=False, include_context=False, include_input=False))
        return f"{error.error_count()} validation error(s) for {error.title}: {where}"
    return str(error) or type(error).__name__


def _chain(error: BaseException | None) -> list[BaseException]:
    seen: list[BaseException] = []
    while error is not None and all(error is not e for e in seen):
        seen.append(error)
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return seen


@cache
def _folders() -> list[tuple[re.Pattern[str], str]]:
    """(pattern of a folder, what the log shows instead), the more specific first: the temporary folder lies in the
    home folder. A folder may come as it is, with forward slashes, resolved (a short or linked name) and with every
    backslash doubled (the text of an OSError writes its path with repr()); on Windows in any case."""
    out = []
    for folder, shown in ((Path(tempfile.gettempdir()), "<tmp>"), (Path.home(), "~")):
        paths = [folder]
        with suppress(OSError):
            paths.append(folder.resolve())
        forms = {form for path in paths for text in (str(path), path.as_posix())
                 for form in (text, text.replace("\\", "\\\\")) if len(text) > 3}  # never a bare drive or "/"
        if forms:
            alternatives = "|".join(re.escape(form) for form in sorted(forms, key=len, reverse=True))
            out.append((re.compile(alternatives, re.IGNORECASE if os.name == "nt" else 0), shown))
    return out


def _errors(record: logging.LogRecord) -> list[BaseException]:
    """The errors a line may quote: the one it logs with its traceback and any error among its arguments."""
    found = _chain(record.exc_info[1]) if record.exc_info else []
    for arg in record.args if isinstance(record.args, tuple) else ():
        if isinstance(arg, BaseException):
            found += _chain(arg)
    return found


def _hide_files(text: str, errors: list[BaseException]) -> str:
    """The file of an OSError (a CV on the command line, a file in an upload folder) shows only its type: <file>.pdf.
    Only the form its text writes (repr()) is replaced: a message that names an upload folder to delete by hand keeps
    it (the folder guard makes it <tmp>/...)."""
    for error in errors:
        if not isinstance(error, OSError):
            continue
        for name in (error.filename, error.filename2):
            if isinstance(name, str | bytes | os.PathLike):  # not None, not a file descriptor
                name = os.fsdecode(name)
                text = text.replace(repr(name), repr("<file>" + PurePath(name).suffix))
    return text


class SafeFormatter(logging.Formatter):
    def formatException(self, ei) -> str:
        text = super().formatException(ei)
        for error in _chain(ei[1]):
            if isinstance(error, ValidationError):
                text = text.replace(str(error), safe_error(error))
        return text

    def format(self, record: logging.LogRecord) -> str:
        cached = record.exc_text  # another handler's text of the traceback: not checked, so not used
        record.exc_text = None
        try:
            text = super().format(record)
        finally:
            record.exc_text = cached
        text = _hide_files(text, _errors(record))
        for pattern, shown in _folders():
            text = pattern.sub(shown, text)  # <tmp> and ~ hold no backslash: nothing in them is a group reference
        return text


@contextmanager
def file_log(path: Path | None) -> Iterator[RotatingFileHandler | None]:
    """While the block runs, the skillpose loggers write from INFO up to the file at path (none: no file)."""
    if path is None:
        yield None
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(SafeFormatter(FORMAT))
    logger = logging.getLogger("skillpose")
    level = logger.level
    logger.addHandler(handler)
    if logger.getEffectiveLevel() > logging.INFO:
        logger.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
        handler.close()
