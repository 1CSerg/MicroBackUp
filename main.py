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

try:
    from backup import run_backup, _validate_archive_name
except ImportError as _exc:  # pragma: no cover - depends on runtime environment
    run_backup = None  # type: ignore[assignment]
    _IMPORT_ERROR = _exc
    _WINDOWS_RESERVED_NAMES = frozenset(
        {"CON", "PRN", "AUX", "NUL"}
        | {f"COM{i}" for i in range(1, 10)}
        | {f"LPT{i}" for i in range(1, 10)}
    )

    def _validate_archive_name(archive_name: str) -> Optional[str]:  # type: ignore[misc]
        if not archive_name or archive_name in (".", ".."):
            return f"Archive name is empty or reserved: {archive_name!r}"
        if "\\" in archive_name or "/" in archive_name:
            return f"Archive name must not contain path separators: {archive_name!r}"
        if re.search(r'[\x00-\x1f<>:"|?*]', archive_name):
            return f"Archive name contains forbidden characters: {archive_name!r}"
        if archive_name != archive_name.rstrip(". "):
            return f"Archive name must not end with dots or spaces: {archive_name!r}"
        stem = archive_name.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            return f"Archive name is a reserved device name: {archive_name!r}"
        return None
else:
    _IMPORT_ERROR = None

__version__ = "1.0.0"

GLOBAL_SECTION = "GLOBAL"
LOGGER_NAME = "microbackup"
DEFAULT_LOG_LEVEL = logging.INFO
DEFAULT_LOG_BACKUP_COUNT = 3
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
PASSWORD_ENV_VAR = "MICROBACKUP_PASSWORD"

# Sentinel for "value not provided" (distinct from None which means "explicitly empty").
_UNSET = object()

logger = logging.getLogger(LOGGER_NAME)


class ConfigError(ValueError):
    """Raised when a config value is invalid."""


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
        self._stream_name = stream_name
        super().__init__(stream=getattr(sys, stream_name, None))

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = getattr(sys, self._stream_name, None)
        if self.stream is None:
            return
        super().emit(record)

    def flush(self) -> None:
        self.stream = getattr(sys, self._stream_name, None)
        if self.stream is None:
            return
        super().flush()


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
        try:
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
        except OSError as e:
            raise ConfigError(f"Could not initialize log file '{log_file}': {e}") from e

    return logger


def parse_size(size_str: Optional[str]) -> Optional[int]:
    if not size_str:
        return None
    size_str = size_str.strip().lower()
    if not size_str:
        raise argparse.ArgumentTypeError(
            "Invalid size format: ''. Use k, m, or g suffixes (e.g., 100m)."
        )

    suffixes = {'k': 1024, 'm': 1024 * 1024, 'g': 1024 * 1024 * 1024}
    multiplier = 1
    number_part = size_str
    # Accept both single-letter (k, m, g) and two-letter (kb, mb, gb) suffixes.
    if len(size_str) >= 2 and size_str[-2:] in ('kb', 'mb', 'gb'):
        multiplier = suffixes[size_str[-2]]
        number_part = size_str[:-2]
    else:
        last = size_str[-1]
        if last in suffixes:
            multiplier = suffixes[last]
            number_part = size_str[:-1]

    try:
        numeric = float(number_part)
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


def parse_log_level(value: Any, context: str) -> int:
    if not value:
        return DEFAULT_LOG_LEVEL
    name = str(value).strip().upper()
    if name not in VALID_LOG_LEVELS:
        raise ConfigError(
            f"{context}: invalid log level '{value}'. "
            f"Use {', '.join(VALID_LOG_LEVELS)}."
        )
    return getattr(logging, name)


def parse_log_backup_count(value: Any, context: str) -> int:
    if value is None or value == "":
        return DEFAULT_LOG_BACKUP_COUNT
    try:
        count = int(value)
        if count < 0:
            raise ValueError
        return count
    except (TypeError, ValueError):
        raise ConfigError(f"{context}: invalid log_backup_count '{value}'. Use a non-negative integer.")


