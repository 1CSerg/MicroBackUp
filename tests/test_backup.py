import json
import os
from pathlib import Path

import multivolumefile
import py7zr
import pytest

from backup import (
    compute_content_hash,
    compute_metadata_hash,
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

    def test_missing_source_logs_warning(self, tmp_path: Path, capsys):
        get_all_paths([str(tmp_path / "does_not_exist")])
        assert "does not exist" in capsys.readouterr().err

    def test_distinct_rel_paths_for_same_basename_file_sources(self, tmp_path: Path):
        dir_a = tmp_path / "A"
        dir_b = tmp_path / "B"
        dir_a.mkdir()
        dir_b.mkdir()
        file_a = dir_a / "file.txt"
        file_b = dir_b / "file.txt"
        file_a.write_text("a", encoding="utf-8")
        file_b.write_text("b", encoding="utf-8")

        paths = get_all_paths([str(file_a), str(file_b)])
        rels = [rel for _abs, rel, kind in paths if kind == "file"]
        # rel_path must be unique per source so hashes don't collapse them.
        assert len(rels) == 2
        assert len(set(rels)) == 2

    def test_names_hash_distinguishes_same_basename_sources_together(self, tmp_path: Path):
        dir_a = tmp_path / "A"
        dir_b = tmp_path / "B"
        dir_a.mkdir()
        dir_b.mkdir()
        (dir_a / "file.txt").write_text("a", encoding="utf-8")
        (dir_b / "file.txt").write_text("b", encoding="utf-8")

        # Both sources passed together: rel_paths are disambiguated, so
        # adding the second same-named source must change names_hash.
        h_one = compute_names_hash(get_all_paths([str(dir_a / "file.txt")]))
        h_both = compute_names_hash(get_all_paths([str(dir_a / "file.txt"), str(dir_b / "file.txt")]))
        assert h_one != h_both


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

    def test_file_hash_unreadable_file_returns_unique_error_marker(self, tmp_path: Path, capsys):
        missing = tmp_path / "gone.bin"
        digest = compute_file_hash(str(missing))
        assert digest.startswith("ERROR:")
        assert "Could not read file" in capsys.readouterr().err

    def test_metadata_hash_changes_on_file_edit(self, tmp_path: Path):
        folder = tmp_path / "src"
        folder.mkdir()
        target = folder / "a.txt"
        target.write_text("v1", encoding="utf-8")
        h1, err1 = compute_metadata_hash(get_all_paths([str(folder)]))
        target.write_text("v2", encoding="utf-8")
        h2, err2 = compute_metadata_hash(get_all_paths([str(folder)]))
        assert h1 != h2
        assert len(h1) == 64
        assert err1 is False
        assert err2 is False

    def test_metadata_hash_returns_none_on_stat_error(self, tmp_path: Path):
        from unittest.mock import patch
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("data", encoding="utf-8")
        paths = get_all_paths([str(src)])
        with patch("backup.os.stat", side_effect=OSError("denied")):
            digest, had_error = compute_metadata_hash(paths)
        assert digest is None
        assert had_error is True

    def test_content_hash_returns_none_on_read_error(self, tmp_path: Path):
        paths = [(str(tmp_path / "missing.txt"), "missing.txt", "file")]
        digest, had_error = compute_content_hash(paths)
        assert digest is None
        assert had_error is True


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
        assert data["metadata_hash"]
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

        run_backup([str(src)], str(dest), "inc")

        info_after = json.loads(info_path.read_text(encoding="utf-8"))
        assert archive.stat().st_mtime == mtime_before
        assert info_after["last_update_date"] == info_before["last_update_date"]
        assert info_after["last_check_date"] >= info_before["last_check_date"]
        assert info_after["metadata_hash"] == info_before["metadata_hash"]

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

        target.write_text("v2", encoding="utf-8")
        # Force mtime forward so metadata hash differs even on coarse-resolution FS.
        forced_mtime = os.stat(target).st_mtime + 10
        os.utime(target, (os.stat(target).st_atime, forced_mtime))
        run_backup([str(src)], str(dest), "inc")

        info_after = json.loads((dest / "inc_hash.json").read_text(encoding="utf-8"))
        assert info_after["metadata_hash"] != info_before["metadata_hash"]
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
        (dest / "bad.7z").write_text("dummy archive", encoding="utf-8")

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

    def test_creates_backup_with_check_content_hash_true(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("content1", encoding="utf-8")
        dest = tmp_path / "dest"
        dest.mkdir()

        run_backup([str(src)], str(dest), "inc", check_content_hash=True)
        info1 = json.loads((dest / "inc_hash.json").read_text(encoding="utf-8"))
        assert "content_hash" in info1

        # Capture original mtime, then change content and restore mtime so that
        # metadata hash stays the same and only content hash differs.
        original_mtime = os.stat(src / "a.txt").st_mtime
        (src / "a.txt").write_text("content2", encoding="utf-8")
        os.utime(src / "a.txt", (os.stat(src / "a.txt").st_atime, original_mtime))

        run_backup([str(src)], str(dest), "inc", check_content_hash=True)
        info2 = json.loads((dest / "inc_hash.json").read_text(encoding="utf-8"))
        # Metadata hash must match (mtime was restored), but content hash must differ.
        assert info2["metadata_hash"] == info1["metadata_hash"]
        assert info2["content_hash"] != info1["content_hash"]
        assert info2["last_update_date"] != info1["last_update_date"]

    def test_missing_archive_triggers_rebuild(self, tmp_path: Path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("stable", encoding="utf-8")
        dest = tmp_path / "dest"
        dest.mkdir()

        run_backup([str(src)], str(dest), "inc")
        assert (dest / "inc.7z").is_file()
        
        # Delete archive but keep hash json
        (dest / "inc.7z").unlink()
        
        run_backup([str(src)], str(dest), "inc")
        assert (dest / "inc.7z").is_file()
        assert "Archive file(s) not found on disk" in capsys.readouterr().out

    def test_header_encryption_prevents_reading_file_list(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "secret.txt").write_text("classified", encoding="utf-8")
        dest = tmp_path / "dest"
        dest.mkdir()

        run_backup([str(src)], str(dest), "secret_arc", password="s3cret")
        
        archive_path = dest / "secret_arc.7z"
        # Try to read without password - should fail because headers are encrypted
        with pytest.raises(py7zr.exceptions.PasswordRequired):
            with py7zr.SevenZipFile(archive_path, 'r') as archive:
                archive.getnames()

    def test_duplicate_archive_names_resolved(self, tmp_path: Path, capsys):
        src1 = tmp_path / "src1"
        src1.mkdir()
        (src1 / "file.txt").write_text("one", encoding="utf-8")
        
        src2 = tmp_path / "src2"
        src2.mkdir()
        (src2 / "file.txt").write_text("two", encoding="utf-8")
        
        dest = tmp_path / "dest"
        dest.mkdir()

        create_archive([str(src1 / "file.txt"), str(src2 / "file.txt")], str(dest), "dup", None, None)
        
        captured = capsys.readouterr()
        out = captured.out
        err = captured.err
        assert "Duplicate archive name detected" in err or "Duplicate archive name detected" in out
        assert "Renamed 'file.txt' to 'file_1.txt'" in out or "Renamed 'file.txt' to 'file_1.txt'" in err

        extracted = tmp_path / "out"
        _extract_archive(dest / "dup.7z", extracted)
        assert (extracted / "file.txt").read_text(encoding="utf-8") == "one"
        assert (extracted / "file_1.txt").read_text(encoding="utf-8") == "two"

    def test_rebuilds_when_split_format_changes(self, tmp_path: Path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("stable", encoding="utf-8")
        dest = tmp_path / "dest"
        dest.mkdir()

        # First run: non-split archive.
        run_backup([str(src)], str(dest), "fmt")
        assert (dest / "fmt.7z").is_file()
        info1 = json.loads((dest / "fmt_hash.json").read_text(encoding="utf-8"))
        assert info1["split_size"] is None

        # Second run: switch to split. Content unchanged, but split_size
        # differs, so a rebuild must happen despite matching hashes.
        run_backup([str(src)], str(dest), "fmt", split_size=8192)
        parts = sorted(dest.glob("fmt.7z.*"))
        assert parts
        assert not (dest / "fmt.7z").exists()
        info2 = json.loads((dest / "fmt_hash.json").read_text(encoding="utf-8"))
        assert info2["split_size"] == 8192
        assert "Split size changed" in capsys.readouterr().out
