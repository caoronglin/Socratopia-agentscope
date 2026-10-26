"""Read-only chapter catalog and create-only Markdown textbook onboarding."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from platform_api.evidence import EvidenceError, MAX_BOOK_BYTES, _read_book
from scripts.lib.repository import textbook_dir, validate_course_name


class BookError(ValueError):
    """Invalid, stale or unsafe course content."""


def _target(root: Path, course: str) -> Path:
    parent = textbook_dir(root, validate_course_name(course))
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / "book.md"
    if path.is_symlink():
        raise BookError("Course book cannot be a symlink")
    return path


def info(root: Path, course: str) -> dict:
    path = _target(root, course)
    if not path.exists():
        return {"course": course, "available": False, "can_import": True, "chapters": []}
    try:
        content, sha = _read_book(root, course)
    except (EvidenceError, ValueError) as exc:
        raise BookError(str(exc)) from exc
    chapters = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        found = re.match(r"^(#{1,4})\s+(.+?)\s*$", line)
        if found:
            chapters.append({
                "level": len(found.group(1)),
                "title": found.group(2)[:120],
                "line": line_number,
            })
            if len(chapters) >= 200:
                break
    return {
        "course": course, "available": True, "can_import": False,
        "chapters": chapters, "sha256": sha, "bytes": len(content.encode("utf-8")),
        "source": f"TEXTBOOK/{course}/book.md",
    }


def import_markdown(root: Path, course: str, content: str) -> dict:
    course = validate_course_name(course)
    if "\x00" in content or not content.strip():
        raise BookError("Course textbook must be non-empty UTF-8 Markdown")
    data = content.encode("utf-8")
    if not 32 <= len(data) <= MAX_BOOK_BYTES:
        raise BookError("Textbook must have 32–2,000,000 bytes")
    path = _target(root, course)
    # Stage a complete, fsynced file before publishing it. Opening book.md
    # with 'x' directly would expose a partial textbook if writing fails.
    # A hard link is an atomic, no-replace publication on the same filesystem:
    # two competing imports cannot overwrite each other or existing course data.
    staged: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=".book-", suffix=".tmp", dir=path.parent)
        staged = Path(name)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.link(staged, path)
    except FileExistsError as exc:
        raise BookError("Course already has a textbook; overwrite is forbidden") from exc
    except OSError as exc:
        # On filesystems without hard-link support, fail closed rather than
        # falling back to a non-atomic rename or direct write.
        raise BookError("Textbook import failed before publication; retry is safe") from exc
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    return {"course": course, "created": True, "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "ready_for_formal_lesson": False}
