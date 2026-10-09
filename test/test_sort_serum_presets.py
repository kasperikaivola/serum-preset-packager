import json
import struct
import zlib
from pathlib import Path

import cbor2
import pytest
import zstandard

import sort_serum_presets as sorter


CONFIG = {
    "fallback_genre": "unsorted",
    "fallback_group": "other",
    "genres": [
        {"name": "psytrance", "match": ["psytrance", "psy"]},
        {"name": "techno", "match": ["techno"]},
    ],
    "groups": [
        {"name": "bass", "match": ["bass", "ba"]},
        {"name": "lead", "match": ["lead"]},
        {"name": "fx", "match": ["fx"]},
        {"name": "syn", "match": ["syn"]},
    ],
}


def write_fxp(path: Path, *, noise="", wt0="", wt_size=0, description=""):
    state = bytearray(20704)
    for offset, text in ((0x3C08, wt0), (0x4008, noise), (0x49D0, description)):
        encoded = text.encode("utf-8")
        state[offset:offset + len(encoded)] = encoded
    struct.pack_into("<I", state, 0x4968, wt_size)
    payload = zlib.compress(bytes(state))
    header = bytearray(60)
    header[0:4] = b"CcnK"
    header[8:12] = b"FPCh"
    header[16:20] = b"XfsX"
    struct.pack_into(">I", header, 4, 60 + len(payload))
    struct.pack_into(">I", header, 56, len(payload))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header) + payload)


def write_serum2(path: Path, *, name, wt=None, noise=None, tags=None):
    data = {}
    if wt:
        data["Oscillator0"] = {"WTOsc0": {"relativePathToWT": wt}}
    if noise:
        data["Oscillator3"] = {"NoiseOsc3": {"relativePathToNoiseSample": noise, "pathToNoiseSample": noise}}
    metadata = {"fileType": "SerumPreset", "presetName": name, "presetAuthor": "tester",
                "presetDescription": "", "tags": tags or [], "product": "Serum2",
                "productVersion": "2.0.16", "vendor": "Xfer Records", "version": 6.0}
    header = json.dumps(metadata, separators=(",", ":")).encode()
    payload = cbor2.dumps(data)
    compressed = zstandard.ZstdCompressor(level=3).compress(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"XferJson\x00" + struct.pack("<Q", len(header)) + header +
                     struct.pack("<II", len(payload), 2) + compressed)


def test_shipped_config_covers_the_requested_groups():
    config = sorter.load_config(sorter.DEFAULT_CONFIG)
    assert [entry["name"] for entry in config["groups"] if entry["name"] in
            {"syn", "fx", "lead", "pad", "chord", "bass", "atmo"}] == [
                "bass", "lead", "pad", "chord", "atmo", "fx", "syn"]
    genre, group = sorter.classify(
        "Zenhiser.Psytrance.For.Serum.3/PSYFORSERUM 3 - Presets/Atmo 02 - PSYFORSERUM3.fxp", config)
    assert (genre, group) == ("psytrance", "atmo")
    assert sorter.classify("PML - Serum Techno Pack/Lead/LD Dark.fxp", config) == ("techno", "lead")
    assert sorter.classify("notes/Init.fxp", config) == (config["fallback_genre"], config["fallback_group"])
    assert config["output"] == "./SerumPresets"
    assert sorter.classify("banks/effects/riser.fxp", config)[1] == "fx"
    assert sorter.classify("banks/sfx/hit.fxp", config)[1] == "fx"
    assert sorter.classify("banks/pads/warm.fxp", config)[1] == "pad"
    assert sorter.classify("banks/ambient/space.fxp", config)[1] == "atmo"


def test_short_tokens_do_not_match_inside_other_words():
    assert sorter.classify("banks/reflex warm.fxp", CONFIG) == ("unsorted", "other")
    assert sorter.classify("banks/FX/riser.fxp", CONFIG) == ("unsorted", "fx")
    assert sorter.classify("psy/BA - Art.fxp", CONFIG) == ("psytrance", "bass")


