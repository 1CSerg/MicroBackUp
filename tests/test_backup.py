import json
import os
import time
from pathlib import Path

import multivolumefile
import py7zr
import pytest

from backup import (
    compute_content_hash,
    compute_file_hash,
    compute_names_hash,
    count_items,
    create_archive,
    get_all_paths,
    run_backup,
)


def _rel_set(paths):
    return {(rel, kind) for _abs, rel, kind in paths}


def _extract_archive(archive_path: Path, dest: Path, password=None, split=False):
    dest.mkdir(parents=True, exist_ok=True)
    if split:
        with multivolumefile.open(archive_path, mode="rb") as target:
            with py7zr.SevenZipFile(target, "r", password=password) as archive:
                archive.extractall(path=dest)
    else:
        with py7zr.SevenZipFile(archive_path, "r", password=password) as archive:
            archive.extractall(path=dest)


def _file_map(root: Path) -> dict:
    return {
        str(p.relative_to(root)).replace("\\", "/"): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


class TestGetAllPathsAndCount:
    def test_directory_and_file_sources(self, source_tree: Path):
        docs = source_tree / "docs"
        single = source_tree / "single.txt"
        paths = get_all_paths([str(docs), str(single)])

        rels = _rel_set(paths)
        assert ("docs", "dir") in rels
        assert ("docs/sub", "dir") in rels or ("docs\\sub", "dir") in rels
        assert ("docs/a.txt", "file") in rels or ("docs\\a.txt", "file") in rels
        assert ("docs/sub/b.txt", "file") in rels or ("docs\\sub\\b.txt", "file") in rels
        assert ("single.txt", "file") in rels

        files_count, dirs_count = count_items(paths)
        assert files_count == 3
        assert dirs_count == 2

    def test_missing_source_is_skipped(self, tmp_path: Path):
        paths = get_all_paths([str(tmp_path / "does_not_exist")])
        assert paths == []
        assert count_items(paths) == (0, 0)


class TestHashing:
    def test_names_hash_stable_and_order_independent(self, source_tree: Path):
        docs = str(source_tree / "docs")
        single = str(source_tree / "single.txt")
        h1 = compute_names_hash(get_all_paths([docs, single]))
        h2 = compute_names_hash(get_all_paths([single, docs]))
        assert h1 == h2
        assert len(h1) == 64

    def test_names_hash_changes_on_rename(self, tmp_path: Path):
        folder = tmp_path / "src"
        folder.mkdir()
        f = folder / "a.txt"
        f.write_text("same", encoding="utf-8")
        before = compute_names_hash(get_all_paths([str(folder)]))
        f.rename(folder / "b.txt")
        after = compute_names_hash(get_all_paths([str(folder)]))
        assert before != after

    def test_file_hash_matches_for_same_content(self, tmp_path: Path):
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"payload")
        b.write_bytes(b"payload")
        assert compute_file_hash(str(a)) == compute_file_hash(str(b))

    def test_file_hash_changes_when_content_changes(self, tmp_path: Path):
        f = tmp_path / "f.bin"
        f.write_bytes(b"one")
        h1 = compute_file_hash(str(f))
        f.write_bytes(b"two")
        h2 = compute_file_hash(str(f))
        assert h1 != h2

    def test_file_hash_unreadable_file_returns_empty_digest(self, tmp_path: Path, capsys):
        missing = tmp_path / "gone.bin"
        digest = compute_file_hash(str(missing))
        assert digest == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert "Could not read file" in capsys.readouterr().err

    def test_content_hash_changes_on_file_edit(self, tmp_path: Path):
        folder = tmp_path / "src"
        folder.mkdir()
        target = folder / "a.txt"
        target.write_text("v1", encoding="utf-8")
        h1 = compute_content_hash(get_all_paths([str(folder)]))
        target.write_text("v2", encoding="utf-8")
        h2 = compute_content_hash(get_all_paths([str(folder)]))
        assert h1 != h2
        assert len(h1) == 64


