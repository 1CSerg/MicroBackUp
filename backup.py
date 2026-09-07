from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path

import multivolumefile
import py7zr
from pathspec import GitIgnoreSpec

from sevenzip import SevenZipError, SevenZipOptions, create_archive as sevenzip_create_archive

logger = logging.getLogger("microbackup")

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB chunk size for hashing

_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _validate_archive_name(archive_name: str) -> str | None:
    """Return an error message if archive_name is unsafe, else None."""
    if not archive_name or archive_name in (".", ".."):
        return f"Archive name is empty or reserved: {archive_name!r}"
    # Reject path separators and traversal segments.
    if "\\" in archive_name or "/" in archive_name:
        return f"Archive name must not contain path separators: {archive_name!r}"
    # Reject Windows-invalid filename characters and non-printable control characters.
    if re.search(r'[\x00-\x1f<>:"|?*]', archive_name):
        return f"Archive name contains forbidden characters: {archive_name!r}"
    # Windows silently strips trailing dots and spaces; reject them so the
    # on-disk name matches what the user asked for.
    if archive_name != archive_name.rstrip(". "):
        return f"Archive name must not end with dots or spaces: {archive_name!r}"
    # Reject Windows-reserved device names (case-insensitive, with or without
    # extension).
    stem = archive_name.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        return f"Archive name is a reserved device name: {archive_name!r}"
    return None


class ExcludeError(ValueError):
    """Raised when an exclude pattern is invalid."""


class ExcludeSpec:
    """Compiled gitignore-style exclude rules matched against source-relative paths."""

    def __init__(self, patterns: list[str], case_sensitive: bool | None = None) -> None:
        if case_sensitive is None:
            case_sensitive = os.name != "nt"
        self.case_sensitive = case_sensitive
        self.excluded_count = 0
        try:
            self._spec = GitIgnoreSpec.from_lines(patterns)
        except ValueError as e:
            raise ExcludeError(_invalid_exclude_message(patterns, e)) from e
        if not case_sensitive:
            self._apply_ignorecase()

    def _apply_ignorecase(self) -> None:
        """Best-effort: pathspec has no public ignore-case API, so retarget regex flags.

        Falls back to case-sensitive matching if internals change.
        """
        originals: list[tuple[object, object]] = []
        try:
            for pattern in self._spec.patterns:
                regex = getattr(pattern, "regex", None)
                if regex is not None:
                    originals.append((pattern, regex))
                    pattern.regex = re.compile(regex.pattern, regex.flags | re.IGNORECASE)
        except Exception:
            for pattern, regex in originals:
                pattern.regex = regex
            logger.warning(
                "Could not enable case-insensitive exclude matching; "
                "patterns will be matched case-sensitively."
            )

    def excludes(self, rel_posix: str, is_dir: bool) -> bool:
        # pathspec requires a trailing slash for directory-only patterns like 'build/'.
        candidate = rel_posix + "/" if is_dir else rel_posix
        matched = bool(self._spec.match_file(candidate))
        if matched:
            self.excluded_count += 1
        return matched


def _invalid_exclude_message(patterns: list[str], error: BaseException) -> str:
    for pattern in patterns:
        try:
            GitIgnoreSpec.from_lines([pattern])
        except ValueError:
            return f"Invalid exclude pattern: {pattern!r}"
    return f"Invalid exclude pattern: {error}"


def build_exclude_spec(
    patterns: list[str] | None,
    case_sensitive: bool | None = None,
) -> ExcludeSpec | None:
    """Compile gitignore-style patterns. None if the list is empty."""
    if not patterns:
        return None
    return ExcludeSpec(patterns, case_sensitive=case_sensitive)


def _arcname_key(name: str) -> str:
    """Compare archive root names the way the local filesystem does."""
    return name.lower() if os.name == "nt" else name


def _source_root_name(src_path: Path) -> str:
    """Archive root for a source. Drive/filesystem roots have an empty Path.name."""
    name = src_path.name
    if name:
        return name
    drive = src_path.drive.replace(":", "").replace("\\", "").replace("/", "")
    if drive:
        return f"{drive}_drive"
    return "root"


