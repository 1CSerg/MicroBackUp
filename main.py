from __future__ import annotations

import argparse
import configparser
import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Any

from backup import run_backup

__version__ = "1.0.0"

GLOBAL_SECTION = "GLOBAL"
LOGGER_NAME = "microbackup"
DEFAULT_LOG_LEVEL = logging.INFO
DEFAULT_LOG_BACKUP_COUNT = 3
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

logger = logging.getLogger(LOGGER_NAME)


class _MaxLevelFilter(logging.Filter):
    """Allow records strictly below max_level (used to keep INFO on stdout)."""

    def __init__(self, max_level: int) -> None:
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self.max_level


class _CurrentStreamHandler(logging.StreamHandler):
    """StreamHandler that always writes to the live sys.stdout or sys.stderr."""

    def __init__(self, stream_name: str) -> None:
        super().__init__(stream=getattr(sys, stream_name))
        self._stream_name = stream_name

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = getattr(sys, self._stream_name)
        super().emit(record)


def _clear_handlers(target: logging.Logger) -> None:
    for handler in list(target.handlers):
        target.removeHandler(handler)
        handler.close()


def _add_console_handlers(target: logging.Logger, level: int) -> None:
    console_fmt = logging.Formatter("%(message)s")

    stdout_handler = _CurrentStreamHandler("stdout")
    stdout_handler.setLevel(level)
    stdout_handler.addFilter(_MaxLevelFilter(logging.WARNING))
    stdout_handler.setFormatter(console_fmt)
    target.addHandler(stdout_handler)

    stderr_handler = _CurrentStreamHandler("stderr")
    stderr_handler.setLevel(max(level, logging.WARNING))
    stderr_handler.setFormatter(console_fmt)
    target.addHandler(stderr_handler)


def setup_logging(log_file: Optional[str] = None, log_level: Optional[int] = None, log_max_size: Optional[int] = None, log_backup_count: Optional[int] = None) -> logging.Logger:
    """Configure console and optional rotating file logging."""
    level = DEFAULT_LOG_LEVEL if log_level is None else log_level
    backup_count = DEFAULT_LOG_BACKUP_COUNT if log_backup_count is None else log_backup_count

    logger.setLevel(level)
    logger.propagate = False
    _clear_handlers(logger)
    _add_console_handlers(logger, level)

    if log_file:
        log_path = Path(log_file)
        if log_path.parent and str(log_path.parent) not in ("", "."):
            log_path.parent.mkdir(parents=True, exist_ok=True)

        max_bytes = log_max_size or 0
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        logger.addHandler(file_handler)

    return logger


def parse_size(size_str: Optional[str]) -> Optional[int]:
    if not size_str:
        return None
    size_str = size_str.strip().lower()
    if size_str.endswith('k'):
        multiplier = 1024
        number_part = size_str[:-1]
        try:
            numeric = float(number_part)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"Invalid size format: {size_str!r}. Use k, m, or g suffixes (e.g., 100m)."
            )
    elif size_str.endswith('m'):
        multiplier = 1024 * 1024
        number_part = size_str[:-1]
        try:
            numeric = float(number_part)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"Invalid size format: {size_str!r}. Use k, m, or g suffixes (e.g., 100m)."
            )
    elif size_str.endswith('g'):
        multiplier = 1024 * 1024 * 1024
        number_part = size_str[:-1]
        try:
            numeric = float(number_part)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"Invalid size format: {size_str!r}. Use k, m, or g suffixes (e.g., 100m)."
            )
    else:
        multiplier = 1
        try:
            numeric = float(size_str)
            if numeric != int(numeric):
                raise ValueError
            numeric = int(numeric)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"Invalid size format: {size_str!r}. Use k, m, or g suffixes (e.g., 100m)."
            )

    if numeric <= 0:
        raise argparse.ArgumentTypeError(
            f"Size must be strictly positive, got {numeric!r}."
        )

    value = int(numeric * multiplier)
    if value <= 0:
        raise argparse.ArgumentTypeError(
            f"Size {numeric!r} with suffix rounds to 0 bytes; use a larger value."
        )
    return value


def parse_sources(sources_str: str) -> list[str]:
    """Split a sources string into paths. Quotes preserve paths with spaces."""
    if not sources_str:
        return []
    matches = re.findall(r'"([^"]+)"|\'([^\']+)\'|(\S+)', sources_str)
    return [m[0] or m[1] or m[2] for m in matches]


def parse_log_level(value: Any, context: str) -> Any:
    if not value:
        return DEFAULT_LOG_LEVEL
    name = str(value).strip().upper()
    if name not in VALID_LOG_LEVELS:
        logger.error(
            f"Error: {context}: invalid log level '{value}'. "
            f"Use {', '.join(VALID_LOG_LEVELS)}."
        )
        return False
    return getattr(logging, name)


