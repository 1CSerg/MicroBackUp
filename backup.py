from __future__ import annotations

import os
import hashlib
import json
import datetime
import logging
import py7zr
import multivolumefile
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger("microbackup")

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB chunk size for hashing

def get_all_paths(sources: list[str]) -> list[tuple[str, str, str]]:
    """
    Returns a list of all files and directories in the given sources.
    Each item is a tuple (absolute_path, relative_path, kind) where kind is
    'file' or 'dir'. relative_path is guaranteed unique across all sources so
    that hashes distinguish items from different sources even when basenames
    collide (e.g. two file sources named "file.txt" in different directories).
    """
    all_paths = []
    seen_rel: set[str] = set()

    def _add(abs_path: Path, rel_path: str, kind: str) -> None:
        unique_rel = rel_path
        counter = 1
        while unique_rel in seen_rel:
            counter += 1
            unique_rel = f"{rel_path}_{counter}"
        seen_rel.add(unique_rel)
        all_paths.append((str(abs_path), unique_rel, kind))

    for src in sources:
        src_path = Path(src).resolve()
        parent_dir = src_path.parent

        if src_path.is_file():
            rel_path = src_path.relative_to(parent_dir)
            _add(src_path, str(rel_path), 'file')
        elif src_path.is_dir():
            rel_path = src_path.relative_to(parent_dir)
            _add(src_path, str(rel_path), 'dir')

            for root, dirs, files in os.walk(src_path):
                for d in dirs:
                    d_path = Path(root) / d
                    d_rel = d_path.relative_to(parent_dir)
                    _add(d_path, str(d_rel), 'dir')
                for f in files:
                    f_path = Path(root) / f
                    f_rel = f_path.relative_to(parent_dir)
                    _add(f_path, str(f_rel), 'file')
        else:
            logger.warning(f"Source path does not exist, skipping: {src}")

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

def compute_content_hash(paths: list[tuple[str, str, str]]) -> tuple[Optional[str], bool]:
    # Sort relative paths to ensure consistent hashing order
    file_paths = sorted([p for p in paths if p[2] == 'file'], key=lambda x: x[1])

    hasher = hashlib.sha256()
    had_error = False
    for abs_path, rel_path, _ in file_paths:
        f_hash = compute_file_hash(abs_path)
        if f_hash.startswith("ERROR:"):
            had_error = True
        hasher.update(f_hash.encode('utf-8'))
    # On any read error the digest is not trustworthy as a content fingerprint:
    # return None so run_backup forces a full backup and doesn't persist a
    # hash that mixes error markers with real data.
    if had_error:
        return None, True
    return hasher.hexdigest(), False

def compute_metadata_hash(paths: list[tuple[str, str, str]]) -> tuple[Optional[str], bool]:
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

def create_archive(sources: list[str], dest: str, archive_name: str, split_size: Optional[int], password: Optional[str]) -> None:
    dest_path = Path(dest)
    archive_path = dest_path / f"{archive_name}.7z"
    
    logger.info(f"Creating archive: {archive_path}")

    seen_arcnames = set()

    def add_to_archive(archive, src_path):
        arcname = src_path.name
        if arcname in seen_arcnames:
            logger.error(f"Duplicate archive name detected: '{arcname}' for path '{src_path}'.")
            original_arcname = arcname
            stem, dot, suffix = original_arcname.partition(".")
            counter = 1
            while arcname in seen_arcnames:
                if dot:
                    arcname = f"{stem}_{counter}.{suffix}"
                else:
                    arcname = f"{original_arcname}_{counter}"
                counter += 1
            logger.warning(f"Renamed '{original_arcname}' to '{arcname}' in the archive to prevent collision.")
        
        seen_arcnames.add(arcname)
        
        if src_path.is_file():
            archive.write(src_path, arcname)
        else:
            archive.writeall(src_path, arcname)

    pattern = re.compile(rf"^{re.escape(archive_name)}\.7z(\.\d+)?$")
    old_files = [p for p in dest_path.iterdir() if p.is_file() and pattern.match(p.name)]

    temp_dir = None
    if old_files:
        temp_dir = tempfile.mkdtemp(prefix=".microbackup_tmp_", dir=dest)
        logger.info(f"Moving {len(old_files)} old archive files to temporary directory")
        for p in old_files:
            shutil.move(str(p), str(Path(temp_dir) / p.name))

    try:
        if split_size:
            logger.info(f"Splitting archive into volumes of size {split_size} bytes")
            with multivolumefile.open(archive_path, mode='wb', volume=split_size) as target_archive:
                with py7zr.SevenZipFile(target_archive, 'w', password=password, header_encryption=bool(password)) as archive:
                    for src in sources:
                        src_path = Path(src).resolve()
                        add_to_archive(archive, src_path)
        else:
            with py7zr.SevenZipFile(archive_path, 'w', password=password, header_encryption=bool(password)) as archive:
                for src in sources:
                    src_path = Path(src).resolve()
                    add_to_archive(archive, src_path)
                        
        if temp_dir:
            logger.info("Removing old archive files")
            try:
                shutil.rmtree(temp_dir)
            except OSError as e:
                logger.warning(f"Could not remove temporary directory {temp_dir}: {e}")
                # Best-effort fallback so leftover temp dirs don't pollute dest.
                shutil.rmtree(temp_dir, ignore_errors=True)
            
        logger.info("Archive created successfully.")
    except Exception:
        if temp_dir:
            logger.info("Archive creation failed, restoring old archive files")
            # Remove any partial new archive files (including extra split volumes
            # that the failed run may have created beyond what the old set had).
            for p in dest_path.iterdir():
                if p.is_file() and pattern.match(p.name):
                    try:
                        p.unlink()
                    except OSError as e:
                        logger.warning(f"Could not remove partial archive file {p}: {e}")
            for p in Path(temp_dir).iterdir():
                shutil.move(str(p), str(dest_path / p.name))
            try:
                Path(temp_dir).rmdir()
            except OSError:
                pass
        raise