def _get_boolean(parser: configparser.ConfigParser, section: str, option: str, context: str, fallback: bool = False) -> bool:
    """Read a boolean option, raising ConfigError on a malformed value."""
    if not parser.has_option(section, option):
        return fallback
    try:
        return parser.getboolean(section, option)
    except ValueError as e:
        raw = parser.get(section, option)
        raise ConfigError(
            f"{context}: invalid boolean '{raw}'. Use true or false."
        ) from e


def _resolve_password(
    cli_password: Optional[str] = None,
    section_password: Any = _UNSET,
    global_password: Optional[str] = None,
) -> Optional[str]:
    """Resolve the archive password with priority: CLI > section (explicit, incl. empty) > env > global.

    `section_password` uses the _UNSET sentinel to distinguish "option absent"
    (fall through to env/global) from "option present but empty" (disable password).
    """
    if cli_password is not None:
        return cli_password
    if section_password is not _UNSET:
        return section_password
    env = os.environ.get(PASSWORD_ENV_VAR)
    if env:
        return env
    return global_password


def execute_backup(sources: list[str], dest: str, archive_name: str, split_size: Optional[int], password: Optional[str], check_content_hash: bool = False) -> bool:
    if run_backup is None:
        logger.error(
            f"Error: required dependency missing ({_IMPORT_ERROR}). "
            f"Install runtime dependencies: pip install -r requirements.txt"
        )
        return False

    if not sources:
        logger.error("Error: Sources list is empty")
        return False

    name_error = _validate_archive_name(archive_name)
    if name_error:
        logger.error(f"Error: {name_error}")
        return False

    for src in sources:
        if not os.path.exists(src):
            logger.error(f"Error: Source path does not exist: {src}")
            return False

    if os.path.isfile(dest):
        logger.error(f"Error: Destination path is an existing file, not a directory: {dest}")
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


def parse_optional_size(value: Optional[str], context: str) -> Optional[int]:
    if not value:
        return None
    val_clean = value.strip().lower()
    if val_clean in ("none", "off"):
        return None
    try:
        return parse_size(value)
    except argparse.ArgumentTypeError as e:
        raise ConfigError(f"{context}: {e}") from e


def _apply_logging_config(global_parser: configparser.ConfigParser, section: Optional[str], overrides: Optional[dict[str, Any]]) -> None:
    """Build logging settings from [GLOBAL] with optional CLI overrides.

    Raises ConfigError on invalid values.
    """
    overrides = overrides or {}

    log_file = overrides.get("log_file")
    if not log_file and section:
        log_file = global_parser.get(section, "log_file", fallback=None) or None

    if overrides.get("log_level") is not None:
        log_level = overrides["log_level"]
    elif section:
        log_level = parse_log_level(
            global_parser.get(section, "log_level", fallback=None),
            f"[{section}] log_level",
        )
    else:
        log_level = DEFAULT_LOG_LEVEL

    if overrides.get("log_max_size") is not None:
        log_max_size = overrides["log_max_size"]
    elif section:
        log_max_size = parse_optional_size(
            global_parser.get(section, "log_max_size", fallback=None),
            f"[{section}] log_max_size",
        )
    else:
        log_max_size = None

    if overrides.get("log_backup_count") is not None:
        log_backup_count = overrides["log_backup_count"]
    elif section:
        log_backup_count = parse_log_backup_count(
            global_parser.get(section, "log_backup_count", fallback=None),
            f"[{section}] log_backup_count",
        )
    else:
        log_backup_count = DEFAULT_LOG_BACKUP_COUNT

    setup_logging(
        log_file=log_file,
        log_level=log_level,
        log_max_size=log_max_size,
        log_backup_count=log_backup_count,
    )


