from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("microbackup")

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PROBE_TIMEOUT_SEC = 15
_TERMINATE_WAIT_SEC = 5


class SevenZipError(RuntimeError):
    """Raised when the external 7z binary cannot be used or returns an error."""


@dataclass(frozen=True)
class SevenZipOptions:
    path: str
    extra_args: tuple[str, ...] = ()


def _unquote_path(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def resolve_binary(raw_path: str) -> str:
    """Resolve sevenzip_path to an executable. Bare names are looked up in PATH."""
    value = _unquote_path(raw_path)
    value = os.path.expanduser(value)
    if not value:
        raise SevenZipError("sevenzip_path is empty")

    if os.path.basename(value) == value:
        found = shutil.which(value)
        if found:
            return found
        raise SevenZipError(f"7z binary not found in PATH: {value}")

    path = Path(value)
    if path.is_file():
        return str(path.resolve())
    raise SevenZipError(f"7z binary not found: {value}")


def _stop_7z_process(proc: subprocess.Popen[bytes]) -> None:
    """Terminate, then kill, a 7z child so rollback does not race a live writer."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=_TERMINATE_WAIT_SEC)
        return
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        return
    try:
        proc.kill()
        proc.wait(timeout=_TERMINATE_WAIT_SEC)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _run_7z(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[bytes]:
    # Popen + explicit stop: subprocess.run leaves a CREATE_NO_WINDOW 7z
    # child writing after Ctrl+C (KeyboardInterrupt is raised only in Python).
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except KeyboardInterrupt:
        _stop_7z_process(proc)
        raise
    except subprocess.TimeoutExpired:
        _stop_7z_process(proc)
        try:
            proc.communicate()
        except OSError:
            pass
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _decode_output(data: bytes | None) -> str:
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")


def probe(binary: str) -> str:
    """Run the binary with no arguments and return the first banner line."""
    try:
        result = _run_7z([binary], timeout=_PROBE_TIMEOUT_SEC)
    except OSError as e:
        raise SevenZipError(f"Could not start 7z binary {binary}: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise SevenZipError(f"Timed out probing 7z binary {binary}") from e

    text = _decode_output(result.stdout) + _decode_output(result.stderr)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    if result.returncode not in (0, 1):
        raise SevenZipError(
            f"7z binary {binary} exited with code {result.returncode} during probe"
        )
    return binary


def build_command(
    binary: str,
    archive_path: str,
    sources: list[str],
    split_size: int | None = None,
    password: str | None = None,
    level: int | None = None,
    extra_args: tuple[str, ...] = (),
    exclude_listfile: str | None = None,
) -> list[str]:
    cmd: list[str] = [
        binary,
        "a",
        "-t7z",
        "-y",
        "-bd",
        "-ssw",
        "-scsUTF-8",
    ]
    if level is not None:
        cmd.append(f"-mx={level}")
    if split_size:
        cmd.append(f"-v{split_size}b")
    if password:
        cmd.append(f"-p{password}")
        cmd.append("-mhe=on")
    if extra_args:
        cmd.extend(extra_args)
    if exclude_listfile:
        cmd.append(f"-xr@{exclude_listfile}")
    cmd.append("--")
    cmd.append(archive_path)
    cmd.extend(sources)
    return cmd


def _write_exclude_listfile(excluded: list[str], handle) -> None:
    """Write archive-relative exclude paths; directories also get a '*' child mask."""
    for item in excluded:
        converted = item.replace("/", os.sep).replace("\\", os.sep)
        if not converted:
            continue
        handle.write(converted + "\n")
        if converted.endswith(os.sep):
            handle.write(converted + "*" + "\n")
        else:
            handle.write(converted + os.sep + "\n")
            handle.write(converted + os.sep + "*" + "\n")


def create_archive(
    options: SevenZipOptions,
    archive_path: str | Path,
    sources: list[str],
    split_size: int | None = None,
    password: str | None = None,
    level: int | None = None,
    excluded: list[str] | None = None,
) -> None:
    binary = resolve_binary(options.path)
    banner = probe(binary)
    logger.info(f"Using external 7z: {binary} ({banner})")

    abs_sources = [str(Path(src).resolve()) for src in sources]
    archive_str = str(archive_path)

    listfile_path: str | None = None
    try:
        if excluded:
            fd, listfile_path = tempfile.mkstemp(prefix="microbackup_7z_x_", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                _write_exclude_listfile(excluded, handle)

        cmd = build_command(
            binary,
            archive_str,
            abs_sources,
            split_size=split_size,
            password=password,
            level=level,
            extra_args=options.extra_args,
            exclude_listfile=listfile_path,
        )
        try:
            result = _run_7z(cmd)
        except OSError as e:
            raise SevenZipError(f"Could not start 7z binary {binary}: {e}") from e

        stdout = _decode_output(result.stdout)
        stderr = _decode_output(result.stderr)
        if stdout.strip():
            logger.debug(stdout.rstrip())

        if result.returncode == 0:
            return

        details = stderr.strip() or stdout.strip() or f"exit code {result.returncode}"
        raise SevenZipError(f"External 7z failed (code {result.returncode}): {details}")
    finally:
        if listfile_path:
            try:
                os.unlink(listfile_path)
            except OSError:
                pass
