#!/usr/bin/env python3
"""Prefix native fixture pairs with pair_, or explicitly mark incorporated pairs checked_."""
import argparse
from pathlib import Path
import time

from fixture_pairs import FIXTURES, fixture_folders, preset_files, rename_folder


def mark_pairs(root: Path, checked: list[str] | None = None) -> int:
    errors = changed = 0
    if checked:
        folders = [root / name for name in checked]
    else:
        # Children first: parent renames cannot invalidate pending paths.
        folders = sorted(fixture_folders(root), key=lambda p: len(p.parts), reverse=True)
    for folder in folders:
        try:
            sources, native = preset_files(folder)
            if not sources or not native:
                if checked:
                    raise ValueError("folder does not contain an FXP and a manual SerumPreset")
                continue
            if folder.name.lower().startswith("checked_"):
                continue
            target = rename_folder(folder, root, "checked_" if checked else "pair_")
            if target != folder:
                print(f"Renamed: {folder.name} -> {target.name}")
                changed += 1
        except (OSError, ValueError) as error:
            print(f"Failed: {folder}: {error}")
            errors += 1
    print(f"Renamed {changed}; errors {errors}.")
    return int(bool(errors))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    parser.add_argument("--checked", action="append", metavar="FOLDER",
                        help="replace pair_ with checked_ for an incorporated folder (repeatable)")
    parser.add_argument("--watch", type=float, metavar="SECONDS", help="rescan while pairs are added")
    args = parser.parse_args(argv)
    if args.watch is not None and (args.watch <= 0 or args.checked):
        parser.error("--watch must be positive and cannot be combined with --checked")
    try:
        root = args.fixtures.resolve(strict=True)
        while True:
            result = mark_pairs(root, args.checked)
            if args.watch is None or result:
                return result
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as error:
        print(f"Cannot mark fixtures: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
