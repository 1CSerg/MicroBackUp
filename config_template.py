"""Commented-out default config written when MicroBackUp.conf is missing."""

DEFAULT_CONFIG_TEMPLATE = """\
# MicroBackUp configuration file
# Launch without -c to use this file next to the program:
#   MicroBackUp.exe
#   python main.py
# Or pass a path explicitly:
#   python main.py -c MicroBackUp.conf
#
# Uncomment and fill in the options you need. Lines starting with # are comments.
#
# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
# [GLOBAL] sets defaults for all jobs: split, password, exclude, compression_level,
# sevenzip_path, sevenzip_args, check_content_hash, and logging.
# Any other section is a separate backup job.
# Do not use [DEFAULT]: INI default inheritance is disabled, and that section is ignored.
#
# Job sections require: sources, dest, name.
# split, password, check_content_hash, compression_level, sevenzip_path and
# sevenzip_args in a job section override [GLOBAL].
# exclude in a job section is appended after the global list
# (a job can restore a globally excluded path with !pattern).
#
# Quote paths that contain spaces in sources.
# Quotes around dest are optional and are stripped automatically.
#
# ---------------------------------------------------------------------------
# sources / dest / name
# ---------------------------------------------------------------------------
# sources  — files and/or directories to back up (space-separated; quote paths
#            with spaces). Example: sources = "D:\\Work\\Project A" C:\\Logs
# dest     — destination directory for the archive and the *_hash.json file
# name     — archive name without extension (must be a valid filename)
#
# ---------------------------------------------------------------------------
# split
# ---------------------------------------------------------------------------
# Split the archive into volumes. Suffixes: k, m, g (also kb, mb, gb).
# Examples: 100m, 1.5g, 4500k
# none / off / empty — do not split.
# A job can disable a global split with: split = none
#
# ---------------------------------------------------------------------------
# password
# ---------------------------------------------------------------------------
# Archive password (stored in this file as plain text).
# Do not commit a filled-in config, do not sync it to a public cloud, and
# restrict OS-level access to this file.
#
# Safer alternative: environment variable MICROBACKUP_PASSWORD.
# Priority: CLI -p > job-section password > env MICROBACKUP_PASSWORD > [GLOBAL] password.
# *_hash.json next to the archive stores a PBKDF2 verifier (not the password)
# so a later run can detect that the password changed.
# An explicit empty password in a job (password =) disables the password for
# that job, overriding both env and [GLOBAL].
#
# ---------------------------------------------------------------------------
# check_content_hash
# ---------------------------------------------------------------------------
# true/false, default false.
# true  — hash full file contents (slow, detects silent corruption/bit flips).
# false — compare size and mtime only (thousands of times faster).
#
# ---------------------------------------------------------------------------
# exclude (gitignore syntax, relative to each source root)
# ---------------------------------------------------------------------------
# One pattern per line; indented continuations are allowed (standard multiline INI).
#   * ? ** [abc]  — wildcards; trailing / — directories only; leading / — source root only;
#   !pattern      — re-include a previously excluded path (does not revive files
#                   inside an already excluded directory).
# The source root itself cannot be excluded — remove it from sources instead.
# On Windows matching is case-insensitive; on Linux it is case-sensitive.
# configparser strips # comments inside INI values; to exclude a name that
# starts with #, escape it: \\#name
#
# ---------------------------------------------------------------------------
# Logging (read from [GLOBAL] only; CLI --log-* overrides these)
# ---------------------------------------------------------------------------
# log_file         — path to the log file; empty or omitted = console only
# log_level        — DEBUG, INFO, WARNING, ERROR, CRITICAL (default INFO)
# log_max_size     — rotation threshold (k/m/g); omitted = no rotation
# log_backup_count — number of old log copies to keep (default 3)
#
# ---------------------------------------------------------------------------
# compression_level
# ---------------------------------------------------------------------------
# Integer 0-9. Applies to both the built-in engine (py7zr) and external 7z.
#   0 — store, no compression
#   1 — fastest
#   5 — default of 7-Zip
#   9 — maximum compression (slowest)
# If omitted, each engine uses its own default.
# none / off / empty in a job section restores the engine default and overrides [GLOBAL].
# Archives produced by the two engines at the same level are not byte-identical.
# Changing this value does not by itself force a rebuild; delete *_hash.json to rebuild.
#
# ---------------------------------------------------------------------------
# External 7z (optional, much faster than the built-in library)
# ---------------------------------------------------------------------------
# sevenzip_path — path to 7z.exe / 7z. Used only when set explicitly (no PATH auto-search
#                 unless the value is a bare name such as 7z).
#                 Windows example: C:\\Program Files\\7-Zip\\7z.exe
#                 Linux example:   /usr/bin/7z
#                 none / off / empty in a job disables external 7z for that job.
# If the binary is missing or 7z fails, MicroBackUp logs a warning and falls back
# to the built-in engine.
# When a password is used with external 7z, it is passed on the 7z command line
# and may be visible in the process list.
#
# sevenzip_args — extra arguments for external 7z only (ignored by the built-in engine).
#                 Example: -mmt=4
#                 Multiline values are allowed.
#
# ---------------------------------------------------------------------------
# Example (uncomment and edit)
# ---------------------------------------------------------------------------
# [GLOBAL]
# split = 100m
# password = <your_password>
# log_file = MicroBackUp.log
# log_level = INFO
# log_max_size = 5m
# log_backup_count = 3
# compression_level = 5
# sevenzip_path = C:\\Program Files\\7-Zip\\7z.exe
# sevenzip_args = -mmt=on
# exclude =
#     *.tmp
#     ~$*
#
# [ProjectA]
# sources = "D:\\Work\\Project A" C:\\Logs
# dest = D:\\Backups\\ProjectA
# name = proj_a_backup
# exclude =
#     node_modules/
#     build/
#     *.log
#     !important.log
#
# [ProjectB]
# sources = D:\\Work\\ProjectB
# dest = D:\\Backups\\ProjectB
# name = proj_b_backup
# split = 500m
# password = specific_secret
# compression_level = 9
"""
