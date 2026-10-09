import pytest

import fxp_to_serumpreset as converter


def test_wildcard_folders_recursive_in_place_and_failure_collection(tmp_path, monkeypatch):
    root = tmp_path / "Serum banks"
    good = root / "psy bank" / "nested psy" / "good.FXP"
    bad = root / "other" / "psy bank" / "bad.fxp"
    ignored = root / "other" / "ignored.fxp"
    for path in (good, bad, ignored):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"original")
    calls = []

    def convert(src, dst, **kwargs):
        calls.append(src)
        if src == bad:
            raise converter.ConversionError("unsupported")
        dst.write_bytes(b"converted")

    monkeypatch.setattr(converter, "convert_file", convert)
    collection = tmp_path / "collected"
    assert converter.main([str(root / "**" / "*psy*"), "-r", "--in-place",
                           "--collect-failures", str(collection)]) == 1
    assert sorted(calls) == sorted([good, bad])
    assert good.with_name("good_serum2.serumpreset").read_bytes() == b"converted"
    assert len(list(collection.glob("*/bad.fxp"))) == 1
    assert all(path.read_bytes() == b"original" for path in (good, bad, ignored))


def test_wildcard_files_output_structure_and_no_matches(tmp_path, monkeypatch):
    root = tmp_path / "Serum banks"
    source = root / "psy" / "patch.FXP"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original")
    calls = []
    monkeypatch.setattr(converter, "convert_file", lambda src, dst, **kwargs: calls.append((src, dst)))
    output = tmp_path / "output"
    assert converter.main([str(root / "*" / "patch.?XP"), str(output)]) == 0
    assert calls == [(source, output / "psy" / "patch.SerumPreset")]
    with pytest.raises(SystemExit) as error:
        converter.main([str(root / "missing*")])
    assert error.value.code == 2
