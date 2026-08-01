#!/usr/bin/env python3
"""
Script to traverse a Windows NTFS directory, filter files using a whitelist/blacklist
of extensions, copy matching files to a raw/ directory, and count unknown files by extension.
"""

import argparse
from collections import Counter
import logging
import os
from pathlib import Path
import shutil
import sys

# Default extensions grouped by category
DEFAULT_WHITELIST = {
    # pdf
    ".pdf",
    # docs
    ".doc", ".docx", ".docm", ".dotx", ".dotm", ".odt", ".rtf",
    # one note
    ".one", ".onetoc2",
    # mails
    ".eml", ".msg"
}

DEFAULT_BLACKLIST = {
    # compressed files
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz",
    # excel
    ".xls", ".xlsx", ".xlsm", ".xlsb", ".ods",
    # csv
    ".csv"
}


def setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%H:%M:%S"
    )


def clean_extensions(ext_list: list[str] | None) -> set[str]:
    """Ensure all extensions in the list start with '.' and are lowercase."""
    if not ext_list:
        return set()
    cleaned = set()
    for ext in ext_list:
        ext = ext.strip().lower()
        if not ext.startswith("."):
            ext = f".{ext}"
        cleaned.add(ext)
    return cleaned


def copy_file(src: Path, dest: Path, preserve_structure: bool, source_root: Path, dry_run: bool, existing_names: set[str] = None) -> Path | None:
    """
    Copies a file to the destination directory.
    If preserve_structure is True, recreates the source relative directory path inside dest.
    If preserve_structure is False, copies flat and handles collisions by appending a counter.
    """
    if preserve_structure:
        try:
            rel_path = src.relative_to(source_root)
            target_path = dest / rel_path
        except ValueError:
            # Fallback in case src is not under source_root
            target_path = dest / src.name
    else:
        target_name = src.name
        if existing_names is not None:
            if target_name.lower() in existing_names:
                base = src.stem
                suffix = src.suffix
                counter = 1
                while True:
                    new_name = f"{base}_{counter}{suffix}"
                    if new_name.lower() not in existing_names:
                        target_name = new_name
                        break
                    counter += 1
            existing_names.add(target_name.lower())
        else:
            target_path = dest / target_name
            if target_path.exists():
                base = target_path.stem
                suffix = target_path.suffix
                counter = 1
                while True:
                    new_name = f"{base}_{counter}{suffix}"
                    candidate = dest / new_name
                    if not candidate.exists():
                        target_path = candidate
                        break
                    counter += 1
                target_name = target_path.name
        target_path = dest / target_name

    if dry_run:
        logging.info(f"[DRY-RUN] Copy {src} -> {target_path}")
        return target_path

    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target_path)
        logging.debug(f"Copied: {src} -> {target_path}")
        return target_path
    except Exception as e:
        logging.error(f"Error copying {src} to {target_path}: {e}")
        return None


def traverse_and_copy(
    source: Path,
    dest: Path,
    whitelist: set[str],
    blacklist: set[str],
    preserve_structure: bool = False,
    dry_run: bool = False
) -> tuple[int, int, Counter]:
    """
    Traverses the source directory, filters files, and copies matching ones.
    Returns (copied_count, blacklisted_count, unknown_counter).
    """
    copied_count = 0
    blacklisted_count = 0
    unknown_counter = Counter()

    # Pre-resolve to absolute paths
    source_resolved = source.resolve()
    dest_resolved = dest.resolve()

    existing_names = set()
    if not preserve_structure and dest_resolved.exists():
        try:
            existing_names = {p.name.lower() for p in dest_resolved.iterdir() if p.is_file()}
        except Exception as e:
            logging.warning(f"Could not read destination directory for collision cache: {e}")

    def on_walk_error(err):
        logging.warning(f"Error accessing path during walk: {err}")

    # Use os.walk and handle permission/access errors gracefully
    for root, dirs, files in os.walk(source_resolved, onerror=on_walk_error):
        # Prevent traversing into the destination directory if it is inside the source directory
        if Path(root).resolve() == dest_resolved:
            logging.debug(f"Skipping traversal of destination folder: {root}")
            dirs.clear()  # Don't recurse into subdirectories of dest
            continue

        for file in files:
            file_path = Path(root) / file
            # Use lower case suffix for matching
            ext = file_path.suffix.lower()

            if ext in whitelist:
                copied = copy_file(
                    src=file_path,
                    dest=dest_resolved,
                    preserve_structure=preserve_structure,
                    source_root=source_resolved,
                    dry_run=dry_run,
                    existing_names=existing_names
                )
                if copied:
                    copied_count += 1
            elif ext in blacklist:
                blacklisted_count += 1
                logging.debug(f"Skipped (blacklisted): {file_path}")
            else:
                unknown_counter[ext] += 1
                logging.debug(f"Skipped (unknown extension '{ext}'): {file_path}")

    return copied_count, blacklisted_count, unknown_counter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Traverse a folder, copy whitelisted files to raw, and report unknown extensions."
    )
    parser.add_argument("source", type=Path, help="Path to NTFS folder to traverse")
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "raw",
        help="Destination directory (default: project's 'raw/' directory)"
    )
    parser.add_argument(
        "--whitelist",
        nargs="+",
        help="Additional file extensions to whitelist (e.g. txt md)"
    )
    parser.add_argument(
        "--blacklist",
        nargs="+",
        help="Additional file extensions to blacklist (e.g. mp4)"
    )
    parser.add_argument(
        "--preserve-structure",
        action="store_true",
        help="Recreate the source directory structure under the destination folder"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate execution without modifying any files"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable detailed debug logging"
    )

    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    if not args.source.exists():
        logging.error(f"Source path '{args.source}' does not exist.")
        return 1
    if not args.source.is_dir():
        logging.error(f"Source path '{args.source}' is not a directory.")
        return 1

    # Initialize extensions
    whitelist = DEFAULT_WHITELIST.copy()
    blacklist = DEFAULT_BLACKLIST.copy()

    # Apply custom overrides
    user_whitelist = clean_extensions(args.whitelist)
    user_blacklist = clean_extensions(args.blacklist)

    whitelist.update(user_whitelist)
    blacklist.update(user_blacklist)

    # If any extension is in both, whitelist takes precedence
    blacklist = blacklist - whitelist

    logging.info(f"Starting traversal of: {args.source}")
    logging.info(f"Destination folder: {args.dest}")
    logging.info(f"Active Whitelist: {', '.join(sorted(whitelist))}")
    logging.info(f"Active Blacklist: {', '.join(sorted(blacklist))}")

    copied, blacklisted, unknown = traverse_and_copy(
        source=args.source,
        dest=args.dest,
        whitelist=whitelist,
        blacklist=blacklist,
        preserve_structure=args.preserve_structure,
        dry_run=args.dry_run
    )

    # Print final summary
    print("\n" + "=" * 50)
    print("NTFS TRAVERSAL SUMMARY")
    print("=" * 50)
    print(f"Files copied:              {copied}")
    print(f"Blacklisted files skipped: {blacklisted}")
    print(f"Unknown files skipped:     {sum(unknown.values())}")
    print("=" * 50)

    if unknown:
        print("\nUnknown files by file extension:")
        # Sort by count (descending), then by extension alphabetically
        for ext, count in sorted(unknown.items(), key=lambda x: (-x[1], x[0])):
            display_ext = ext if ext else "(no extension)"
            print(f"  {display_ext:<15} : {count}")
        print("=" * 50)

    return 0


if __name__ == "__main__":
    sys.exit(main())