def parse_log_backup_count(value: Any, context: str) -> Any:
    if value is None or value == "":
        return DEFAULT_LOG_BACKUP_COUNT
    try:
        count = int(value)
        if count < 0:
            raise ValueError
        return count
    except (TypeError, ValueError):
        logger.error(f"Error: {context}: invalid log_backup_count '{value}'. Use a non-negative integer.")
        return False


def _validate_archive_name(archive_name: str) -> Optional[str]:
    """Return an error message if archive_name is unsafe, else None."""
    if not archive_name or archive_name in (".", ".."):
        return f"Archive name is empty or reserved: {archive_name!r}"
    # Reject path separators and traversal segments.
    if "\\" in archive_name or "/" in archive_name:
        return f"Archive name must not contain path separators: {archive_name!r}"
    # Reject Windows-invalid filename characters.
    if re.search(r'[<>:"|?*]', archive_name):
        return f"Archive name contains forbidden characters: {archive_name!r}"
    return None


def execute_backup(sources: list[str], dest: str, archive_name: str, split_size: Optional[int], password: Optional[str], check_content_hash: bool = False) -> bool:
    name_error = _validate_archive_name(archive_name)
    if name_error:
        logger.error(f"Error: {name_error}")
        return False

    for src in sources:
        if not os.path.exists(src):
            logger.error(f"Error: Source path does not exist: {src}")
            return False

    if not os.path.exists(dest):
        try:
            os.makedirs(dest)
        except OSError as e:
            logger.error(f"Error: Could not create destination directory {dest}: {e}")
            return False

    try:
        run_backup(
            sources=sources,
            dest=dest,
            archive_name=archive_name,
            split_size=split_size,
            password=password,
            check_content_hash=check_content_hash
        )
    except Exception as e:
        logger.error(f"Backup failed: {e}")
        return False

    return True


def parse_optional_size(value: Optional[str], context: str) -> Any:
    if not value:
        return None
    try:
        return parse_size(value)
    except argparse.ArgumentTypeError as e:
        logger.error(f"Error: {context}: {e}")
        return False


def _apply_logging_config(global_parser: configparser.ConfigParser, section: Optional[str], overrides: Optional[dict[str, Any]]) -> bool:
    """Build logging settings from [GLOBAL] with optional CLI overrides."""
    overrides = overrides or {}

    log_file = overrides.get("log_file")
    if not log_file and section:
        log_file = global_parser.get(section, "log_file", fallback=None) or None

    if overrides.get("log_level") is not None:
        log_level = overrides["log_level"]
    elif section:
        parsed = parse_log_level(
            global_parser.get(section, "log_level", fallback=None),
            f"[{section}] log_level",
        )
        if parsed is False:
            return False
        log_level = parsed
    else:
        log_level = DEFAULT_LOG_LEVEL

    if overrides.get("log_max_size") is not None:
        log_max_size = overrides["log_max_size"]
    elif section:
        parsed = parse_optional_size(
            global_parser.get(section, "log_max_size", fallback=None),
            f"[{section}] log_max_size",
        )
        if parsed is False:
            return False
        log_max_size = parsed
    else:
        log_max_size = None

    if overrides.get("log_backup_count") is not None:
        log_backup_count = overrides["log_backup_count"]
    elif section:
        parsed = parse_log_backup_count(
            global_parser.get(section, "log_backup_count", fallback=None),
            f"[{section}] log_backup_count",
        )
        if parsed is False:
            return False
        log_backup_count = parsed
    else:
        log_backup_count = DEFAULT_LOG_BACKUP_COUNT

    setup_logging(
        log_file=log_file,
        log_level=log_level,
        log_max_size=log_max_size,
        log_backup_count=log_backup_count,
    )
    return True


def run_from_config(config_path: str, log_overrides: Optional[dict[str, Any]] = None, cli_check_content_hash: bool = False) -> bool:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(config_path, encoding='utf-8') as f:
            parser.read_file(f)
    except OSError as e:
        logger.error(f"Error: Could not read config file {config_path}: {e}")
        return False
    except configparser.Error as e:
        logger.error(f"Error: Invalid config file {config_path}: {e}")
        return False

    global_split = None
    global_password = None
    global_section_name = None
    global_check_content_hash = False
    job_sections = []

    for section in parser.sections():
        if section.upper() == GLOBAL_SECTION:
            global_section_name = section
            split_raw = parser.get(section, 'split', fallback=None)
            parsed = parse_optional_size(split_raw, f"[{section}] split")
            if parsed is False:
                return False
            global_split = parsed
            global_password = parser.get(section, 'password', fallback=None) or None
            global_check_content_hash = parser.getboolean(section, 'check_content_hash', fallback=False)
        else:
            job_sections.append(section)

    if _apply_logging_config(parser, global_section_name, log_overrides) is False:
        return False

    if not job_sections:
        logger.error(f"Error: No backup sections found in {config_path}")
        return False

    any_ok = False
    any_failed = False

    for section in job_sections:
        logger.info(f"\n=== Section [{section}] ===")
        sources_str = parser.get(section, 'sources', fallback='').strip()
        dest = parser.get(section, 'dest', fallback='').strip()
        name = parser.get(section, 'name', fallback='').strip()

        missing = [field for field, value in (('sources', sources_str), ('dest', dest), ('name', name)) if not value]
        if missing:
            logger.warning(f"Warning: Skipping section [{section}]: missing {', '.join(missing)}")
            any_failed = True
            continue

        sources = parse_sources(sources_str)
        if not sources:
            logger.warning(f"Warning: Skipping section [{section}]: sources is empty")
            any_failed = True
            continue

        split_raw = parser.get(section, 'split', fallback=None)
        if split_raw:
            split_size = parse_optional_size(split_raw, f"[{section}] split")
            if split_size is False:
                any_failed = True
                continue
        else:
            split_size = global_split

        password = parser.get(section, 'password', fallback=None) or global_password
        
        check_content_hash = cli_check_content_hash or parser.getboolean(section, 'check_content_hash', fallback=global_check_content_hash)

        if execute_backup(sources, dest, name, split_size, password, check_content_hash=check_content_hash):
            any_ok = True
        else:
            any_failed = True

    if not any_ok:
        logger.error("Error: No backup jobs completed successfully.")
        return False

    return not any_failed


