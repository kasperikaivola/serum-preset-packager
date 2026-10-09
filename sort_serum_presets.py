#!/usr/bin/env python3
"""Copy Serum presets into genre and group folders without modifying the sources.

Classification strings live in sort_serum_presets.json. Add a genre or a group
by appending an object; earlier objects win.
"""
import argparse
import filecmp
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import cbor2
import zstandard

from fxp_to_serumpreset import _text, default_roots

MAGIC = b"XferJson\x00"
AUDIO_SUFFIXES = {".wav", ".aif", ".aiff", ".flac"}
PATH_KEYS = {
    "relativePathToWT": "Tables",
    "relativepathtowt": "Tables",
    "relativePathToNoiseSample": "Noises",
    "relativepathtonoisesample": "Noises",
    "pathToNoiseSample": "Noises",
    "pathtonoisesample": "Noises",
}
DEFAULT_CONFIG = Path(__file__).with_name("sort_serum_presets.json")
UNSORTED_LIST = Path(__file__).with_name("unsorted-presets.txt")
_TOKEN = re.compile(r"[^0-9a-z]+")


class SortError(ValueError):
    """The config or the requested folder cannot be used."""


@dataclass
class PresetInfo:
    path: Path
    text: str
    references: list[tuple[str, str]] = field(default_factory=list)
    warning: str = ""


@dataclass
class SortResult:
    copied: int = 0
    samples_copied: int = 0
    skipped_existing: int = 0
    ignored: int = 0
    missing_samples: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)
    unsorted: list[str] = field(default_factory=list)


def _words(text: str) -> str:
    return " ".join(part for part in _TOKEN.split(text.lower()) if part)


def _tokens(text: str) -> list[str]:
    return [part for part in _TOKEN.split(text.lower()) if part]


def _entry_matches(tokens: list[str], joined: str, match: str) -> bool:
    needle = _words(match)
    if not needle:
        return False
    if " " in needle:
        return needle in joined
    if len(needle) <= 2:
        return needle in tokens
    return any(token == needle or token.startswith(needle) for token in tokens)


def _first_name(entries: list[dict], tokens: list[str], joined: str, fallback: str) -> str:
    for entry in entries:
        if any(_entry_matches(tokens, joined, match) for match in entry["match"]):
            return entry["name"]
    return fallback


def classify(text: str, config: dict) -> tuple[str, str]:
    tokens = _tokens(text)
    joined = " ".join(tokens)
    genre = _first_name(config["genres"], tokens, joined, config["fallback_genre"])
    group = _first_name(config["groups"], tokens, joined, config["fallback_group"])
    return genre, group


