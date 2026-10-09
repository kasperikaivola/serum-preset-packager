# Serum 2 native FXP import

`native_serum.py` is an optional Windows backend. Offline translation in
`fxp_to_serumpreset.py` is the default. The native path loads one verified
Serum 2.0.16 binary, in its own process, and never attaches it to a DAW.

- SHA-256: `a2fd61e3269edcc693e4a845a72b80247ec939db43bc73f9a192a9f9566e617f`
- Default location: `%CommonProgramFiles%/VST3/Serum2.vst3/Contents/x86_64-win/Serum2.vst3`
- Any other hash is rejected. Pass another file with `--plugin` only when it matches this hash.

The worker calls the private legacy importer at module base + `0x2A57D0`.
That address belongs to this binary only. It is not an Xfer API.

The importer returns intermediate JSON (`serum1ChunkVersion` / `serum1Version`
and migrated modules). Packaging that JSON directly is not a saved preset.
The worker merges it onto a freshly saved processor state, clears
`lockOversampling` and `lockTuning`, and rewrites fields the headless engine
does not finish:

- `mpeEnabled` may arrive as an integer. The engine reader wants a boolean, so the worker keeps the processor's initialized boolean.
- Native JSON booleans occupy one byte. The other seven bytes of the union are padding.
- Wavetable position is quantized with the same rules as the offline converter. The voice-filter cutoff ceiling is applied again.
- The completed state is saved and must reload with status 0 before it is written. A failed reload is not published.

`cli.py import-fxp` and `fxp_to_serumpreset.py` select this path with
`--backend native`. `compare_conversion_backends.py` compares canonical
processor read-back of both backends. It caches references by source hash and
importer revision, and reports metadata, tags, and MPE as ordinary
differences. Neither tool modifies the source FXP or installs the result into
a preset library.

Enum coverage in the offline translator (filters, warps, distortion, unison
stacks, detune modes) was taken from this binary's runtime parameter table.
Legacy files still need the version migrations in the format notes. The raw
FXP selector scale is not the Serum 2 scale.

This backend does not claim byte-identical GUI saves or rendered-audio parity.
Formats the offline parser rejects remain rejected here only when the importer
itself rejects them. See [FXP format](fxp-format.md) for the layouts and
value laws both paths share.
