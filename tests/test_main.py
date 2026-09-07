import argparse
import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

import py7zr
import pytest

from main import (
    ConfigError,
    __version__,
    _program_dir,
    _source_containing_dest,
    _unquote_path,
    execute_backup,
    logger,
    main,
    parse_compression_level,
    parse_exclude_patterns,
    parse_log_backup_count,
    parse_log_level,
    parse_optional_size,
    parse_sevenzip_args,
    parse_sevenzip_path,
    parse_size,
    parse_sources,
    run_from_config,
    setup_logging,
)


class TestParseSize:
    def test_empty_and_none_return_none(self):
        assert parse_size(None) is None
        assert parse_size("") is None

    def test_whitespace_only_is_invalid(self):
        with pytest.raises(argparse.ArgumentTypeError, match="Invalid size format"):
            parse_size("   ")

    def test_kilobytes(self):
        assert parse_size("100k") == 100 * 1024
        assert parse_size("1.5K") == int(1.5 * 1024)

    def test_megabytes(self):
        assert parse_size("100m") == 100 * 1024 * 1024
        assert parse_size(" 2M ") == 2 * 1024 * 1024

    def test_gigabytes(self):
        assert parse_size("1g") == 1024 * 1024 * 1024
        assert parse_size("1.5g") == int(1.5 * 1024 * 1024 * 1024)

    def test_two_letter_suffixes(self):
        assert parse_size("100kb") == 100 * 1024
        assert parse_size("2mb") == 2 * 1024 * 1024
        assert parse_size("1gb") == 1024 * 1024 * 1024
        assert parse_size("1.5KB") == int(1.5 * 1024)

    def test_plain_bytes(self):
        assert parse_size("1024") == 1024

    def test_invalid_format_raises(self):
        with pytest.raises(argparse.ArgumentTypeError, match="Invalid size format"):
            parse_size("abc")

    def test_negative_size_raises(self):
        with pytest.raises(argparse.ArgumentTypeError, match="Size must be strictly positive"):
            parse_size("-100m")
        with pytest.raises(argparse.ArgumentTypeError, match="Size must be strictly positive"):
            parse_size("0")

    def test_vanishingly_small_size_raises(self):
        with pytest.raises(argparse.ArgumentTypeError, match="rounds to 0 bytes"):
            parse_size("0.0000000001")
        with pytest.raises(argparse.ArgumentTypeError, match="rounds to 0 bytes"):
            parse_size("0.000001k")

    def test_non_finite_size_raises(self):
        for raw in ("inf", "+inf", "-inf", "nan", "NaN", "infinity"):
            with pytest.raises(argparse.ArgumentTypeError, match="strictly positive"):
                parse_size(raw)


class TestParseSources:
    def test_simple_paths(self):
        assert parse_sources(r"C:\Logs D:\Work") == [r"C:\Logs", r"D:\Work"]

    def test_quoted_path_with_spaces(self):
        result = parse_sources(r'"D:\Work\Project A" C:\Logs')
        assert result == [r"D:\Work\Project A", r"C:\Logs"]

    def test_single_quoted_path_with_spaces(self):
        result = parse_sources(r"'C:\My Documents' C:\Logs")
        assert result == [r"C:\My Documents", r"C:\Logs"]

    def test_empty_string(self):
        assert parse_sources("") == []


class TestParseExcludePatterns:
    def test_empty_and_none(self):
        assert parse_exclude_patterns(None) == []
        assert parse_exclude_patterns("") == []
        assert parse_exclude_patterns("   \n  \n") == []

    def test_multiline_indented_patterns(self):
        raw = "\n*.tmp\n~$*\n    node_modules/\n"
        assert parse_exclude_patterns(raw) == ["*.tmp", "~$*", "node_modules/"]

    def test_strips_each_line(self):
        assert parse_exclude_patterns("  *.log  \n  !keep.log ") == ["*.log", "!keep.log"]


class TestUnquotePath:
    def test_strips_double_quotes(self):
        assert _unquote_path(r'"E:\root\cloud\mail\BackUp (sync)"') == r"E:\root\cloud\mail\BackUp (sync)"

    def test_strips_single_quotes(self):
        assert _unquote_path(r"'E:\root\cloud\mail\BackUp_sync'") == r"E:\root\cloud\mail\BackUp_sync"

    def test_strips_whitespace_around_quotes(self):
        assert _unquote_path('  "D:\\Backups"  ') == r"D:\Backups"

    def test_unquoted_path_unchanged(self):
        assert _unquote_path(r"E:\root\cloud\mail\BackUp (sync)") == r"E:\root\cloud\mail\BackUp (sync)"

    def test_unbalanced_quotes_unchanged(self):
        assert _unquote_path(r'"E:\path') == r'"E:\path'

    def test_empty_quotes_become_empty(self):
        assert _unquote_path('""') == ""
        assert _unquote_path("''") == ""


class TestParseOptionalSize:
    def test_empty_returns_none(self):
        assert parse_optional_size(None, "ctx") is None
        assert parse_optional_size("", "ctx") is None

    def test_valid_size(self):
        assert parse_optional_size("10k", "ctx") == 10 * 1024

    def test_invalid_size_raises_config_error(self):
        with pytest.raises(ConfigError, match=r"\[GLOBAL\] split: Invalid size format"):
            parse_optional_size("nope", "[GLOBAL] split")


class TestParseCompressionLevel:
    def test_empty_and_none_return_none(self):
        assert parse_compression_level(None, "ctx") is None
        assert parse_compression_level("", "ctx") is None
        assert parse_compression_level("  ", "ctx") is None
        assert parse_compression_level("none", "ctx") is None
        assert parse_compression_level("OFF", "ctx") is None

    def test_valid_range(self):
        assert parse_compression_level("0", "ctx") == 0
        assert parse_compression_level("5", "ctx") == 5
        assert parse_compression_level("9", "ctx") == 9

    def test_out_of_range_raises(self):
        with pytest.raises(ConfigError, match="integer 0-9"):
            parse_compression_level("10", "[GLOBAL] compression_level")
        with pytest.raises(ConfigError, match="integer 0-9"):
            parse_compression_level("-1", "ctx")

    def test_non_integer_raises(self):
        with pytest.raises(ConfigError, match="integer 0-9"):
            parse_compression_level("fast", "ctx")


