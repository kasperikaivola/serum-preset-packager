# serum-preset-packager

Tools for [Serum 2](https://xferrecords.com/products/serum-2) `.SerumPreset` files and Serum 1 `.fxp` presets.

- Translate supported Serum 1 FXPs into Serum 2 presets, offline or through one pinned Serum 2.0.16 binary.
- Unpack and pack Serum 2 presets as JSON, and round-trip Serum 1 FXPs through a separate JSON schema.
- Copy Serum 1 and Serum 2 presets into genre and group folders.

> [!TIP]
> Looking for a `.SerumPreset` editor and organizer? [See preset.tools](https://preset.tools)<br/>
> It's based on this project and offers an easy to use web-based editor for preset metadata.

## Install

```shell
pip install -r requirements.txt
```

Python dependencies are `cbor2` and `zstandard`. Tests also use `pytest` and `syrupy`.

## Convert Serum 1 FXPs to Serum 2

`fxp_to_serumpreset.py` translates supported Serum 1 presets into `.SerumPreset` files. The default `offline` backend is Python only. It does not load Serum, a VST, or any other executable.

```shell
# One preset. The output defaults to the same folder.
python fxp_to_serumpreset.py MyPreset.fxp MyPreset.SerumPreset

# A bank, preserving subfolders.
python fxp_to_serumpreset.py "Serum 1 bank" "Serum 2 bank" --recursive

# Beside each source, and copy failures into test/fixtures.
python fxp_to_serumpreset.py "G:/My Serum Library" --recursive --in-place --collect-failures

# Match folders whose names contain psy, at any depth.
python fxp_to_serumpreset.py "G:/AudioAssets/**/*psy*" --recursive --in-place

# Read external wavetable and noise metadata, and also write module JSON.
python fxp_to_serumpreset.py "Serum 1 bank" "Serum 2 bank" -r --json --serum-root "C:/Users/me/Documents/Xfer/Serum Presets"
```

The full path of each FXP must contain `serum`, case-insensitively. Other FXPs are ignored and are not collected as failures. This applies to a single file as well as a directory scan. Original FXPs are never modified.

`--in-place` writes `<name>_serum2.serumpreset` beside each FXP and cannot be combined with an output path. Existing outputs are skipped unless `--overwrite` is set. Batch conversion continues after a failure, prints a summary, and exits with status 1 if any file fails.

Quoted patterns support `*`, `?`, and `[abc]`. A `**` segment matches any number of directories when `--recursive` is set. Each FXP is converted once even if several patterns hit it. An explicit output folder keeps the relative path under the pattern's fixed prefix.

`--serum-root` may be repeated. The default search is `~/Documents/Xfer/Serum 2 Presets`, then `~/Documents/Xfer/Serum Presets`. Each folder must contain `Tables/` and `Noises/`. Embedded wavetables, AnaMark tuning, and embedded noise are copied into the preset. External samples stay external. Missing noise files produce a warning and keep their path. A missing external wavetable is an error.

`--collect-failures` copies each failed FXP into `test/fixtures/` by default, or into the directory given after the option. The folder name is `<name>__<content-hash>` and includes `conversion-error.txt`. Reruns reuse an identical collected file and leave a native `.SerumPreset` in that folder alone. The collection directory is excluded from the scan. `checked_` and `pair_` folders are reused instead of created again.

`--json` writes Serum 2 module JSON beside each preset. The same translation for one file is:

```shell
python cli.py import-fxp MyPreset.fxp MyPreset_serum2.json
python cli.py pack MyPreset_serum2.json MyPreset.SerumPreset
```

`import-fxp` refuses to overwrite. Its JSON is the migrated Serum 2 document. Packing it does not recreate the original FXP. Use `unpack` for that.

### Native backend (Windows)

`--backend native` runs the Serum 2.0.16 importer in a separate process. It requires 64-bit Python on Windows and a binary whose SHA-256 is `a2fd61e3269edcc693e4a845a72b80247ec939db43bc73f9a192a9f9566e617f`. Any other binary is rejected. `--plugin` selects another copy of that same file.

```shell
python fxp_to_serumpreset.py MyPreset.fxp Native.SerumPreset --backend native
python cli.py import-fxp MyPreset.fxp Native.json --backend native
python compare_conversion_backends.py test/fixtures --jobs 3
```

`compare_conversion_backends.py` reads both results back through the processor. It caches references in `research/native-reference-cache` and writes `research/backend-comparison.json`. Metadata, tags, and MPE stay visible as differences. The offline backend never falls back to Serum. Processor agreement is not a rendered-audio comparison.

Details of the private importer are in [docs/serum2-native-import.md](docs/serum2-native-import.md).

### Compatibility

Supported decompressed state sizes are **20,704**, **28,232**, **33,872**, and **172,736** bytes. The translator covers oscillators, filters, envelopes, eight LFOs, Chaos, macros, the modulation matrix, embedded wavetables, tuning, noise, and the verified effect modules. Unknown layouts, warp modes, and modulation destinations raise an error.

**Compatibility is experimental.** Audition converted presets before replacing a bank. Limits and value laws are in [docs/fxp-format.md](docs/fxp-format.md).

## Unpack, pack, and edit

`cli.py` reads and writes the Serum 2 container, and a separate Serum 1 schema.

```shell
python cli.py unpack MyPreset.SerumPreset MyPreset.json
python cli.py unpack MyPreset.fxp MyPreset_serum1.json
python cli.py pack MyPreset.json MyPreset.SerumPreset
python cli.py pack MyPreset_serum1.json Rebuilt.fxp

# Every matching file in a folder. Add --recursive for subfolders.
python cli.py unpack ./presets ./json
python cli.py pack ./json ./presets --recursive
```

A single file using the Serum 2 container can be unpacked even when the extension is something else Serum 2 writes, such as `.XferArpBank`. Folder mode selects `.SerumPreset` files, plus `.fxp` when unpacking, and `.json` when packing.

When the output path is omitted, the new file is written beside the input. An FXP unpacked that way is named `<name>_serum1.json`, so it does not collide with Serum 2 JSON. Packing Serum 1 JSON requires a `.fxp` destination. Packing Serum 2 module JSON as `.fxp` is rejected.

Serum 1 JSON has `"format": "Serum1FXP"`. Header fields and unknown state stay in integer arrays. Known controls are in `data.normalizedParameters`; `null` keeps the original bits. Repacking preserves the decompressed contents. Compression can still change the file bytes. This schema does not reverse a Serum 2 preset into an FXP.

Both commands accept a file or a folder. An output folder keeps the input's relative paths. **Existing outputs are overwritten.** A batch continues after a bad file and exits with status 1 if any file fails or nothing matched. Pack input must be JSON from `unpack` or `import-fxp`.

`edit` unpacks one preset, opens it in `$EDITOR` (`vi` when unset), and packs it back when the editor exits.

```shell
python cli.py edit MyPreset.SerumPreset
```

## Sort presets

`sort_serum_presets.py` copies presets into `<folder>/sorted/<genre>/<group>`. Sources are not modified. The first matching genre wins, then the first matching group. Strings live in `sort_serum_presets.json`.

Matching uses the folder path, filename, internal name, author, description, and tags. A match of one or two characters must be a whole word. A longer match may be a word prefix, so `bass` also matches `basses`.

```shell
python sort_serum_presets.py "G:/My Serum Library" --recursive --dry-run
python sort_serum_presets.py "G:/My Serum Library" --serum2 --recursive
python sort_serum_presets.py "G:/My Serum Library" --serum1 --serum-root "C:/Users/me/Documents/Xfer/Serum Presets"
```

With neither `--serum1` nor `--serum2`, both formats are included. Referenced wavetables and noise samples are copied into the same genre/group folder when they can be resolved. Presets that match no genre are still copied under `sorted/unsorted/` and listed in `unsorted-presets.txt`. The command exits with status 1 when a copy fails or a referenced sample is missing.

## File format

Serum 2 preset files use this container:

1. `XferJson\0`, a little-endian uint32 JSON length, a zero uint32, then the metadata JSON.
2. A little-endian uint32 CBOR length, a uint32 `2`, then a zstandard frame of the CBOR payload.

`cli.py` unpacks that payload to JSON and packs the same shape back. Valid module fields are whatever Serum writes. The migrated subset produced from FXPs is described in [docs/fxp-format.md](docs/fxp-format.md).

Serum 1 FXPs are VST2 chunk presets (`CcnK` / `FPCh` / `XfsX`). `serum1_json.py` also accepts the Serum FX id `XfsY`. The converter does not.

Thanks to [@0xdevalias](https://github.com/0xdevalias) for [the gist](https://gist.github.com/0xdevalias/5a06349b376d01b2a76ad27a86b08c1b) that started the Serum 2 container work.

## Fixture workflow

These commands maintain native comparison pairs under `test/fixtures/`. They are for extending the converter.

```shell
python mark_fixture_pairs.py
python mark_fixture_pairs.py --watch 5
python mark_fixture_pairs.py --checked "pair_My Preset__contenthash"
python test_fixture_conversions.py --verbose
python clean_unconverted_fixtures.py           # preview
python clean_unconverted_fixtures.py --delete
python clean_test_fixtures.py --dry-run
python clean_test_fixtures.py
```

`pair_` means a folder has an FXP and a hand-saved `.SerumPreset`. `checked_` means that pair was investigated and incorporated. `--checked` renames only the folder you name. A passing comparison does not rename it. Preset bytes stay unchanged.

`test_fixture_conversions.py` writes `test_<name>.SerumPreset` beside each fixture FXP and compares it with the hand-saved preset. Passing outputs are deleted. Failures remain, and the report defaults to `test/fixtures/conversion-test-report.json`. Tolerances are `--abs-tol 0.00001` and `--rel-tol 0.000001`. `--controls-only` drops UI and playback fields. Hand-saved presets omit defaults, so this report is a different check from `compare_conversion_backends.py`.

`clean_unconverted_fixtures.py` previews immediate fixture folders that contain no hand-saved `.SerumPreset`. `--delete` removes those folders. `clean_test_fixtures.py` removes generated `test_*` files and leaves folders in place. Neither script follows a linked directory.

`make test` runs the suite. `make mark-pairs`, `make test-fixtures`, and `make compare-backends` run the three fixture commands above.

## Related projects

- [preset.tools](https://preset.tools) is a web editor for `.SerumPreset` metadata.
- [node-serum2-preset-packager](https://github.com/CharlesBT/node-serum2-preset-packager) is a TypeScript packer based on this container research.
- [serum2vital FORMATS.md](https://github.com/btesser/serum2vital/blob/main/docs/FORMATS.md) documents Serum 1 offsets used while checking this converter. Its code is not included here.