# FILE_ATTRIBUTE_REPARSE_POINT — junctions and other reparse dirs on Windows.
# os.walk(followlinks=False) skips symlinks but still descends into junctions.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_windows_junction(path: Path) -> bool:
    """True for a Windows directory junction (os.walk follows these)."""
    if os.name != "nt":
        return False
    try:
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction):
            return bool(is_junction())
        st = os.lstat(path)
        attrs = getattr(st, "st_file_attributes", 0)
        return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT) and not path.is_symlink()
    except OSError:
        return False


def _raise_walk_error(error: OSError) -> None:
    """os.walk onerror: log listing failures instead of treating them as empty."""
    logger.error(f"Error: Could not list directory {error.filename}: {error}")
    raise error


def _dedupe_sources(sources: list[str]) -> list[str]:
    """Drop later sources that resolve to a path already listed."""
    seen: set[str] = set()
    unique: list[str] = []
    for src in sources:
        key = str(Path(src).resolve())
        if os.name == "nt":
            key = key.lower()
        if key in seen:
            logger.warning(f"Duplicate source path skipped: {src}")
            continue
        seen.add(key)
        unique.append(src)
    return unique


def _unique_root_arcname(name: str, seen: set[str], src_path: Path) -> str:
    """Return a unique archive root name, matching historical create_archive renaming."""
    if not name:
        raise ValueError(
            f"Source path has no archive root name (drive or filesystem root?): {src_path}"
        )
    arcname = name
    seen_keys = {_arcname_key(existing) for existing in seen}
    if _arcname_key(arcname) in seen_keys:
        logger.warning(f"Duplicate archive name detected: '{arcname}' for path '{src_path}'.")
        original_arcname = arcname
        stem, dot, suffix = original_arcname.partition(".")
        counter = 1
        while _arcname_key(arcname) in seen_keys:
            if dot:
                arcname = f"{stem}_{counter}.{suffix}"
            else:
                arcname = f"{original_arcname}_{counter}"
            counter += 1
        logger.warning(
            f"Renamed '{original_arcname}' to '{arcname}' in the archive to prevent collision."
        )
    seen.add(arcname)
    return arcname


# PBKDF2-HMAC-SHA256 iterations for the password verifier stored in dest.
# The hash file often lives next to the archive in a cloud folder; a fast SHA-256
# digest would be weaker than attacking the 7z file itself.
_PASSWORD_KDF = "pbkdf2-sha256"
_PBKDF2_ITERATIONS = 600_000
# Reject huge iteration counts from a tampered hash file (would stall the process).
_PBKDF2_MAX_ITERATIONS = max(2_000_000, _PBKDF2_ITERATIONS)


def source_containing_dest(dest: str, sources: list[str]) -> str | None:
    """Return the source that contains dest (or dest itself), else None."""
    dest_path = Path(dest).resolve()
    for src in sources:
        src_path = Path(src).resolve()
        if dest_path == src_path:
            return src
        if not src_path.is_dir():
            continue
        try:
            dest_path.relative_to(src_path)
        except ValueError:
            continue
        return src
    return None


def _legacy_password_hash(password: str, salt: str) -> str:
    """Previous SHA-256(salt:password) verifier; kept to read old hash files."""
    return hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()


def _pbkdf2_password_hash(password: str, salt: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        iterations,
    ).hex()


def _hashes_equal(left: str, right: str) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def _password_matches(password: str, old_info: dict) -> bool:
    """True if password matches the verifier in a previous hash file."""
    salt = old_info.get("password_salt")
    stored = old_info.get("password_hash")
    if not isinstance(salt, str) or not isinstance(stored, str) or not salt or not stored:
        return False
    kdf = old_info.get("password_kdf")
    if kdf == _PASSWORD_KDF:
        iterations = old_info.get("password_iterations", _PBKDF2_ITERATIONS)
        if (
            not isinstance(iterations, int)
            or isinstance(iterations, bool)
            or iterations < 1
            or iterations > _PBKDF2_MAX_ITERATIONS
        ):
            return False
        try:
            computed = _pbkdf2_password_hash(password, salt, iterations)
        except ValueError:
            return False
        return _hashes_equal(computed, stored)
    if kdf is not None:
        return False
    return _hashes_equal(_legacy_password_hash(password, salt), stored)


