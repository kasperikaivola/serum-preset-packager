# Serum 1 FXP format

`fxp_to_serumpreset.py` translates these files offline. The clones under
`research/` are local references only. They are not imported at runtime.
`serum1_json.py` is a separate lossless FXP codec: it keeps unknown state
words and can round-trip FXP JSON, including `XfsY` Serum FX files. It does
not invert a Serum 2 migration.

Public notes used while recovering the layout, without copying their code:

- [btesser/serum2vital FORMATS.md](https://github.com/btesser/serum2vital/blob/main/docs/FORMATS.md)
- [0xdevalias Serum notes](https://gist.github.com/0xdevalias/135a18e979ac8e302ebbc700a50a8d74)
- [dafaronbi/Serum-FXP-Python-Reader](https://github.com/dafaronbi/Serum-FXP-Python-Reader)
- [potatoTeto/SerumPresetGenerator](https://github.com/potatoTeto/SerumPresetGenerator)
- [CharlesBT/node-serum2-preset-packager](https://github.com/CharlesBT/node-serum2-preset-packager)

Serum 2 preset containers are the `XferJson` plus zstd-CBOR layout described
in the main README. Numeric laws below are what the current translator writes.

## Container

A Serum preset is `CcnK` / `FPCh` / `XfsX`. The converter rejects every other
plugin id. The big-endian size at offset 4 may be the full file length or the
VST convention (length minus 8). The big-endian chunk size at offset 56 must
be `len(file) - 60`. The payload starts at byte 60:

1. zlib-compressed state
2. optional second zlib stream (embedded wavetables, tuning, then planar noise)
3. a trailing little-endian uint32 equal to the first compressed stream's length

The parser rejects a decompressed stream above 32 MiB.

## State layouts

Exactly four decompressed sizes are accepted: 20,704, 28,232, 33,872, and
172,736 bytes. Anything else raises `ConversionError`.

| | 20,704 | 28,232 | 33,872 and 172,736 |
| --- | --- | --- | --- |
| Parameters | 228 floats at `0x3460` | plus 45 floats at `0x4AE0` | plus 87 floats at `0x4AE0` (through parameter 314) |
| Version float | `0x4994` | same | same |
| Global switches | 18 floats at `0x37F8`; reverb type is the byte at `0x3B04` | 25 floats at `0x4B9C` | 25 floats at `0x4C44`, or `0x4C48` when that alignment matches |
| Modulation | 16 records of 40 bytes from offset 0 | 32 records; slots 17–32 start at `0x50E0` | same as 28,232 |
| LFOs | four curves at `0x0280`; flags at `0x1AE0` | LFOs 5–8 shapes at `0x5558`, flags at `0x6DB8` | 172,736 only: eight blocks at `0x84D8`, stride `0x2D28` |
| Filter menu divisor | 88 | 89, or 95 when the stored indices only fit 95 | 95 |
| Point assignments | none | none | count at `0x8448`; 44-byte records at `0x6E48` |

The bytes after the 18-float early switch block are padding. Do not read them
as the later switch fields. Sub direct output is the float immediately before
the switch block. Noise direct output is switch field 1.

Preset author is 48 bytes at `0x49A0`. Description is 144 bytes at `0x49D0`,
ending where the macro names begin. Each macro label is 32 bytes at `0x4A60`.
The Serum 2 preset name is the FXP filename. Oscillator table paths are 512
bytes at `0x3C08`; the noise path is 512 bytes at `0x4008`.

## Modulation, curves, and assets

Each modulation record is 40 bytes. Source and auxiliary are uint16s at +20.
Destination is the uint16 at +26. Amount and output are floats at +4. The
bipolar flag is the byte at +12. Mode and the auxiliary-invert word are
uint16s at +28. Curve bytes are at +32 and +33. Bytes +34 and +35 must be
`(slot, 0xFF)`. A record with both source and auxiliary zero is inactive, even
if it still holds a stale destination id.

The early layout uses `SOURCES`. Every larger layout uses `EXTENDED_SOURCES`.
Those tables differ. Chaos is LFO 8 or 9 (`Lorenz`, `Rossler`, or `RandomSH`).
Mode 1 inverts the auxiliary. Mode 2 bypasses the slot, and also inverts the
auxiliary when the word at +30 is 1. An inverted auxiliary with source zero
bypasses the slot. Destination 315 (host bypass) is dropped. On a 20,704-byte
state, destination 228 and above is the unassigned sentinel and is skipped.
Unknown sources and destinations are rejected.

FX order at `0x3BE0` is ten positions, not effect type ids. Disabled effects
stay in the rack when a live matrix row, or a legacy MIDI-learn proxy, still
targets them. Proxy bytes start at `0x3840` (248 bytes), with an extended map
at `0x5360`. From file version 0.05, the bypass byte at `0x3976 + 68*effect`
forces that effect off.

Classic LFO curves are 65-point double arrays. On the 172,736-byte layout each
curve block starts with doubles at +8, +`0xF08`, and +`0x1E08`; flags are at
+`0x2D08` and the segment count at +`0x2D10`. Block `0x1B70` holds four remap
graphs (WT A/B, distortion A/B). Their counts are the bytes at `0x33D0`. On
the large layout those remaps are curve blocks 8–11. LFO phase is a point
index converted through that curve's x coordinate. Rise and delay are read
from the timing mirrors (`+0x80` / `+0x70` on classic blocks, `+0x2D28` /
`+0x2D24` on modern blocks), not from parameters 273–288.

Velocity and note response curves are six 17-double arrays at `0x4620`
(velocity first). Counts are the bytes at `0x4950`. From version 0.143 the
float at `0x4958` is cast to a one-byte boolean; only zero is false.

Embedded wavetable byte counts are uint32s at `0x4968`. Samples are contiguous
float32s in the second zlib stream, a multiple of 2,048, and finite.
Interpolation is the byte at `0x4970` and must be 0–4 for embedded tables.
Non-interpolated tables quantize to the real frame count. A single leading
slash in an asset name is the Serum data root. Parent traversal is rejected.
A noise path that is a drive path or a UNC path is kept as an external
reference and is not resolved under `--serum-root`. The same forms are
rejected for wavetables. Missing noise samples stay as relative paths. WAV
`clm ` metadata and AIFF `COMM` / `SSND` supply frame count, channels, rate,
and the interpolation flag. Some factory WAVs overstate the RIFF size by four
bytes; parsing stops at the declared chunks.

Tuning, when present, is the uint32 size at `0x53E0` (name at `0x53E4`) or a
leading `; AnaMark section` in older writers. Noise then follows in planar
float32: channel count and byte length at `0x5540`, loop and boundary at
`0x5530`, detune factor at `0x5550`.

## Value laws

Normalized source values use these Serum 2 scales:

- Master volume: `0.8541468079 * n**3`
- Oscillator and noise volume, and oscillator detune: `n**2`
- Envelope attack, hold, decay, and release: `32 * n**5` seconds. Sustain stays linear.
- Portamento: `8 * n**5` seconds
- LFO rate: `100 * n**4`. Effect rate: `20 * n**4` Hz
- Voice-filter cutoff stays normalized, capped at `0.9997483491897583`
- FX-filter cutoff: `0.935672514619883 * n`
- EQ frequency: `(44100/2048) * (20000/(44100/2048))**n`
- Chorus filter `50 * 400**n` Hz, delay filter `40 * 450**n` Hz, chorus depth `26 * n**2` ms
- Distortion bandwidth: `.075 + 7.5 * n**2`. Compressor attack `.1 + 999.9 * n**2` ms, release `.1 + 999.9 * float32(0.9991 * n)**2` ms, makeup `1 + 30 * n**2`
- Compressor ratio: `1 / (1 - n + n/1_000_000)`. The default legacy ratio is left as Serum 2's compatibility default.
- Diode 1 drive: `12.5 + 0.875 * oldDrive`
- Hall delay `250 * n**2` ms. Plate predelay `.25 * n**2` seconds.
- Octave menu: `floor(8*n + 0.5) - 4`. Octave modulation output is scaled by 8/9, with a fixed-source compensation slot when the rounded octave differs.
- Semitone modulation output is scaled by 24/25. The base control is `kParamPitch`.
- Unison range is the rounded 0–48 menu. Warp menu is `round(n * 23)`. Quantize warp of exactly zero is written as `0.0001`.
- Filter Mix versus Level is switch field 19. Level mode sets Wet to 100 and `LevelOut = 0.5 * sqrt(n49)`. Modulation into parameter 49 still targets Wet.

Version gates, compared as float32 values:

- Below 0.139, legato is cleared when mono is off.
- At 0.154 and above, initial phase 360 with random phase 100 selects per-voice phase memory and keeps the 17 stored positions at `0x5418`. Older files use contiguous phase memory and reset random phase to zero.
- Distortion selector denominators are 12 before 0.156, 13 before 0.157, and 15 from 0.157.
- Free-running, unsynchronized LFOs are anchored. Envelope and retrigger modes keep the stored anchor flag.
- Extended layouts write global tuning as `430 + 20*n` Hz from the first switch mirror.

Warp, distortion, sub shape, unison stack, detune mode, and filter names are
the tables at the top of `fxp_to_serumpreset.py`. An index that is not in
those tables is an error. The same is true of an effect that is enabled but
has no verified parameter map.

## What this does not establish

External wavetables still need their files and interpolation metadata at
playback. State acceptance and processor read-back are not a rendered-audio
comparison. Hand-saved Serum presets omit defaults and include playback
fields, so a strict JSON diff against those saves is a different check from
the native-importer comparison. Both workflows are in the README.

The optional Windows backend is pinned to one Serum 2.0.16 binary. See
[native import](serum2-native-import.md).
