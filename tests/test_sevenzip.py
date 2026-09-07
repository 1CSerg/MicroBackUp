from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sevenzip import (
    SevenZipError,
    SevenZipOptions,
    _run_7z,
    _write_exclude_listfile,
    build_command,
    create_archive,
    probe,
    resolve_binary,
)


def _find_7z() -> str | None:
    candidates = [
        shutil.which("7z"),
        shutil.which("7z.exe"),
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
        "/usr/bin/7z",
        "/usr/local/bin/7z",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


SEVENZIP_BIN = _find_7z()
requires_7z = pytest.mark.skipif(SEVENZIP_BIN is None, reason="7z binary not found")


def _completed(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class TestBuildCommand:
    def test_basic_flags(self):
        cmd = build_command("7z", "arc.7z", ["C:\\src"])
        assert cmd[:8] == ["7z", "a", "-t7z", "-y", "-bd", "-ssw", "-scsUTF-8", "--"]
        assert cmd[-2:] == ["arc.7z", "C:\\src"]
        assert not any(a.startswith("-mx=") for a in cmd)

    def test_level_zero_adds_mx(self):
        cmd = build_command("7z", "arc.7z", ["src"], level=0)
        assert "-mx=0" in cmd

    def test_split_password_extra_exclude(self, tmp_path: Path):
        listfile = str(tmp_path / "x.txt")
        cmd = build_command(
            "7z",
            "arc.7z",
            ["src"],
            split_size=1024,
            password="secret",
            level=5,
            extra_args=("-mmt=4",),
            exclude_listfile=listfile,
        )
        assert "-mx=5" in cmd
        assert "-v1024b" in cmd
        assert "-psecret" in cmd
        assert "-mhe=on" in cmd
        assert "-mmt=4" in cmd
        assert f"-xr@{listfile}" in cmd
        mx_at = cmd.index("-mx=5")
        extra_at = cmd.index("-mmt=4")
        assert extra_at > mx_at
        assert cmd[cmd.index("--") + 1] == "arc.7z"


class TestResolveBinary:
    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(SevenZipError, match="not found"):
            resolve_binary(str(tmp_path / "no-such-7z.exe"))

    def test_empty_raises(self):
        with pytest.raises(SevenZipError, match="empty"):
            resolve_binary("  ")

    def test_absolute_file(self, tmp_path: Path):
        binary = tmp_path / "7z.exe"
        binary.write_bytes(b"")
        assert resolve_binary(str(binary)) == str(binary.resolve())

    def test_quoted_path(self, tmp_path: Path):
        binary = tmp_path / "7z.exe"
        binary.write_bytes(b"")
        assert resolve_binary(f'"{binary}"') == str(binary.resolve())

    def test_bare_name_uses_which(self, tmp_path: Path, monkeypatch):
        binary = tmp_path / "7z"
        binary.write_bytes(b"")
        monkeypatch.setattr("sevenzip.shutil.which", lambda name: str(binary) if name == "7z" else None)
        assert resolve_binary("7z") == str(binary)

    def test_bare_name_missing(self, monkeypatch):
        monkeypatch.setattr("sevenzip.shutil.which", lambda name: None)
        with pytest.raises(SevenZipError, match="PATH"):
            resolve_binary("7z")


class TestCreateArchiveProcess:
    def test_returncode_zero_success(self, tmp_path: Path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        archive = tmp_path / "a.7z"
        monkeypatch.setattr("sevenzip.resolve_binary", lambda path: "7z")
        monkeypatch.setattr("sevenzip.probe", lambda binary: "7-Zip mock")
        monkeypatch.setattr("sevenzip._run_7z", lambda cmd, timeout=None: _completed(0, b"ok"))
        create_archive(SevenZipOptions(path="7z"), archive, [str(src)])

    def test_returncode_one_raises(self, tmp_path: Path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        archive = tmp_path / "a.7z"
        monkeypatch.setattr("sevenzip.resolve_binary", lambda path: "7z")
        monkeypatch.setattr("sevenzip.probe", lambda binary: "7-Zip mock")
        monkeypatch.setattr(
            "sevenzip._run_7z",
            lambda cmd, timeout=None: _completed(1, b"", b"file locked"),
        )
        with pytest.raises(SevenZipError, match="code 1"):
            create_archive(SevenZipOptions(path="7z"), archive, [str(src)])

    def test_returncode_two_raises(self, tmp_path: Path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        archive = tmp_path / "a.7z"
        monkeypatch.setattr("sevenzip.resolve_binary", lambda path: "7z")
        monkeypatch.setattr("sevenzip.probe", lambda binary: "7-Zip mock")
        monkeypatch.setattr(
            "sevenzip._run_7z",
            lambda cmd, timeout=None: _completed(2, b"", b"fatal"),
        )
        with pytest.raises(SevenZipError, match="code 2"):
            create_archive(SevenZipOptions(path="7z"), archive, [str(src)])

    def test_exclude_listfile_written_and_removed(self, tmp_path: Path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        archive = tmp_path / "a.7z"
        seen: dict[str, str | None] = {"listfile": None}
        original_mkstemp = __import__("tempfile").mkstemp

        def tracking_mkstemp(*args, **kwargs):
            fd, path = original_mkstemp(*args, **kwargs)
            seen["listfile"] = path
            return fd, path

        def fake_run(cmd, timeout=None):
            listfile = seen["listfile"]
            assert listfile is not None
            assert Path(listfile).is_file()
            text = Path(listfile).read_text(encoding="utf-8")
            assert "proj" in text.replace("/", os.sep).replace("\\", os.sep) or "skip.log" in text
            assert any(a.startswith("-xr@") for a in cmd)
            return _completed(0)

        monkeypatch.setattr("sevenzip.resolve_binary", lambda path: "7z")
        monkeypatch.setattr("sevenzip.probe", lambda binary: "7-Zip mock")
        monkeypatch.setattr("tempfile.mkstemp", tracking_mkstemp)
        monkeypatch.setattr("sevenzip._run_7z", fake_run)
        create_archive(
            SevenZipOptions(path="7z"),
            archive,
            [str(src)],
            excluded=["proj/skip.log", "proj/build"],
        )
        assert seen["listfile"] is not None
        assert not Path(seen["listfile"]).exists()

    def test_write_exclude_listfile_adds_dir_masks(self, tmp_path: Path):
        target = tmp_path / "x.txt"
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            _write_exclude_listfile(["proj/build", "proj/a.log"], handle)
        text = target.read_text(encoding="utf-8")
        assert "build" in text
        assert "*" in text


class TestRun7zStop:
    def test_keyboard_interrupt_terminates_child(self, monkeypatch):
        proc = MagicMock()
        proc.communicate.side_effect = KeyboardInterrupt()
        proc.poll.return_value = None
        proc.wait.return_value = 1

        monkeypatch.setattr("sevenzip.subprocess.Popen", lambda *a, **k: proc)

        with pytest.raises(KeyboardInterrupt):
            _run_7z(["7z", "a", "x"])

        proc.terminate.assert_called_once()

    def test_timeout_terminates_child(self, monkeypatch):
        proc = MagicMock()
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd=["7z"], timeout=1),
            (b"", b""),
        ]
        proc.poll.return_value = None
        proc.wait.return_value = 1
        proc.returncode = -1

        monkeypatch.setattr("sevenzip.subprocess.Popen", lambda *a, **k: proc)

        with pytest.raises(subprocess.TimeoutExpired):
            _run_7z(["7z"], timeout=1)

        proc.terminate.assert_called_once()

    def test_kills_if_terminate_does_not_stop(self, monkeypatch):
        proc = MagicMock()
        proc.communicate.side_effect = KeyboardInterrupt()
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd=["7z"], timeout=5)

        monkeypatch.setattr("sevenzip.subprocess.Popen", lambda *a, **k: proc)

        with pytest.raises(KeyboardInterrupt):
            _run_7z(["7z"])

        proc.terminate.assert_called_once()
        proc.kill.assert_called()

    def test_real_child_is_stopped_on_interrupt(self, monkeypatch):
        import sys

        captured: dict[str, subprocess.Popen] = {}
        real_popen = subprocess.Popen

        def fake_popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            captured["proc"] = proc

            def comm(timeout=None):
                raise KeyboardInterrupt()

            proc.communicate = comm  # type: ignore[method-assign]
            return proc

        monkeypatch.setattr("sevenzip.subprocess.Popen", fake_popen)
        with pytest.raises(KeyboardInterrupt):
            _run_7z([sys.executable, "-c", "import time; time.sleep(60)"])

        assert captured["proc"].poll() is not None


class TestProbe:
    def test_probe_reads_banner(self, monkeypatch):
        monkeypatch.setattr(
            "sevenzip._run_7z",
            lambda cmd, timeout=None: _completed(0, b"\n7-Zip 26.02\n", b""),
        )
        assert probe("7z") == "7-Zip 26.02"

    def test_probe_oserror(self, monkeypatch):
        def boom(cmd, timeout=None):
            raise OSError("cannot exec")

        monkeypatch.setattr("sevenzip._run_7z", boom)
        with pytest.raises(SevenZipError, match="Could not start"):
            probe("7z")


def _7z_list_names(archive: Path) -> set[str]:
    assert SEVENZIP_BIN is not None
    result = subprocess.run(
        [SEVENZIP_BIN, "l", "-ba", str(archive)],
        capture_output=True,
        check=False,
    )
    names: set[str] = set()
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split()
        if not parts:
            continue
        names.add(parts[-1].replace("\\", "/"))
    return names


@requires_7z
class TestRealSevenZip:
    def test_single_source(self, tmp_path: Path):
        src = tmp_path / "docs"
        src.mkdir()
        (src / "a.txt").write_text("hello", encoding="utf-8")
        archive = tmp_path / "out.7z"
        create_archive(SevenZipOptions(path=SEVENZIP_BIN), archive, [str(src)])  # type: ignore[arg-type]
        assert archive.is_file()
        names = _7z_list_names(archive)
        assert any(n.endswith("a.txt") for n in names)

    def test_multiple_parents(self, tmp_path: Path):
        a = tmp_path / "A" / "Project"
        b = tmp_path / "B" / "Logs"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        (a / "a.txt").write_text("A", encoding="utf-8")
        (b / "l.txt").write_text("L", encoding="utf-8")
        archive = tmp_path / "multi.7z"
        create_archive(SevenZipOptions(path=SEVENZIP_BIN), archive, [str(a), str(b)])  # type: ignore[arg-type]
        names = _7z_list_names(archive)
        assert any("Project" in n and n.endswith("a.txt") for n in names)
        assert any("Logs" in n and n.endswith("l.txt") for n in names)

    def test_exclude_directory_prunes_subtree(self, tmp_path: Path):
        src = tmp_path / "proj"
        src.mkdir()
        (src / "keep.txt").write_text("keep", encoding="utf-8")
        nested = src / "build" / "nested"
        nested.mkdir(parents=True)
        (nested / "out.bin").write_text("gone", encoding="utf-8")
        archive = tmp_path / "ex.7z"
        create_archive(
            SevenZipOptions(path=SEVENZIP_BIN),  # type: ignore[arg-type]
            archive,
            [str(src)],
            excluded=["proj/build"],
        )
        names = _7z_list_names(archive)
        assert any(n.endswith("keep.txt") for n in names)
        assert not any("out.bin" in n for n in names)

    def test_split_volumes(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "data.bin").write_bytes(os.urandom(64 * 1024))
        archive = tmp_path / "split.7z"
        create_archive(
            SevenZipOptions(path=SEVENZIP_BIN),  # type: ignore[arg-type]
            archive,
            [str(src)],
            split_size=16 * 1024,
        )
        volumes = sorted(tmp_path.glob("split.7z.*"))
        assert len(volumes) >= 2
        first = tmp_path / "split.7z.001"
        assert first.is_file()
        result = subprocess.run(
            [SEVENZIP_BIN, "t", str(first)],
            capture_output=True,
            check=False,
        )
        assert result.returncode in (0, 1)

    def test_password_archive(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "s.txt").write_text("secret", encoding="utf-8")
        archive = tmp_path / "pw.7z"
        create_archive(
            SevenZipOptions(path=SEVENZIP_BIN),  # type: ignore[arg-type]
            archive,
            [str(src)],
            password="s3cret",
        )
        denied = subprocess.run(
            [SEVENZIP_BIN, "t", str(archive)],
            capture_output=True,
            check=False,
        )
        assert denied.returncode != 0
        ok = subprocess.run(
            [SEVENZIP_BIN, "t", "-ps3cret", str(archive)],
            capture_output=True,
            check=False,
        )
        assert ok.returncode == 0

    def test_backup_create_archive_excludes_junction(self, tmp_path: Path, monkeypatch):
        from backup import create_archive as backup_create_archive

        src = tmp_path / "src"
        src.mkdir()
        (src / "keep.txt").write_text("keep", encoding="utf-8")
        junction = src / "to_dest"
        junction.mkdir()
        (junction / "leaked.txt").write_text("leaked", encoding="utf-8")

        monkeypatch.setattr(
            "backup._is_windows_junction",
            lambda path: Path(path).resolve() == junction.resolve(),
        )

        dest = tmp_path / "dest"
        dest.mkdir()
        backup_create_archive(
            [str(src)],
            str(dest),
            "arc",
            None,
            None,
            sevenzip=SevenZipOptions(path=SEVENZIP_BIN),  # type: ignore[arg-type]
        )
        names = _7z_list_names(dest / "arc.7z")
        assert any(n.endswith("keep.txt") for n in names)
        assert not any("leaked" in n for n in names)
        assert not any("to_dest" in n for n in names)
