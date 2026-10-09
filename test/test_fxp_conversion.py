"""Independent Serum-generated references and focused batch safety checks."""
import hashlib
import json
from pathlib import Path
import struct
import zlib

import cbor2
import pytest
import zstandard

from fxp_to_serumpreset import ConversionError, convert_file, main, read_fxp, translate_fxp
import fxp_to_serumpreset as converter

FIXTURES = Path(__file__).parent / "fixtures"
PAIRS = ("Atmos__Temple", "Synth__LFOish", "Bpm Syn 20 - PSYFORSERUM5 Zenhiser",
         "Seq 05 - PSYFORSERUM3 Zenhiser", "SMGP2 - LD Backround Arp",
         "SMGP2 - AR Flag", "SMGP2 - BA Tech House 03", "03 Quick Amp", "11 PWM",
         "26 Ring Zap", "30 Big Bendy", "Atmos__Drony", "Atmos__Hollow",
         "Bass__BasicPsy", "Bass__Kromeish", "Bass - 777", "Lead - Fastfast",
         "Lead - Float", "Lead - Kenny", "Lead - Mangokyu", "Lead - matrïx",
         "Lead - princess bitch", "Lead - Space Cadet", "Organ - Castlë", "Pluck - All Day",
         "Chord - Dream Saw", "Lead - KrankY", "Lead - Türban", "Lead - voicË", "Lead - Wonky")


PAIRS += (
    'Antidote - Arp 01',
    'Antidote - Arp 02',
    'Antidote - Arp 03',
    'Antidote - Arp 04',
    'Antidote - Arp 05',
    'Bass - GëeK',
    'Bass - Vampires',
    'BzZz - K3n',
    'Chord - Middle Child',
    'Keys - Lala',
    'Keys - Poppers',
    'Keys - Velvet',
    'Lead - banihana',
    'Lead - cashcashcashbitch',
    'Lead - Favorite',
    'Lead - m3tÄ',
    'Lead - Motor',
    'Lead - völum3',
    'Pad - Fafa',
    'Pad - Saucers',
    'Pad - Smööthh',
    'Pluck - Bangbros',
    'Pluck - Chimes',
    'Pluck - Serum Bell',
    'Pluck - Tübe',
    'Pluck - Will',
    'Puck - m3h',
    'Seq - Gritty',
    'Seq - Motherland',
    'Synth - 4eveR',
    'Synth - Dream Sequence',
    'Synth - Samë',
    'Synth - Super Strings',
    'Synth - Transmutation',
)


def pair_folder(name):
    matches = [p for p in FIXTURES.iterdir() if p.is_dir() and (p / f"{name}.fxp").is_file()]
    if not matches:
        pytest.skip(f"manual comparison fixture is not present: {name}")
    assert len(matches) == 1, f"Expected one native fixture pair for {name}: {matches}"
    return matches[0]


def decode(path):
    raw = path.read_bytes()
    assert raw[:9] == b"XferJson\0"
    size, = struct.unpack_from("<Q", raw, 9)
    off = 17 + size
    metadata = json.loads(raw[17:off])
    expected, encoding = struct.unpack_from("<II", raw, off)
    assert encoding == 2
    payload = raw[off + 8:]
    assert metadata["hash"] == hashlib.md5(payload).hexdigest()
    cbor = zstandard.ZstdDecompressor().decompress(payload)
    assert len(cbor) == expected
    return {"metadata": metadata, "data": cbor2.loads(cbor)}


