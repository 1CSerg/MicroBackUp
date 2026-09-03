import os
import hashlib
import json
import datetime
import logging
import py7zr
import multivolumefile
from pathlib import Path

logger = logging.getLogger("microbackup")

def get_all_paths(sources):
    """
    Returns a list of all files and directories in the given sources.
    Each item is a tuple (absolute_path, relative_path_for_archive).
    """
    all_paths = []
    
    for src in sources:
        src_path = Path(src).resolve()
        parent_dir = src_path.parent
        
        if src_path.is_file():
            rel_path = src_path.relative_to(parent_dir)
            all_paths.append((str(src_path), str(rel_path), 'file'))
        elif src_path.is_dir():
            rel_path = src_path.relative_to(parent_dir)
            all_paths.append((str(src_path), str(rel_path), 'dir'))
            
            for root, dirs, files in os.walk(src_path):
                for d in dirs:
                    d_path = Path(root) / d
                    d_rel = d_path.relative_to(parent_dir)
                    all_paths.append((str(d_path), str(d_rel), 'dir'))
                for f in files:
                    f_path = Path(root) / f
                    f_rel = f_path.relative_to(parent_dir)
                    all_paths.append((str(f_path), str(f_rel), 'file'))
                    
    return all_paths

def count_items(paths):
    files_count = sum(1 for p in paths if p[2] == 'file')
    dirs_count = sum(1 for p in paths if p[2] == 'dir')
    return files_count, dirs_count

def compute_names_hash(paths):
    # Sort relative paths to ensure consistent hashing
    rel_paths = sorted([p[1] for p in paths])
    hasher = hashlib.sha256()
    for p in rel_paths:
        hasher.update(p.encode('utf-8'))
    return hasher.hexdigest()

def compute_file_hash(filepath):
    hasher = hashlib.sha256()
    try:
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(4096 * 1024), b""):
                hasher.update(chunk)
    except Exception as e:
        logger.warning(f"Warning: Could not read file {filepath} for hashing: {e}")
    return hasher.hexdigest()

def compute_content_hash(paths):
    # Sort relative paths to ensure consistent hashing order
    file_paths = sorted([p for p in paths if p[2] == 'file'], key=lambda x: x[1])
    
    hasher = hashlib.sha256()
    for abs_path, rel_path, _ in file_paths:
        f_hash = compute_file_hash(abs_path)
        hasher.update(f_hash.encode('utf-8'))
    return hasher.hexdigest()

def create_archive(sources, dest, archive_name, split_size, password):
    archive_path = Path(dest) / f"{archive_name}.7z"
    
    logger.info(f"Creating archive: {archive_path}")
    
    filters = None
    if password:
        # Note: py7zr handles password encryption
        pass

    if split_size:
        logger.info(f"Splitting archive into volumes of size {split_size} bytes")
        with multivolumefile.open(archive_path, mode='wb', volume=split_size) as target_archive:
            with py7zr.SevenZipFile(target_archive, 'w', password=password) as archive:
                for src in sources:
                    src_path = Path(src).resolve()
                    arcname = src_path.name
                    if src_path.is_file():
                        archive.write(src_path, arcname)
                    else:
                        archive.writeall(src_path, arcname)
    else:
        with py7zr.SevenZipFile(archive_path, 'w', password=password) as archive:
            for src in sources:
                src_path = Path(src).resolve()
                arcname = src_path.name
                if src_path.is_file():
                    archive.write(src_path, arcname)
                else:
                    archive.writeall(src_path, arcname)
                    
    logger.info("Archive created successfully.")

def run_backup(sources, dest, archive_name, split_size=None, password=None):
    info_file = Path(dest) / f"{archive_name}_hash.json"
    
    logger.info("Gathering file list...")
    all_paths = get_all_paths(sources)
    files_count, dirs_count = count_items(all_paths)
    
    logger.info(f"Found {files_count} files and {dirs_count} directories.")
    
    need_backup = True
    names_hash = None
    content_hash = None
    
    if info_file.exists():
        try:
            with open(info_file, 'r', encoding='utf-8') as f:
                old_info = json.load(f)
                
            logger.info("Checking state against previous backup...")
            
            # Step 1: Check counts
            if old_info.get('files_count') == files_count and old_info.get('dirs_count') == dirs_count:
                logger.info("Counts match. Checking names hash...")
                # Step 2: Check names hash
                names_hash = compute_names_hash(all_paths)
                if old_info.get('names_hash') == names_hash:
                    logger.info("Names hash matches. Checking content hash...")
                    # Step 3: Check content hash
                    content_hash = compute_content_hash(all_paths)
                    if old_info.get('content_hash') == content_hash:
                        logger.info("Content hash matches. No backup needed.")
                        need_backup = False
                    else:
                        logger.info("Content hash differs.")
                else:
                    logger.info("Names hash differs.")
            else:
                logger.info("Counts differ.")
                
        except Exception as e:
            logger.error(f"Error reading info file {info_file}: {e}. Will perform full backup.")
    else:
        logger.info("No previous backup info found. Will perform full backup.")

    if need_backup:
        # Compute hashes if not already computed during checks
        if names_hash is None:
            logger.info("Computing names hash...")
            names_hash = compute_names_hash(all_paths)
        if content_hash is None:
            logger.info("Computing content hash...")
            content_hash = compute_content_hash(all_paths)
            
        create_archive(sources, dest, archive_name, split_size, password)
        
        now_str = datetime.datetime.now().isoformat()
        
        new_info = {
            'files_count': files_count,
            'dirs_count': dirs_count,
            'names_hash': names_hash,
            'content_hash': content_hash,
            'last_check_date': now_str,
            'last_update_date': now_str
        }
        
        with open(info_file, 'w', encoding='utf-8') as f:
            json.dump(new_info, f, indent=4)
        logger.info(f"Updated info file: {info_file}")
        
    else:
        # Just update the check date
        old_info['last_check_date'] = datetime.datetime.now().isoformat()
        with open(info_file, 'w', encoding='utf-8') as f:
            json.dump(old_info, f, indent=4)
        logger.info(f"Updated check date in info file: {info_file}")