class TestParseSevenZip:
    def test_args_empty(self):
        assert parse_sevenzip_args(None, "ctx") == ()
        assert parse_sevenzip_args("", "ctx") == ()
        assert parse_sevenzip_args("  ", "ctx") == ()

    def test_args_split(self):
        assert parse_sevenzip_args("-mmt=4", "ctx") == ("-mmt=4",)
        parsed = parse_sevenzip_args("-mmt=4 -ms=off", "ctx")
        assert "-mmt=4" in parsed
        assert "-ms=off" in parsed

    def test_path_none_and_off(self):
        assert parse_sevenzip_path(None, "ctx") is None
        assert parse_sevenzip_path("", "ctx") is None
        assert parse_sevenzip_path("none", "ctx") is None
        assert parse_sevenzip_path("OFF", "ctx") is None

    def test_path_unquotes(self):
        assert parse_sevenzip_path(r'"C:\Program Files\7-Zip\7z.exe"', "ctx") == r"C:\Program Files\7-Zip\7z.exe"

    def test_args_unclosed_quote_raises(self, monkeypatch):
        def boom(*args, **kwargs):
            raise ValueError("No closing quotation")

        monkeypatch.setattr("main.shlex.split", boom)
        with pytest.raises(ConfigError, match="sevenzip_args"):
            parse_sevenzip_args('"-mmt', "ctx")



class TestValidateArchiveName:
    def test_valid_name_passes(self):
        from main import _validate_archive_name
        assert _validate_archive_name("my_backup") is None
        assert _validate_archive_name("my-backup.2026") is None

    def test_path_separators_rejected(self):
        from main import _validate_archive_name
        assert _validate_archive_name("a/b") is not None
        assert _validate_archive_name("a\\b") is not None

    def test_forbidden_chars_rejected(self):
        from main import _validate_archive_name
        for name in ("a:b", "a*b", "a?b", "a|b", 'a"b', "a<b", "a>b"):
            assert _validate_archive_name(name) is not None

    def test_trailing_dots_and_spaces_rejected(self):
        from main import _validate_archive_name
        assert _validate_archive_name("name.") is not None
        assert _validate_archive_name("name ") is not None
        assert _validate_archive_name("name..") is not None

    def test_reserved_device_names_rejected(self):
        from main import _validate_archive_name
        for name in ("CON", "con", "PRN", "AUX", "NUL", "COM1", "LPT9", "con.txt"):
            assert _validate_archive_name(name) is not None

    def test_empty_and_dotdot_rejected(self):
        from main import _validate_archive_name
        assert _validate_archive_name("") is not None
        assert _validate_archive_name(".") is not None
        assert _validate_archive_name("..") is not None

    def test_control_chars_rejected(self):
        from main import _validate_archive_name
        assert _validate_archive_name("test\x00name") is not None
        assert _validate_archive_name("test\x1fname") is not None
        assert _validate_archive_name("test\nname") is not None


