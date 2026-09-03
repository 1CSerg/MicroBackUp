import argparse
import sys
import os
from backup import run_backup

def parse_size(size_str):
    if not size_str:
        return None
    size_str = size_str.strip().lower()
    if size_str.endswith('k'):
        return int(float(size_str[:-1]) * 1024)
    elif size_str.endswith('m'):
        return int(float(size_str[:-1]) * 1024 * 1024)
    elif size_str.endswith('g'):
        return int(float(size_str[:-1]) * 1024 * 1024 * 1024)
    else:
        try:
            return int(size_str)
        except ValueError:
            raise argparse.ArgumentTypeError(f"Invalid size format: {size_str}. Use k, m, or g suffixes (e.g., 100m).")

def main():
    parser = argparse.ArgumentParser(description="MicroBackUp - Simple cross-platform incremental backup utility.")
    
    parser.add_argument(
        '-s', '--sources',
        nargs='+',
        required=True,
        help="List of source directories and/or files to backup."
    )
    
    parser.add_argument(
        '-d', '--dest',
        required=True,
        help="Destination directory where the backup archive will be saved."
    )
    
    parser.add_argument(
        '-n', '--name',
        required=True,
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

    args = parser.parse_args()

    # Validate sources
    for src in args.sources:
        if not os.path.exists(src):
            print(f"Error: Source path does not exist: {src}", file=sys.stderr)
            sys.exit(1)

    # Ensure destination exists
    if not os.path.exists(args.dest):
        try:
            os.makedirs(args.dest)
        except OSError as e:
            print(f"Error: Could not create destination directory {args.dest}: {e}", file=sys.stderr)
            sys.exit(1)

    try:
        run_backup(
            sources=args.sources,
            dest=args.dest,
            archive_name=args.name,
            split_size=args.split,
            password=args.password
        )
    except Exception as e:
        print(f"Backup failed: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