class TestBackupIntegration:
    def test_creates_archive_and_extracted_files_match(self, source_tree: Path, tmp_path: Path):
        dest = tmp_path / "backup"
        dest.mkdir()
        sources = [str(source_tree / "docs"), str(source_tree / "single.txt")]

        run_backup(sources, str(dest), "my_backup")

        archive = dest / "my_backup.7z"
        info = dest / "my_backup_hash.json"
        assert archive.is_file()
        assert info.is_file()

        data = json.loads(info.read_text(encoding="utf-8"))
        assert data["files_count"] == 3
        assert data["dirs_count"] == 2
        assert data["names_hash"]
        assert data["content_hash"]
        assert data["last_check_date"]
        assert data["last_update_date"]

        extracted = tmp_path / "extracted"
        _extract_archive(archive, extracted)

        files = _file_map(extracted)
        assert files["docs/a.txt"] == b"hello"
        assert files["docs/sub/b.txt"] == b"world"
        assert files["single.txt"] == b"file"

    def test_password_protected_archive_roundtrip(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "secret.txt").write_text("classified", encoding="utf-8")
        dest = tmp_path / "dest"
        dest.mkdir()

        run_backup([str(src)], str(dest), "secret_arc", password="s3cret")

        extracted = tmp_path / "out"
        _extract_archive(dest / "secret_arc.7z", extracted, password="s3cret")
        assert (extracted / "src" / "secret.txt").read_text(encoding="utf-8") == "classified"

        with pytest.raises(Exception):
            _extract_archive(dest / "secret_arc.7z", tmp_path / "bad", password="wrong")

    def test_skips_backup_when_nothing_changed(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("stable", encoding="utf-8")
        dest = tmp_path / "dest"
        dest.mkdir()

        run_backup([str(src)], str(dest), "inc")
        archive = dest / "inc.7z"
        info_path = dest / "inc_hash.json"
        mtime_before = archive.stat().st_mtime
        info_before = json.loads(info_path.read_text(encoding="utf-8"))

        time.sleep(0.05)
        run_backup([str(src)], str(dest), "inc")

        info_after = json.loads(info_path.read_text(encoding="utf-8"))
        assert archive.stat().st_mtime == mtime_before
        assert info_after["last_update_date"] == info_before["last_update_date"]
        assert info_after["last_check_date"] >= info_before["last_check_date"]
        assert info_after["content_hash"] == info_before["content_hash"]

    def test_rebuilds_archive_when_content_changes(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        target = src / "a.txt"
        target.write_text("v1", encoding="utf-8")
        dest = tmp_path / "dest"
        dest.mkdir()

        run_backup([str(src)], str(dest), "inc")
        info_before = json.loads((dest / "inc_hash.json").read_text(encoding="utf-8"))
        mtime_before = (dest / "inc.7z").stat().st_mtime

        time.sleep(0.05)
        target.write_text("v2", encoding="utf-8")
        run_backup([str(src)], str(dest), "inc")

        info_after = json.loads((dest / "inc_hash.json").read_text(encoding="utf-8"))
        assert info_after["content_hash"] != info_before["content_hash"]
        assert info_after["last_update_date"] != info_before["last_update_date"]
        assert (dest / "inc.7z").stat().st_mtime >= mtime_before

        extracted = tmp_path / "out"
        _extract_archive(dest / "inc.7z", extracted)
        assert (extracted / "src" / "a.txt").read_text(encoding="utf-8") == "v2"

    def test_rebuilds_archive_when_file_added(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("one", encoding="utf-8")
        dest = tmp_path / "dest"
        dest.mkdir()

        run_backup([str(src)], str(dest), "inc")
        info_before = json.loads((dest / "inc_hash.json").read_text(encoding="utf-8"))

        (src / "b.txt").write_text("two", encoding="utf-8")
        run_backup([str(src)], str(dest), "inc")

        info_after = json.loads((dest / "inc_hash.json").read_text(encoding="utf-8"))
        assert info_after["files_count"] == info_before["files_count"] + 1
        assert info_after["last_update_date"] != info_before["last_update_date"]

    def test_corrupt_hash_json_triggers_full_backup(self, tmp_path: Path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("data", encoding="utf-8")
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "bad_hash.json").write_text("{not-json", encoding="utf-8")

        run_backup([str(src)], str(dest), "bad")
        assert (dest / "bad.7z").is_file()
        assert "Error reading info file" in capsys.readouterr().err

    def test_split_archive_into_volumes(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        payload = os.urandom(80_000)
        (src / "big.bin").write_bytes(payload)
        dest = tmp_path / "dest"
        dest.mkdir()
        volume = 16_384

        run_backup([str(src)], str(dest), "split_arc", split_size=volume)

        volume_parts = sorted(dest.glob("split_arc.7z.*"))
        assert any(p.name.endswith(".0001") for p in volume_parts)
        assert len(volume_parts) >= 2
        assert (dest / "split_arc_hash.json").is_file()

        extracted = tmp_path / "out"
        _extract_archive(dest / "split_arc.7z", extracted, split=True)
        assert (extracted / "src" / "big.bin").read_bytes() == payload

    def test_split_single_file_source(self, tmp_path: Path):
        payload = os.urandom(40_000)
        src_file = tmp_path / "only.bin"
        src_file.write_bytes(payload)
        dest = tmp_path / "dest"
        dest.mkdir()

        run_backup([str(src_file)], str(dest), "file_split", split_size=8192)

        volume_parts = sorted(dest.glob("file_split.7z.*"))
        assert len(volume_parts) >= 2

        extracted = tmp_path / "out"
        _extract_archive(dest / "file_split.7z", extracted, split=True)
        assert (extracted / "only.bin").read_bytes() == payload

    def test_rebuilds_archive_when_name_changes(self, tmp_path: Path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        old = src / "a.txt"
        old.write_text("same", encoding="utf-8")
        dest = tmp_path / "dest"
        dest.mkdir()

        run_backup([str(src)], str(dest), "inc")
        info_before = json.loads((dest / "inc_hash.json").read_text(encoding="utf-8"))

        old.rename(src / "b.txt")
        run_backup([str(src)], str(dest), "inc")

        info_after = json.loads((dest / "inc_hash.json").read_text(encoding="utf-8"))
        assert info_after["files_count"] == info_before["files_count"]
        assert info_after["names_hash"] != info_before["names_hash"]
        assert "Names hash differs" in capsys.readouterr().out

    def test_create_archive_without_split(self, tmp_path: Path):
        src_file = tmp_path / "only.txt"
        src_file.write_text("solo", encoding="utf-8")
        dest = tmp_path / "dest"
        dest.mkdir()

        create_archive([str(src_file)], str(dest), "direct", None, None)
        extracted = tmp_path / "out"
        _extract_archive(dest / "direct.7z", extracted)
        assert (extracted / "only.txt").read_text(encoding="utf-8") == "solo"
