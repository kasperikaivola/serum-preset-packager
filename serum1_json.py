"""Lossless, editable Serum 1 FXP container codec (no plugin required).

Unknown state fields are retained as little-endian uint32 words. Parameters
are exposed separately as floats; packing applies these over the retained words.
"""
from pathlib import Path
import math
import struct
import zlib

FORMAT = "Serum1FXP"
LIMIT = 32 * 1024 * 1024


def _inflate(payload):
    decoder = zlib.decompressobj()
    data = decoder.decompress(payload, LIMIT + 1)
    if len(data) > LIMIT or not decoder.eof:
        raise ValueError("truncated or oversized Serum zlib stream")
    return data, decoder.unused_data, len(payload) - len(decoder.unused_data)


def _words(data):
    if len(data) % 4:
        raise ValueError("legacy state is not aligned to uint32 words")
    return list(struct.unpack(f"<{len(data) // 4}I", data))


def _bytes(words):
    if not isinstance(words, list) or len(words) > LIMIT // 4:
        raise ValueError("invalid/oversized state words")
    if any(type(n) is not int or not 0 <= n <= 0xffffffff for n in words):
        raise ValueError("state words must be uint32 integers")
    return bytearray(struct.pack(f"<{len(words)}I", *words))


def unpack_fxp(path):
    raw = Path(path).read_bytes()
    if len(raw) < 60 or raw[:4] != b"CcnK" or raw[8:12] != b"FPCh" or raw[16:20] not in (b"XfsX", b"XfsY"):
        raise ValueError("expected a Serum VST2 chunk preset")
    size, = struct.unpack_from(">I", raw, 4)
    length, = struct.unpack_from(">I", raw, 56)
    if size not in (len(raw), len(raw) - 8) or length != len(raw) - 60:
        raise ValueError("FXP length fields do not match the file")
    state, tail, compressed_length = _inflate(raw[60:])
    wavetable = b""
    if len(tail) > 4:
        wavetable, tail, _ = _inflate(tail)
    if tail not in (b"", struct.pack("<I", compressed_length)):
        raise ValueError("unknown Serum chunk trailer")
    parameters = []
    if len(state) in (20704, 28232, 33872, 172736):
        parameters = list(struct.unpack_from("<228f", state, 0x3460))
        if len(state) != 20704:
            count = 45 if len(state) == 28232 else 87
            parameters.extend(struct.unpack_from(f"<{count}f", state, 0x4ae0))
        # Uninitialized legacy fields may contain NaNs. Null keeps their bits.
        parameters = [n if math.isfinite(n) else None for n in parameters]
    return dict(format=FORMAT, schemaVersion=1, headerBytes=list(raw[:60]),
                byteSizeIncludesHeader=size == len(raw),
                data=dict(stateWords=_words(state), wavetableWords=_words(wavetable[:len(wavetable)//4*4]),
                          wavetableTailBytes=list(wavetable[len(wavetable)//4*4:]),
                          compressedLengthTrailer=bool(tail), normalizedParameters=parameters))


def pack_fxp(obj):
    if obj.get("format") != FORMAT or obj.get("schemaVersion") != 1:
        raise ValueError("unsupported Serum 1 JSON schema")
    header_values = obj["headerBytes"]
    if not isinstance(header_values, list) or len(header_values) != 60 or any(type(n) is not int or not 0 <= n <= 255 for n in header_values):
        raise ValueError("headerBytes must contain 60 byte values")
    header = bytearray(header_values)
    if header[:4] != b"CcnK" or header[8:12] != b"FPCh" or header[16:20] not in (b"XfsX", b"XfsY"):
        raise ValueError("invalid Serum FXP header")
    data = obj["data"]
    if type(obj['byteSizeIncludesHeader']) is not bool or type(data['compressedLengthTrailer']) is not bool:
        raise ValueError('container convention flags must be booleans')
    state = _bytes(data["stateWords"])
    parameters = data.get("normalizedParameters", [])
    count = {20704: 228, 28232: 273, 33872: 315, 172736: 315}.get(len(state), 0)
    if not isinstance(parameters, list) or len(parameters) != count:
        raise ValueError("normalized parameter count does not match the state layout")
    for i, n in enumerate(parameters):
        if n is None:
            continue
        if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n):
            raise ValueError("parameters must be finite numbers or null to retain original bits")
        offset = 0x3460 + 4 * i if i < 228 else 0x4ae0 + 4 * (i - 228)
        # Preserve signed zero and NaN bit patterns when the JSON value is unchanged.
        old, = struct.unpack_from("<f", state, offset)
        if old != n:
            struct.pack_into("<f", state, offset, n)
    first = zlib.compress(state)
    wavetable = _bytes(data["wavetableWords"])
    tail = data.get('wavetableTailBytes', [])
    if not isinstance(tail, list) or len(tail) > 3 or any(type(n) is not int or not 0 <= n <= 255 for n in tail):
        raise ValueError('wavetableTailBytes must contain at most three bytes')
    wavetable.extend(tail)
    payload = first + (zlib.compress(wavetable) if wavetable else b"")
    if data["compressedLengthTrailer"]:
        # Empty wavetable streams are present in real presets too.
        if not wavetable:
            payload += zlib.compress(b"")
        payload += struct.pack("<I", len(first))
    struct.pack_into(">I", header, 4, 60 + len(payload) - (0 if obj["byteSizeIncludesHeader"] else 8))
    struct.pack_into(">I", header, 56, len(payload))
    return bytes(header) + payload