def load_config(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SortError(f"cannot read classification config {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise SortError("classification config must be a JSON object")
    for key in ("fallback_genre", "fallback_group"):
        value = document.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SortError(f"config {key} must be a non-empty string")
    for key in ("genres", "groups"):
        entries = document.get(key)
        if not isinstance(entries, list) or not entries:
            raise SortError(f"config {key} must be a non-empty list")
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise SortError(f"each {key} entry must be an object with name and match")
            name = entry.get("name")
            matches = entry.get("match")
            if not isinstance(name, str) or not name.strip():
                raise SortError(f"each {key} entry needs a name")
            if name in seen:
                raise SortError(f"duplicate {key} name: {name}")
            seen.add(name)
            if (not isinstance(matches, list) or not matches or
                    any(not isinstance(item, str) or not item.strip() for item in matches)):
                raise SortError(f"{key} entry {name} needs a list of match strings")
    return document


def _safe_part(part: str) -> str:
    cleaned = re.sub(r'[<>:"|?*\x00-\x1f]', "_", part).rstrip(" .")
    if cleaned in ("", ".", ".."):
        raise SortError(f"unsafe folder name: {part}")
    return cleaned


def _serum1_state(path: Path) -> bytes | None:
    """Return the decompressed state, or None when the file is not a Serum FXP."""
    raw = path.read_bytes()
    if len(raw) < 60 or raw[:4] != b"CcnK" or raw[8:12] != b"FPCh" or raw[16:20] not in (b"XfsX", b"XfsY"):
        return None
    decoder = zlib.decompressobj()
    try:
        state = decoder.decompress(raw[60:])
    except zlib.error as exc:
        raise SortError(f"invalid Serum 1 preset: {exc}") from exc
    if not decoder.eof:
        raise SortError("truncated Serum 1 preset")
    return state


def _field(state: bytes, offset: int, size: int) -> str:
    if len(state) < offset + size:
        return ""
    return _text(state, offset, size).replace("\\", "/").strip()


def read_preset(path: Path, relative: Path) -> PresetInfo | None:
    """Read classification text and external sample references. None means ignore the file."""
    location = relative.with_suffix("").as_posix()
    suffix = path.suffix.lower()
    if suffix == ".fxp":
        try:
            state = _serum1_state(path)
        except SortError as exc:
            return PresetInfo(path, location, warning=str(exc))
        if state is None:
            return None
        pieces = [location, _field(state, 0x49A0, 48), _field(state, 0x49D0, 144)]
        references = []
        sizes = struct.unpack_from("<2I", state, 0x4968) if len(state) >= 0x4970 else (0, 0)
        for oscillator, offset in enumerate((0x3C08, 0x3E08)):
            name = _field(state, offset, 512)
            if name and not sizes[oscillator]:
                references.append(("Tables", name))
        noise = _field(state, 0x4008, 512)
        if noise:
            references.append(("Noises", noise))
        return PresetInfo(path, " ".join(piece for piece in pieces if piece), references)
    if suffix != ".serumpreset":
        return None
    try:
        metadata, data = _read_serum2(path)
    except SortError as exc:
        return PresetInfo(path, location, warning=str(exc))
    if metadata is None:
        return None
    tags = metadata.get("tags") if isinstance(metadata.get("tags"), list) else []
    pieces = [location, metadata.get("presetName"), metadata.get("presetAuthor"),
              metadata.get("presetDescription"), " ".join(str(tag) for tag in tags)]
    references = []
    _collect_references(data, references)
    return PresetInfo(path, " ".join(str(piece) for piece in pieces if piece), _unique_refs(references))


def _read_serum2(path: Path) -> tuple[dict | None, object]:
    raw = path.read_bytes()
    if not raw.startswith(MAGIC):
        return None, None
    if len(raw) < len(MAGIC) + 8:
        raise SortError("truncated Serum 2 preset")
    length = struct.unpack_from("<Q", raw, len(MAGIC))[0]
    start = len(MAGIC) + 8
    if length > 16 * 1024 * 1024 or start + length + 8 > len(raw):
        raise SortError("truncated Serum 2 preset header")
    try:
        metadata = json.loads(raw[start:start + length])
    except json.JSONDecodeError as exc:
        raise SortError(f"invalid Serum 2 metadata: {exc}") from exc
    if not isinstance(metadata, dict):
        raise SortError("Serum 2 metadata must be an object")
    offset = start + length
    cbor_length, encoding = struct.unpack_from("<II", raw, offset)
    if encoding != 2 or offset + 8 + 1 > len(raw):
        raise SortError("unsupported Serum 2 preset payload")
    try:
        payload = zstandard.ZstdDecompressor().decompress(
            raw[offset + 8:], max_output_size=64 * 1024 * 1024)
    except zstandard.ZstdError as exc:
        raise SortError(f"invalid Serum 2 preset: {exc}") from exc
    if len(payload) != cbor_length:
        raise SortError("Serum 2 preset payload length mismatch")
    try:
        data = cbor2.loads(payload)
    except Exception as exc:
        raise SortError(f"invalid Serum 2 preset data: {exc}") from exc
    return metadata, data


def _collect_references(node, found: list[tuple[str, str]]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            kind = PATH_KEYS.get(key) or PATH_KEYS.get(str(key).lower())
            if kind and isinstance(value, str) and value.strip():
                found.append((kind, value.replace("\\", "/").strip()))
            else:
                _collect_references(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_references(item, found)


def _unique_refs(references: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen = set()
    unique = []
    for kind, name in references:
        identity = (kind, name.casefold())
        if identity in seen:
            continue
        seen.add(identity)
        unique.append((kind, name))
    return unique


class SampleIndex:
    def __init__(self):
        self.by_name: dict[str, list[Path]] = {}

    def add_tree(self, root: Path, skip: Path | None = None) -> None:
        if not root.is_dir():
            return
        skip_resolved = skip.resolve() if skip is not None else None
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)
            try:
                if skip_resolved is not None and (current.resolve() == skip_resolved or
                                                   skip_resolved in current.resolve().parents):
                    dirnames[:] = []
                    continue
            except OSError:
                continue
            kept = []
            for name in dirnames:
                child = current / name
                try:
                    resolved = child.resolve()
                except OSError:
                    continue
                if skip_resolved is not None and resolved == skip_resolved:
                    continue
                kept.append(name)
            dirnames[:] = kept
            for name in filenames:
                if Path(name).suffix.lower() not in AUDIO_SUFFIXES:
                    continue
                path = current / name
                bucket = self.by_name.setdefault(name.casefold(), [])
                if path not in bucket:
                    bucket.append(path)


def _relative_parts(reference: str) -> tuple[Path | None, PurePosixPath | None]:
    text = reference.strip()
    if not text or text.startswith("//") or re.match(r"^[A-Za-z]:", text):
        candidate = Path(text)
        return (candidate if candidate.is_file() else None), None
    relative = PurePosixPath(text.lstrip("/"))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        return None, None
    return None, relative


def resolve_sample(reference: str, kind: str, preset: Path, scan_root: Path,
                   serum_roots: list[Path], index: SampleIndex) -> Path | None:
    absolute, relative = _relative_parts(reference)
    if absolute is not None:
        return absolute
    if relative is None:
        return None
    parts = list(relative.parts)
    folders = [preset.parent]
    current = preset.parent
    while current != scan_root and scan_root in current.parents:
        current = current.parent
        folders.append(current)
    for folder in serum_roots + folders:
        for prefix in ((kind,), ()):
            candidate = folder.joinpath(*prefix, *parts)
            if candidate.is_file():
                return candidate
    matches = index.by_name.get(relative.name.casefold(), [])
    tail = tuple(part.casefold() for part in parts)
    ranked = [path for path in matches
              if tuple(part.casefold() for part in path.parts[-len(tail):]) == tail]
    if ranked:
        return min(ranked, key=lambda path: len(path.parts))
    if len(matches) == 1:
        return matches[0]
    return None


def _iter_presets(folder: Path, output: Path, recursive: bool,
                  serum1: bool, serum2: bool):
    suffixes = set()
    if serum1:
        suffixes.add(".fxp")
    if serum2:
        suffixes.add(".serumpreset")
    output_resolved = output.resolve()
    if not recursive:
        files = [path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in suffixes]
        for path in sorted(files, key=lambda item: item.name.casefold()):
            yield path
        return
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        current = Path(dirpath)
        try:
            resolved = current.resolve()
        except OSError:
            dirnames[:] = []
            continue
        if resolved == output_resolved or output_resolved in resolved.parents:
            dirnames[:] = []
            continue
        dirnames[:] = [name for name in dirnames if (current / name).resolve() != output_resolved]
        for name in sorted(filenames, key=str.casefold):
            path = current / name
            if path.suffix.lower() in suffixes and not name.startswith("._"):
                yield path


def _unique_destination(path: Path, source: Path) -> Path:
    if not path.exists() or filecmp.cmp(source, path, shallow=False):
        return path
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:8]
    candidate = path.with_name(f"{path.stem}__{digest}{path.suffix}")
    if not candidate.exists() or filecmp.cmp(source, candidate, shallow=False):
        return candidate
    raise SortError(f"could not find a free destination name for {path.name}")


def _place_sample(source: Path, destination: Path) -> str:
    target = _unique_destination(destination, source)
    if target.exists() and filecmp.cmp(source, target, shallow=False):
        return "existing"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return "copied"


def _write_unsorted(path: Path, names: list[str]) -> None:
    ordered = sorted(names, key=str.casefold)
    path.write_text(("\n".join(ordered) + "\n") if ordered else "", encoding="utf-8")


def sort_folder(folder: Path, *, serum1: bool, serum2: bool, recursive: bool,
                config: dict, serum_roots: list[Path] | None = None,
                dry_run: bool = False, unsorted_list: Path | None = None) -> SortResult:
    folder = folder.resolve()
    if not folder.is_dir():
        raise SortError(f"input is not a directory: {folder}")
    output = folder / "sorted"
    roots = [path.resolve() for path in (default_roots() if serum_roots is None else serum_roots) if path.is_dir()]
    index = SampleIndex()
    index.add_tree(folder, output)
    for root in roots:
        index.add_tree(root, output)
    result = SortResult()
    copied_samples = set()
    for path in _iter_presets(folder, output, recursive, serum1, serum2):
        try:
            relative = path.resolve().relative_to(folder)
        except (OSError, ValueError):
            result.failed.append(f"{path}: path is outside the input folder")
            continue
        if output.resolve() in path.resolve().parents:
            continue
        info = read_preset(path, relative)
        if info is None:
            result.ignored += 1
            continue
        if info.warning:
            print(f"Warning: {path}: {info.warning}", file=sys.stderr)
        genre, group = classify(info.text, config)
        destination_dir = output / _safe_part(genre) / _safe_part(group)
        result.counts[(genre, group)] += 1
        if genre == config["fallback_genre"]:
            result.unsorted.append(relative.as_posix())
        try:
            preset_destination = _unique_destination(destination_dir / path.name, path)
            already = preset_destination.exists() and filecmp.cmp(path, preset_destination, shallow=False)
            if not dry_run and not already:
                destination_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, preset_destination)
            if already:
                result.skipped_existing += 1
            else:
                result.copied += 1
            action = "Would copy" if dry_run and not already else "Copied" if not already else "Exists"
            print(f"{action}: {relative} -> {preset_destination.relative_to(folder)}")
            for kind, reference in info.references:
                sample = resolve_sample(reference, kind, path, folder, roots, index)
                label = f"{relative}: {kind}/{reference}"
                if sample is None:
                    if label not in result.missing_samples:
                        result.missing_samples.append(label)
                        print(f"Missing sample: {label}", file=sys.stderr)
                    continue
                absolute, relative_parts = _relative_parts(reference)
                if relative_parts is None:
                    sample_destination = destination_dir / "Samples" / _safe_part(sample.name)
                else:
                    sample_destination = destination_dir / kind
                    for part in relative_parts.parts:
                        sample_destination /= _safe_part(part)
                identity = (sample.resolve(), sample_destination)
                if identity in copied_samples:
                    continue
                copied_samples.add(identity)
                if dry_run:
                    if not (sample_destination.exists() and filecmp.cmp(sample, sample_destination, shallow=False)):
                        result.samples_copied += 1
                        print(f"Would copy sample: {sample} -> {sample_destination.relative_to(folder)}")
                    continue
                placed = _place_sample(sample, sample_destination)
                if placed == "copied":
                    result.samples_copied += 1
                    print(f"Copied sample: {sample} -> {sample_destination.relative_to(folder)}")
        except (OSError, SortError) as exc:
            result.failed.append(f"{path}: {exc}")
            print(f"Failed: {path}: {exc}", file=sys.stderr)
    if unsorted_list is not None:
        _write_unsorted(unsorted_list, result.unsorted)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copy Serum 1 and Serum 2 presets into genre/group folders. "
                                     "Sources are never modified. Edit sort_serum_presets.json to add genres or groups.")
    parser.add_argument("folder", type=Path, help="folder whose presets are copied into <folder>/sorted")
    parser.add_argument("--serum1", action="store_true", help="include Serum 1 .fxp presets")
    parser.add_argument("--serum2", action="store_true", help="include Serum 2 .SerumPreset presets")
    parser.add_argument("-r", "--recursive", action="store_true", help="include presets in subfolders")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="JSON file listing genre and group match strings")
    parser.add_argument("--serum-root", action="append", type=Path,
                        help="folder containing Tables/ and Noises/; repeat to search several. "
                             "Defaults to the Documents/Xfer Serum folders.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the sort plan without copying presets or samples")
    parser.add_argument("--unsorted-list", type=Path, default=UNSORTED_LIST,
                        help="file that receives presets which matched no genre (default: unsorted-presets.txt beside this script)")
    args = parser.parse_args(argv)
    serum1 = args.serum1 or not args.serum2
    serum2 = args.serum2 or not args.serum1
    try:
        config = load_config(args.config)
        result = sort_folder(args.folder, serum1=serum1, serum2=serum2, recursive=args.recursive,
                             config=config, serum_roots=args.serum_root, dry_run=args.dry_run,
                             unsorted_list=args.unsorted_list)
    except SortError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print("Groups:")
    for (genre, group), count in sorted(result.counts.items()):
        print(f"  {genre}/{group}: {count}")
    print(f"Copied {result.copied} presets and {result.samples_copied} samples. "
          f"Existing {result.skipped_existing}. Ignored {result.ignored}. "
          f"Missing samples {len(result.missing_samples)}. Failed {len(result.failed)}.")
    print(f"Unsorted presets ({len(result.unsorted)}): {args.unsorted_list}")
    return 1 if result.failed or result.missing_samples else 0


if __name__ == "__main__":
    raise SystemExit(main())