def _new_password_verifier(password: str) -> dict[str, str | int]:
    salt = secrets.token_hex(16)
    return {
        "password_salt": salt,
        "password_hash": _pbkdf2_password_hash(password, salt, _PBKDF2_ITERATIONS),
        "password_kdf": _PASSWORD_KDF,
        "password_iterations": _PBKDF2_ITERATIONS,
    }


def get_all_paths(
    sources: list[str],
    exclude_spec: ExcludeSpec | None = None,
    excluded_out: list[str] | None = None,
) -> list[tuple[str, str, str]]:
    """
    Returns a list of all files and directories in the given sources.
    Each item is a tuple (absolute_path, relative_path, kind) where kind is
    'file' or 'dir'. relative_path is the posix archive path, unique across
    sources: colliding source roots are renamed (file.txt -> file_1.txt).

    exclude_spec matches paths relative to each source root (not including
    the source root name). The source root itself is never excluded.
    Excluded directories are pruned from os.walk, so their children are
    not visited (gitignore semantics: a negation cannot revive files under
    an excluded directory).
    """
    all_paths: list[tuple[str, str, str]] = []
    seen_arcnames: set[str] = set()
    sources = _dedupe_sources(sources)

    for src in sources:
        src_path = Path(src).resolve()

        if src_path.is_file():
            root_arc = _unique_root_arcname(_source_root_name(src_path), seen_arcnames, src_path)
            all_paths.append((str(src_path), root_arc, "file"))
        elif src_path.is_dir():
            root_arc = _unique_root_arcname(_source_root_name(src_path), seen_arcnames, src_path)
            all_paths.append((str(src_path), root_arc, "dir"))

            for root, dirs, files in os.walk(src_path, onerror=_raise_walk_error):
                kept_dirs: list[str] = []
                for d in dirs:
                    d_path = Path(root) / d
                    if _is_windows_junction(d_path):
                        logger.warning(f"Skipping Windows junction: {d_path}")
                        # External 7z walks source trees itself and treats
                        # junctions as normal directories; list them in
                        # excluded_out so -xr@ skips the junction target.
                        if excluded_out is not None:
                            match_rel = d_path.relative_to(src_path).as_posix()
                            excluded_out.append(f"{root_arc}/{match_rel}")
                        continue
                    match_rel = d_path.relative_to(src_path).as_posix()
                    if exclude_spec is not None and exclude_spec.excludes(match_rel, is_dir=True):
                        logger.debug(f"Excluded directory: {d_path}")
                        if excluded_out is not None:
                            excluded_out.append(f"{root_arc}/{match_rel}")
                        continue
                    kept_dirs.append(d)
                    all_paths.append((str(d_path), f"{root_arc}/{match_rel}", "dir"))
                dirs[:] = kept_dirs

                for f in files:
                    f_path = Path(root) / f
                    match_rel = f_path.relative_to(src_path).as_posix()
                    if exclude_spec is not None and exclude_spec.excludes(match_rel, is_dir=False):
                        logger.debug(f"Excluded file: {f_path}")
                        if excluded_out is not None:
                            excluded_out.append(f"{root_arc}/{match_rel}")
                        continue
                    all_paths.append((str(f_path), f"{root_arc}/{match_rel}", "file"))
        else:
            raise FileNotFoundError(f"Source path does not exist: {src}")

    return all_paths

def count_items(paths: list[tuple[str, str, str]]) -> tuple[int, int]:
    files_count = sum(1 for p in paths if p[2] == 'file')
    dirs_count = sum(1 for p in paths if p[2] == 'dir')
    return files_count, dirs_count

def compute_names_hash(paths: list[tuple[str, str, str]]) -> str:
    # Sort relative paths to ensure consistent hashing.
    # Normalize separators to '/' so hashes stay stable across platforms.
    rel_paths = sorted(p[1].replace('\\', '/') for p in paths)
    hasher = hashlib.sha256()
    for p in rel_paths:
        hasher.update(p.encode('utf-8'))
    return hasher.hexdigest()