def _cli_log_overrides(args: argparse.Namespace) -> Any:
    overrides = {}
    if args.log_file is not None:
        overrides["log_file"] = args.log_file
    if args.log_level is not None:
        parsed = parse_log_level(args.log_level, "--log-level")
        if parsed is False:
            return False
        overrides["log_level"] = parsed
    if args.log_max_size is not None:
        overrides["log_max_size"] = args.log_max_size
    if args.log_backup_count is not None:
        parsed = parse_log_backup_count(args.log_backup_count, "--log-backup-count")
        if parsed is False:
            return False
        overrides["log_backup_count"] = parsed
    return overrides


def main() -> None:
    parser = argparse.ArgumentParser(description="MicroBackUp - Simple cross-platform incremental backup utility.")

    parser.add_argument(
        '-v', '--version',
        action='version',
        version=f"MicroBackUp {__version__}",
        help="Show program's version number and exit."
    )

    parser.add_argument(
        '-c', '--config',
        help="Optional. Path to an INI config file with backup jobs."
    )

    parser.add_argument(
        '-s', '--sources',
        nargs='+',
        help="List of source directories and/or files to backup."
    )

    parser.add_argument(
        '-d', '--dest',
        help="Destination directory where the backup archive will be saved."
    )

    parser.add_argument(
        '-n', '--name',
        help="Name of the archive (without extension)."
    )

    parser.add_argument(
        '--split',
        type=parse_size,
        help="Optional. Split archive into volumes. Use suffixes k, m, g (e.g., 4500k, 100m, 1.5g)."
    )

    parser.add_argument(
        '-p', '--password',
        help="Optional. Password for the archive."
    )

    parser.add_argument(
        '--hide',
        action='store_true',
        help="Windows only: Hide the console window immediately after starting."
    )

    parser.add_argument(
        '--log-file',
        help="Optional. Path to a log file. Without -c, omit to log to the console only. With -c, overrides [GLOBAL] log_file."
    )

    parser.add_argument(
        '--log-level',
        help="Optional. Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL. Default: INFO."
    )

    parser.add_argument(
        '--log-max-size',
        type=parse_size,
        help="Optional. Rotate the log file when it reaches this size. Use suffixes k, m, g (e.g., 5m). Omit to disable rotation."
    )

    parser.add_argument(
        '--log-backup-count',
        type=int,
        help="Optional. Number of rotated log files to keep. Default: 3."
    )

    parser.add_argument(
        '--check-content-hash',
        action='store_true',
        help="Optional. Force checking full file content hashes instead of just metadata."
    )

    args = parser.parse_args()

    # Скрываем окно консоли в Windows, если запрошено
    if args.hide and os.name == 'nt':
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)

    log_overrides = _cli_log_overrides(args)
    if log_overrides is False:
        sys.exit(1)

    if args.config:
        if not os.path.exists(args.config):
            logger.error(f"Error: Config file does not exist: {args.config}")
            sys.exit(1)
        if not run_from_config(args.config, log_overrides=log_overrides, cli_check_content_hash=args.check_content_hash):
            sys.exit(1)
        return

    setup_logging(
        log_file=log_overrides.get("log_file"),
        log_level=log_overrides.get("log_level"),
        log_max_size=log_overrides.get("log_max_size"),
        log_backup_count=log_overrides.get("log_backup_count"),
    )

    if not args.sources or not args.dest or not args.name:
        parser.error("the following arguments are required: -s/--sources, -d/--dest, -n/--name (or use -c/--config)")

    if not execute_backup(args.sources, args.dest, args.name, args.split, args.password, args.check_content_hash):
        sys.exit(1)


# Default console logging so library callers and tests see output without main().
logger.propagate = False
if not logger.handlers:
    logger.setLevel(DEFAULT_LOG_LEVEL)
    _add_console_handlers(logger, DEFAULT_LOG_LEVEL)


if __name__ == "__main__":
    main()
