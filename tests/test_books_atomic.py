"""Create-only textbook import must never publish partial or overwrite course data."""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from platform_api import books


def _book(root: Path, course: str) -> Path:
    return root / "TEXTBOOK" / course / "book.md"


def _text(index: int = 0) -> str:
    return f"# 遗传学 第{index}版\n减数分裂是细胞分裂的一种形式。遗传信息由亲代传递给子代。\n"


def _assert_no_staging(root: Path, course: str) -> None:
    assert not list(_book(root, course).parent.glob(".book-*.tmp"))


def test_success_publishes_exact_content_and_digest(tmp_path: Path) -> None:
    content = _text()
    result = books.import_markdown(tmp_path, "遗传学", content)
    assert _book(tmp_path, "遗传学").read_bytes() == content.encode("utf-8")
    assert result["sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert result["created"] is True
    assert result["ready_for_formal_lesson"] is False
    _assert_no_staging(tmp_path, "遗传学")


def test_existing_book_is_never_overwritten(tmp_path: Path) -> None:
    first = _text(1)
    books.import_markdown(tmp_path, "遗传学", first)
    with pytest.raises(books.BookError, match="overwrite is forbidden"):
        books.import_markdown(tmp_path, "遗传学", _text(2))
    assert _book(tmp_path, "遗传学").read_text(encoding="utf-8") == first
    _assert_no_staging(tmp_path, "遗传学")


def test_fsync_failure_leaves_no_partial_authoritative_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed_fsync(_fd: int) -> None:
        raise OSError("simulated storage failure")

    monkeypatch.setattr(books.os, "fsync", failed_fsync)
    with pytest.raises(books.BookError, match="before publication"):
        books.import_markdown(tmp_path, "遗传学", _text())
    assert not _book(tmp_path, "遗传学").exists()
    _assert_no_staging(tmp_path, "遗传学")


def test_publish_failure_does_not_touch_progress_or_other_course(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    progress = tmp_path / "DATA" / "遗传学" / "PROGRESS.md"
    progress.parent.mkdir(parents=True)
    progress.write_bytes(b"keep existing progress")
    books.import_markdown(tmp_path, "另一课程", _text(7))

    def failed_link(_src: str | Path, _dst: str | Path) -> None:
        raise OSError("simulated unsupported hard links")

    monkeypatch.setattr(books.os, "link", failed_link)
    with pytest.raises(books.BookError, match="before publication"):
        books.import_markdown(tmp_path, "遗传学", _text())
    assert not _book(tmp_path, "遗传学").exists()
    assert progress.read_bytes() == b"keep existing progress"
    assert _book(tmp_path, "另一课程").read_text(encoding="utf-8") == _text(7)
    _assert_no_staging(tmp_path, "遗传学")


def test_publish_occurs_only_after_complete_staged_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_link = books.os.link
    content = _text(3)
    target = _book(tmp_path, "遗传学")

    def checked_link(src: str | Path, dst: str | Path) -> None:
        assert Path(dst) == target
        assert not target.exists()
        assert Path(src).read_bytes() == content.encode("utf-8")
        original_link(src, dst)

    monkeypatch.setattr(books.os, "link", checked_link)
    books.import_markdown(tmp_path, "遗传学", content)
    assert target.read_text(encoding="utf-8") == content
    _assert_no_staging(tmp_path, "遗传学")


def test_competing_imports_have_exactly_one_winner(tmp_path: Path) -> None:
    payloads = [_text(i) for i in range(12)]

    def attempt(content: str) -> bool:
        try:
            books.import_markdown(tmp_path, "并发课程", content)
            return True
        except books.BookError as exc:
            assert "overwrite is forbidden" in str(exc)
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, payloads))
    assert sum(results) == 1
    assert _book(tmp_path, "并发课程").read_text(encoding="utf-8") in payloads
    _assert_no_staging(tmp_path, "并发课程")
