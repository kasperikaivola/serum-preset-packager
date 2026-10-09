#!/usr/bin/env python3
import argparse, sys, json, struct, pathlib, cbor2, zstandard as zstd, os, tempfile, subprocess

MAGIC = b"XferJson\x00"

def unpack(src: pathlib.Path, dst: pathlib.Path):
    if src.suffix.lower() == '.fxp':
        from serum1_json import unpack_fxp
        dst.write_text(json.dumps(unpack_fxp(src), ensure_ascii=False, indent=2), encoding='utf-8')
        return
    buf = src.read_bytes()
    off = len(MAGIC)
    jlen, _ = struct.unpack_from("<II", buf, off); off += 8
    meta = json.loads(buf[off:off + jlen]); off += jlen
    clen, _ = struct.unpack_from("<II", buf, off); off += 8
    cbor = zstd.ZstdDecompressor().decompress(buf[off:])
    assert len(cbor) == clen
    dst.write_text(json.dumps({"metadata": meta, "data": cbor2.loads(cbor)}, ensure_ascii=False, indent=2), encoding='utf-8')

def pack(src: pathlib.Path, dst: pathlib.Path):
    obj = json.loads(src.read_text(encoding='utf-8'))
    if obj.get('nativeImportStage') == 'intermediate':
        raise ValueError('raw native import JSON must pass engine loading before it can be packed')
    if obj.get('format') == 'Serum1FXP':
        from serum1_json import pack_fxp
        if dst.suffix.lower() != '.fxp':
            raise ValueError('Serum 1 JSON output must end in .fxp')
        dst.write_bytes(pack_fxp(obj))
        return
    if dst.suffix.lower() == '.fxp':
        raise ValueError('Serum 2 module JSON cannot be packed as a Serum 1 FXP')
    m = json.dumps(obj["metadata"], separators=(",", ":")).encode()
    c = cbor2.dumps(obj["data"])
    z = zstd.ZstdCompressor(level=3).compress(c)
    out = bytearray()
    out += MAGIC
    out += struct.pack("<II", len(m), 0)
    out += m
    out += struct.pack("<II", len(c), 2)
    out += z
    dst.write_bytes(out)

def edit(preset: pathlib.Path):
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        tmp_path = pathlib.Path(tmp.name)

    try:
        # Unpack preset to temporary JSON file
        unpack(preset, tmp_path)

        # Open editor
        subprocess.run([editor, str(tmp_path)], check=True)

        # Pack modified JSON back to preset
        pack(tmp_path, preset)
    finally:
        # Clean up temporary file
        tmp_path.unlink(missing_ok=True)

def main(argv=None):
    parser = argparse.ArgumentParser(description="Unpack and pack Serum 1 and Serum 2 presets.")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("unpack", "pack"):
        subparser = commands.add_parser(command)
        subparser.add_argument("input", type=pathlib.Path, help="Input file or folder")
        subparser.add_argument("output", type=pathlib.Path, nargs="?",
                               help="Output file, or output folder for batch processing")
        subparser.add_argument("-r", "--recursive", action="store_true",
                               help="Include all subfolders when processing a folder")
    edit_parser = commands.add_parser("edit")
    edit_parser.add_argument("input", type=pathlib.Path)
    import_parser = commands.add_parser('import-fxp', help='Translate a Serum 1 FXP into Serum 2 module JSON')
    import_parser.add_argument('input', type=pathlib.Path)
    import_parser.add_argument('output', type=pathlib.Path, nargs='?')
    import_parser.add_argument('--serum-root', action='append', type=pathlib.Path,
                               help='optional Serum data folder for external asset metadata')
    import_parser.add_argument('--backend', choices=('offline', 'native'), default='offline')
    import_parser.add_argument('--plugin', type=pathlib.Path, help='verified Serum 2.0.16 binary; native backend only')
    args = parser.parse_args(argv)

    if not args.input.exists():
        parser.error(f"Input does not exist: {args.input}")
    if args.command == 'import-fxp':
        from fxp_to_serumpreset import translate_fxp, encode_preset
        try:
            dst = args.output or args.input.with_name(args.input.stem + '_serum2.json')
            if dst.resolve() == args.input.resolve() or dst.exists():
                raise ValueError('output must be a new file distinct from the source')
            if args.backend == 'native':
                from native_serum import native_import
                obj = native_import(args.input, plugin=args.plugin)
            else:
                if args.plugin is not None:
                    raise ValueError('--plugin requires --backend native')
                obj = translate_fxp(args.input, serum_roots=args.serum_root)
            encode_preset(obj)  # Include the same payload hash as direct conversion.
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'{args.input} -> {dst}')
            return 0
        except Exception as error:
            print(f'Failed: {args.input}: {error}', file=sys.stderr)
            return 1
    if args.command == "edit":
        if not args.input.is_file():
            parser.error("edit requires a file")
        edit(args.input)
        return 0

    convert = unpack if args.command == "unpack" else pack
    input_suffix = ".serumpreset" if args.command == "unpack" else ".json"
    output_suffix = ".json" if args.command == "unpack" else ".SerumPreset"
    batch = args.input.is_dir()
    if batch:
        if args.output is not None and args.output.exists() and not args.output.is_dir():
            parser.error("Batch output must be a folder")
        candidates = args.input.rglob("*") if args.recursive else args.input.iterdir()
        sources = sorted(path for path in candidates
                         if path.is_file() and (path.suffix.lower() == input_suffix or
                             (args.command == 'unpack' and path.suffix.lower() == '.fxp')))
        if not sources:
            print(f"No {input_suffix} files found in {args.input}", file=sys.stderr)
            return 1
    else:
        sources = [args.input]

    failed = 0
    for src in sources:
        current_suffix = output_suffix
        if args.command == 'pack':
            try:
                if json.loads(src.read_text(encoding='utf-8')).get('format') == 'Serum1FXP':
                    current_suffix = '.fxp'
            except Exception:
                pass  # Report malformed input in the per-file conversion below.
        if batch:
            dst = ((args.output / src.relative_to(args.input)).with_suffix(current_suffix)
                   if args.output is not None else src.with_suffix(current_suffix))
        else:
            dst = args.output if args.output is not None else src.with_suffix(current_suffix)
        if args.command == 'unpack' and src.suffix.lower() == '.fxp' and (batch or args.output is None):
            # A comparison pair often has both formats with the same stem.
            dst = dst.with_name(src.stem + '_serum1.json')
        try:
            if src.resolve() == dst.resolve():
                raise ValueError("Input and output must be different files")
            dst.parent.mkdir(parents=True, exist_ok=True)
            convert(src, dst)
            print(f"{src} -> {dst}")
        except Exception as exc:
            failed += 1
            print(f"Failed: {src}: {exc}", file=sys.stderr)
    if batch:
        print(f"Converted {len(sources) - failed} file(s); {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
