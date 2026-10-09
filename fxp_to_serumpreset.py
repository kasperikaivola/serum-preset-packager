#!/usr/bin/env python3
"""Offline batch conversion of classic Serum 1 FXP presets to Serum 2.

Format observations: docs/fxp-format.md. Known unsupported layouts/features raise
ConversionError; no dependency on a plugin, DAW, or the research clones.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import os
import re
from pathlib import Path, PurePosixPath
import struct
import sys
import tempfile
import zlib

import cbor2
import zstandard

MAGIC = b"XferJson\0"
MAX_STATE = 32 * 1024 * 1024
# Source IDs in the early four-LFO layout (the supplied 20,704-byte states).
SOURCES = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 7, 7: 8, 8: 9,
           9: 16, 10: 17, 11: 18, 12: 14, 13: 15, 14: 20, 15: 21,
           16: 22, 17: 25, 18: 26, 19: 27, 20: 28, 21: 33, 22: 38}
EXTENDED_SOURCES = {**{i: i for i in range(5)}, **{i: i + 1 for i in range(5, 13)},
                    13: 16, 14: 17, 15: 18, 16: 19, 17: 14, 18: 15, 19: 20,
                    20: 21, 21: 22, 22: 23, 23: 24,
                    **{i: i + 1 for i in range(24, 28)}, 28: 33,
                    29: 34, 30: 35, 31: 36, 32: 37, 33: 38}
FILTERS = (
    'MgL6', 'MgL12', 'MgL18', 'MgL24', 'L6', 'L12',
    'L18', 'L24', 'H6', 'H12', 'H18', 'H24',
    'B12', 'B24', 'P12', 'P24', 'N12', 'N24',
    'LH6', 'LH12', 'LB12', 'LP12', 'LN12', 'HB12',
    'HP12', 'HN12', 'BP12', 'BN12', 'PP12', 'PN12',
    'NN12', 'LBH12', 'LBH24', 'LPH12', 'LPH24', 'LNH12',
    'LNH24', 'BPN12', 'BPN24', 'CombP', 'CombN', 'CombL6P',
    'CombL6N', 'CombH6P', 'CombH6N', 'CombHL6P', 'CombHL6N', 'FlangeP',
    'FlangeN', 'FlangeL6P', 'FlangeL6N', 'FlangeH6P', 'FlangeH6N', 'FlangeHL6P',
    'FlangeHL6N', 'Phase12P', 'Phase12N', 'Phase24P', 'Phase24N', 'Phase36P',
    'Phase36N', 'Phase48P', 'Phase48N', 'Phase48L6P', 'Phase48L6N', 'Phase48H6P',
    'Phase48H6N', 'Phase48HL6P', 'Phase48HL6N', 'FlangePhase12HL6P', 'FlangePhase12HL6N', 'LEQ6',
    'LEQ12', 'BEQ12', 'HEQ6', 'HEQ12', 'RM', 'RMT',
    'SNH1', 'SNH2', 'Combs', 'Allpasses', 'Reverb1', 'Scream',
    'ZDF_A', 'ADD_BASS', 'FormantONE', 'FormantTWO', 'FormantTWB', 'BandReject',
    'DistComb1LP', 'DistComb1BP', 'DistComb2LP', 'DistComb2BP', 'Scream3LP', 'Scream3BP',
)
# Verified enum spellings; other warp modes fail instead of changing the sound.
WARPS = {0: "kOff", 1: "kSync", 2: "kSync", 3: "kSync", 4: "kBendPos", 6: "kBendPosNeg", 7: "kPWM",
         8: "kASYMPos", 10: "kASYMPosNeg", 12: "kDLM", 14: "kRemap_2",
         17: "kQuantize", 18: "kPD_OSC", 20: "kRM_OSC", 21: "kFM_NOISE"}
WARPS.update({11: "kFlip", 16: "kRemap_4", 19: "kAM_OSC", 22: "kFM_SUB"})
DISTORTIONS = {0: "kTube", 1: "kSoftClip", 2: "kHardClip", 4: "kDiode2", 5: "kLinFold", 10: "kRectify",
               6: "kSinFold", 7: "kZeroSquare", 8: "kDownsample",
               12: "kXShaperAsym", 15: "kTapeSat"}
DISTORTIONS[13] = "kSineShaper"
# Additional spellings recovered from the native parameter descriptor table.
WARPS.update({5: 'kBendNeg', 9: 'kASYMNeg', 13: 'kRemap_1', 15: 'kRemap_3'})
DISTORTIONS.update({3: 'kDiode1', 9: 'kAsym', 11: 'kXShaper', 14: 'kStompBox'})
UNISON_STACKS = ('kNoUnisonStack', 'kOctave1', 'kOctave2', 'kOctave3',
                'kOctaveFifth1', 'kOctaveFifth2', 'kOctaveFifth3', 'kCenter12', 'kCenter24')
SHAPES = ("kSine", "kRoundRect", "kTriangle", "kSaw", "kSquare", "kPulse")
FX_NAMES = ("FXDistortion", "FXFlanger", "FXPhaser", "FXChorus", "FXDelay",
            "FXComp", "FXReverb", "FXEQ", "FXFilter", "FXHyperD")


class ConversionError(ValueError):
    """Malformed input or a feature whose translation is not yet verified."""


def _text(blob: bytes, offset: int, size: int) -> str:
    # Older Windows presets may contain ANSI names rather than UTF-8.
    raw = blob[offset:offset + size].split(b"\0", 1)[0]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _read_fxp(path: Path) -> tuple[bytes, bytes]:
    raw = path.read_bytes()
    if len(raw) < 60 or raw[:4] != b"CcnK" or raw[8:12] != b"FPCh":
        raise ConversionError("expected a VST2 chunk preset (CcnK/FPCh)")
    if raw[16:20] != b"XfsX":
        raise ConversionError("FXP belongs to another plugin, not Serum")
    size, = struct.unpack_from(">I", raw, 4)
    chunk_size, = struct.unpack_from(">I", raw, 56)
    # Real Serum files use both the VST convention (size minus eight) and size.
    if size not in (len(raw), len(raw) - 8) or chunk_size != len(raw) - 60:
        raise ConversionError("FXP length fields do not match the file (truncated input)")
    try:
        dec = zlib.decompressobj()
        state = dec.decompress(raw[60:], MAX_STATE + 1)
    except zlib.error as exc:
        raise ConversionError(f"invalid Serum zlib state: {exc}") from exc
    if len(state) > MAX_STATE or not dec.eof:
        raise ConversionError("truncated or oversized Serum zlib state")
    if len(state) not in (20704, 28232, 33872, 172736):
        raise ConversionError(f"unsupported Serum 1 state layout ({len(state):,} bytes); "
                              "supported state sizes: 20,704 / 28,232 / 33,872 / 172,736 bytes")
    embedded = b""
    if dec.unused_data:
        tail = zlib.decompressobj()
        try:
            embedded = tail.decompress(dec.unused_data, MAX_STATE + 1)
        except zlib.error as exc:
            raise ConversionError("invalid legacy wavetable trailer") from exc
        if len(embedded) > MAX_STATE or not tail.eof or tail.unused_data != struct.pack("<I", len(raw) - 60 - len(dec.unused_data)):
            raise ConversionError("truncated/oversized wavetable data or an unknown trailer")
    return state, embedded


def read_fxp(path: Path) -> bytes:
    return _read_fxp(Path(path))[0]


def _params(blob: bytes) -> list[float]:
    values = list(struct.unpack_from("<228f", blob, 0x3460))
    if len(blob) != 20704:
        # Older writers leave the remainder of the later parameter block as
        # uninitialized memory. Only read the fields this layout actually owns.
        values.extend(struct.unpack_from("<45f" if len(blob) == 28232 else "<87f", blob, 0x4AE0))
    if any(not math.isfinite(n) or not -1e-6 <= n <= 1.000001 for n in values):
        raise ConversionError("invalid normalized parameter values")
    return [min(1.0, max(0.0, n)) for n in values]


def _filter(n: float, divisor: int = 88) -> str:
    index = round(n * divisor)
    if divisor == 89 and not math.isclose(n * divisor, index, abs_tol=2e-5):
        divisor = 95  # Later Serum writers reused the middle-sized state layout.
        index = round(n * divisor)
    if not math.isclose(n * divisor, index, abs_tol=2e-5):
        raise ConversionError("unrecognized legacy filter index encoding")
    if index == 82 and divisor in (88, 95):
        return "Reverb1"
    if divisor == 88 and index in (83, 86):
        return {83: "Scream", 86: "FormantONE"}[index]
    if divisor == 95 and index == 75:
        return "HEQ12"
    if divisor == 95 and index in (41, 68, 78, 79, 80, 83, 84, 87):
        return {41: "CombL6P", 68: "Phase48HL6N", 78: "SNH1", 79: "SNH2", 80: "Combs",
                83: "Scream", 84: "ZDF_A", 87: "FormantTWO"}[index]
    if index >= len(FILTERS):
        raise ConversionError(f"filter model {index} is not yet verified")
    return FILTERS[index]


def _plain(**values):
    return {"plainParams": values or "default"}


def _curve(blob: bytes, base: int, index: int, count: int) -> dict:
    if not 1 <= count < 65:
        raise ConversionError(f"invalid curve segment count: {count}")
    arrays = [list(struct.unpack_from(f"<{count + 1}d", blob, base + 520 * (index + 4 * a)))
              for a in range(3)]
    return _checked_curve(arrays, count)


def _checked_curve(arrays: list[list[float]], count: int) -> dict:
    curves, xs, ys = arrays
    if any(not math.isfinite(v) or not 0 <= v <= 1 for array in arrays for v in array):
        raise ConversionError("invalid legacy curve coordinates")
    if xs != sorted(xs) or not math.isclose(xs[0], 0, abs_tol=1e-7) or not math.isclose(xs[-1], 1, abs_tol=1e-7):
        raise ConversionError("invalid legacy curve endpoints")
    return dict(curveVals=curves, xVals=xs, yVals=ys, numPoints=count)


def _new_curve(blob: bytes, index: int) -> dict:
    base = 0x84D8 + index * 0x2D28
    count, = struct.unpack_from("<I", blob, base + 0x2D10)
    if not 1 <= count < 480:
        raise ConversionError(f"invalid new-layout curve segment count: {count}")
    return _checked_curve([list(struct.unpack_from(f"<{count + 1}d", blob, base + offset))
                           for offset in (8, 0xF08, 0x1E08)], count)


def _f32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def _settings(blob: bytes) -> list[float]:
    early = len(blob) == 20704
    bases = (0x37F8,) if early else ((0x4B9C,) if len(blob) == 28232 else (0x4C44, 0x4C48))
    for base in bases:
        # The early binary mirror owns 18 floats; the following bytes are padding.
        values = list(struct.unpack_from("<18f" if early else "<25f", blob, base))
        if early:
            values += [0.0] * 7
            if blob[0x3B04] not in (0, 1):
                raise ConversionError("invalid legacy reverb switch")
            values[23] = float(blob[0x3B04])
        # Sub direct output precedes the switch block; Noise direct is field 1.
        values.append(struct.unpack_from("<f", blob, base - 4)[0])
        if math.isfinite(values[0]) and 0 <= values[0] <= 1 and values[8] in (0, .5, 1) and values[12] in (0, 1):
            if any(not math.isfinite(values[i]) or not 0 <= values[i] <= 1
                   for i in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 23, 25)):
                continue  # Try the other known switch-block alignment.
            if any(values[i] not in (0, 1) for i in (1, 4, 5, 6, 7, 9, 10, 16, 17, 18, 19, 20, 21, 23, 25)):
                continue  # Try the other known switch-block alignment.
            if any(not math.isclose(values[i]*4, round(values[i]*4), abs_tol=2e-5) for i in (2, 3)):
                continue  # Try the other known switch-block alignment.
            # The native key-track switch tests nonzero, not a 0.5 threshold.
            values[13] = float(values[13] > 0)
            if abs(values[11] * 31 - round(values[11] * 31)) > 1e-5:
                continue  # Try the other known switch-block alignment.
            return values
    raise ConversionError("unrecognized legacy global switch block")


def _mod_records(blob: bytes) -> list[tuple[int, int, int, int, int]]:
    records = []
    for slot in range(16 if len(blob) == 20704 else 32):
        base = slot * 40 if slot < 16 else 0x50E0 + (slot - 16) * 40
        source, auxiliary = struct.unpack_from("<2H", blob, base + 20)
        if not source and not auxiliary:
            continue  # Inactive slots can retain stale UI record identifiers.
        if blob[base + 34:base + 36] != bytes((slot, 0xFF)):
            raise ConversionError(f"invalid modulation record {slot + 1}")
        destination, = struct.unpack_from("<H", blob, base + 26)
        records.append((slot, base, source, auxiliary, destination))
    return records


def _wavetable(blob: bytes, embedded: bytes, osc: int, roots: list[Path]) -> dict:
    name = _text(blob, 0x3C08 + osc * 512, 512).replace("\\", "/")
    sizes = struct.unpack_from("<2I", blob, 0x4968)
    size = sizes[osc]
    if size:
        start = sum(sizes[:osc])
        if size % (2048 * 4) or start + size > len(embedded):
            raise ConversionError(f"invalid/truncated embedded wavetable {osc + 1}")
        samples = list(struct.unpack_from(f"<{size // 4}f", embedded, start))
        if any(not math.isfinite(v) for v in samples):
            raise ConversionError("non-finite embedded wavetable samples")
        interpolation = blob[0x4970 + osc]
        if interpolation not in (0, 1, 2, 3, 4):
            raise ConversionError("unsupported embedded wavetable interpolation mode")
        result = dict(embeddedWTData=samples, interpolateAfterLoad=interpolation)
        if name:
            result.update(relativePathToWT=name, tableDisplayName=PurePosixPath(name).stem)
        return result
    if not name:
        return {}  # Native import preserves empty references, including active oscillators.
    return _asset(name, "Tables", roots, wavetable=True)


def _lfo(blob: bytes, params: list[float], index: int) -> dict:
    if len(blob) == 172736:
        base = 0x84D8 + index * 0x2D28
        curve = _new_curve(blob, index)
        flags = list(blob[base + 0x2D08:base + 0x2D0E])
        length, = struct.unpack_from("<I", blob, base + 0x2D18)
        phase_point, = struct.unpack_from("<i", blob, base + 0x2D14)
        # Native import reads timing mirrors even when automation parameters
        # 273..288 remain zero in the saved FXP.
        rise, = struct.unpack_from("<f", blob, base + 0x2D28)
        delay, = struct.unpack_from("<f", blob, base + 0x2D24)
    else:
        base = 0x1AE0 if index < 4 else 0x6DB8
        offset = index % 4
        curve = _curve(blob, 0x280 if index < 4 else 0x5558, offset, blob[base + offset])
        flags = [blob[base + field + offset] for field in (0x14, 0x18, 0x1C, 0x20, 0x24, 0x28)]
        length, = struct.unpack_from("<I", blob, base + 0x50 + 4 * offset)
        phase_point, = struct.unpack_from("<i", blob, base + 0x40 + 4 * offset)
        rise, = struct.unpack_from("<f", blob, base + 0x80 + 4 * offset)
        delay, = struct.unpack_from("<f", blob, base + 0x70 + 4 * offset)
    if any(flag not in (0, 1) for flag in flags):
        raise ConversionError("invalid LFO switch bytes")
    anchor, hz, dot, trip, retrigger, envelope = flags
    if any(not math.isfinite(v) or not 0 <= v <= 1 for v in (rise, delay)):
        raise ConversionError("invalid legacy LFO rise/delay values")
    rate = 61 + index if index < 4 else 260 + index - 4
    smooth = 223 + index if index < 4 else 264 + index - 4
    smoothing = params[smooth]
    if len(blob) != 172736 and index >= 4:
        smoothing, = struct.unpack_from("<f", blob, base + 0x60 + 4 * offset)
    if len(blob) == 172736 and index >= 4:
        smoothing, = struct.unpack_from("<f", blob, base + 0x2D20)
        if not math.isfinite(smoothing) or not 0 <= smoothing <= 1:
            raise ConversionError("invalid modern LFO smoothing mirror")
    pp = dict(kParamRate=100 * params[rate]**4, kParamSmooth=100 * smoothing,
              kParamBeatSync=float(not hz), kParamDotted=float(dot), kParamTriplets=float(trip),
              kParamDefaultMode=0.0 if envelope or not retrigger else 1.0,
              kParamAnchored=float(anchor or (hz and not retrigger and not envelope)),
              kParamRise=4 * rise, kParamDelay=4 * delay)
    if envelope or not retrigger:
        pp["kParamMode"] = "Envelope" if envelope else "Free"
    if phase_point >= 0:
        if phase_point > curve["numPoints"]:
            raise ConversionError("invalid LFO phase start point")
        pp["kParamPhase"] = 360 * curve["xVals"][phase_point]
    curve["loopbackPointNum"] = min(481, length)
    return dict(curveData=curve, pathData={}, plainParams=pp)


def _scalars(blob: bytes) -> dict:
    result = {}
    for i, key in enumerate(("velo", "note")):
        count = blob[0x4950 + i]
        if not 1 <= count <= 16:
            raise ConversionError("invalid note/velocity response curve count")
        curve = _checked_curve([list(struct.unpack_from(f"<{count + 1}d", blob,
            0x4620 + 136 * (i + 2 * array))) for array in range(3)], count)
        legato, = struct.unpack_from("<f", blob, 0x4958 + 4 * i)
        # The native importer casts this mirror to bool, including stale NaNs
        # in early writers. Only zero means false.
        version, = struct.unpack_from("<f", blob, 0x4994)
        curve["legato"] = version >= _f32(.143) and bool(legato)
        result[key] = curve
    return result


def _embedded_noise(blob: bytes, embedded: bytes, offset: int) -> dict:
    if len(blob) == 20704:
        if offset != len(embedded):
            raise ConversionError("unknown data after the embedded wavetable samples")
        return {}
    channels, size = struct.unpack_from("<2I", blob, 0x5540)
    if not size:
        if offset != len(embedded):
            raise ConversionError("unknown data after the embedded wavetable samples")
        return {}
    if channels not in (1, 2) or size % (4 * channels) or offset + size != len(embedded):
        raise ConversionError("invalid/truncated embedded noise data")
    frames = size // (4 * channels)
    samples = [list(struct.unpack_from(f"<{frames}f", embedded, offset + channel * frames * 4))
               for channel in range(channels)]
    if any(not math.isfinite(value) for channel in samples for value in channel):
        raise ConversionError("non-finite embedded noise samples")
    loop, boundary = struct.unpack_from("<2Q", blob, 0x5530)
    detune, = struct.unpack_from("<d", blob, 0x5550)
    if not math.isfinite(detune):
        raise ConversionError("invalid embedded noise detune factor")
    return dict(embeddedNoiseData=samples, loopback64=loop, boundary64=boundary,
                detuneFactor=detune)


def _point_assignments(blob: bytes) -> list[dict]:
    count, = struct.unpack_from("<I", blob, 0x8448)
    if count > 128:
        raise ConversionError("invalid LFO point assignment count")
    assignments = []
    for i in range(count):
        lfo, point, target, destination = struct.unpack_from("<4I", blob, 0x6E48 + i * 44)
        bus = destination - 147
        points = (_new_curve(blob, lfo)["numPoints"] if len(blob) == 172736 and lfo < 8
                  else blob[(0x1AE0 if lfo < 4 else 0x6DB8) + lfo % 4] if lfo < 8 else -1)
        if lfo >= 8 or bus not in range(16) or point > points:
            raise ConversionError("invalid legacy LFO point assignment")
        assignments.append(dict(busID=bus, lfoID=lfo, lfoType=0, pointID=point, target=min(target, 3)))
    return assignments


def _wav_info(path: Path, *, wavetable: bool = False) -> dict:
    """Read WAV/AIFF descriptors without copying their external sample data."""
    with path.open("rb") as f:
        file_size = os.fstat(f.fileno()).st_size
        header = f.read(12)
        if header[:4] == b"FORM":
            return _aiff_info(f, path, header, file_size)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise ConversionError(f"unsupported WAV asset: {path}")
        riff_end = 8 + struct.unpack_from('<I', header, 4)[0]
        if riff_end == file_size + 4:
            riff_end = file_size  # Serum's factory table writer overcounts RIFF by four bytes.
        if not 12 <= riff_end <= file_size:
            raise ConversionError(f'truncated WAV asset: {path}')
        fmt = None
        data_size = None
        interpolation = None
        while f.tell() < riff_end:
            chunk = f.read(min(8, riff_end - f.tell()))
            if len(chunk) != 8:
                raise ConversionError(f"truncated WAV asset: {path}")
            tag, size = struct.unpack("<4sI", chunk)
            if f.tell() + size > riff_end:
                raise ConversionError(f"truncated WAV asset: {path}")
            if tag == b"fmt ":
                content = f.read(min(size, 40))
                if len(content) < 16:
                    raise ConversionError(f"invalid WAV format: {path}")
                _, channels, rate, _, align, _ = struct.unpack_from("<HHIIHH", content)
                fmt = channels, rate, align
                f.seek(size - len(content), 1)
            elif tag == b"data":
                data_size = size
                f.seek(size, 1)
            elif tag == b"clm " and wavetable:
                content = f.read(min(size, 160))
                f.seek(size - len(content), 1)
                match = re.match(rb"<!>(\d+)\s+([0-4])", content)
                if match:
                    if not 0 < int(match[1]) <= 1048576:
                        raise ConversionError(f"invalid wavetable cycle size: {path}")
                    interpolation = int(match[2])
            else:
                f.seek(size, 1)
            if size & 1:
                f.seek(1, 1)
        if fmt is None or data_size is None or not all(fmt) or data_size % fmt[2]:
            raise ConversionError(f"WAV asset has no valid format/data: {path}")
        info = dict(numChannels=fmt[0], sampleRate=fmt[1], numFrames=data_size // fmt[2])
        if wavetable and interpolation is not None:
            info["_legacyInterpolation"] = interpolation
        return info


def _aiff_info(f, path: Path, header: bytes, file_size: int) -> dict:
    if len(header) != 12 or header[8:] not in (b"AIFF", b"AIFC"):
        raise ConversionError(f"unsupported AIFF asset: {path}")
    form_end = 8 + struct.unpack_from(">I", header, 4)[0]
    if not 12 <= form_end <= file_size:
        raise ConversionError(f"truncated AIFF asset: {path}")
    info = None
    sound_size = None
    while f.tell() < form_end:
        chunk = f.read(8)
        if len(chunk) != 8:
            raise ConversionError(f"truncated AIFF chunk: {path}")
        tag, size = struct.unpack(">4sI", chunk)
        end = f.tell() + size
        if end + (size & 1) > form_end:
            raise ConversionError(f"truncated AIFF chunk: {path}")
        if tag == b"COMM":
            content = f.read(min(size, 22))
            if len(content) < (22 if header[8:] == b"AIFC" else 18):
                raise ConversionError(f"invalid AIFF format: {path}")
            channels, frames, bits, exponent, significand = struct.unpack_from(">HIHHQ", content)
            if not channels or not frames or not 1 <= bits <= 64 or not 16383 <= exponent <= 16414:
                raise ConversionError(f"invalid AIFF sample format: {path}")
            rate = math.ldexp(significand, exponent - 16383 - 63)
            if not 1 <= rate <= 2**32 - 1 or not math.isclose(rate, round(rate), abs_tol=1e-4, rel_tol=0):
                raise ConversionError(f"invalid AIFF sample rate: {path}")
            if header[8:] == b"AIFC" and content[18:22] not in (b"NONE", b"sowt", b"fl32", b"FL32", b"fl64", b"FL64"):
                raise ConversionError(f"compressed AIFF asset is not supported: {path}")
            info = dict(numChannels=channels, numFrames=frames, sampleRate=round(rate))
            expected_bytes = frames * channels * ((bits + 7) // 8)
        elif tag == b"SSND":
            if size < 8:
                raise ConversionError(f"invalid AIFF sound chunk: {path}")
            offset, _ = struct.unpack(">II", f.read(8))
            sound_size = size - 8 - offset
            if sound_size < 0:
                raise ConversionError(f"invalid AIFF sound offset: {path}")
        f.seek(end + (size & 1))
    if info is None or sound_size is None or sound_size < expected_bytes:
        raise ConversionError(f"AIFF asset has no valid format/data: {path}")
    return info


def default_roots() -> list[Path]:
    documents = Path.home() / "Documents" / "Xfer"
    return [documents / "Serum 2 Presets", documents / "Serum Presets"]


def _asset(name: str, kind: str, roots: list[Path], *, wavetable: bool = False) -> dict:
    if not name:
        raise ConversionError(f"empty {kind} asset reference; embedded/built-in tables need native conversion")
    name = name.replace("\\", "/")
    if kind == "Noises" and (name.startswith("//") or ":" in name):
        # Absolute sample references from another machine remain usable there;
        # never resolve them beneath a supplied asset root.
        return dict(relativePathToNoiseSample=name, pathToNoiseSample=name)
    if name.startswith("//"):
        raise ConversionError(f"asset reference must be relative to the Serum data folder: {name}")
    # A single leading slash denotes the Serum data root, not the OS root.
    relative = PurePosixPath(name.lstrip("/"))
    if relative.is_absolute() or ".." in relative.parts or ":" in name:
        raise ConversionError(f"asset reference must be relative to the Serum data folder: {name}")
    key = "relativePathToWT" if kind == "Tables" else "relativePathToNoiseSample"
    for root in roots:
        asset = root / kind / Path(*relative.parts)
        if asset.is_file():
            return {key: name, **_wav_info(asset, wavetable=wavetable)}
    if kind == "Noises":
        # Native saves also retain unresolved noise references without descriptors.
        print(f"Warning: missing noise asset '{relative}'; keeping its external reference", file=sys.stderr)
        return {key: name}
    raise ConversionError(f"missing {kind} asset '{relative}'; supply --serum-root with the "
                          "Serum 1/2 data folder containing it")


# Parameter index -> (module type, instance ID, parameter name, local param ID).
# The local IDs matter: Serum uses them as well as the readable names in routes.
DESTINATIONS = {}


def _dest(index: int, module: str, instance: int, name: str, local: int):
    DESTINATIONS[index] = (module, instance, name, local)


for oscillator, offset in enumerate((1, 14)):
    for delta, name, local in ((0, "Volume", 1), (1, "Pan", 2), (2, "Octave", 3),
                               (3, "Pitch", 4), (4, "Fine", 5), (5, "Unison", 24),
                               (6, "Detune", 26), (7, "DetuneWid", 27),
                               (9, "CoarsePit", 6)):
        _dest(offset + delta, "Oscillator", oscillator, "kParam" + name, local)
    for delta, name, local in ((8, "Warp", 0), (10, "TablePos", 6),
                               (11, "RandomPhase", 9), (12, "InitialPhase", 8)):
        _dest(offset + delta, "WTOsc", oscillator, "kParam" + name, local)
    _dest(172 + oscillator, "Oscillator", oscillator, "kParamUnisonStereo", 28)
    _dest(174 + oscillator, "Oscillator", oscillator, "kParamUnisonWarp", 31)
    _dest(176 + oscillator, "WTOsc", oscillator, "kParamUnisonWTPos", 7)
for i, name, local in ((27, "Volume", 1), (30, "Pan", 2), (33, "Volume", 1), (34, "Pan", 2)):
    _dest(i, "Oscillator", 3 if i < 33 else 4, "kParam" + name, local)
for i, name, local in ((28, "Color", 0), (29, "Fine", 1), (31, "RandomPhase", 3), (32, "InitialPhase", 2)):
    _dest(i, "NoiseOsc", 3, "kParam" + name, local)
for env, offset in enumerate((35, 51, 56)):
    for delta, name in enumerate(("Attack", "Hold", "Decay", "Sustain", "Release")):
        _dest(offset + delta, "Env", env, "kParam" + name, delta)
    for delta in range(3):
        _dest(71 + 3 * env + delta, "Env", env, f"kParamCurve{delta + 1}", 5 + delta)
for i, name, local in ((45, "Freq", 3), (46, "Reso", 4), (47, "Drive", 5),
                      (48, "Var", 6), (49, "Wet", 1), (50, "Stereo", 7)):
    _dest(i, "VoiceFilter", 0, "kParam" + name, local)
for lfo in range(4):
    _dest(61 + lfo, "LFO", lfo, "kParamRate", 0)
    _dest(223 + lfo, "LFO", lfo, "kParamSmooth", 1)
    _dest(260 + lfo, "LFO", 4 + lfo, "kParamRate", 0)
    _dest(264 + lfo, "LFO", 4 + lfo, "kParamSmooth", 1)
for lfo in range(8):
    _dest(273 + lfo, "LFO", lfo, "kParamRise", 2)
    _dest(281 + lfo, "LFO", lfo, "kParamDelay", 3)
for i in range(2):
    _dest(69 + i, "LFO", 8 + i, "kParamRate", 0)
for i, name, local in ((0, "MasterVolume", 0), (65, "PortamentoTime", 3),
                      (66, "PortamentoCurve", 4), (80, "MasterTuning", 1), (222, "VoiceAmp", 2)):
    _dest(i, "Global", 0, "kParam" + name, local)
for macro in range(4):
    _dest(218 + macro, "Macro", macro, "kParamValue", 0)
for bus in range(16):
    _dest(299 + bus, "LFOPointModBus", bus, "kParamValue", 0)
for slot in range(16, 32):
    _dest(228 + (slot - 16) * 2, "ModSlot", slot, "kParamAmount", 0)
    _dest(229 + (slot - 16) * 2, "ModSlot", slot, "kParamOut", 1)


# Enabled effects supported by the paired fixtures. Each tuple is
# (legacy parameter index, Serum 2 name, Serum 2 local parameter ID, transform).
FX_PARAMS = {
    0: [(96, "Wet", 1, "percent"), (97, "Drive", 2, "percent"), (98, "LPHP", 3, "percent"),
        (100, "Freq", 5, "identity"), (101, "BW", 6, "distbw")],
    1: [(103, "Wet", 1, "percent"), (104, "BeatSync", 3, "identity"),
        (105, "Rate", 2, "fxrate"), (106, "Depth", 4, "percent"),
        (107, "Feedback", 5, "percent"), (108, "Width", 6, "degrees")],
    2: [(109, "Wet", 1, "percent"), (110, "BeatSync", 2, "identity"),
        (111, "Rate", 3, "fxrate"), (112, "Depth", 4, "percent"),
        (113, "Freq", 6, "phaserfreq"), (114, "Feedback", 7, "percent"),
        (115, "Width", 8, "degrees")],
    3: [(116, "Wet", 1, "percent"), (117, "BeatSync", 2, "identity"),
        (118, "Rate", 2, "fxrate"), (119, "Delay", 4, "chorusdelay"),
        (120, "Delay2", 5, "chorusdelay2"), (121, "Depth", 6, "chorusdepth"),
        (122, "Feedback", 7, "chorusfeedback"), (123, "Filt", 8, "chorusfilter")],
    4: [(124, "Wet", 1, "percent"), (125, "Freq", 2, "delayfilter"),
        (126, "BW", 11, "bandwidth"), (127, "BeatSync", 4, "identity"),
        (128, "Link", 3, "identity"), (129, "TimeL", 4, "delaytime"),
        (130, "TimeR", 5, "delaytime"), (131, "Mode", 8, "delaymode"),
        (132, "Feedback", 9, "percent"), (133, "OffsetL", 6, "delayoffset"),
        (134, "OffsetR", 7, "delayoffset")],
    5: [(135, "Thresh", 2, "identity"), (136, "Ratio", 3, "compratio"),
        (137, "Attack", 4, "compattack"), (138, "Release", 5, "comprelease"),
        (139, "Makeup", 6, "compmakeup"), (140, "Multiband", 7, "identity"),
        (269, "Wet", 1, "percent"), (270, "ThreshUD0", 14, "bandgain"),
        (271, "ThreshUD1", 15, "bandgain"), (272, "ThreshUD2", 16, "bandgain")],
    6: [(81, "Wet", 1, "percent"), (82, "Size", 2, "percent"),
        (83, "Delay", 3, "reverbdelay"), (84, "Freq", 4, "percent"),
        (85, "Feedback", 5, "percent"), (86, "FreqB", 6, "percent"),
        (87, "Width", 7, "percent")],
    7: [(88, "Freq1", 1, "eqfrequency"), (89, "Freq2", 2, "eqfrequency"),
        (90, "Reso1", 3, "percent"), (91, "Reso2", 4, "percent"),
        (92, "Gain1", 5, "eqgain"), (93, "Gain2", 6, "eqgain"),
        (94, "Type1", 7, "eqtype"), (95, "Type2", 8, "eqtype")],
    8: [(141, "Wet", 1, "percent"), (143, "Freq", 3, "fxcutoff"),
        (144, "Reso", 4, "percent"), (145, "Drive", 5, "percent"),
        (146, "Var", 6, "percent"), (268, "Stereo", 7, "percent")],
    # Local IDs recovered from Serum 2's runtime descriptor table.
    9: [(147, "Wet", 1, "percent"), (148, "Rate", 2, "percent"),
        (149, "Detune", 3, "percent"), (150, "Unison", 4, "hyperunison"),
        (152, "DimESize", 7, "percent"), (153, "DimEWet", 8, "percent")],
}

for effect, local in enumerate((8, 7, 9, 9, 12, 11, 10)):
    FX_PARAMS[effect].append((289 + effect, "LevelOut", local, "identity"))
FX_PARAMS[8].append((297, "LevelOut", 8, "identity"))
FX_PARAMS[9].extend(((296, "DimELevelOut", 9, "identity"), (298, "LevelOut", 6, "identity")))


def _fx_type(index: int) -> int | None:
    ranges = ((81, 88, 6), (88, 96, 7), (96, 103, 0), (103, 109, 1),
              (109, 116, 2), (116, 124, 3), (124, 135, 4), (135, 141, 5),
              (141, 147, 8), (147, 154, 9), (269, 273, 5))
    for start, end, effect in ranges:
        if start <= index < end:
            return effect
    if 154 <= index < 164:
        return index - 154
    if 289 <= index <= 298:
        return (0, 1, 2, 3, 4, 5, 6, 9, 8, 9)[index - 289]
    return 8 if index == 268 else None


def _retained_effects(blob: bytes) -> set[int]:
    # Native pruning inspects even inactive matrix destinations and retains FX
    # referenced by legacy MIDI learn proxies. These affect rack instance IDs.
    effects = set()
    for slot in range(16 if len(blob) == 20704 else 32):
        base = slot * 40 if slot < 16 else 0x50E0 + (slot - 16) * 40
        source, auxiliary = struct.unpack_from("<2H", blob, base + 20)
        if not source and not auxiliary:
            continue  # Native migration resets both-empty slots to an invalid destination.
        effect = _fx_type(struct.unpack_from("<H", blob, base + 26)[0])
        if effect is not None:
            effects.add(effect)
    blocks = [(0, blob[0x3840:0x3840 + 248])]
    if len(blob) != 20704:
        blocks.append((248, blob[0x5360:0x5360 + 37]))
    for start, block in blocks:
        for offset, cc in enumerate(block):
            if cc in (0, 255):
                continue
            if cc >= 128:
                break  # Native abandons this map; previously created proxies remain.
            effect = _fx_type(start + offset)
            if effect is not None:
                effects.add(effect)
    return effects


def _fx_value(kind: str, n: float) -> float:
    return {
        "identity": lambda: n, "percent": lambda: 100 * n,
        "degrees": lambda: 360 * n, "fxrate": lambda: 20 * n**4,
        "eqfrequency": lambda: (44100 / 2048) * (20000 / (44100 / 2048))**n,
        "chorusfilter": lambda: 50 * 400**n,
        "delayfilter": lambda: 40 * 450**n, "bandwidth": lambda: .75 + 7.5 * n,
        "chorusdelay": lambda: 20 * n**2,
        "chorusdelay2": lambda: 20 * n**2,
        "chorusdepth": lambda: 26 * n**2,
        "chorusfeedback": lambda: 95 * n,
        "delaytime": lambda: .001 + .5 * n**4,
        "delaymode": lambda: float(round(2 * n)),
        "delayoffset": lambda: .5 + n,
        "reverbdelay": lambda: 0.0,  # Old Plate uses a separate predelay mapping.
        "distbw": lambda: .075 + 7.5 * n**2,
        "phaserfreq": lambda: 20 * 900**n,
        "compratio": lambda: 1 / (1 - n + n / 1_000_000),
        "compattack": lambda: .1 + 999.9 * n**2,
        "comprelease": lambda: .1 + 999.9 * _f32(n * .9991)**2,
        "compmakeup": lambda: 1 + 30 * n**2,
        "bandgain": lambda: 200 * n,
        "pan": lambda: 100 * n - 50,
        # The legacy FX filter and Serum 2 use different normalized ranges.
        # The same scale is established by all three independent new pairs.
        "fxcutoff": lambda: .935672514619883 * n,
        "eqgain": lambda: 48 * n - 24,
        "eqtype": lambda: float(round(2 * n)),
        "hyperunison": lambda: 7 * n,
    }[kind]()


def _table_position(blob, n, offset, wt, warp_index, middle_modern, osc, asset_interpolation=None):
    """Native GUI wavetable position quantization established by paired fixtures."""
    position = (n[offset + 10] if len(blob) == 172736 else
                _f32(round(255 * n[offset + 10]) / 255))
    tables = None
    if ("embeddedWTData" in wt and wt["interpolateAfterLoad"] == 0
            and (len(blob) != 172736 or warp_index not in (1, 2, 3, 14, 18, 22))):
        tables = len(wt["embeddedWTData"]) // 2048
    elif ("numFrames" in wt and len(blob) != 172736
          and (middle_modern or blob[0x4970 + osc] in (0, 1, 255))):
        tables = wt["numFrames"] // 2048
    if len(blob) == 172736 and warp_index not in (14, 18, 22):
        if "embeddedWTData" in wt:
            if wt["interpolateAfterLoad"] in (1, 4):
                tables = 256
        elif "numFrames" in wt:
            interpolate = (asset_interpolation if asset_interpolation is not None else
                           (0 if blob[0x4970 + osc] == 0 else 1))
            tables = 256 if interpolate else wt["numFrames"] // 2048
    if tables is not None:
        position = (_f32(min(tables - 1, math.floor(n[offset + 10] * tables)) / (tables - 1))
                    if tables > 1 else 0.0)
    return position


def translate_fxp(path: Path, serum_roots: list[Path] | None = None) -> dict:
    """Return the same metadata/data JSON shape as cli.unpack()."""
    blob, embedded = _read_fxp(Path(path))
    version, = struct.unpack_from("<f", blob, 0x4994)
    n = _params(blob)
    settings = _settings(blob)
    extended = len(blob) != 20704
    records = _mod_records(blob)
    sources = EXTENDED_SOURCES if extended else SOURCES
    filter_divisor = {20704: 88, 28232: 89, 33872: 95, 172736: 95}[len(blob)]
    middle_modern = len(blob) == 28232 and any(
        math.isclose(value * 95, round(value * 95), abs_tol=2e-5)
        and not math.isclose(value * 89, round(value * 89), abs_tol=2e-5)
        for value in (n[44], n[142]))
    roots = default_roots() if serum_roots is None else serum_roots
    # Native import uses the FXP filename. Internal names are often stale or
    # abbreviated, and the newer presets can have names longer than 32 bytes.
    name = Path(path).stem
    author = _text(blob, 0x49A0, 48)
    description = _text(blob, 0x49D0, 144)
    table_bytes = sum(struct.unpack_from("<2I", blob, 0x4968))
    # The second stream contains tables, optional tuning, then planar noise.
    # A tuning marker cannot delimit it: noise samples can follow the text.
    tuning_size = struct.unpack_from("<I", blob, 0x53E0)[0] if len(blob) != 20704 else 0
    if tuning_size:
        if tuning_size > 32767 or table_bytes + tuning_size > len(embedded):
            raise ConversionError("invalid/truncated embedded tuning")
        tuning = embedded[table_bytes:table_bytes + tuning_size]
    elif embedded[table_bytes:].startswith(b"; AnaMark section"):
        tuning = embedded[table_bytes:]  # Earlier tuning writers lack the size mirror.
    else:
        tuning = b""
    noise_data = _embedded_noise(blob, embedded, table_bytes + len(tuning))
    tags = (["Custom-Tuning"] if tuning else []) + (["Wavetable"] if n[212] or n[213] else [])
    if table_bytes or noise_data:
        tags.append("Embedded-Data")
    tags.append("Mono" if settings and settings[4] else "Poly")
    meta = dict(fileType="SerumPreset", presetName=name, presetAuthor=author,
                presetDescription=description, product="Serum2", productVersion="2.0.16",
                tags=tags, url="https://xferrecords.com/",
                vendor="Xfer Records", version=6.0)
    data = dict(meta, mpeEnabled=True, mpeConfig=0, mpePitchBendRange=48,
                lockTuning=False, lockOversampling=False)
    if tuning:
        tuning_name = _text(blob, 0x53E4, 352) if tuning_size else "(special)"
        data.update(tuningData=list(tuning), tuningName=tuning_name or "(special)")
    # Initialize absent Serum 2 modules explicitly; this prevents stale modules
    # from an earlier preset when the generated file is loaded into a used instance.
    for prefix, count in (("Env", 4), ("LFO", 10), ("Macro", 8), ("ModSlot", 64),
                          ("RoutingSlot", 7), ("LFOPointModBus", 16), ("VoiceFilter", 2)):
        for i in range(count):
            data[f"{prefix}{i}"] = _plain()
    for i in range(3):
        data[f"FXRack{i}"] = dict(FX=[], displayName="", plainParams="default")
    for prefix in ("ArpClip", "MidiClip"):
        for i in range(12):
            data[f"{prefix}{i}"] = dict(clip={}, plainParams="default")
    for module in ("Arp0", "ClipPlayer0", "RetriggerState0", "VoicePanel0", "PitchQuantizer0"):
        data[module] = _plain()
    data["Global0"] = _plain(kParamMasterVolume=.8541468079 * n[0]**3,
                             kParamMasterTuning=n[80], kParamVoiceAmp=n[222],
                             kParamPortamentoTime=8 * n[65]**5,
                             kParamPortamentoCurve=200 * n[66] - 100,
                             kParamBendRangeUp=float(round(48 * n[166] - 24)),
                             kParamBendRangeDn=float(round(48 * n[167] - 24)),
                             kParamOversampling=float(round(2 * settings[8])) if settings else 2.0,
                             kParamPolyCount=float(round(settings[11] * 31) + 1) if settings else 16.0,
                             kParamLimitSameNotePolyphony=1.0, kParamS1Compatibility=1.0)
    data["Global0"]["plainParams"]["kParamModWheel"] = 100 * n[217]
    if len(blob) in (33872, 172736):
        for i in range(16):
            data[f"LFOPointModBus{i}"] = _plain(kParamValue=100 * n[299 + i])
        assignments = _point_assignments(blob)
        if assignments:
            data["lfoPointModAssignments"] = assignments
    if settings:
        data["Global0"]["plainParams"].update(kParamMonoToggle=settings[4],
            kParamLegato=0.0 if version < _f32(.139) and not settings[4] else settings[5],
            kParamPortaAlways=settings[6], kParamPortaScaled=settings[7])
        if extended:
            data["Global0"]["plainParams"]["kParamGlobalTuning"] = 430 + 20 * settings[0]
    for osc, offset in enumerate((1, 14)):
        params = dict(kParamEnable=n[212 + osc], kParamVolume=n[offset]**2,
                      kParamPan=100 * n[offset + 1] - 50,
                      kParamOctave=float(math.floor(8 * n[offset + 2] + .5) - 4),
                      kParamPitch=float(round(24 * n[offset + 3] - 12)),
                      kParamFine=200 * n[offset + 4] - 100,
                      kParamUnison=float(round(15 * n[offset + 5]) + 1),
                      kParamDetune=n[offset + 6]**2, kParamDetuneWid=100 * n[offset + 7],
                      kParamCoarsePit=128 * n[offset + 9] - 64,
                      kParamPitchTrack=n[164 + osc])
        if settings:
            unison_range = float(math.floor(48 * settings[14 + osc] + .5))
            if abs(unison_range - 2) > 1e-5:
                params["kParamUnisonRange"] = unison_range
        params["kParamUnisonStereo"] = 100 * n[172 + osc]
        detune_modes = ('kDetuneLinear', 'kDetuneSuper', 'kDetuneExp', 'kDetuneInv', 'kDetuneRandom')
        detune_mode = round(settings[2 + osc] * 4)
        if detune_mode:
            params['kParamDetuneMode'] = detune_modes[detune_mode]
        stack = round(n[178 + osc] * 8)
        if not math.isclose(n[178 + osc] * 8, stack, abs_tol=2e-5):
            raise ConversionError('unrecognized legacy unison stack encoding')
        if stack:
            params['kParamUnisonStack'] = UNISON_STACKS[stack]
        params["kParamUnisonWarp"] = 200 * n[174 + osc] - 100
        warp_index = round(n[168 + osc] * 23)
        if warp_index not in WARPS:
            raise ConversionError(f"oscillator {osc + 1} uses an unsupported warp algorithm (mode {warp_index})")
        wt = _wavetable(blob, embedded, osc, roots)
        asset_interpolation = wt.pop("_legacyInterpolation", None)
        position = _table_position(blob, n, offset, wt, warp_index, middle_modern, osc, asset_interpolation)
        wt["plainParams"] = dict(kParamWarpMenu=WARPS[warp_index],
                                 kParamWarp=.0001 if warp_index == 17 and n[offset + 8] == 0 else n[offset + 8],
                                 kParamTablePos=1 + 255 * position,
                                 kParamRandomPhase=100 * n[offset + 11],
                                 kParamInitialPhase=360 * n[offset + 12])
        if abs(n[176 + osc] - .5) > 1e-6:
            wt["plainParams"]["kParamUnisonWTPos"] = 200 * n[176 + osc] - 100
        if warp_index in (2, 3):
            wt["plainParams"]["kParamWarpVar"] = .5 if warp_index == 2 else 1.0
        if n[offset + 12] == 1:
            per_voice = version >= _f32(.154) and n[offset + 11] == 1
            wt["plainParams"]["kParamPhaseMemory"] = "kPerVoice" if per_voice else "kContiguous"
            if per_voice:
                wt["storedPhasePos"] = list(struct.unpack_from("<17Q", blob, 0x5418 + osc * 136))
            elif n[offset + 11] == 1:
                wt["plainParams"]["kParamRandomPhase"] = 0.0
        wt["flex"] = (_new_curve(blob, 8 + osc) if len(blob) == 172736 else
                      _curve(blob, 0x1B70, osc, blob[0x33D0 + osc]))
        oscillator = _plain(**params)
        for prefix in ("GranularOsc", "MultiSampleOsc", "SampleOsc", "SpectralOsc"):
            oscillator[f"{prefix}{osc}"] = _plain()
        oscillator[f"WTOsc{osc}"] = wt
        data[f"Oscillator{osc}"] = oscillator
    data["Oscillator2"] = _plain(kParamEnable=0.0)
    for prefix in ("GranularOsc", "MultiSampleOsc", "SampleOsc", "SpectralOsc"):
        data["Oscillator2"][f"{prefix}2"] = _plain()
    data["Oscillator2"]["WTOsc2"] = dict(flex={}, numChannels=1, numFrames=18432,
        sampleRate=44100, relativePathToWT="S2 Tables/Default Shapes.wav", plainParams="default")
    for osc, index in ((0, 40), (1, 41), (3, 42), (4, 43)):
        direct = (osc == 3 and settings[1]) or (osc == 4 and settings[25])
        data[f"RoutingSlot{osc}"] = _plain(kParamRoutingDest="kRoutingDestDirect" if direct else
            "kRoutingDestFilter" if n[index] >= .5 else "kRoutingDestMaster")
    noise_path = _text(blob, 0x4008, 512)
    noise_name = noise_path.replace("\\", "/")
    noise = (dict(relativePathToNoiseSample=noise_name, pathToNoiseSample=noise_path,
                  **noise_data) if noise_data else _asset(noise_name, "Noises", roots))
    noise["plainParams"] = dict(kParamColor=n[28], kParamFine=2 * n[29] - 1,
                                kParamRandomPhase=100 * n[31], kParamInitialPhase=100 * n[32])
    if settings:
        noise["plainParams"]["kParamOneShot"] = settings[9]
    noise_pitch_track = settings[10] if settings else struct.unpack_from("<I", blob, 0x4208)[0]
    if noise_pitch_track not in (0, 1):
        raise ConversionError("unrecognized legacy noise pitch-track switch")
    data["Oscillator3"] = dict(NoiseOsc3=noise, plainParams=dict(
        kParamEnable=n[214], kParamVolume=n[27]**2, kParamPan=100 * n[30] - 50,
        kParamPitchTrack=float(noise_pitch_track)))
    shape = round(n[170] * 5)
    if shape not in range(6):
        raise ConversionError(f"sub oscillator shape {shape} is not yet verified")
    data["Oscillator4"] = dict(SubOsc4=_plain(kParamShape=SHAPES[shape]), plainParams=dict(
        kParamEnable=n[215], kParamVolume=n[33]**2, kParamPan=100 * n[34] - 50,
        kParamOctave=float(round(8 * n[171] - 4))))
    if n[13] == 1:
        # Remembering oscillator A's phase also makes the legacy sub continuous.
        data["Oscillator4"]["SubOsc4"]["plainParams"]["kParamContiguousPhase"] = 1.0
    for env, offset in enumerate((35, 51, 56)):
        data[f"Env{env}"] = _plain(**{
            "kParam" + key: (n[offset + j] if j == 3 else 32 * n[offset + j]**5)
            for j, key in enumerate(("Attack", "Hold", "Decay", "Sustain", "Release"))},
            **{f"kParamCurve{j + 1}": 100 * n[71 + 3 * env + j] for j in range(3)})
    data["Env3"] = _plain(kParamCurve1=50.0, kParamCurve2=66.6, kParamCurve3=66.6)
    data["VoiceFilter0"] = _plain(kParamEnable=n[216], kParamType=_filter(n[44], filter_divisor),
                                  kParamFreq=min(n[45], .9997483491897583), kParamReso=100 * n[46],
                                  kParamDrive=100 * n[47], kParamVar=100 * n[48],
                                  kParamWet=100 * n[49], kParamStereo=100 * n[50])
    if settings:
        data["VoiceFilter0"]["plainParams"]["kParamKeyTrack"] = settings[13]
        if settings[19]:
            # The legacy Filter Mix/Level switch changes the base control.
            # Native import retains Wet modulation destinations, even in Level mode.
            data["VoiceFilter0"]["plainParams"].update(
                kParamWet=100.0, kParamLevelOut=.5 * math.sqrt(n[49]))
    for i in range(8 if extended else 4):
        data[f"LFO{i}"] = _lfo(blob, n, i)
    if not extended:
        for i in range(4, 8):
            data[f"LFO{i}"] = dict(curveData={}, pathData={}, plainParams=dict(kParamMode="Free", kParamDefaultMode=0.0))
    for i in range(2):
        data[f"LFO{8 + i}"] = dict(curveData={}, pathData={}, plainParams=dict(
            kParamRate=1000 * n[69 + i]**5 / 10, kParamBeatSync=n[67 + i],
            kParamType="RandomSH" if settings[20 + i] else "Lorenz" if i == 0 else "Rossler", kParamRate10x=1.0,
            kParamDotted=1.0, kParamTriplets=1.0, kParamDefaultMode=0.0))
        data[f"LFO{8 + i}"]["plainParams"]["kParamMono"] = settings[16 + i]
    for i in range(4):
        macro = _plain(kParamValue=100 * n[218 + i])
        label = _text(blob, 0x4A60 + 32 * i, 32)
        if label:
            macro["name"] = label
        data[f"Macro{i}"] = macro
    order = struct.unpack_from("<10i", blob, 0x3BE0)
    if sorted(order) != list(range(10)):
        raise ConversionError("invalid effect chain order")
    destinations = dict(DESTINATIONS)
    effect_positions = {}
    targeted = {destination for _, _, _, _, destination in records}
    retained = _retained_effects(blob)
    for effect in sorted(range(10), key=lambda i: order[i]):
        mapping = FX_PARAMS.get(effect, [])
        required = any(index in targeted for index, _, _, _ in mapping)
        if n[154 + effect] < .5 and not required and effect not in retained:
            continue
        if not mapping:
            raise ConversionError(f"enabled {FX_NAMES[effect]} is not yet verified")
        effect_positions[effect] = len(data["FXRack0"]["FX"])
        pp = {"kParam" + key: _fx_value(kind, n[index]) for index, key, _, kind in mapping if index < len(n)}
        pp["kParamEnable"] = n[154 + effect]
        version, = struct.unpack_from("<f", blob, 0x4994)
        if version >= _f32(.05) and blob[0x3976 + 68 * effect]:
            pp["kParamEnable"] = 0.0
        if effect == 0:
            divisor = 15 if version >= _f32(.157) else 13 if version >= _f32(.156) else 12
            mode = round(n[99] * divisor)
            if mode not in DISTORTIONS or n[102] not in (0, .5, 1):
                raise ConversionError("this distortion mode/filter routing is not yet verified")
            pp["kParamMode"] = DISTORTIONS[mode]
            pp.update(kParamLPHP=100 * n[98], kParamPrePost=2 * n[102])
            if mode == 3:
                # The native migration preserves Diode 1's old drive range.
                pp["kParamDrive"] = 12.5 + .875 * pp["kParamDrive"]
        elif effect == 2:
            depth2, = struct.unpack_from("<f", blob, 0x3B30)
            if not math.isfinite(depth2) or not 0 <= depth2 <= 1:
                raise ConversionError("invalid legacy phaser second depth")
            if depth2 != .5:
                pp["kParamDepth2"] = depth2
        elif effect == 3 and settings[18]:
            pp["kParamFiltMode"] = 1.0
        elif effect == 5:
            pp.update(kParamGain0=4.6, kParamGain2=4.6, kParamRatioBelow=.75,
                      kParamCompensatedWetDry=0.0)
            if abs(n[136] - .75) < 1e-6:
                # The native legacy importer retains Serum 2's default here;
                # compatibility mode and RatioBelow supply the legacy behavior.
                pp.pop("kParamRatio")
        elif effect == 6:
            hall = bool(settings[23]) if settings else bool(blob[0x3B04])
            pp.update(kParamType="kHall" if hall else "kPlate")
            if hall:
                pp["kParamDelay"] = 250 * n[83]**2
            else:
                pp["kParamPreDelay"] = .25 * n[83]**2
        elif effect == 8:
            pp["kParamType"] = _filter(n[142], filter_divisor)
        elif effect == 9:
            pp["kParamRetrig"] = n[151]
        entry = {"type": effect, FX_NAMES[effect]: _plain(**pp)}
        if effect == 0:
            entry["flex"] = ([_new_curve(blob, i) for i in (10, 11)] if len(blob) == 172736 else
                             [_curve(blob, 0x1B70, i, blob[0x33D0 + i]) for i in (2, 3)])
        data["FXRack0"]["FX"].append(entry)
        for index, key, local, _ in mapping:
            if local is not None:
                destinations[index] = (FX_NAMES[effect], effect_positions[effect], "kParam" + key, local)
    octave_targets = set()
    semitone_targets = set()
    for slot, base, source, auxiliary, destination in records:
        if source not in sources or auxiliary not in sources:
            raise ConversionError(f"unknown modulation source in slot {slot + 1}: {source}/{auxiliary}")
        if destination == 315:
            # Native Serum 2 import drops legacy host-bypass modulation.
            print(f"Warning: dropped host-bypass modulation in slot {slot + 1}, matching native import", file=sys.stderr)
            continue
        if len(blob) == 20704 and destination >= 228:
            # Before the extended matrix existed, 228 is the unassigned-target
            # sentinel. The native importer keeps no working destination.
            continue
        if destination not in destinations:
            raise ConversionError(f"unknown modulation destination in slot {slot + 1}: parameter {destination}")
        module, instance, param, local = destinations[destination]
        amount, output = struct.unpack_from("<2f", blob, base + 4)
        if not math.isfinite(amount) or not math.isfinite(output) or not -1 <= amount <= 1 or not 0 <= output <= 1:
            raise ConversionError(f"invalid modulation amount/output in slot {slot + 1}")
        pp = dict(kParamAmount=100 * amount, kParamOut=100 * output,
                  kParamBipolar=float(blob[base + 12] == 1))
        curve_byte = blob[base + 33]
        pp["kParamAuxCurve"] = 100 * (curve_byte - 128) / (128 if curve_byte <= 128 else 127.5)
        input_curve = blob[base + 32]
        if input_curve != 128:
            pp["kParamCurveIn"] = 100 * (input_curve - 128) / (128 if input_curve <= 128 else 127.5)
        mode, auxiliary_inverted = struct.unpack_from("<2H", blob, base + 28)
        # Native switch tests exact words; stale padding must not invent flags.
        if mode == 1 or (mode == 2 and auxiliary_inverted == 1):
            pp["kParamAuxInverted"] = 1.0
        # An inverted auxiliary with no auxiliary source is a legacy bypass.
        if mode == 2 or (auxiliary == 0 and pp.get("kParamAuxInverted")):
            pp["kParamBypass"] = 1.0
        if param == "kParamOctave":
            # Serum 2's destination spans nine octaves; Serum 1 spans eight.
            pp["kParamOut"] *= 8 / 9
            octave_targets.add(instance)
        elif param == "kParamPitch":
            # Legacy semitone modulation spans 24 semitones; Serum 2 spans 25.
            pp["kParamOut"] *= 24 / 25
            if module == "Oscillator" and instance in (0, 1):
                semitone_targets.add(instance)
        data[f"ModSlot{slot}"] = dict(source=[sources[source], sources[auxiliary]],
            destModuleTypeString=module, destModuleID=instance,
            destModuleParamName=param, destModuleParamID=local, plainParams=pp)
    for osc in sorted(octave_targets | semitone_targets):
        offset = (1, 14)[osc] if osc in (0, 1) else None
        if offset is None:
            raise ConversionError("octave modulation for this oscillator is not yet verified")
        if osc in octave_targets:
            old_octave = 8 * n[offset + 2] - 4
            new_octave = data[f"Oscillator{osc}"]["plainParams"]["kParamOctave"]
            compensation = old_octave - new_octave * 9 / 8
            if abs(compensation) > 1e-6:
                free = next(i for i in range(64) if "source" not in data[f"ModSlot{i}"])
                data[f"ModSlot{free}"] = dict(source=[38, 0], destModuleTypeString="Oscillator",
                    destModuleID=osc, destModuleParamName="kParamOctave", destModuleParamID=3,
                    plainParams=dict(kParamAmount=100 * compensation / 9))
        if osc not in semitone_targets:
            continue
        # Match the native float migration between the old 24-step and new
        # 25-step normalized ranges, including its half-step rounding bias.
        semitone = _f32(_f32(24 * n[offset + 3]) + .5)
        compensation = _f32(_f32(semitone * _f32(1 / 25))
                            - _f32(math.floor(semitone) * _f32(1 / 24)))
        if compensation != 0:
            free = next(i for i in range(64) if "source" not in data[f"ModSlot{i}"])
            data[f"ModSlot{free}"] = dict(source=[38, 0], destModuleTypeString="Oscillator",
                destModuleID=osc, destModuleParamName="kParamPitch", destModuleParamID=4,
                plainParams=dict(kParamAmount=100 * compensation))
    data["scalars"] = _scalars(blob)
    return {"metadata": meta, "data": data}


def encode_preset(obj: dict) -> bytes:
    cbor = cbor2.dumps(obj["data"])
    payload = zstandard.ZstdCompressor(level=3).compress(cbor)
    meta = dict(obj["metadata"], hash=hashlib.md5(payload).hexdigest())
    obj["metadata"] = meta
    header = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return MAGIC + struct.pack("<Q", len(header)) + header + struct.pack("<II", len(cbor), 2) + payload


def _write(path: Path, content: bytes, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Publish only complete files. Hard-link creation is atomic and fails when
    # a destination already exists; replacement is atomic when opted into.
    fd, temporary = tempfile.mkstemp(prefix=".serum-convert-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def convert_file(src: Path, dst: Path, *, serum_roots: list[Path] | None = None,
                 overwrite: bool = False, json_output: bool = False,
                 backend: str = "offline", plugin: Path | None = None) -> dict:
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        raise ConversionError("input and output must be different files")
    if dst.suffix.lower() != ".serumpreset":
        raise ConversionError("output filename must end in .SerumPreset")
    if dst.exists() and not overwrite:
        raise FileExistsError(f"output exists: {dst}; use --overwrite to replace it")
    if backend == "offline":
        if plugin is not None:
            raise ConversionError("--plugin requires --backend native")
        obj = translate_fxp(src, serum_roots)
    elif backend == "native":
        from native_serum import native_import
        try:
            obj = native_import(src, plugin=plugin)
        except ValueError as error:
            raise ConversionError(str(error)) from error
    else:
        raise ConversionError(f"unknown conversion backend: {backend}")
    content = encode_preset(obj)
    if json_output:
        sidecar = dst.with_suffix(".json")
        if sidecar.resolve() == src.resolve() or (sidecar.exists() and not overwrite):
            raise FileExistsError(f"JSON output exists: {sidecar}")
        _write(sidecar, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"), overwrite)
    _write(dst, content, overwrite)
    return obj


def _collect_failure(src: Path, directory: Path, error: Exception) -> Path:
    """Keep separate native-pair work folders without replacing their files."""
    content = src.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    folder = directory / f"{src.stem}__{digest[:16]}"
    # Reuse review-stage folders, including legacy folders without a hash.
    for candidate in (directory / f"checked_{folder.name}", directory / f"pair_{folder.name}",
                      directory / f"checked_{src.stem}", directory / f"pair_{src.stem}"):
        existing = candidate / src.name
        if existing.is_file() and existing.read_bytes() == content:
            folder = candidate
            break
    copied = folder / src.name
    try:
        _write(copied, content, overwrite=False)
    except FileExistsError:
        if copied.read_bytes() != content:
            raise ConversionError(f"fixture collision; existing file differs: {copied}")
    note = f"Original: {src.resolve()}\nSHA256: {digest}\nConversion error: {error}\n"
    _write(folder / "conversion-error.txt", note.encode("utf-8"), overwrite=True)
    return copied


def _is_serum_path(path: Path) -> bool:
    return "serum" in str(path.absolute()).casefold()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Experimental offline Serum 1 to Serum 2 converter "
        "for validated four/eight-LFO layouts. See README.md for compatibility limits.")
    parser.add_argument("input", type=Path, help="Serum 1 .fxp file, directory or quoted wildcard pattern; full file path must contain 'serum' (case-insensitive)")
    parser.add_argument("output", type=Path, nargs="?", help="output preset or directory (default: beside input)")
    parser.add_argument("-r", "--recursive", action="store_true", help="include subdirectories and preserve their structure")
    parser.add_argument("--in-place", action="store_true",
                        help="write <name>_serum2.serumpreset beside each FXP; originals are preserved")
    parser.add_argument("--collect-failures", nargs="?", type=Path,
                        const=Path(__file__).resolve().parent / "test" / "fixtures",
                        help="copy failed FXPs and error notes to DIRECTORY (default: repository test/fixtures)")
    parser.add_argument("--serum-root", action="append", type=Path,
                        help="Serum data folder containing Tables/ and Noises/; repeat to search multiple folders")
    parser.add_argument("--overwrite", action="store_true", help="replace existing output files")
    parser.add_argument("--json", action="store_true", help="also write unpacked JSON beside each output")
    parser.add_argument("--backend", choices=("offline", "native"), default="offline",
                        help="offline Python (default), or isolated Serum 2.0.16 importer on Windows")
    parser.add_argument("--plugin", type=Path, help="verified Serum 2 binary path; requires --backend native")
    args = parser.parse_args(argv)
    if args.plugin is not None and args.backend != "native":
        parser.error("--plugin requires --backend native")
    if args.in_place and args.output is not None:
        parser.error("--in-place cannot be combined with an output path")
    wildcard = glob.has_magic(str(args.input)) and not args.input.exists()
    if wildcard:
        sources = sorted({Path(p) for p in glob.glob(str(args.input), recursive=args.recursive)})
        if not sources:
            parser.error(f"input pattern matched no files or directories: {args.input}")
        # Preserve paths beneath the fixed part of the pattern for batch output.
        anchor = Path(args.input.anchor)
        for part in args.input.parts:
            if part == args.input.anchor:
                continue
            if glob.has_magic(part):
                break
            anchor /= part
        if args.output is not None and args.output.exists() and not args.output.is_dir():
            parser.error("wildcard input requires a directory output")
    else:
        sources = [args.input]
    collection = args.collect_failures.resolve() if args.collect_failures is not None else None
    jobs = []
    seen = set()
    for source in sources:
        if collection is not None and source.resolve().is_relative_to(collection):
            if wildcard:
                continue
            parser.error("input must be outside the failure collection directory")
        if source.is_dir():
            files = sorted(p for p in (source.rglob("*") if args.recursive else source.iterdir())
                           if p.is_file() and p.suffix.lower() == ".fxp")
            if args.output is not None and args.output.exists() and not args.output.is_dir():
                parser.error("directory input requires a directory output")
        elif source.is_file() and source.suffix.lower() == ".fxp":
            files = [source]
        elif wildcard:
            continue
        else:
            parser.error("input must be an existing .fxp file or directory")
        for path in files:
            resolved = path.resolve()
            if (resolved in seen or not _is_serum_path(path) or
                    (collection is not None and resolved.is_relative_to(collection))):
                continue
            seen.add(resolved)
            if args.in_place:
                output = path.with_name(path.stem + "_serum2.serumpreset")
            elif wildcard and args.output is not None:
                output = args.output / path.relative_to(anchor).with_suffix(".SerumPreset")
            elif source.is_dir():
                output = (args.output or source) / path.relative_to(source).with_suffix(".SerumPreset")
            else:
                output = args.output or path.with_suffix(".SerumPreset")
                if output.is_dir():
                    output /= path.with_suffix(".SerumPreset").name
            jobs.append((path, output))
    if not jobs:
        print("No .fxp files with 'serum' in their full path found.", file=sys.stderr)
        return 1
    converted = failed = skipped = 0
    for src, dst in jobs:
        try:
            convert_file(src, dst, serum_roots=args.serum_root,
                         overwrite=args.overwrite, json_output=args.json,
                         backend=args.backend, plugin=args.plugin)
            converted += 1
            print(f"Converted: {src} -> {dst}")
        except FileExistsError as exc:
            skipped += 1
            print(f"Skipped: {src}: {exc}", file=sys.stderr)
        except (ConversionError, OSError) as exc:
            failed += 1
            print(f"Failed: {src}: {exc}", file=sys.stderr)
            if collection is not None:
                try:
                    copied = _collect_failure(src, collection, exc)
                    print(f"Collected: {src} -> {copied}")
                except (ConversionError, OSError) as collection_error:
                    print(f"Could not collect {src}: {collection_error}", file=sys.stderr)
    print(f"Converted {converted}, skipped {skipped}, failed {failed}.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