@pytest.fixture(scope="module")
def assets(tmp_path_factory):
    # Stub only WAV/AIFF descriptors; no copyrighted sample audio is copied.
    root = tmp_path_factory.mktemp("Serum data")
    for name in PAIRS:
        reference = decode(pair_folder(name) / f"{name}.SerumPreset")["data"]
        for osc in range(4):
            module = reference[f"Oscillator{osc}"]
            key = f"WTOsc{osc}" if osc < 3 else "NoiseOsc3"
            info = module[key]
            if "numFrames" not in info:
                if osc >= 3 or "embeddedWTData" in info or "relativePathToWT" not in info:
                    continue
                # Native save lacks a descriptor for Hypa; the source uses FM
                # from Sub, whose continuous position is independent of frame count.
                info = dict(info, numChannels=1, sampleRate=44100, numFrames=2048)
            asset_key = "relativePathToWT" if osc < 3 else "relativePathToNoiseSample"
            path = root / ("Tables" if osc < 3 else "Noises") / info[asset_key].lstrip("/")
            path.parent.mkdir(parents=True, exist_ok=True)
            channels, rate, frames = info["numChannels"], info["sampleRate"], info["numFrames"]
            if path.suffix.lower() in (".aif", ".aiff"):
                size = frames * channels * 2
                power = rate.bit_length() - 1
                comm = struct.pack(">HIHHQ", channels, frames, 16,
                                   16383 + power, rate << (63 - power))
                header = (b"FORM" + struct.pack(">I", size + 46) + b"AIFFCOMM" +
                          struct.pack(">I", 18) + comm + b"SSND" +
                          struct.pack(">III", size + 8, 0, 0))
                with path.open("wb") as f:
                    f.write(header)
                    f.truncate(len(header) + size)
                continue
            size = frames * channels * 4
            extra = b""
            if osc < 3:
                # Factory table metadata, independent of the converter's cache logic.
                interpolation = 0 if info[asset_key].lstrip("/") in (
                    "Analog/Basic Shapes.wav", "Analog/Basic Mini.wav", "Analog/SawRounded.wav") else 1
                payload = f"<!>2048 {interpolation}1000000 wavetable".encode()
                extra = b"clm " + struct.pack("<I", len(payload)) + payload + b"\0" * (len(payload) & 1)
            header = (b"RIFF" + struct.pack("<I", size + 36 + len(extra)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 3, channels,
                      rate, rate * channels * 4, channels * 4, 32) + extra + b"data" + struct.pack("<I", size))
            with path.open("wb") as f:
                f.write(header)
                f.truncate(len(header) + size)
    return root


def check_reference(actual, reference, path="", ignore_paths=()):
    """Check every serialized reference control; absent values mean defaults.

    Master volume is checked against Serum 1 separately: native saves may have
    manual gain changes. Effect phasors
    and noise detuneFactor describe transient playback state, not controls.
    """
    if path in ignore_paths:
        return
    if isinstance(reference, dict):
        for key, value in reference.items():
            if path + "/" + key in ignore_paths:
                continue
            if key in ("lfophasor", "detuneFactor"):
                continue
            if key == "lfo" and "FXHyperD" in path:
                continue  # Hyper's randomized playback phases are transient state.
            if key.startswith("kUI") or key in ("Osc", "WTOsc", "Filter", "SerumGUI", "ClipPlayer",
                    "GranularOsc", "MultiSampleOsc", "SpectralOsc", "arpBankDisplayName", "clipBankDisplayName"):
                continue
            if key in ("scale", "scaleName", "displayName"):
                continue
            if key == "plainParams" and value == "default":
                continue  # The converter writes explicit controls, including defaults.
            assert key in actual, path + "/" + key
            check_reference(actual[key], value, path + "/" + key, ignore_paths)
    elif isinstance(reference, list):
        assert len(actual) == len(reference), path
        for i, (a, b) in enumerate(zip(actual, reference)):
            check_reference(a, b, f"{path}/{i}", ignore_paths)
    elif isinstance(reference, (int, float)) and not isinstance(reference, bool):
        assert actual == pytest.approx(reference, rel=1e-6, abs=1e-5), path
    else:
        assert actual == reference, path