class TestExecuteBackup:
    def test_empty_sources_returns_false(self, tmp_path, capsys):
        dest = tmp_path / "dest"
        ok = execute_backup([], str(dest), "arc", None, None)
        assert ok is False
        assert "Sources list is empty" in capsys.readouterr().err

    def test_invalid_archive_name_returns_false(self, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest"
        ok = execute_backup([str(src)], str(dest), "bad:name", None, None)
        assert ok is False
        assert "forbidden characters" in capsys.readouterr().err

    def test_missing_source_returns_false(self, tmp_path, capsys):
        missing = tmp_path / "no_such_dir"
        dest = tmp_path / "dest"
        ok = execute_backup([str(missing)], str(dest), "arc", None, None)
        assert ok is False
        assert "Source path does not exist" in capsys.readouterr().err

    def test_creates_destination_and_runs_backup(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("data", encoding="utf-8")
        dest = tmp_path / "new_dest"
        assert not dest.exists()

        ok = execute_backup([str(src)], str(dest), "arc", None, None)

        assert ok is True
        assert dest.is_dir()
        assert (dest / "arc.7z").is_file()
        assert (dest / "arc_hash.json").is_file()

    def test_quoted_dest_with_spaces_creates_directory(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("data", encoding="utf-8")
        dest = tmp_path / "BackUp (sync)"

        ok = execute_backup([str(src)], f'"{dest}"', "arc", None, None)

        assert ok is True
        assert dest.is_dir()
        assert (dest / "arc.7z").is_file()

    def test_backup_exception_returns_false(self, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()

        with patch("main.run_backup", side_effect=RuntimeError("boom")):
            ok = execute_backup([str(src)], str(dest), "arc", None, None)

        assert ok is False
        assert "Backup failed: boom" in capsys.readouterr().err

    def test_destination_create_failure_returns_false(self, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest"

        with patch("os.makedirs", side_effect=OSError("denied")):
            ok = execute_backup([str(src)], str(dest), "arc", None, None)

        assert ok is False
        assert "Could not create destination directory" in capsys.readouterr().err

    def test_destination_is_file_returns_false(self, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("data", encoding="utf-8")
        dest_file = tmp_path / "dest_file.txt"
        dest_file.write_text("already a file", encoding="utf-8")

        ok = execute_backup([str(src)], str(dest_file), "arc", None, None)
        assert ok is False
        assert "Destination path is an existing file, not a directory" in capsys.readouterr().err

    def test_generic_exception_returns_false(self, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()

        with patch("main.run_backup", side_effect=Exception("py7zr archive exploded")):
            ok = execute_backup([str(src)], str(dest), "arc", None, None)

        assert ok is False
        assert "Backup failed: py7zr archive exploded" in capsys.readouterr().err

    def test_dest_inside_source_returns_false(self, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("data", encoding="utf-8")
        dest = src / "backups"

        ok = execute_backup([str(src)], str(dest), "arc", None, None)

        assert ok is False
        assert "inside a source path" in capsys.readouterr().err
        assert not dest.exists()

    def test_dest_equals_source_returns_false(self, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("data", encoding="utf-8")

        ok = execute_backup([str(src)], str(src), "arc", None, None)

        assert ok is False
        assert "inside a source path" in capsys.readouterr().err
        assert not (src / "arc.7z").exists()

    def test_source_containing_dest_sibling_is_ok(self, tmp_path):
        src = tmp_path / "src"
        dest = tmp_path / "dest"
        src.mkdir()
        dest.mkdir()
        assert _source_containing_dest(str(dest), [str(src)]) is None


class TestRunFromConfig:
    def _write_conf(self, path: Path, text: str) -> Path:
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_file_returns_false(self, tmp_path, capsys):
        ok = run_from_config(str(tmp_path / "missing.conf"))
        assert ok is False
        assert "Could not read config file" in capsys.readouterr().err

    def test_invalid_ini_returns_false(self, tmp_path, capsys):
        conf = self._write_conf(tmp_path / "bad.conf", "[[[not ini")
        ok = run_from_config(str(conf))
        assert ok is False
        assert "Invalid config file" in capsys.readouterr().err

    def test_no_job_sections_returns_false(self, tmp_path, capsys):
        conf = self._write_conf(
            tmp_path / "empty.conf",
            "[GLOBAL]\nsplit = 10m\npassword = secret\n",
        )
        ok = run_from_config(str(conf))
        assert ok is False
        assert "No backup sections found" in capsys.readouterr().err

    def test_invalid_global_split_returns_false(self, tmp_path, capsys):
        conf = self._write_conf(
            tmp_path / "bad_split.conf",
            "[GLOBAL]\nsplit = xyz\n\n[Job]\nsources = a\ndest = b\nname = n\n",
        )
        ok = run_from_config(str(conf))
        assert ok is False
        assert "[GLOBAL] split" in capsys.readouterr().err

    def test_duplicate_global_section_returns_false(self, tmp_path, capsys):
        conf = self._write_conf(
            tmp_path / "dup_global.conf",
            "[GLOBAL]\nsplit = 10m\n\n[global]\nsplit = 20m\n\n[Job]\nsources = a\ndest = b\nname = n\n",
        )
        ok = run_from_config(str(conf))
        assert ok is False
        assert "Duplicate [GLOBAL] section" in capsys.readouterr().err

    def test_skips_section_with_missing_fields(self, tmp_path, capsys):
        conf = self._write_conf(
            tmp_path / "partial.conf",
            "[Job]\nsources = a\ndest =\nname =\n",
        )
        ok = run_from_config(str(conf))
        assert ok is False
        err = capsys.readouterr().err
        assert "Skipping section [Job]: missing dest, name" in err
        assert "No backup jobs completed successfully" in err

    def test_skips_section_with_empty_sources(self, tmp_path, capsys):
        conf = self._write_conf(
            tmp_path / "empty_src.conf",
            "[Job]\nsources =    \ndest = D:\\x\nname = n\n",
        )
        ok = run_from_config(str(conf))
        assert ok is False
        assert "missing sources" in capsys.readouterr().err

    def test_skips_section_when_parsed_sources_empty(self, tmp_path, capsys):
        conf = self._write_conf(
            tmp_path / "quoted_empty.conf",
            "[Job]\nsources = placeholder\ndest = D:\\x\nname = n\n",
        )
        with patch("main.parse_sources", return_value=[]):
            ok = run_from_config(str(conf))
        assert ok is False
        assert "sources is empty" in capsys.readouterr().err

    def test_invalid_job_split_skips_section(self, tmp_path, capsys):
        conf = self._write_conf(
            tmp_path / "job_split.conf",
            "[Job]\nsources = a\ndest = b\nname = n\nsplit = bad\n",
        )
        ok = run_from_config(str(conf))
        assert ok is False
        assert "[Job] split" in capsys.readouterr().err

    def test_invalid_global_check_content_hash_returns_false(self, tmp_path, capsys):
        conf = self._write_conf(
            tmp_path / "bad_cch.conf",
            "[GLOBAL]\ncheck_content_hash = maybe\n\n[Job]\nsources = a\ndest = b\nname = n\n",
        )
        ok = run_from_config(str(conf))
        assert ok is False
        assert "[GLOBAL] check_content_hash: invalid boolean" in capsys.readouterr().err

    def test_invalid_job_check_content_hash_skips_section(self, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("A", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = self._write_conf(
            tmp_path / "job_cch.conf",
            f"[Job]\nsources = {src}\ndest = {dest}\nname = n\ncheck_content_hash = maybe\n",
        )
        ok = run_from_config(str(conf))
        assert ok is False
        assert "[Job] check_content_hash: invalid boolean" in capsys.readouterr().err

    def test_global_defaults_and_local_overrides(self, tmp_path):
        src_a = tmp_path / "Project A"
        src_a.mkdir()
        (src_a / "a.txt").write_text("A", encoding="utf-8")
        src_logs = tmp_path / "Logs"
        src_logs.mkdir()
        (src_logs / "l.txt").write_text("L", encoding="utf-8")
        src_b = tmp_path / "ProjectB"
        src_b.mkdir()
        (src_b / "b.txt").write_text("B", encoding="utf-8")

        dest_a = tmp_path / "Backups" / "A"
        dest_b = tmp_path / "Backups" / "B"

        conf = self._write_conf(
            tmp_path / "jobs.conf",
            f"""[GLOBAL]
split = 100m
password = my_global_secret

[ProjectA]
sources = "{src_a}" {src_logs}
dest = {dest_a}
name = proj_a_backup

[ProjectB]
sources = {src_b}
dest = {dest_b}
name = proj_b_backup
split = 500m
password = specific_secret
""",
        )

        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(
                {
                    "sources": sources,
                    "dest": dest,
                    "name": archive_name,
                    "split_size": split_size,
                    "password": password,
                    "check_content_hash": check_content_hash,
                }
            )
            return True

        with patch("main.execute_backup", side_effect=fake_execute):
            ok = run_from_config(str(conf))

        assert ok is True
        assert len(captured) == 2

        job_a = captured[0]
        assert job_a["name"] == "proj_a_backup"
        assert job_a["sources"] == [str(src_a), str(src_logs)]
        assert job_a["dest"] == str(dest_a)
        assert job_a["split_size"] == 100 * 1024 * 1024
        assert job_a["password"] == "my_global_secret"

        job_b = captured[1]
        assert job_b["name"] == "proj_b_backup"
        assert job_b["sources"] == [str(src_b)]
        assert job_b["split_size"] == 500 * 1024 * 1024
        assert job_b["password"] == "specific_secret"
        assert job_b["check_content_hash"] is False

    def test_ini_default_section_does_not_inherit_password(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MICROBACKUP_PASSWORD", raising=False)
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("A", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = self._write_conf(
            tmp_path / "default.conf",
            f"[DEFAULT]\npassword = from_default\n\n[Job]\nsources = {src}\ndest = {dest}\nname = n\n",
        )
        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(password)
            return True

        with patch("main.execute_backup", side_effect=fake_execute):
            ok = run_from_config(str(conf))

        assert ok is True
        assert captured == [None]

    def test_only_default_section_is_not_a_job(self, tmp_path, capsys):
        conf = self._write_conf(
            tmp_path / "only_default.conf",
            "[DEFAULT]\npassword = from_default\n\n[GLOBAL]\nsplit = 10m\n",
        )
        ok = run_from_config(str(conf))
        err = capsys.readouterr().err
        assert ok is False
        assert "Ignoring [DEFAULT]" in err
        assert "No backup sections found" in err

    def test_utf8_bom_preserves_global_section(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("A", encoding="utf-8")
        dest = tmp_path / "dest"
        body = (
            f"[GLOBAL]\npassword = from_global\n\n"
            f"[Job]\nsources = {src}\ndest = {dest}\nname = bom\n"
        )
        conf = tmp_path / "bom.conf"
        conf.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(password)
            return True

        with patch("main.execute_backup", side_effect=fake_execute):
            ok = run_from_config(str(conf))

        assert ok is True
        assert captured == ["from_global"]

    def test_quoted_dest_with_spaces_is_unquoted(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("A", encoding="utf-8")
        dest = tmp_path / "BackUp (sync)"
        conf = self._write_conf(
            tmp_path / "quoted_dest.conf",
            f'[Job]\nsources = {src}\ndest = "{dest}"\nname = n\n',
        )

        ok = run_from_config(str(conf))

        assert ok is True
        assert dest.is_dir()
        assert (dest / "n.7z").is_file()

    def test_config_check_content_hash(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("A", encoding="utf-8")
        dest = tmp_path / "dest"
        
        conf = self._write_conf(
            tmp_path / "jobs.conf",
            f"""[GLOBAL]
check_content_hash = true

[Job1]
sources = {src}
dest = {dest}
name = job1

[Job2]
sources = {src}
dest = {dest}
name = job2
check_content_hash = false
""",
        )

        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(check_content_hash)
            return True

        with patch("main.execute_backup", side_effect=fake_execute):
            ok = run_from_config(str(conf))

        assert ok is True
        assert captured == [True, False]

    def test_partial_success_returns_false(self, tmp_path, capsys):
        src = tmp_path / "ok_src"
        src.mkdir()
        (src / "f.txt").write_text("ok", encoding="utf-8")
        dest = tmp_path / "ok_dest"

        conf = self._write_conf(
            tmp_path / "mixed.conf",
            f"""[Good]
sources = {src}
dest = {dest}
name = good

[Bad]
sources = {tmp_path / "missing"}
dest = {tmp_path / "bad_dest"}
name = bad
""",
        )

        ok = run_from_config(str(conf))
        assert ok is False
        assert (dest / "good.7z").is_file()
        assert "Source path does not exist" in capsys.readouterr().err

    def test_unexpected_exception_does_not_abort_other_jobs(self, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("A", encoding="utf-8")
        dest_bad = tmp_path / "dest_bad"
        dest_ok = tmp_path / "dest_ok"
        conf = self._write_conf(
            tmp_path / "jobs.conf",
            f"""[Bad]
sources = {src}
dest = {dest_bad}
name = bad

[Good]
sources = {src}
dest = {dest_ok}
name = good
""",
        )
        names: list[str] = []

        def fake_run(*args, **kwargs):
            name = kwargs.get("archive_name", args[2] if len(args) > 2 else None)
            names.append(name)
            if name == "bad":
                raise Exception("archive exploded")

        with patch("main.run_backup", side_effect=fake_run):
            ok = run_from_config(str(conf))

        assert ok is False
        assert names == ["bad", "good"]
        assert "archive exploded" in capsys.readouterr().err

    def test_empty_section_password_clears_global(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("A", encoding="utf-8")
        dest = tmp_path / "dest"

        conf = self._write_conf(
            tmp_path / "pw.conf",
            f"""[GLOBAL]
password = global_secret

[NoPassword]
sources = {src}
dest = {dest}
name = no_pw
password =
""",
        )

        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(password)
            return True

        with patch("main.execute_backup", side_effect=fake_execute):
            ok = run_from_config(str(conf))

        assert ok is True
        assert captured == [None]

    def test_cli_check_content_hash_respects_explicit_section_false(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("A", encoding="utf-8")
        dest = tmp_path / "dest"

        conf = self._write_conf(
            tmp_path / "cch.conf",
            f"""[GLOBAL]
check_content_hash = true

[ForceOff]
sources = {src}
dest = {dest}
name = off
check_content_hash = false
""",
        )

        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(check_content_hash)
            return True

        with patch("main.execute_backup", side_effect=fake_execute):
            ok = run_from_config(str(conf), cli_check_content_hash=True)

        assert ok is True
        # Explicit `false` in section must win over CLI flag.
        assert captured == [False]

    def test_section_split_empty_or_disabled_overrides_global(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = tmp_path / "split_override.conf"
        conf.write_text(
            f"""[GLOBAL]
split = 100m

[JobEmpty]
sources = {src}
dest = {dest}
name = arc_empty
split =

[JobNone]
sources = {src}
dest = {dest}
name = arc_none
split = none

[JobOff]
sources = {src}
dest = {dest}
name = arc_off
split = off
""",
            encoding="utf-8",
        )
        splits = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            splits.append((archive_name, split_size))
            return True

        with patch("main.execute_backup", side_effect=fake_execute):
            ok = run_from_config(str(conf))

        assert ok is True
        assert splits == [
            ("arc_empty", None),
            ("arc_none", None),
            ("arc_off", None),
        ]

    def test_cli_split_overrides_all_sections(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = tmp_path / "cli_split.conf"
        conf.write_text(
            f"""[GLOBAL]
split = 100m

[Job1]
sources = {src}
dest = {dest}
name = arc1
split = 50m

[Job2]
sources = {src}
dest = {dest}
name = arc2
""",
            encoding="utf-8",
        )
        splits = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            splits.append((archive_name, split_size))
            return True

        with patch("main.execute_backup", side_effect=fake_execute):
            ok = run_from_config(str(conf), cli_split=10 * 1024 * 1024)

        assert ok is True
        assert splits == [
            ("arc1", 10 * 1024 * 1024),
            ("arc2", 10 * 1024 * 1024),
        ]


class TestResolvePassword:
    """Priority: CLI > section (explicit, incl. empty) > env > global."""

    def _conf(self, path: Path, body: str) -> Path:
        path.write_text(body, encoding="utf-8")
        return path

    def test_env_password_used_when_no_cli_and_no_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MICROBACKUP_PASSWORD", "env_secret")
        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(password)
            return True

        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("A", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = self._conf(
            tmp_path / "no_pw.conf",
            f"[Job]\nsources = {src}\ndest = {dest}\nname = n\n",
        )
        with patch("main.execute_backup", side_effect=fake_execute):
            ok = run_from_config(str(conf))
        assert ok is True
        assert captured == ["env_secret"]

    def test_cli_password_overrides_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MICROBACKUP_PASSWORD", "env_secret")
        from main import _resolve_password
        assert _resolve_password("cli_secret") == "cli_secret"

    def test_section_password_overrides_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MICROBACKUP_PASSWORD", "env_secret")
        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(password)
            return True

        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("A", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = self._conf(
            tmp_path / "sec_pw.conf",
            f"[Job]\nsources = {src}\ndest = {dest}\nname = n\npassword = sec\n",
        )
        with patch("main.execute_backup", side_effect=fake_execute):
            ok = run_from_config(str(conf))
        assert ok is True
        assert captured == ["sec"]

    def test_env_overrides_global_when_no_section_password(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MICROBACKUP_PASSWORD", "env_secret")
        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(password)
            return True

        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("A", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = self._conf(
            tmp_path / "glob_pw.conf",
            f"[GLOBAL]\npassword = glob\n\n[Job]\nsources = {src}\ndest = {dest}\nname = n\n",
        )
        with patch("main.execute_backup", side_effect=fake_execute):
            ok = run_from_config(str(conf))
        assert ok is True
        assert captured == ["env_secret"]

    def test_empty_section_password_disables_env_and_global(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MICROBACKUP_PASSWORD", "env_secret")
        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(password)
            return True

        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("A", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = self._conf(
            tmp_path / "empty_pw.conf",
            f"[GLOBAL]\npassword = glob\n\n[Job]\nsources = {src}\ndest = {dest}\nname = n\npassword =\n",
        )
        with patch("main.execute_backup", side_effect=fake_execute):
            ok = run_from_config(str(conf))
        assert ok is True
        assert captured == [None]

    def test_no_env_no_section_falls_back_to_global(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MICROBACKUP_PASSWORD", raising=False)
        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(password)
            return True

        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("A", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = self._conf(
            tmp_path / "fallback.conf",
            f"[GLOBAL]\npassword = glob\n\n[Job]\nsources = {src}\ndest = {dest}\nname = n\n",
        )
        with patch("main.execute_backup", side_effect=fake_execute):
            ok = run_from_config(str(conf))
        assert ok is True
        assert captured == ["glob"]

    def test_cli_password_overrides_config_job(self, tmp_path):
        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(password)
            return True

        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("A", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = self._conf(
            tmp_path / "cli_override.conf",
            f"[GLOBAL]\npassword = glob\n\n[Job]\nsources = {src}\ndest = {dest}\nname = n\npassword = sec\n",
        )
        with patch("main.execute_backup", side_effect=fake_execute):
            ok = run_from_config(str(conf), cli_password="cli_override")
        assert ok is True
        assert captured == ["cli_override"]



class TestMainCli:
    def test_missing_required_args_creates_default_config(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        conf = tmp_path / "MicroBackUp.conf"
        assert conf.is_file()
        captured = capsys.readouterr()
        assert "Created default config file" in captured.out
        import configparser
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(conf.read_text(encoding="utf-8"))
        assert parser.sections() == []

    def test_version_flag_prints_version_and_exits(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["main.py", "-v"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "MicroBackUp" in out
        assert __version__ in out

    def test_missing_config_file(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["main.py", "-c", str(tmp_path / "no.conf")])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        assert "Config file does not exist" in capsys.readouterr().err

    def test_config_mode_success(self, tmp_path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = tmp_path / "ok.conf"
        conf.write_text(
            f"[Job]\nsources = {src}\ndest = {dest}\nname = from_cli\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(sys, "argv", ["main.py", "-c", str(conf)])
        main()
        assert (dest / "from_cli.7z").is_file()

    def test_cli_password_with_config_invokes_backup_with_cli_password(self, tmp_path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = tmp_path / "pw.conf"
        conf.write_text(
            f"[GLOBAL]\npassword = glob\n\n[Job]\nsources = {src}\ndest = {dest}\nname = arc\npassword = sec\n",
            encoding="utf-8",
        )
        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(password)
            return True

        monkeypatch.setattr(
            sys,
            "argv",
            ["main.py", "-c", str(conf), "-p", "cli_master_key"],
        )
        with patch("main.execute_backup", side_effect=fake_execute):
            main()
        assert captured == ["cli_master_key"]

    def test_cli_args_success(self, tmp_path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dest = tmp_path / "dest"

        monkeypatch.setattr(
            sys,
            "argv",
            ["main.py", "-s", str(src), "-d", str(dest), "-n", "cli_arc", "--check-content-hash"],
        )
        main()
        assert (dest / "cli_arc.7z").is_file()
        info = json.loads((dest / "cli_arc_hash.json").read_text(encoding="utf-8"))
        assert "content_hash" in info

    def test_cli_backup_failure_exits(self, tmp_path, monkeypatch):
        dest = tmp_path / "dest"
        monkeypatch.setattr(
            sys,
            "argv",
            ["main.py", "-s", str(tmp_path / "missing"), "-d", str(dest), "-n", "x"],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    def test_config_mode_failure_exits(self, tmp_path, monkeypatch):
        conf = tmp_path / "empty.conf"
        conf.write_text("[GLOBAL]\nsplit = 1m\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["main.py", "-c", str(conf)])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    def test_cli_log_file_creates_log(self, tmp_path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dest = tmp_path / "dest"
        log_file = tmp_path / "cli.log"

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "main.py",
                "-s", str(src),
                "-d", str(dest),
                "-n", "cli_arc",
                "--log-file", str(log_file),
            ],
        )
        main()
        assert log_file.is_file()
        text = log_file.read_text(encoding="utf-8")
        assert "INFO" in text
        assert "Gathering file list" in text

    def test_cli_split_flag_runs_backup(self, tmp_path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.bin").write_bytes(os.urandom(40_000))
        dest = tmp_path / "dest"

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "main.py",
                "-s", str(src),
                "-d", str(dest),
                "-n", "cli_split",
                "--split", "8k",
            ],
        )
        main()
        parts = sorted(dest.glob("cli_split.7z.*"))
        assert len(parts) >= 2
        assert (dest / "cli_split_hash.json").is_file()

    def test_cli_invalid_split_flag_exits(self, tmp_path, monkeypatch, capsys):
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest"

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "main.py",
                "-s", str(src),
                "-d", str(dest),
                "-n", "x",
                "--split", "not_a_size",
            ],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        # argparse exits with code 2 on argument type errors.
        assert exc.value.code == 2

    def test_cli_log_max_size_and_backup_count_flags(self, tmp_path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dest = tmp_path / "dest"
        log_file = tmp_path / "cli.log"

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "main.py",
                "-s", str(src),
                "-d", str(dest),
                "-n", "cli_arc",
                "--log-file", str(log_file),
                "--log-max-size", "200",
                "--log-backup-count", "2",
            ],
        )
        main()
        assert log_file.is_file()
        for handler in logger.handlers:
            handler.flush()
        # Force rotation by emitting more lines beyond max size.
        for i in range(50):
            logger.info("x" * 40 + f" line-{i}")
        for handler in logger.handlers:
            handler.flush()
        assert (tmp_path / "cli.log.1").is_file()

    def test_cli_invalid_log_backup_count_flag_exits(self, tmp_path, monkeypatch, capsys):
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest"

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "main.py",
                "-s", str(src),
                "-d", str(dest),
                "-n", "x",
                "--log-backup-count", "not_an_int",
            ],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2

    def test_cli_unwriteable_log_file_exits(self, tmp_path, monkeypatch, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dest = tmp_path / "dest"
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "main.py",
                "-s", str(src),
                "-d", str(dest),
                "-n", "cli_arc",
                "--log-file", str(tmp_path / "bad.log"),
            ],
        )
        with (
            patch("main.setup_logging", side_effect=ConfigError("Failed to open log")),
            pytest.raises(SystemExit) as exc,
        ):
            main()
        assert exc.value.code == 1
        assert "Failed to open log" in capsys.readouterr().err

    def test_cli_split_with_config_mode_passes_cli_split(self, tmp_path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = tmp_path / "split.conf"
        conf.write_text(
            f"[GLOBAL]\nsplit = 50m\n\n[Job]\nsources = {src}\ndest = {dest}\nname = arc\n",
            encoding="utf-8",
        )
        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(split_size)
            return True

        monkeypatch.setattr(
            sys,
            "argv",
            ["main.py", "-c", str(conf), "--split", "2m"],
        )
        with patch("main.execute_backup", side_effect=fake_execute):
            main()
        assert captured == [2 * 1024 * 1024]

    def test_hide_flag_non_windows_logged_once(self, tmp_path, monkeypatch, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dest = tmp_path / "dest"
        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setattr(
            sys,
            "argv",
            ["main.py", "-s", str(src), "-d", str(dest), "-n", "arc", "--hide"],
        )
        with patch("main.execute_backup", return_value=True):
            main()
        out = capsys.readouterr().out
        assert out.count("--hide is supported only on Windows") == 1


class TestLogHelpers:
    def test_parse_log_level_default(self):
        assert parse_log_level(None, "ctx") == logging.INFO
        assert parse_log_level("", "ctx") == logging.INFO

    def test_parse_log_level_valid(self):
        assert parse_log_level("debug", "ctx") == logging.DEBUG
        assert parse_log_level("ERROR", "ctx") == logging.ERROR

    def test_parse_log_level_invalid(self):
        with pytest.raises(ConfigError, match=r"\[GLOBAL\] log_level: invalid log level"):
            parse_log_level("nope", "[GLOBAL] log_level")

    def test_parse_log_backup_count_default(self):
        assert parse_log_backup_count(None, "ctx") == 3
        assert parse_log_backup_count("", "ctx") == 3

    def test_parse_log_backup_count_valid(self):
        assert parse_log_backup_count("5", "ctx") == 5
        assert parse_log_backup_count(0, "ctx") == 0

    def test_parse_log_backup_count_invalid(self):
        with pytest.raises(ConfigError, match="invalid log_backup_count"):
            parse_log_backup_count("-1", "ctx")
        with pytest.raises(ConfigError, match="invalid log_backup_count"):
            parse_log_backup_count("abc", "ctx")


class TestLoggingSetup:
    def test_writes_to_log_file(self, tmp_path, capsys):
        log_file = tmp_path / "app.log"
        setup_logging(log_file=str(log_file), log_level=logging.INFO)
        logger.info("hello-info")
        logger.error("hello-error")

        captured = capsys.readouterr()
        assert "hello-info" in captured.out
        assert "hello-error" in captured.err

        text = log_file.read_text(encoding="utf-8")
        assert "INFO" in text
        assert "hello-info" in text
        assert "ERROR" in text
        assert "hello-error" in text

    def test_debug_hidden_at_info_level(self, tmp_path, capsys):
        log_file = tmp_path / "app.log"
        setup_logging(log_file=str(log_file), log_level=logging.INFO)
        logger.debug("secret-debug")
        logger.info("visible-info")

        captured = capsys.readouterr()
        assert "secret-debug" not in captured.out
        assert "visible-info" in captured.out
        text = log_file.read_text(encoding="utf-8")
        assert "secret-debug" not in text
        assert "visible-info" in text

    def test_rotates_when_max_size_exceeded(self, tmp_path):
        log_file = tmp_path / "rotate.log"
        max_bytes = 200
        setup_logging(
            log_file=str(log_file),
            log_level=logging.INFO,
            log_max_size=max_bytes,
            log_backup_count=2,
        )
        for i in range(50):
            logger.info("x" * 40 + f" line-{i}")

        for handler in logger.handlers:
            handler.flush()

        rotated = tmp_path / "rotate.log.1"
        assert log_file.is_file()
        # Rotation must have happened (rotated file exists) and the active file
        # must not have grown unbounded. The exact boundary is implementation-
        # dependent (RotatingFileHandler checks size after each emit), so we
        # only assert it stays within a small multiple of max_bytes rather than
        # an exact threshold.
        assert rotated.is_file()
        assert log_file.stat().st_size < 2 * max_bytes

    def test_config_logging_and_invalid_level(self, tmp_path, capsys):
        conf = tmp_path / "bad_level.conf"
        conf.write_text(
            "[GLOBAL]\nlog_level = verbose\n\n[Job]\nsources = a\ndest = b\nname = n\n",
            encoding="utf-8",
        )
        ok = run_from_config(str(conf))
        assert ok is False
        assert "[GLOBAL] log_level" in capsys.readouterr().err

    def test_config_writes_log_file(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("ok", encoding="utf-8")
        dest = tmp_path / "dest"
        log_file = tmp_path / "from_conf.log"
        conf = tmp_path / "jobs.conf"
        conf.write_text(
            f"""[GLOBAL]
log_file = {log_file}
log_level = INFO
log_max_size = 1m
log_backup_count = 2

[Job]
sources = {src}
dest = {dest}
name = logged
""",
            encoding="utf-8",
        )
        assert run_from_config(str(conf)) is True
        assert log_file.is_file()
        text = log_file.read_text(encoding="utf-8")
        assert "Section [Job]" in text
        assert "INFO" in text

    def test_quoted_log_file_in_config(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("ok", encoding="utf-8")
        dest = tmp_path / "dest"
        log_file = tmp_path / "quoted.log"
        conf = tmp_path / "jobs.conf"
        conf.write_text(
            f'''[GLOBAL]
log_file = "{log_file}"

[Job]
sources = {src}
dest = {dest}
name = logged
''',
            encoding="utf-8",
        )
        assert run_from_config(str(conf)) is True
        assert log_file.is_file()
        assert "Section [Job]" in log_file.read_text(encoding="utf-8")

    def test_invalid_log_max_size_in_config(self, tmp_path, capsys):
        conf = tmp_path / "bad_size.conf"
        conf.write_text(
            "[GLOBAL]\nlog_max_size = xyz\n\n[Job]\nsources = a\ndest = b\nname = n\n",
            encoding="utf-8",
        )
        ok = run_from_config(str(conf))
        assert ok is False
        assert "[GLOBAL] log_max_size" in capsys.readouterr().err

    def test_cli_invalid_log_level_exits(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            sys,
            "argv",
            ["main.py", "-s", "a", "-d", "b", "-n", "n", "--log-level", "nope"],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        assert "invalid log level" in capsys.readouterr().err

    def test_setup_logging_oserror_raises_config_error(self, tmp_path):
        with (
            patch("main.RotatingFileHandler", side_effect=OSError("Permission denied")),
            pytest.raises(ConfigError, match="Could not initialize log file"),
        ):
            setup_logging(log_file=str(tmp_path / "denied.log"))

    def test_current_stream_handler_flush(self):
        from main import _CurrentStreamHandler
        handler = _CurrentStreamHandler("stdout")
        with patch.object(sys.stdout, "flush") as mock_flush:
            handler.flush()
            mock_flush.assert_called_once()

    def test_current_stream_handler_when_streams_are_none(self, tmp_path, monkeypatch):
        """Emulate pythonw / PyInstaller --noconsole environment where stdout/stderr are None."""
        from main import _CurrentStreamHandler
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)

        stdout_handler = _CurrentStreamHandler("stdout")
        stderr_handler = _CurrentStreamHandler("stderr")

        assert stdout_handler.stream is None
        assert stderr_handler.stream is None

        record = logging.LogRecord(
            name="microbackup",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="test noconsole",
            args=(),
            exc_info=None,
        )

        # emit and flush should complete cleanly without throwing AttributeError
        stdout_handler.emit(record)
        stdout_handler.flush()
        stderr_handler.emit(record)
        stderr_handler.flush()

        # setup_logging with file logging works even when console streams are None
        log_file = tmp_path / "bg.log"
        test_logger = setup_logging(log_file=str(log_file), log_level=logging.INFO)
        test_logger.info("bg info message")
        test_logger.warning("bg warning message")
        test_logger.error("bg error message")

        assert log_file.is_file()
        content = log_file.read_text(encoding="utf-8")
        assert "bg info message" in content
        assert "bg warning message" in content
        assert "bg error message" in content


class TestExcludeConfig:
    def _write_conf(self, path: Path, text: str) -> Path:
        path.write_text(text, encoding="utf-8")
        return path

    def test_section_exclude_only(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("A", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = self._write_conf(
            tmp_path / "jobs.conf",
            f"""[Job]
sources = {src}
dest = {dest}
name = job
exclude =
    *.log
    node_modules/
""",
        )
        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(exclude)
            return True

        with patch("main.execute_backup", side_effect=fake_execute):
            assert run_from_config(str(conf)) is True
        assert captured == [["*.log", "node_modules/"]]

    def test_global_exclude_only(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("A", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = self._write_conf(
            tmp_path / "jobs.conf",
            f"""[GLOBAL]
exclude =
    *.tmp
    ~$*

[Job]
sources = {src}
dest = {dest}
name = job
""",
        )
        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(exclude)
            return True

        with patch("main.execute_backup", side_effect=fake_execute):
            assert run_from_config(str(conf)) is True
        assert captured == [["*.tmp", "~$*"]]

    def test_global_and_section_are_merged(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("A", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = self._write_conf(
            tmp_path / "jobs.conf",
            f"""[GLOBAL]
exclude =
    *.tmp

[Job]
sources = {src}
dest = {dest}
name = job
exclude =
    *.log
    !keep.log
""",
        )
        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(exclude)
            return True

        with patch("main.execute_backup", side_effect=fake_execute):
            assert run_from_config(str(conf)) is True
        assert captured == [["*.tmp", "*.log", "!keep.log"]]

    def test_section_negation_overrides_global(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "keep.tmp").write_text("keep", encoding="utf-8")
        (src / "drop.tmp").write_text("drop", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = self._write_conf(
            tmp_path / "jobs.conf",
            f"""[GLOBAL]
exclude =
    *.tmp

[Job]
sources = {src}
dest = {dest}
name = job
exclude =
    !keep.tmp
""",
        )
        assert run_from_config(str(conf)) is True
        extracted = tmp_path / "out"
        with py7zr.SevenZipFile(dest / "job.7z", "r") as archive:
            archive.extractall(path=extracted)
        assert (extracted / "src" / "keep.tmp").read_text(encoding="utf-8") == "keep"
        assert not (extracted / "src" / "drop.tmp").exists()

    def test_invalid_pattern_skips_section_and_continues(self, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("A", encoding="utf-8")
        dest_bad = tmp_path / "dest_bad"
        dest_ok = tmp_path / "dest_ok"
        conf = self._write_conf(
            tmp_path / "jobs.conf",
            f"""[Bad]
sources = {src}
dest = {dest_bad}
name = bad
exclude = !

[Good]
sources = {src}
dest = {dest_ok}
name = good
""",
        )
        ok = run_from_config(str(conf))
        captured = capsys.readouterr()
        assert ok is False
        assert "[Bad] exclude:" in captured.err
        assert "Invalid exclude pattern" in captured.err
        assert (dest_ok / "good.7z").is_file()
        assert not (dest_bad / "bad.7z").exists()

    def test_missing_exclude_backs_up_all_files(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("A", encoding="utf-8")
        (src / "b.log").write_text("B", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = self._write_conf(
            tmp_path / "jobs.conf",
            f"""[Job]
sources = {src}
dest = {dest}
name = job
""",
        )
        assert run_from_config(str(conf)) is True
        extracted = tmp_path / "out"
        with py7zr.SevenZipFile(dest / "job.7z", "r") as archive:
            archive.extractall(path=extracted)
        assert (extracted / "src" / "a.txt").read_text(encoding="utf-8") == "A"
        assert (extracted / "src" / "b.log").read_text(encoding="utf-8") == "B"


class TestProgramDir:
    def test_source_layout(self, tmp_path):
        source = tmp_path / "pkg" / "main.py"
        source.parent.mkdir()
        source.write_text("#", encoding="utf-8")
        assert _program_dir(False, str(tmp_path / "python.exe"), str(source)) == source.parent.resolve()

    def test_frozen_layout(self, tmp_path):
        exe = tmp_path / "dist" / "MicroBackUp.exe"
        exe.parent.mkdir()
        exe.write_bytes(b"")
        assert _program_dir(True, str(exe), str(tmp_path / "unused.py")) == exe.parent.resolve()


class TestAutoConfig:
    def test_existing_default_config_is_used(self, tmp_path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = tmp_path / "MicroBackUp.conf"
        conf.write_text(
            f"[Job]\nsources = {src}\ndest = {dest}\nname = auto_arc\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(sys, "argv", ["main.py"])
        main()
        assert (dest / "auto_arc.7z").is_file()

    def test_cli_sources_skip_autoconfig(self, tmp_path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dest = tmp_path / "dest"
        monkeypatch.setattr(
            sys,
            "argv",
            ["main.py", "-s", str(src), "-d", str(dest), "-n", "cli_arc"],
        )
        main()
        assert (dest / "cli_arc.7z").is_file()
        assert not (tmp_path / "MicroBackUp.conf").exists()

    def test_explicit_config_skips_autoconfig(self, tmp_path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dest = tmp_path / "dest"
        other = tmp_path / "other.conf"
        other.write_text(
            f"[Job]\nsources = {src}\ndest = {dest}\nname = from_c\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(sys, "argv", ["main.py", "-c", str(other)])
        main()
        assert (dest / "from_c.7z").is_file()
        assert not (tmp_path / "MicroBackUp.conf").exists()

    def test_create_config_oserror_exits_1(self, tmp_path, monkeypatch, capsys):
        def boom(path):
            raise OSError("denied")

        monkeypatch.setattr("main.write_default_config", boom)
        monkeypatch.setattr(sys, "argv", ["main.py"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        assert "Could not create config file" in capsys.readouterr().err


class TestCompressionAndSevenZipConfig:
    def _write_conf(self, path: Path, text: str) -> Path:
        path.write_text(text, encoding="utf-8")
        return path

    def test_global_and_section_priority(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dest_a = tmp_path / "a"
        dest_b = tmp_path / "b"
        conf = self._write_conf(
            tmp_path / "jobs.conf",
            f"""[GLOBAL]
compression_level = 5
sevenzip_path = C:\\\\global\\\\7z.exe
sevenzip_args = -mmt=2

[JobA]
sources = {src}
dest = {dest_a}
name = a

[JobB]
sources = {src}
dest = {dest_b}
name = b
compression_level = 9
sevenzip_path = none
sevenzip_args = -mmt=8
""",
        )
        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(
                {
                    "name": archive_name,
                    "compression_level": compression_level,
                    "sevenzip": sevenzip,
                }
            )
            return True

        with patch("main.execute_backup", side_effect=fake_execute):
            assert run_from_config(str(conf)) is True

        job_a = captured[0]
        job_b = captured[1]
        assert job_a["compression_level"] == 5
        assert job_a["sevenzip"] is not None
        assert job_a["sevenzip"].path.endswith("7z.exe")
        assert job_a["sevenzip"].extra_args == ("-mmt=2",)
        assert job_b["compression_level"] == 9
        assert job_b["sevenzip"] is None

    def test_empty_section_level_clears_global(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = self._write_conf(
            tmp_path / "jobs.conf",
            f"""[GLOBAL]
compression_level = 7

[Job]
sources = {src}
dest = {dest}
name = n
compression_level =
""",
        )
        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(compression_level)
            return True

        with patch("main.execute_backup", side_effect=fake_execute):
            assert run_from_config(str(conf)) is True
        assert captured == [None]

    def test_invalid_compression_level_returns_false(self, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest"
        conf = self._write_conf(
            tmp_path / "bad.conf",
            f"[GLOBAL]\ncompression_level = 99\n\n[Job]\nsources = {src}\ndest = {dest}\nname = n\n",
        )
        assert run_from_config(str(conf)) is False
        assert "compression_level" in capsys.readouterr().err

    def test_invalid_job_compression_level_skips(self, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = self._write_conf(
            tmp_path / "bad.conf",
            f"[Job]\nsources = {src}\ndest = {dest}\nname = n\ncompression_level = abc\n",
        )
        ok = run_from_config(str(conf))
        assert ok is False
        assert "compression_level" in capsys.readouterr().err

    def test_cli_overrides_config(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dest = tmp_path / "dest"
        conf = self._write_conf(
            tmp_path / "jobs.conf",
            f"""[GLOBAL]
compression_level = 1
sevenzip_path = C:\\\\cfg\\\\7z.exe
sevenzip_args = -mmt=1

[Job]
sources = {src}
dest = {dest}
name = n
""",
        )
        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append((compression_level, sevenzip))
            return True

        with patch("main.execute_backup", side_effect=fake_execute):
            assert run_from_config(
                str(conf),
                cli_compression_level=9,
                cli_sevenzip_path=r"D:\cli\7z.exe",
                cli_sevenzip_args=("-mmt=4",),
            ) is True
        level, options = captured[0]
        assert level == 9
        assert options.path == r"D:\cli\7z.exe"
        assert options.extra_args == ("-mmt=4",)

    def test_cli_mode_passes_compression_level(self, tmp_path, monkeypatch):
        src = tmp_path / "src"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dest = tmp_path / "dest"
        captured = []

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False, exclude=None, sevenzip=None, compression_level=None):
            captured.append(compression_level)
            return True

        monkeypatch.setattr(
            sys,
            "argv",
            ["main.py", "-s", str(src), "-d", str(dest), "-n", "n", "--compression-level", "0"],
        )
        with patch("main.execute_backup", side_effect=fake_execute):
            main()
        assert captured == [0]

    def test_cli_invalid_compression_level_exits(self, tmp_path, monkeypatch, capsys):
        src = tmp_path / "src"
        src.mkdir()
        dest = tmp_path / "dest"
        monkeypatch.setattr(
            sys,
            "argv",
            ["main.py", "-s", str(src), "-d", str(dest), "-n", "n", "--compression-level", "99"],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        assert "compression_level" in capsys.readouterr().err