def compute_file_hash(filepath: str) -> str:
    hasher = hashlib.sha256()
    try:
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
                hasher.update(chunk)
    except OSError as e:
        logger.error(f"Error: Could not read file {filepath} for hashing: {e}")
        return f"ERROR:{filepath}"
    return hasher.hexdigest()

def compute_content_hash(paths: list[tuple[str, str, str]]) -> tuple[str | None, bool]:
    # Sort relative paths to ensure consistent hashing order
    file_paths = sorted([p for p in paths if p[2] == 'file'], key=lambda x: x[1])

    hasher = hashlib.sha256()
    had_error = False
    for abs_path, rel_path, _ in file_paths:
        f_hash = compute_file_hash(abs_path)
        if f_hash.startswith("ERROR:"):
            had_error = True
        norm_rel = rel_path.replace('\\', '/')
        hasher.update(f"{norm_rel}:{f_hash}".encode())
    # On any read error the digest is not trustworthy as a content fingerprint:
    # return None so run_backup forces a full backup and doesn't persist a
    # hash that mixes error markers with real data.
    if had_error:
        return None, True
    return hasher.hexdigest(), False

def compute_metadata_hash(paths: list[tuple[str, str, str]]) -> tuple[str | None, bool]:
    # Sort relative paths to ensure consistent hashing order
    file_paths = sorted([p for p in paths if p[2] == 'file'], key=lambda x: x[1])

    hasher = hashlib.sha256()
    had_error = False
    for abs_path, rel_path, _ in file_paths:
        try:
            stat = os.stat(abs_path)
            # Normalize separators so metadata hash is stable across platforms.
            norm_rel = rel_path.replace('\\', '/')
            meta_str = f"{norm_rel}:{stat.st_size}:{stat.st_mtime}"
            hasher.update(meta_str.encode('utf-8'))
        except OSError as e:
            logger.error(f"Error: Could not read metadata for {abs_path}: {e}")
            had_error = True
    if had_error:
        return None, True
    return hasher.hexdigest(), False

def _write_paths(archive: py7zr.SevenZipFile, paths: list[tuple[str, str, str]]) -> None:
    for abs_path, rel_path, _kind in paths:
        archive.write(abs_path, rel_path)


def _build_filters(level: int | None, password: str | None) -> list[dict] | None:
    """Build a py7zr filter chain for compression_level. None keeps engine defaults."""
    if level is None:
        return None
    if level == 0:
        filters: list[dict] = [{"id": py7zr.FILTER_COPY}]
    else:
        filters = [{"id": py7zr.FILTER_LZMA2, "preset": level}]
    if password:
        filters.append({"id": py7zr.FILTER_CRYPTO_AES256_SHA256})
    return filters


def _create_with_py7zr(
    archive_path: Path,
    paths: list[tuple[str, str, str]],
    split_size: int | None,
    password: str | None,
    compression_level: int | None,
) -> None:
    filters = _build_filters(compression_level, password)
    kwargs: dict = {
        "password": password,
        "header_encryption": bool(password),
    }
    if filters is not None:
        kwargs["filters"] = filters

    if split_size:
        logger.info(f"Splitting archive into volumes of size {split_size} bytes")
        with (
            multivolumefile.open(archive_path, mode="wb", volume=split_size) as target_archive,
            py7zr.SevenZipFile(target_archive, "w", **kwargs) as archive,
        ):
            _write_paths(archive, paths)
    else:
        with py7zr.SevenZipFile(archive_path, "w", **kwargs) as archive:
            _write_paths(archive, paths)


def _remove_matching_archive_files(dest_path: Path, pattern: re.Pattern[str]) -> None:
    if not dest_path.exists():
        return
    for p in dest_path.iterdir():
        if p.is_file() and pattern.match(p.name):
            try:
                p.unlink()
            except OSError as e:
                logger.warning(f"Could not remove partial archive file {p}: {e}")


def _collect_created_volumes(dest_path: Path, archive_name: str) -> list[str]:
    single_file = dest_path / f"{archive_name}.7z"
    if single_file.is_file():
        return [single_file.name]

    vol_pattern = re.compile(rf"^{re.escape(archive_name)}\.7z\.(\d+)$")
    created = [p.name for p in dest_path.iterdir() if p.is_file() and vol_pattern.match(p.name)]
    return sorted(created, key=lambda n: int(vol_pattern.match(n).group(1)))  # type: ignore[union-attr]