def run_from_config(
    config_path: str,
    log_overrides: Optional[dict[str, Any]] = None,
    cli_check_content_hash: bool = False,
    cli_password: Optional[str] = None,
    cli_split: Optional[int] = None,
) -> bool:
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

    try:
        for section in parser.sections():
            if section.upper() == GLOBAL_SECTION:
                if global_section_name is not None:
                    raise ConfigError(
                        f"Duplicate [GLOBAL] section: both [{global_section_name}] and "
                        f"[{section}] normalize to GLOBAL; keep exactly one."
                    )
                global_section_name = section
                split_raw = parser.get(section, 'split', fallback=None)
                global_split = parse_optional_size(split_raw, f"[{section}] split")
                global_password = parser.get(section, 'password', fallback=None) or None
                global_check_content_hash = _get_boolean(parser, section, 'check_content_hash', f"[{section}] check_content_hash")
            else:
                job_sections.append(section)
    except ConfigError as e:
        logger.error(f"Error: {e}")
        return False

    try:
        _apply_logging_config(parser, global_section_name, log_overrides)
    except ConfigError as e:
        logger.error(f"Error: {e}")
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

        if cli_split is not None:
            split_size = cli_split
        elif parser.has_option(section, 'split'):
            split_raw = parser.get(section, 'split').strip()
            if not split_raw or split_raw.lower() in ("none", "off"):
                split_size = None
            else:
                try:
                    split_size = parse_optional_size(split_raw, f"[{section}] split")
                except ConfigError as e:
                    logger.error(f"Error: {e}")
                    any_failed = True
                    continue
        else:
            split_size = global_split

        if parser.has_option(section, 'password'):
            section_password = parser.get(section, 'password').strip() or None
        else:
            section_password = _UNSET
        password = _resolve_password(cli_password, section_password, global_password)

        if parser.has_option(section, 'check_content_hash'):
            try:
                check_content_hash = _get_boolean(parser, section, 'check_content_hash', f"[{section}] check_content_hash")
            except ConfigError as e:
                logger.error(f"Error: {e}")
                any_failed = True
                continue
        else:
            check_content_hash = cli_check_content_hash or global_check_content_hash

        if execute_backup(sources, dest, name, split_size, password, check_content_hash=check_content_hash):
            any_ok = True
        else:
            any_failed = True

    if not any_ok:
        logger.error("Error: No backup jobs completed successfully.")
        return False

    return not any_failed


def _cli_log_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """Build CLI logging overrides. Raises ConfigError on invalid values."""
    overrides: dict[str, Any] = {}
    if args.log_file is not None:
        overrides["log_file"] = args.log_file
    if args.log_level is not None:
        overrides["log_level"] = parse_log_level(args.log_level, "--log-level")
    if args.log_max_size is not None:
        overrides["log_max_size"] = args.log_max_size
    if args.log_backup_count is not None:
        overrides["log_backup_count"] = parse_log_backup_count(args.log_backup_count, "--log-backup-count")
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

    # Скрываем окно консоли в Windows сразу, чтобы не мелькало
    if args.hide:
        if os.name == 'nt':
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)
        else:
            logger.info("--hide is supported only on Windows; ignored on this platform.")

    try:
        log_overrides = _cli_log_overrides(args)
    except ConfigError as e:
        logger.error(f"Error: {e}")
        sys.exit(1)

    if args.config:
        if not os.path.exists(args.config):
            logger.error(f"Error: Config file does not exist: {args.config}")
            sys.exit(1)
        if not run_from_config(
            args.config,
            log_overrides=log_overrides,
            cli_check_content_hash=args.check_content_hash,
            cli_password=args.password,
            cli_split=args.split,
        ):
            sys.exit(1)
        return

    try:
        setup_logging(
            log_file=log_overrides.get("log_file"),
            log_level=log_overrides.get("log_level"),
            log_max_size=log_overrides.get("log_max_size"),
            log_backup_count=log_overrides.get("log_backup_count"),
        )
    except ConfigError as e:
        logger.error(f"Error: {e}")
        sys.exit(1)

    if not args.sources or not args.dest or not args.name:
        parser.error("the following arguments are required: -s/--sources, -d/--dest, -n/--name (or use -c/--config)")

    if not execute_backup(args.sources, args.dest, args.name, args.split, _resolve_password(args.password), args.check_content_hash):
        sys.exit(1)


# Default console logging so library callers and tests see output without main().
logger.propagate = False
if not logger.handlers:
    logger.setLevel(DEFAULT_LOG_LEVEL)
    _add_console_handlers(logger, DEFAULT_LOG_LEVEL)


if __name__ == "__main__":
    main()