@pytest.mark.parametrize("name", PAIRS)
def test_native_conversion_reference(name, assets, tmp_path):
    source = pair_folder(name) / f"{name}.fxp"
    destination = tmp_path / f"{name}.SerumPreset"
    convert_file(source, destination, serum_roots=[assets])
    result = decode(destination)
    reference = decode(pair_folder(name) / f"{name}.SerumPreset")
    ignore_paths = ()
    if name in ("Keys - Velvet", "Synth - Samë"):
        index, module, parameter = ((1, "FXDistortion", 154) if name == "Keys - Velvet" else (0, "FXHyperD", 163))
        enable = result["data"]["FXRack0"]["FX"][index][module]["plainParams"]["kParamEnable"]
        assert converter._params(read_fxp(source))[parameter] == 1
        assert enable == 0
        assert reference["data"]["FXRack0"]["FX"][index][module]["plainParams"]["kParamEnable"] == 0
    if name in ("Keys - Poppers", "Pluck - Will"):
        pp = result["data"]["VoiceFilter0"]["plainParams"]
        assert pp["kParamWet"] == 100
        assert pp["kParamLevelOut"] == pytest.approx(.5 * converter.math.sqrt(converter._params(read_fxp(source))[49]))
    if name == "Lead - Motor":
        color = result["data"]["Oscillator3"]["NoiseOsc3"]["plainParams"]["kParamColor"]
        assert color == converter._params(read_fxp(source))[28]
        ignore_paths += ("/Oscillator3/NoiseOsc3/plainParams/kParamColor",)
    if name in ("Pad - Fafa", "Synth - Super Strings"):
        for i, fx in enumerate(result["data"]["FXRack0"]["FX"]):
            if fx["type"] == 5:
                ratio = fx["FXComp"]["plainParams"]["kParamRatio"]
                expected = reference["data"]["FXRack0"]["FX"][i]["FXComp"]["plainParams"]["kParamRatio"]
                assert ratio == pytest.approx(expected, abs=1e-10)
    blob, embedded = converter._read_fxp(source)
    sizes = struct.unpack_from("<2I", blob, 0x4968)
    for i in (0, 1):
        wt = result["data"][f"Oscillator{i}"][f"WTOsc{i}"]
        if wt.get("interpolateAfterLoad") not in (2, 3, 4):
            continue
        # Spectral interpolation regenerates samples during native import.
        # Preserve the complete FXP source rather than baking a native save's cache.
        start, size = sum(sizes[:i]), sizes[i]
        assert wt["embeddedWTData"] == list(struct.unpack_from(f"<{size // 4}f", embedded, start))
        native = reference["data"][f"Oscillator{i}"][f"WTOsc{i}"]
        assert len(wt["embeddedWTData"]) == len(native["embeddedWTData"])
        assert wt["interpolateAfterLoad"] == native["interpolateAfterLoad"]
        if wt["interpolateAfterLoad"] == 2:
            assert wt["embeddedWTData"][:-2048] == pytest.approx(native["embeddedWTData"][:-2048], abs=1e-5)
            assert max(abs(a-b) for a,b in zip(wt["embeddedWTData"][-2048:], native["embeddedWTData"][-2048:])) < .0007
        ignore_paths += (f"/Oscillator{i}/WTOsc{i}/embeddedWTData",)
    if name == "Lead - Float":
        # The independent legacy bypass mirror overrides the enable parameter.
        fx = result["data"]["FXRack0"]["FX"][3]["FXDistortion"]["plainParams"]
        assert fx["kParamEnable"] == 0 and fx["kParamWet"] == 0
        assert not any(slot.get("destModuleTypeString") == "FXDistortion" and
                       slot.get("destModuleParamName") == "kParamWet" for key, slot in result["data"].items()
                       if key.startswith("ModSlot"))
    if name in ("Lead - KrankY", "Lead - voicË"):
        # Mode-2's final frame differs in the serialized native save. Preserve
        # every source sample: processor read-back converges within 1e-6.
        wt = result["data"]["Oscillator0"]["WTOsc0"]
        native = reference["data"]["Oscillator0"]["WTOsc0"]["embeddedWTData"]
        blob, embedded = converter._read_fxp(source)
        size, = struct.unpack_from("<I", blob, 0x4968)
        assert wt["interpolateAfterLoad"] == 2
        assert wt["embeddedWTData"] == list(struct.unpack_from(f"<{size // 4}f", embedded))
        assert len(wt["embeddedWTData"]) == len(native)
        assert wt["embeddedWTData"][:-2048] == pytest.approx(native[:-2048], abs=1e-5)
        assert max(abs(a - b) for a, b in zip(wt["embeddedWTData"][-2048:], native[-2048:])) < .0007
        ignore_paths = ("/Oscillator0/WTOsc0/embeddedWTData",)
        if name == "Lead - voicË":
            # Native position is 168, while source-derived continuous position
            # is 167.64474. Do not infer a universal rounding rule from this pair.
            assert wt["plainParams"]["kParamTablePos"] == pytest.approx(1 + 255 * converter._params(blob)[11])
            ignore_paths += ("/Oscillator0/WTOsc0/plainParams/kParamTablePos",)
    check_reference(result["data"], reference["data"], ignore_paths=ignore_paths)
    assert result["metadata"]["presetAuthor"] == reference["metadata"]["presetAuthor"]
    raw_volume = struct.unpack_from("<f", read_fxp(source), 0x3460)[0]
    assert result["data"]["Global0"]["plainParams"]["kParamMasterVolume"] == pytest.approx(.8541468079 * raw_volume**3)


