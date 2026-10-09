"""Compare standalone conversion with isolated Serum import and processor read-back.

This development tool requires the verified Serum binary. Neither converter's
output is installed into the source library, and FXPs are never modified.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import tempfile

from fxp_to_serumpreset import translate_fxp
from native_serum import native_import, canonicalize, BINARY_SHA256, DEFAULT_PLUGIN
from test_fixture_conversions import compare


def write_json(path, document, *, indent=None):
    """Publish complete cache/report files, even with concurrent workers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, ensure_ascii=False, indent=indent)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def summarize(results):
    """Keep every discrepancy, distinguishing migration failures from preferences."""
    return dict(
        total=len(results),
        failed=sum(bool(r['errors'] or r['differences']) for r in results),
        conversion_errors=sum(bool(r['errors']) for r in results),
        patch_mismatches=sum(any(d['path'].startswith('/data/')
                                 and d['path'] != '/data/mpeEnabled'
                                 for d in r['differences']) for r in results),
    )


def compare_file(source, *, plugin=None, serum_roots=None, cache=None):
    result = dict(source=str(source), errors={}, differences=[])
    documents = {}
    # Cache only the reference, keyed by source, binary and importer code.
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    revision = hashlib.sha256(Path(__file__).with_name('native_serum.py').read_bytes()).hexdigest()[:16]
    cached = cache / f'{digest}-{revision}.json' if cache else None
    try:
        if cached and cached.is_file():
            documents['native'] = json.loads(cached.read_text(encoding='utf-8'))
        else:
            documents['native'] = canonicalize(native_import(source, plugin=plugin), plugin=plugin)
            if cached:
                write_json(cached, documents['native'])
    except Exception as error:
        result['errors']['native'] = str(error)
    try:
        offline = translate_fxp(source, serum_roots)
        # Native import already performs one processor load/save. Embedded
        # table interpolation can change samples on every load; compare equal
        # numbers of interpolation passes rather than different render stages.
        if any(offline['data'].get(f'Oscillator{i}', {}).get(f'WTOsc{i}', {})
               .get('interpolateAfterLoad') for i in (0, 1)):
            offline = canonicalize(offline, plugin=plugin)
        documents['offline'] = canonicalize(offline, plugin=plugin)
    except Exception as error:
        result['errors']['offline'] = str(error)
    if len(documents) == 2:
        # Canonicalization removes explicit default controls on both sides.
        result['differences'] = compare(documents['offline'], documents['native'],
                                        controls_only=True, canonicalized=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, nargs='?', default=Path('test/fixtures'))
    parser.add_argument('--report', type=Path, default=Path('research/backend-comparison.json'))
    parser.add_argument('--cache', type=Path, default=Path('research/native-reference-cache'))
    parser.add_argument('--plugin', type=Path)
    parser.add_argument('--serum-root', action='append', type=Path)
    parser.add_argument('--jobs', type=int, default=1, help='isolated workers in parallel (1 to 4)')
    args = parser.parse_args(argv)
    if args.jobs not in range(1, 5):
        parser.error('--jobs must be between 1 and 4')
    binary = (args.plugin or DEFAULT_PLUGIN).resolve()
    if not binary.is_file() or hashlib.sha256(binary.read_bytes()).hexdigest() != BINARY_SHA256:
        parser.error('comparison requires the verified Serum 2.0.16 binary')
    if args.report.suffix.lower() != '.json':
        parser.error('--report must be a JSON filename')
    if args.input.is_file():
        sources = [args.input] if args.input.suffix.lower() == '.fxp' else []
    else:
        sources = sorted(p for p in args.input.rglob('*')
                         if p.is_file() and p.suffix.lower() == '.fxp' and not p.name.startswith('._'))
    if not sources:
        parser.error('no FXP sources found')
    results = []
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(compare_file, source, plugin=args.plugin,
                               serum_roots=args.serum_root, cache=args.cache) for source in sources]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(f"{len(results)}/{len(sources)}: {result['source']}: "
                  f"{len(result['differences'])} differences; {result['errors']}", flush=True)
            summary = summarize(results)
            write_json(args.report, dict(**summary,
                                                  mode='canonicalized controls', binary_sha256=BINARY_SHA256,
                                                  results=sorted(results, key=lambda r: r['source'])),
                       indent=2)
    print(f"Compared {summary['total']}: {summary['conversion_errors']} conversion errors, "
          f"{summary['patch_mismatches']} patch mismatches. Report: {args.report}", flush=True)
    return int(bool(summary['failed']))


if __name__ == '__main__':
    raise SystemExit(main())