def _sevenzip_skip_reason(sources: list[str], excluded: list[str] | None) -> str | None:
    seen: set[str] = set()
    for src in sources:
        src_path = Path(src).resolve()
        name = src_path.name
        root_name = _source_root_name(src_path)
        # py7zr stores a synthesized root (C_drive / root) when Path.name is empty;
        # external 7z would store the raw drive/UNC path instead.
        if root_name != name:
            return f"synthesized archive root {root_name!r}"
        key = root_name.lower() if os.name == "nt" else root_name
        if key in seen:
            return f"duplicate source root name {root_name!r}"
        seen.add(key)
    for item in excluded or []:
        if "*" in item or "?" in item:
            return f"excluded path contains wildcards: {item!r}"
    return None


def create_archive(
    sources: list[str],
    dest: str,
    archive_name: str,
    split_size: int | None,
    password: str | None,
    paths: list[tuple[str, str, str]] | None = None,
    sevenzip: SevenZipOptions | None = None,
    excluded: list[str] | None = None,
    compression_level: int | None = None,
) -> list[str]:
    name_error = _validate_archive_name(archive_name)
    if name_error:
        raise ValueError(f"Invalid archive_name: {name_error}")

    sources = _dedupe_sources(sources)
    dest_path = Path(dest)
    if dest_path.is_file():
        raise ValueError(f"Destination path is an existing file, not a directory: {dest}")
    containing = source_containing_dest(dest, sources)
    if containing is not None:
        raise ValueError(
            f"Destination path is inside a source path: {dest} is under {containing}"
        )
    dest_path.mkdir(parents=True, exist_ok=True)
    archive_path = dest_path / f"{archive_name}.7z"
    if paths is None:
        collected_excluded: list[str] = []
        paths = get_all_paths(sources, excluded_out=collected_excluded)
        if excluded is None:
            excluded = collected_excluded
        elif collected_excluded:
            excluded = list(excluded) + collected_excluded

    logger.info(f"Creating archive: {archive_path}")

    pattern = re.compile(rf"^{re.escape(archive_name)}\.7z(\.\d+)?$")
    old_files = [p for p in dest_path.iterdir() if p.is_file() and pattern.match(p.name)]

    temp_dir = None
    old_files_moved = False
    committed = False
    try:
        if old_files:
            temp_dir = tempfile.mkdtemp(prefix=".microbackup_tmp_", dir=dest)
            logger.info(f"Moving {len(old_files)} old archive files to temporary directory")
            for p in old_files:
                shutil.move(str(p), str(Path(temp_dir) / p.name))
        old_files_moved = True

        used_external = False
        if sevenzip is not None:
            skip_reason = _sevenzip_skip_reason(sources, excluded)
            if skip_reason:
                logger.warning(
                    f"External 7z cannot be used ({skip_reason}); falling back to built-in py7zr."
                )
            else:
                try:
                    sevenzip_create_archive(
                        sevenzip,
                        archive_path,
                        sources,
                        split_size=split_size,
                        password=password,
                        level=compression_level,
                        excluded=excluded,
                    )
                    used_external = True
                except SevenZipError as e:
                    logger.warning(f"External 7z failed: {e}. Falling back to built-in py7zr.")
                    _remove_matching_archive_files(dest_path, pattern)

        if not used_external:
            _create_with_py7zr(archive_path, paths, split_size, password, compression_level)

        # Archive bytes are on disk. Do not delete them if cleanup/collect is interrupted.
        committed = True

        if temp_dir:
            logger.info("Removing old archive files")
            try:
                shutil.rmtree(temp_dir)
            except OSError as e:
                logger.warning(f"Could not remove temporary directory {temp_dir}: {e}")
                # Best-effort fallback so leftover temp dirs don't pollute dest.
                shutil.rmtree(temp_dir, ignore_errors=True)

        logger.info("Archive created successfully.")
        return _collect_created_volumes(dest_path, archive_name)
    except BaseException:
        # KeyboardInterrupt/SIGINT is a BaseException: still restore old volumes
        # if the new archive never finished. After commit, keep the new files
        # even if temp cleanup or volume listing is interrupted (rmtree of a
        # cloud-synced dest can be slow; rolling back then can wipe both).
        if not committed:
            if old_files_moved:
                _remove_matching_archive_files(dest_path, pattern)

            if temp_dir:
                logger.info("Archive creation failed, restoring old archive files")
                try:
                    for p in Path(temp_dir).iterdir():
                        try:
                            shutil.move(str(p), str(dest_path / p.name))
                        except OSError as e:
                            logger.warning(f"Could not restore old archive file {p}: {e}")
                    try:
                        Path(temp_dir).rmdir()
                    except OSError:
                        pass
                except OSError as e:
                    logger.warning(f"Could not restore old archive files from {temp_dir}: {e}")
        raise


