#!/usr/bin/env python3
"""Remove test_* files recursively from this repository's test/fixtures folder."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

FIXTURES = Path(__file__).resolve().parent / "test" / "fixtures"


def _linked(path: Path) -> bool:
    return path.is_symlink() or getattr(os.path, "isjunction", lambda p: False)(path)


def clean_test_files(root: Path, *, dry_run: bool = False) -> int:
    if _linked(root):
        raise ValueError("fixtures root must not be a linked directory")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("fixtures path must be a directory")
    removed = errors = 0

    def onerror(error: OSError) -> None:
        nonlocal errors
        errors += 1
        print(f"Failed: {error}", file=sys.stderr)

    for base, directories, files in os.walk(root, followlinks=False, onerror=onerror):
        directories[:] = sorted(name for name in directories
                                if not _linked(Path(base) / name))
        for name in sorted(files):
            if not name.casefold().startswith("test_"):
                continue
            path = Path(base) / name
            try:
                if _linked(path) or not path.is_file():
                    continue
                if not path.resolve(strict=True).is_relative_to(root):
                    raise ValueError("file resolves outside fixtures")
                if not dry_run:
                    path.unlink()
                removed += 1
                print(f"{'Would remove' if dry_run else 'Removed'}: {path.relative_to(root)}")
            except (OSError, ValueError) as error:
                errors += 1
                print(f"Failed: {path}: {error}", file=sys.stderr)
    print(f"{'Would remove' if dry_run else 'Removed'} {removed} files; errors {errors}.")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="preview without deleting files")
    args = parser.parse_args(argv)
    try:
        return clean_test_files(FIXTURES, dry_run=args.dry_run)
    except (OSError, ValueError) as error:
        print(f"Cannot clean fixtures: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