def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON to path atomically via a temp file + os.replace."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)
    os.replace(tmp_path, path)


def run_backup(sources: list[str], dest: str, archive_name: str, split_size: Optional[int] = None, password: Optional[str] = None, check_content_hash: bool = False) -> None:
    info_file = Path(dest) / f"{archive_name}_hash.json"
    
    logger.info("Gathering file list...")
    all_paths = get_all_paths(sources)
    files_count, dirs_count = count_items(all_paths)
    
    logger.info(f"Found {files_count} files and {dirs_count} directories.")
    
    need_backup = True
    names_hash = None
    metadata_hash = None
    content_hash = None
    
    if info_file.exists():
        archive_path = Path(dest) / f"{archive_name}.7z"
        archive_exists = archive_path.exists()
        if not archive_exists:
            pattern = re.compile(rf"^{re.escape(archive_name)}\.7z\.\d+$")
            archive_exists = any(p.is_file() and pattern.match(p.name) for p in Path(dest).iterdir())
            
        if not archive_exists:
            logger.info("Archive file(s) not found on disk. Will perform full backup.")
        else:
            try:
                with open(info_file, 'r', encoding='utf-8') as f:
                    old_info = json.load(f)

                logger.info("Checking state against previous backup...")

                # Check split format change first: if the requested split_size
                # differs from the previous run, the archive layout changes and
                # we must rebuild regardless of content hashes.
                old_split = old_info.get('split_size')
                if old_split != split_size:
                    logger.info(
                        f"Split size changed (was {old_split}, now {split_size}). "
                        f"Will perform full backup."
                    )
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

            except (OSError, ValueError) as e:
                logger.error(f"Error reading info file {info_file}: {e}. Will perform full backup.")
    else:
        logger.info("No previous backup info found. Will perform full backup.")

    if need_backup:
        # Compute hashes if not already computed during checks
        if names_hash is None:
            logger.info("Computing names hash...")
            names_hash = compute_names_hash(all_paths)
        if metadata_hash is None:
            logger.info("Computing metadata hash...")
            metadata_hash, _ = compute_metadata_hash(all_paths)
        if check_content_hash and content_hash is None:
            logger.info("Computing content hash...")
            content_hash, _ = compute_content_hash(all_paths)

        create_archive(sources, dest, archive_name, split_size, password)

        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()

        new_info = {
            'files_count': files_count,
            'dirs_count': dirs_count,
            'names_hash': names_hash,
            'metadata_hash': metadata_hash,
            'split_size': split_size,
            'last_check_date': now_str,
            'last_update_date': now_str
        }
        if check_content_hash:
            new_info['content_hash'] = content_hash

        _atomic_write_json(info_file, new_info)
        logger.info(f"Updated info file: {info_file}")

    else:
        # Just update the check date
        old_info['last_check_date'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _atomic_write_json(info_file, old_info)
        logger.info(f"Updated check date in info file: {info_file}")