def _check_archive_exists(dest_path: Path, archive_name: str, old_info: dict) -> bool:
    """Check that all required archive volumes exist on disk."""
    if "volumes" in old_info:
        volumes = old_info["volumes"]
        if not isinstance(volumes, list) or not volumes:
            return False
        for vol in volumes:
            if not isinstance(vol, str) or not vol or Path(vol).name != vol:
                return False
        return all((dest_path / vol).is_file() for vol in volumes)

    # Legacy fallback for info files created without 'volumes'
    old_split = old_info.get("split_size")
    if old_split is None:
        return (dest_path / f"{archive_name}.7z").is_file()

    pattern = re.compile(rf"^{re.escape(archive_name)}\.7z\.(\d+)$")
    parts = []
    for p in dest_path.iterdir():
        if p.is_file():
            m = pattern.match(p.name)
            if m:
                parts.append(int(m.group(1)))
    if not parts:
        return False
    parts.sort()
    return parts[0] == 1 and parts == list(range(1, len(parts) + 1))


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON to path atomically via a temp file + os.replace."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def run_backup(sources: list[str], dest: str, archive_name: str, split_size: int | None = None, password: str | None = None, check_content_hash: bool = False, exclude: list[str] | None = None, sevenzip: SevenZipOptions | None = None, compression_level: int | None = None) -> None:
    name_error = _validate_archive_name(archive_name)
    if name_error:
        raise ValueError(f"Invalid archive_name: {name_error}")

    sources = _dedupe_sources(sources)
    dest_path = Path(dest)
    if dest_path.is_file():
        raise ValueError(f"Destination path is an existing file, not a directory: {dest}")
    containing = source_containing_dest(dest, sources)
    if containing is not None:
        raise ValueError(
            f"Destination path is inside a source path: {dest} is under {containing}"
        )

    info_file = dest_path / f"{archive_name}_hash.json"

    logger.info("Gathering file list...")
    # Changing exclude patterns that still produce the same file set will not
    # force a rebuild (counts/names_hash stay the same). No exclude_hash is
    # stored in *_hash.json.
    exclude_spec = build_exclude_spec(exclude)
    excluded_out: list[str] = []
    all_paths = get_all_paths(sources, exclude_spec, excluded_out=excluded_out)
    files_count, dirs_count = count_items(all_paths)
    excluded_count = exclude_spec.excluded_count if exclude_spec is not None else 0

    logger.info(f"Found {files_count} files and {dirs_count} directories.")
    if excluded_count:
        logger.info(f"Excluded {excluded_count} items by exclude patterns")

    if files_count == 0 and excluded_count:
        raise ValueError("All files are excluded by exclude patterns; nothing to back up")
    
    need_backup = True
    names_hash = None
    metadata_hash = None
    content_hash = None
    old_info = None

    if info_file.exists():
        try:
            with open(info_file, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("info file must contain a JSON object")
            old_info = loaded
        except (OSError, ValueError) as e:
            logger.error(f"Error reading info file {info_file}: {e}. Will perform full backup.")
            old_info = None

        if old_info is not None:
            if not _check_archive_exists(dest_path, archive_name, old_info):
                logger.info("Archive file(s) not found on disk. Will perform full backup.")
            else:
                logger.info("Checking state against previous backup...")

                # Check split format change first: if the requested split_size
                # differs from the previous run, the archive layout changes and
                # we must rebuild regardless of content hashes.
                old_split = old_info.get('split_size')
                old_has_password = old_info.get('has_password')
                curr_has_password = bool(password)

                if old_split != split_size:
                    logger.info(
                        f"Split size changed (was {old_split}, now {split_size}). "
                        f"Will perform full backup."
                    )
                elif old_has_password is not None and old_has_password != curr_has_password:
                    logger.info("Password protection setting changed. Will perform full backup.")
                elif old_has_password is None and curr_has_password:
                    logger.info("Password protection added to legacy backup. Will perform full backup.")
                elif curr_has_password and not _password_matches(password, old_info):
                    logger.info("Password changed. Will perform full backup.")
                else:
                    # Step 1: Check counts
                    if old_info.get('files_count') == files_count and old_info.get('dirs_count') == dirs_count:
                        logger.info("Counts match. Checking names hash...")
                        # Step 2: Check names hash
                        names_hash = compute_names_hash(all_paths)
                        if old_info.get('names_hash') == names_hash:
                            logger.info("Names hash matches. Checking metadata hash...")
                            # Step 3: Check metadata hash
                            metadata_hash, meta_error = compute_metadata_hash(all_paths)
                            if meta_error:
                                logger.info("Metadata read error detected. Will perform full backup.")
                            elif old_info.get('metadata_hash') == metadata_hash:
                                logger.info("Metadata hash matches.")
                                # Step 4: Check content hash if requested
                                if check_content_hash:
                                    logger.info("Checking content hash...")
                                    content_hash, content_error = compute_content_hash(all_paths)
                                    if content_error:
                                        logger.info("Content read error detected. Will perform full backup.")
                                    elif old_info.get('content_hash') == content_hash:
                                        logger.info("Content hash matches. No backup needed.")
                                        need_backup = False
                                    else:
                                        logger.info("Content hash differs.")
                                else:
                                    logger.info("Content hash check skipped. No backup needed.")
                                    need_backup = False
                            else:
                                logger.info("Metadata hash differs.")
                        else:
                            logger.info("Names hash differs.")
                    else:
                        logger.info("Counts differ.")
    else:
        logger.info("No previous backup info found. Will perform full backup.")

    if need_backup:
        # Compute hashes if not already computed during checks
        meta_error = False
        content_error = False

        if names_hash is None:
            logger.info("Computing names hash...")
            names_hash = compute_names_hash(all_paths)
        if metadata_hash is None:
            logger.info("Computing metadata hash...")
            metadata_hash, meta_error = compute_metadata_hash(all_paths)
        if check_content_hash and content_hash is None:
            logger.info("Computing content hash...")
            content_hash, content_error = compute_content_hash(all_paths)

        created_volumes = create_archive(
            sources,
            dest,
            archive_name,
            split_size,
            password,
            paths=all_paths,
            sevenzip=sevenzip,
            excluded=excluded_out,
            compression_level=compression_level,
        )

        if meta_error or metadata_hash is None or (check_content_hash and (content_error or content_hash is None)):
            logger.error("Error computing hashes due to read/stat failure. Skipping hash file creation.")
            if info_file.exists():
                try:
                    info_file.unlink()
                except OSError:
                    pass
        else:
            now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
            has_password = bool(password)

            new_info = {
                'files_count': files_count,
                'dirs_count': dirs_count,
                'names_hash': names_hash,
                'metadata_hash': metadata_hash,
                'split_size': split_size,
                'volumes': created_volumes,
                'has_password': has_password,
                'last_check_date': now_str,
                'last_update_date': now_str
            }
            if has_password and password:
                new_info.update(_new_password_verifier(password))
            if check_content_hash:
                new_info['content_hash'] = content_hash

            _atomic_write_json(info_file, new_info)
            logger.info(f"Updated info file: {info_file}")

    else:
        # Just update the check date. Also upgrade a legacy SHA-256 verifier so
        # dest does not keep a fast password hash next to the archive.
        old_info["last_check_date"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if password and old_info.get("password_kdf") != _PASSWORD_KDF:
            old_info.update(_new_password_verifier(password))
        _atomic_write_json(info_file, old_info)
        logger.info(f"Updated check date in info file: {info_file}")