def test_batch_paths_and_existing_files(assets, tmp_path, capsys):
    source = tmp_path / "inputs"
    for name in PAIRS:
        folder = source / name
        folder.mkdir(parents=True)
        (folder / f"{name}.FXP").write_bytes((pair_folder(name) / f"{name}.fxp").read_bytes())
    (source / "bad.fxp").write_bytes(b"invalid")
    output = tmp_path / "outputs"
    assert main([str(source), str(output), "--recursive", "--serum-root", str(assets), "--json"]) == 1
    assert f"Converted {len(PAIRS)}, skipped 0, failed 1" in capsys.readouterr().out
    before = {p: p.read_bytes() for p in output.rglob("*") if p.is_file()}
    assert main([str(source), str(output), "-r", "--serum-root", str(assets)]) == 1
    assert f"Converted 0, skipped {len(PAIRS)}, failed 1" in capsys.readouterr().out
    assert all(p.read_bytes() == content for p, content in before.items())


def test_in_place_deep_batch_collects_failures_without_changing_originals(assets, tmp_path, capsys):
    bank = tmp_path / "bank"
    deep = bank / "category" / "bank" / "subbank" / "presets"
    deep.mkdir(parents=True)
    source = deep / "My Preset.FXP"
    source.write_bytes((pair_folder(PAIRS[0]) / f"{PAIRS[0]}.fxp").read_bytes())
    bad1, bad2 = bank / "bad.fxp", deep / "bad.fxp"
    bad1.write_bytes(b"broken one")
    bad2.write_bytes(b"broken two")
    originals = {p: p.read_bytes() for p in (source, bad1, bad2)}
    collection = bank / "fixtures"
    collection.mkdir()
    # Existing collected inputs must not be scanned on subsequent bank runs.
    ignored = collection / "already collected.fxp"
    ignored.write_bytes(b"ignore this")
    argv = [str(bank), "-r", "--in-place", "--collect-failures", str(collection),
            "--serum-root", str(assets)]
    assert main(argv) == 1
    assert "Converted 1, skipped 0, failed 2" in capsys.readouterr().out
    output = deep / "My Preset_serum2.serumpreset"
    assert decode(output)["metadata"]["presetName"] == "My Preset"
    collected = list(collection.glob("*/bad.fxp"))
    assert len(collected) == 2
    assert {p.read_bytes() for p in collected} == {b"broken one", b"broken two"}
    for p in collected:
        note = (p.parent / "conversion-error.txt").read_text(encoding="utf-8")
        assert "Conversion error:" in note and "Original:" in note
    native = collected[0].with_suffix(".SerumPreset")
    native.write_bytes(b"user supplied native conversion")
    # Investigated folders must be reused rather than recreated on another batch.
    checked = collected[0].parent.with_name("checked_" + collected[0].parent.name)
    collected[0].parent.rename(checked)
    native = checked / native.name
    before = output.read_bytes()
    assert main(argv) == 1
    assert "Converted 0, skipped 1, failed 2" in capsys.readouterr().out
    assert output.read_bytes() == before
    assert native.read_bytes() == b"user supplied native conversion"
    assert len(list(collection.glob("*/bad.fxp"))) == 2
    assert all(p.read_bytes() == value for p, value in originals.items())


def test_in_place_rejects_explicit_output(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main([str(tmp_path), str(tmp_path / "output"), "--in-place"])
    assert exc.value.code == 2


def test_cli_filters_full_paths_before_converting_or_collecting(tmp_path, monkeypatch, capsys):
    bank = tmp_path / "mixed bank"
    paths = ("SeRuM/deep/patch.FXP", "Synths/SERUM bass.fxp", "Other/piano.fxp")
    for relative in paths:
        path = bank / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"invalid fixture")
    # The real test workspace itself contains 'serum'; simulate a bank outside it.
    original_filter = converter._is_serum_path
    monkeypatch.setattr(converter, "_is_serum_path",
                        lambda p: original_filter(Path("Z:/Audio Assets") / p.relative_to(bank)))
    collection = tmp_path / "collected"
    assert main([str(bank), "-r", "--in-place", "--collect-failures", str(collection)]) == 1
    captured = capsys.readouterr()
    assert "Converted 0, skipped 0, failed 2" in captured.out
    assert "piano" not in captured.out + captured.err
    assert len([p for p in collection.glob("*/*") if p.suffix.lower() == ".fxp"]) == 2
    assert main([str(bank / "Other/piano.fxp"), "--collect-failures", str(collection)]) == 1
    assert "No .fxp files with 'serum'" in capsys.readouterr().err


