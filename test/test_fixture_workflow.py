import json
from pathlib import Path

from fixture_pairs import preset_files
from mark_fixture_pairs import mark_pairs
import test_fixture_conversions as runner
import fxp_to_serumpreset as converter


def test_pair_labels_ignore_generated_files_and_replace_prefix(tmp_path, capsys):
    pending = tmp_path / "Pending"
    pending.mkdir()
    (pending / "patch.fxp").write_bytes(b"source")
    (pending / "test_patch.SerumPreset").write_bytes(b"generated")
    assert mark_pairs(tmp_path) == 0
    assert pending.exists()
    (pending / "patch.SeRuMpReSeT").write_bytes(b"manual")
    assert mark_pairs(tmp_path) == 0
    pair = tmp_path / "pair_Pending"
    assert pair.is_dir()
    assert mark_pairs(tmp_path) == 0
    assert mark_pairs(tmp_path, [pair.name]) == 0
    checked = tmp_path / "checked_Pending"
    assert checked.is_dir()
    assert (checked / "patch.fxp").read_bytes() == b"source"
    assert mark_pairs(tmp_path) == 0
    assert len(preset_files(checked)[1]) == 1


def test_all_differences_are_collected_without_known_patch_exceptions():
    a = {"metadata": {"hash": "a"}, "data": {"volume": .2, "values": [1, 2], "extra": 1}}
    b = {"metadata": {"hash": "b"}, "data": {"volume": .8, "values": [3, 4], "missing": 2}}
    differences = runner.compare(a, b)
    assert {d["path"] for d in differences} == {
        "/data/volume", "/data/values/0", "/data/values/1", "/data/extra", "/data/missing"}
    assert runner.compare({"data": {"plainParams": {"x": 1}}},
                          {"data": {"plainParams": "default"}}, controls_only=True) == []


def test_runner_deletes_passes_keeps_failures_and_preserves_originals(tmp_path, monkeypatch):
    payload = {"metadata": {"presetAuthor": "Author"}, "data": {"control": 1}}
    folders = {}
    for name in ("pass", "diff", "broken", "unpaired"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / f"{name}.fxp").write_bytes(b"original FXP")
        if name != "unpaired":
            expected = {**payload, "data": {"control": 2 if name == "diff" else 1}}
            (folder / f"{name}.SerumPreset").write_bytes(converter.encode_preset(expected))
        folders[name] = folder
    originals = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    def fake_convert(source, output, **kwargs):
        if source.stem == "broken":
            raise converter.ConversionError("unsupported mode")
        output.write_bytes(converter.encode_preset(payload))

    monkeypatch.setattr(runner, "convert_file", fake_convert)
    assert runner.main(["--fixtures", str(tmp_path)]) == 1
    report = json.loads((tmp_path / "conversion-test-report.json").read_text(encoding="utf-8"))
    assert report["total"] == 4 and report["failed"] == 3
    assert not (folders["pass"] / "test_pass.SerumPreset").exists()
    assert (folders["diff"] / "test_diff.SerumPreset").exists()
    assert (folders["unpaired"] / "test_unpaired.SerumPreset").exists()
    assert all(p.read_bytes() == content for p, content in originals.items())
    assert any(r["output_deleted"] for r in report["results"])


def test_collector_reuses_pair_prefix(tmp_path):
    source = tmp_path / "patch.fxp"
    source.write_bytes(b"original")
    collection = tmp_path / "fixtures"
    copied = converter._collect_failure(source, collection, ValueError("error"))
    pending = copied.parent.with_name("pair_" + copied.parent.name)
    copied.parent.rename(pending)
    copied_again = converter._collect_failure(source, collection, ValueError("error again"))
    assert copied_again.parent == pending
    assert len(list(collection.iterdir())) == 1


def test_canonical_comparison_reports_extra_nondefault_controls():
    generated = {"data": {"Oscillator0": {"plainParams": {"volume": 1, "pitch": 12}}}}
    native = {"data": {"Oscillator0": {"plainParams": {"volume": 1}}}}
    differences = runner.compare(generated, native, controls_only=True, canonicalized=True)
    assert [d["path"] for d in differences] == ["/data/Oscillator0/plainParams/pitch"]


def test_canonical_comparison_reports_nondefault_against_default():
    generated = {"data": {"Oscillator0": {"plainParams": {"pitch": 12}}}}
    native = {"data": {"Oscillator0": {"plainParams": "default"}}}
    assert runner.compare(generated, native, controls_only=True, canonicalized=True)