def test_sort_copies_presets_and_samples_without_touching_sources(tmp_path):
    root = tmp_path / "AudioAssets"
    serum = tmp_path / "Serum Presets"
    table = serum / "Tables" / "Analog" / "Basic.wav"
    noise = root / "pack" / "Noises" / "Organic" / "click.wav"
    table.parent.mkdir(parents=True)
    noise.parent.mkdir(parents=True)
    table.write_bytes(b"table-bytes")
    noise.write_bytes(b"noise-bytes")
    fxp = root / "Endeavour - Psytrance" / "Bass" / "BA - Sub.fxp"
    serum2 = root / "techno bank" / "Psy Lead.SerumPreset"
    foreign = root / "other.fxp"
    write_fxp(fxp, noise="Organic/click.wav", wt0="Analog/Basic.wav", description="psy bass")
    write_serum2(serum2, name="Psy Lead", wt="Analog/Basic.wav")
    foreign.write_bytes(b"CcnK-not-serum")
    originals = {path: path.read_bytes() for path in (fxp, serum2, foreign, table, noise)}

    result = sorter.sort_folder(root, serum1=True, serum2=True, recursive=True, config=CONFIG,
                                serum_roots=[serum])

    assert result.copied == 2
    assert result.ignored == 1
    assert result.failed == []
    assert result.missing_samples == []
    assert (root / "sorted" / "psytrance" / "bass" / "BA - Sub.fxp").read_bytes() == originals[fxp]
    assert (root / "sorted" / "psytrance" / "lead" / "Psy Lead.SerumPreset").read_bytes() == originals[serum2]
    assert (root / "sorted" / "psytrance" / "bass" / "Tables" / "Analog" / "Basic.wav").read_bytes() == b"table-bytes"
    assert (root / "sorted" / "psytrance" / "bass" / "Noises" / "Organic" / "click.wav").read_bytes() == b"noise-bytes"
    assert (root / "sorted" / "psytrance" / "lead" / "Tables" / "Analog" / "Basic.wav").read_bytes() == b"table-bytes"
    assert not (root / "sorted" / "techno").exists()
    assert all(path.read_bytes() == content for path, content in originals.items())


def test_flags_limit_types_and_depth(tmp_path):
    root = tmp_path / "bank"
    write_fxp(root / "techno" / "Bass One.fxp", description="techno bass")
    write_serum2(root / "techno" / "Lead.SerumPreset", name="techno lead")
    write_fxp(root / "techno" / "inner" / "Bass Two.fxp", description="techno bass")
    shallow = sorter.sort_folder(root, serum1=True, serum2=False, recursive=False, config=CONFIG, serum_roots=[])
    assert shallow.copied == 0
    recursive = sorter.sort_folder(root, serum1=True, serum2=False, recursive=True, config=CONFIG, serum_roots=[])
    assert recursive.copied == 2
    assert list((root / "sorted").rglob("*.SerumPreset")) == []
    assert list((root / "sorted").rglob("*.fxp"))


def test_sibling_folder_sample_is_preferred_over_another_pack(tmp_path):
    root = tmp_path / "AudioAssets"
    preset = root / "pack1" / "folder1" / "Psy Bass.SerumPreset"
    near = root / "pack1" / "folder2" / "required_audio.wav"
    far = root / "pack2" / "folder2" / "required_audio.wav"
    near.parent.mkdir(parents=True)
    far.parent.mkdir(parents=True)
    near.write_bytes(b"near-pack")
    far.write_bytes(b"other-pack")
    write_serum2(preset, name="Psy Bass", wt="required_audio.wav")
    result = sorter.sort_folder(root, serum1=True, serum2=True, recursive=True, config=CONFIG, serum_roots=[])
    copied = root / "sorted" / "psytrance" / "bass" / "Tables" / "required_audio.wav"
    assert result.missing_samples == []
    assert copied.read_bytes() == b"near-pack"
    assert near.read_bytes() == b"near-pack"
    assert far.read_bytes() == b"other-pack"


def test_missing_sample_is_reported_and_embedded_table_is_not(tmp_path):
    root = tmp_path / "bank"
    external = root / "psy" / "Needs Noise.fxp"
    embedded = root / "psy" / "Embedded.fxp"
    write_fxp(external, noise="Organic/missing.wav", description="psy bass")
    write_fxp(embedded, wt0="Analog/missing.wav", wt_size=2048, description="psy bass")
    result = sorter.sort_folder(root, serum1=True, serum2=True, recursive=True, config=CONFIG, serum_roots=[])
    assert result.copied == 2
    assert len(result.missing_samples) == 1
    assert "Organic/missing.wav" in result.missing_samples[0]
    assert (root / "sorted" / "psytrance" / "bass" / "Embedded.fxp").is_file()


