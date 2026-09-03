import argparse
import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from main import (
    ConfigError,
    execute_backup,
    logger,
    main,
    parse_log_backup_count,
    parse_log_level,
    parse_optional_size,
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


class TestParseOptionalSize:
    def test_empty_returns_none(self):
        assert parse_optional_size(None, "ctx") is None
        assert parse_optional_size("", "ctx") is None

    def test_valid_size(self):
        assert parse_optional_size("10k", "ctx") == 10 * 1024

    def test_invalid_size_raises_config_error(self):
        with pytest.raises(ConfigError, match=r"\[GLOBAL\] split: Invalid size format"):
            parse_optional_size("nope", "[GLOBAL] split")


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


class TestExecuteBackup:
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

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False):
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

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False):
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

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False):
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

        def fake_execute(sources, dest, archive_name, split_size, password, check_content_hash=False):
            captured.append(check_content_hash)
            return True

        with patch("main.execute_backup", side_effect=fake_execute):
            ok = run_from_config(str(conf), cli_check_content_hash=True)

        assert ok is True
        # Explicit `false` in section must win over CLI flag.
        assert captured == [False]


class TestMainCli:
    def test_missing_required_args(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        with pytest.raises(SystemExit):
            main()

    def test_version_flag_prints_version_and_exits(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["main.py", "-v"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "MicroBackUp" in out
        assert "1.0.0" in out

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
        setup_logging(
            log_file=str(log_file),
            log_level=logging.INFO,
            log_max_size=200,
            log_backup_count=2,
        )
        for i in range(50):
            logger.info("x" * 40 + f" line-{i}")

        for handler in logger.handlers:
            handler.flush()

        rotated = tmp_path / "rotate.log.1"
        assert log_file.is_file()
        assert rotated.is_file()
        assert log_file.stat().st_size <= 200 + 100

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