def test_failures_do_not_publish(assets, tmp_path):
    source = pair_folder(PAIRS[0]) / f"{PAIRS[0]}.fxp"
    dst = tmp_path / "output.SerumPreset"
    with pytest.raises(ConversionError, match="missing Tables asset"):
        convert_file(source, dst, serum_roots=[])
    assert not dst.exists()
    broken = tmp_path / "truncated.fxp"
    broken.write_bytes(source.read_bytes()[:-1])
    with pytest.raises(ConversionError, match="length"):
        convert_file(broken, dst, serum_roots=[assets])
    assert not dst.exists()
    wrong_plugin = bytearray(source.read_bytes())
    wrong_plugin[16:20] = b"XXXX"
    broken.write_bytes(wrong_plugin)
    with pytest.raises(ConversionError, match="another plugin"):
        convert_file(broken, dst, serum_roots=[assets])


def test_unsupported_layout_is_explicit(tmp_path):
    source = pair_folder(PAIRS[0]) / f"{PAIRS[0]}.fxp"
    raw = bytearray(source.read_bytes()[:60])
    state = zlib.decompress(source.read_bytes()[60:]) + b"\0" * 100
    chunk = zlib.compress(state)
    struct.pack_into(">I", raw, 4, len(raw) + len(chunk))
    struct.pack_into(">I", raw, 56, len(chunk))
    unsupported = tmp_path / "new_layout.fxp"
    unsupported.write_bytes(raw + chunk)
    with pytest.raises(ConversionError, match="unsupported Serum 1 state layout"):
        translate_fxp(unsupported, [])


def test_overwrite_and_source_protection(assets, tmp_path):
    source = pair_folder(PAIRS[0]) / f"{PAIRS[0]}.fxp"
    dst = tmp_path / "output.SerumPreset"
    dst.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        convert_file(source, dst, serum_roots=[assets])
    assert dst.read_bytes() == b"existing"
    convert_file(source, dst, serum_roots=[assets], overwrite=True)
    assert decode(dst)["metadata"]["presetName"] == PAIRS[0]
    with pytest.raises(ConversionError, match="different files"):
        convert_file(source, source, overwrite=True)


def edited_fxp(source, destination, edit):
    raw = source.read_bytes()
    decoder = zlib.decompressobj()
    state = bytearray(decoder.decompress(raw[60:]))
    embedded = bytearray(zlib.decompress(decoder.unused_data))
    edit(state, embedded)
    first = zlib.compress(state)
    chunk = first + zlib.compress(embedded) + struct.pack("<I", len(first))
    header = bytearray(raw[:60])
    struct.pack_into(">I", header, 4, len(header) + len(chunk))
    struct.pack_into(">I", header, 56, len(chunk))
    destination.write_bytes(header + chunk)


def test_embedded_audio_and_custom_response_come_from_input(assets, tmp_path):
    source = pair_folder(PAIRS[4]) / f"{PAIRS[4]}.fxp"
    changed = tmp_path / "edited.fxp"

    def edit(state, embedded):
        struct.pack_into("<f", embedded, 0, .125)
        state[0x4950] = 2
        for offset, values in ((0x4620, (.2, .7, .4)),
                               (0x4730, (0, .5, 1)), (0x4840, (1, .75, 0))):
            struct.pack_into("<3d", state, offset, *values)
        struct.pack_into("<f", state, 0x4958, 0)

    edited_fxp(source, changed, edit)
    result = translate_fxp(changed, [assets])["data"]
    assert result["Oscillator0"]["WTOsc0"]["embeddedWTData"][0] == .125
    assert result["scalars"]["velo"] == dict(curveVals=[.2, .7, .4],
        xVals=[0, .5, 1], yVals=[1, .75, 0], numPoints=2, legato=False)


def test_truncated_embedded_audio_does_not_publish(assets, tmp_path):
    source = pair_folder(PAIRS[4]) / f"{PAIRS[4]}.fxp"
    changed = tmp_path / "broken.fxp"
    edited_fxp(source, changed, lambda state, embedded: embedded.__delitem__(slice(-4, None)))
    destination = tmp_path / "broken.SerumPreset"
    with pytest.raises(ConversionError, match="truncated embedded wavetable"):
        convert_file(changed, destination, serum_roots=[assets])
    assert not destination.exists()