def test_rerun_skips_sorted_output_and_identical_files(tmp_path):
    root = tmp_path / "bank"
    source = root / "psy" / "BA - One.fxp"
    write_fxp(source, description="psy bass")
    first = sorter.sort_folder(root, serum1=True, serum2=False, recursive=True, config=CONFIG, serum_roots=[])
    second = sorter.sort_folder(root, serum1=True, serum2=False, recursive=True, config=CONFIG, serum_roots=[])
    assert first.copied == 1
    assert second.copied == 0
    assert second.skipped_existing == 1
    assert list((root / "sorted").rglob("*.fxp")).__len__() == 1


def test_name_collision_keeps_both_presets(tmp_path):
    root = tmp_path / "bank"
    first = root / "a" / "psy" / "BA - Same.fxp"
    second = root / "b" / "psy" / "BA - Same.fxp"
    write_fxp(first, description="psy bass one")
    write_fxp(second, description="psy bass two")
    result = sorter.sort_folder(root, serum1=True, serum2=True, recursive=True, config=CONFIG, serum_roots=[])
    assert result.copied == 2
    copies = list((root / "sorted" / "psytrance" / "bass").glob("*.fxp"))
    assert len(copies) == 2
    assert {path.read_bytes() for path in copies} == {first.read_bytes(), second.read_bytes()}


def test_unsorted_names_are_saved_beside_the_script(tmp_path):
    root = tmp_path / "bank"
    listed = tmp_path / "unsorted-presets.txt"
    write_fxp(root / "packs" / "Warm Pad.fxp", description="just a pad")
    write_fxp(root / "psy" / "BA - Known.fxp", description="psy bass")
    write_serum2(root / "notes" / "Loose Synth.SerumPreset", name="Loose Synth")
    result = sorter.sort_folder(root, serum1=True, serum2=True, recursive=True, config=CONFIG,
                                serum_roots=[], dry_run=True, unsorted_list=listed)
    assert sorted(result.unsorted, key=str.casefold) == [
        "notes/Loose Synth.SerumPreset",
        "packs/Warm Pad.fxp",
    ]
    assert listed.read_text(encoding="utf-8").splitlines() == [
        "notes/Loose Synth.SerumPreset",
        "packs/Warm Pad.fxp",
    ]
    assert not (root / "sorted").exists()


def test_config_output_is_relative_to_the_source_folder(tmp_path):
    config_path = tmp_path / "elsewhere" / "sort_serum_presets.json"
    config = dict(CONFIG, output="./SerumPresets")
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(config), encoding="utf-8")
    loaded = sorter.load_config(config_path)
    root = tmp_path / "AudioAssets"
    source = root / "psy" / "BA - Out.fxp"
    write_fxp(source, description="psy bass")
    original = source.read_bytes()
    output = sorter.resolve_output(loaded, root)
    result = sorter.sort_folder(root, serum1=True, serum2=True, recursive=True, config=loaded,
                                serum_roots=[], output=output)
    copied = root / "SerumPresets" / "psytrance" / "bass" / "BA - Out.fxp"
    assert result.copied == 1
    assert copied.read_bytes() == original
    assert not (root / "sorted").exists()
    assert not (config_path.parent / "SerumPresets").exists()
    assert source.read_bytes() == original
    again = sorter.sort_folder(root, serum1=True, serum2=True, recursive=True, config=loaded,
                               serum_roots=[], output=output)
    assert again.copied == 0
    assert again.skipped_existing == 1


def test_dry_run_writes_nothing(tmp_path):
    root = tmp_path / "bank"
    write_fxp(root / "psy" / "BA - Dry.fxp", description="psy bass")
    result = sorter.sort_folder(root, serum1=True, serum2=True, recursive=True, config=CONFIG,
                                serum_roots=[], dry_run=True)
    assert result.copied == 1
    assert not (root / "sorted").exists()


def test_cli_requires_a_directory(tmp_path, capsys):
    missing = tmp_path / "missing"
    assert sorter.main([str(missing), "--serum1"]) == 1
    assert "not a directory" in capsys.readouterr().err


def test_invalid_config_is_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"fallback_genre": "unsorted", "fallback_group": "other", "genres": [], "groups": []}',
                    encoding="utf-8")
    with pytest.raises(sorter.SortError):
        sorter.load_config(path)
