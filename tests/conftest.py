from pathlib import Path

import pytest

from main import setup_logging


@pytest.fixture(autouse=True)
def _reset_logging():
    """Give each test a fresh console logger and close handlers afterwards."""
    setup_logging()
    yield
    setup_logging()


@pytest.fixture(autouse=True)
def _isolate_program_dir(tmp_path, monkeypatch):
    """Keep auto-created MicroBackUp.conf out of the repository directory."""
    monkeypatch.setattr("main.program_dir", lambda: tmp_path)


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    """Create a small nested source tree used by backup tests."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("hello", encoding="utf-8")
    sub = docs / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("world", encoding="utf-8")
    (tmp_path / "single.txt").write_text("file", encoding="utf-8")
    return tmp_path
