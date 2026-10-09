#!/usr/bin/env python3
"""Remove fixture folders without a native Serum preset; preview by default."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys

FIXTURES = Path(__file__).resolve().parent / "test" / "fixtures"


def _linked(path: Path) -> bool:
    return path.is_symlink() or getattr(os.path, "isjunction", lambda p: False)(path)


def _has_preset(folder: Path) -> bool:
    found = False

    def onerror(error: OSError) -> None:
        raise error

    for base, directories, files in os.walk(folder, followlinks=False, onerror=onerror):
        # A linked subtree may lead outside fixtures: keep the whole folder.
        if any(_linked(Path(base) / name) for name in directories):
            raise ValueError("contains a linked directory")
        found |= any(Path(name).suffix.casefold() == ".serumpreset"
                     and not name.casefold().startswith("test_") for name in files)
    return found


def clean_fixtures(root: Path, *, delete: bool = False) -> int:
    if _linked(root):
        raise ValueError("fixtures root must not be a linked directory")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("fixtures path must be a directory")
    removed = kept = skipped = errors = 0
    for folder in sorted(root.iterdir()):
        try:
            if _linked(folder):
                skipped += 1
                print(f"Skipped linked entry: {folder.name}")
                continue
            if not folder.is_dir():
                continue
            if folder.resolve(strict=True).parent != root:
                raise ValueError("directory resolves outside fixtures")
            if _has_preset(folder):
                kept += 1
                continue
            if delete:
                # Verify the final target and contents again immediately before removal.
                if _linked(folder) or folder.resolve(strict=True).parent != root:
                    raise ValueError("directory target changed during cleanup")
                if _has_preset(folder):
                    kept += 1
                    continue
                shutil.rmtree(folder)
            removed += 1
            print(f"{'Removed' if delete else 'Would remove'}: {folder.name}")
        except ValueError as error:
            skipped += 1
            print(f"Skipped: {folder.name}: {error}", file=sys.stderr)
        except OSError as error:
            errors += 1
            print(f"Failed: {folder.name}: {error}", file=sys.stderr)
    action = "Removed" if delete else "Would remove"
    print(f"{action} {removed} folders; kept {kept}; skipped {skipped}; errors {errors}.")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delete", action="store_true",
                        help="remove folders; without this option, only preview")
    args = parser.parse_args(argv)
    try:
        return clean_fixtures(FIXTURES, delete=args.delete)
    except (OSError, ValueError) as error:
        print(f"Cannot clean fixtures: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
