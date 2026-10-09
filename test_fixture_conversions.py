#!/usr/bin/env python3
"""Convert every fixture FXP and report differences from manual native saves."""
__test__ = False  # This is a CLI runner, not a pytest test module.
import argparse
import json
import math
from pathlib import Path
import struct

import cbor2
import zstandard

from fixture_pairs import FIXTURES, fixture_folders, preset_files, linked
from fxp_to_serumpreset import convert_file, MAGIC


def decode_preset(path: Path):
    raw = path.read_bytes()
    if len(raw) < 25 or raw[:9] != MAGIC:
        raise ValueError("invalid SerumPreset header")
    size, = struct.unpack_from("<Q", raw, 9)
    offset = 17 + size
    if offset + 8 >= len(raw):
        raise ValueError("truncated SerumPreset metadata")
    metadata = json.loads(raw[17:offset])
    length, encoding = struct.unpack_from("<II", raw, offset)
    if encoding != 2 or not 0 < length <= 64 * 1024 * 1024:
        raise ValueError("unsupported/oversized SerumPreset payload")
    payload = zstandard.ZstdDecompressor().decompress(raw[offset + 8:], max_output_size=64 * 1024 * 1024)
    if len(payload) != length:
        raise ValueError("SerumPreset payload length mismatch")
    return {"metadata": metadata, "data": cbor2.loads(payload)}


def compare(actual, expected, *, controls_only=False, abs_tol=1e-5, rel_tol=1e-6,
            canonicalized=False):
    """Collect every difference; do not fail fast or special-case known presets."""
    differences = []
    ui_keys = {"Osc", "WTOsc", "Filter", "SerumGUI", "ClipPlayer", "GranularOsc",
               "MultiSampleOsc", "SpectralOsc", "arpBankDisplayName", "clipBankDisplayName",
               "scale", "scaleName", "displayName"}

    def record(path, kind, a=None, b=None):
        def display(value):
            if isinstance(value, (dict, list, bytes)):
                return repr(value)[:240]
            return value
        differences.append(dict(path=path, kind=kind, generated=display(a), native=display(b)))

    def walk(a, b, path):
        if isinstance(a, dict) and isinstance(b, dict):
            keys = (b.keys() if controls_only and not canonicalized and path.startswith("/data")
                    else a.keys() | b.keys())
            for key in sorted(keys):
                if path == "/metadata" and key == "hash":
                    continue  # Different compression/serialization changes the hash.
                if controls_only and path.startswith("/data"):
                    if key.startswith("kUI") or key in ui_keys or key in ("lfophasor", "detuneFactor"):
                        continue
                    if key == "lfo" and "FXHyperD" in path:
                        continue
                    if key == "plainParams" and b.get(key) == "default" and not canonicalized:
                        continue  # Extra explicit defaults need native processor read-back.
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                next_path = path + "/" + escaped
                if key not in a:
                    record(next_path, "missing generated key", b=b[key])
                elif key not in b:
                    record(next_path, "extra generated key", a=a[key])
                else:
                    walk(a[key], b[key], next_path)
        elif isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                record(path, "array length", len(a), len(b))
            for i, (x, y) in enumerate(zip(a, b)):
                walk(x, y, f"{path}/{i}")
        elif isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
            if not math.isclose(a, b, abs_tol=abs_tol, rel_tol=rel_tol):
                record(path, "numeric value", a, b)
        elif type(a) != type(b) or a != b:
            record(path, "value/type", a, b)

    walk(actual, expected, "")
    return differences


def run_fixtures(root: Path, *, serum_roots=None, controls_only=False, abs_tol=1e-5, rel_tol=1e-6,
                 backend="offline", plugin=None):
    results = []
    for folder in fixture_folders(root):
        sources, native = preset_files(folder)
        for source in sources:
            output = folder / f"test_{source.stem}.SerumPreset"
            result = dict(source=str(source), output=str(output), reference=None,
                          differences=[], error=None, output_deleted=False)
            results.append(result)
            try:
                if linked(output):
                    raise ValueError("generated output is a linked file")
                # Only replace this runner's reserved test_ output, never a reference.
                convert_file(source, output, serum_roots=serum_roots, overwrite=True,
                             backend=backend, plugin=plugin)
                matches = [p for p in native if p.stem.casefold() == source.stem.casefold()]
                if not matches and len(native) == 1:
                    matches = native
                if len(matches) != 1:
                    raise ValueError("no unique manual reference (test_ presets are excluded)")
                result["reference"] = str(matches[0])
                result["differences"] = compare(decode_preset(output), decode_preset(matches[0]),
                                                controls_only=controls_only, abs_tol=abs_tol, rel_tol=rel_tol)
                if not result["differences"]:
                    if linked(output) or output.resolve().parent != folder.resolve():
                        raise ValueError("generated output target changed before cleanup")
                    output.unlink()
                    result["output_deleted"] = True
            except Exception as error:
                # Continue through unsupported, malformed, unpaired and missing-asset inputs.
                result["error"] = f"{type(error).__name__}: {error}"
            count = len(result["differences"])
            status = "ERROR" if result["error"] else "DIFF" if count else "PASS"
            print(f"{status}: {source.parent.name}/{source.name}: {result['error'] or str(count) + ' differences'}")
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    parser.add_argument("--serum-root", action="append", type=Path, help="asset root (repeatable)")
    parser.add_argument("--report", type=Path, help="complete JSON report; default fixtures/conversion-test-report.json")
    parser.add_argument("--controls-only", action="store_true",
                        help="compare native serialized controls; omit UI/playback and extra generated defaults")
    parser.add_argument("--abs-tol", type=float, default=1e-5)
    parser.add_argument("--rel-tol", type=float, default=1e-6)
    parser.add_argument("--verbose", action="store_true", help="also print every discrepancy")
    parser.add_argument("--backend", choices=("offline", "native"), default="offline")
    parser.add_argument("--plugin", type=Path, help="verified Serum binary, native backend only")
    args = parser.parse_args(argv)
    if any(not math.isfinite(n) or n < 0 for n in (args.abs_tol, args.rel_tol)):
        parser.error("numeric tolerances must be finite and nonnegative")
    try:
        root = args.fixtures.resolve(strict=True)
        results = run_fixtures(root, serum_roots=args.serum_root, controls_only=args.controls_only,
                               abs_tol=args.abs_tol, rel_tol=args.rel_tol,
                               backend=args.backend, plugin=args.plugin)
        failed = sum(bool(r["error"] or r["differences"]) for r in results)
        report = args.report or root / "conversion-test-report.json"
        # Reports may never replace source/reference/test presets.
        if report.suffix.lower() != ".json" or linked(report):
            raise ValueError("report must be an unlinked .json path")
        report.write_text(json.dumps(dict(mode="controls-only" if args.controls_only else "strict",
                                         abs_tol=args.abs_tol, rel_tol=args.rel_tol,
                                         total=len(results), failed=failed, results=results),
                                    ensure_ascii=False, indent=2), encoding="utf-8")
        if args.verbose:
            for result in results:
                for difference in result["differences"]:
                    print(result["source"], json.dumps(difference, ensure_ascii=False))
        print(f"Tested {len(results)}; passed {len(results) - failed}; failed {failed}. Full report: {report}")
        if not results:
            print("No FXP fixtures found.")
        return int(bool(failed) or not results)
    except (OSError, ValueError) as error:
        print(f"Cannot test fixtures: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
