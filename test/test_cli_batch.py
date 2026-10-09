import json

import pytest

from cli import main, pack, unpack


PAYLOAD = {"metadata": {"name": "Batch test"}, "data": {"value": 42}}


def make_input(path, command):
    path.parent.mkdir(parents=True, exist_ok=True)
    if command == "pack":
        path.write_text(json.dumps(PAYLOAD))
    else:
        seed = path.parent / "seed.json"
        seed.write_text(json.dumps(PAYLOAD))
        pack(seed, path)
        seed.unlink()


def read_output(path, command, tmp_path):
    if command == "pack":
        decoded = tmp_path / "decoded.json"
        unpack(path, decoded)
        return json.loads(decoded.read_text())
    return json.loads(path.read_text())


@pytest.mark.parametrize("command,suffix,out_suffix", [
    ("unpack", ".sErUmPrEsEt", ".json"),
    ("pack", ".JsOn", ".SerumPreset"),
])
def test_batch_recursive_and_overwrite(tmp_path, command, suffix, out_suffix):
    root = tmp_path / "inputs"
    top = root / ("top" + suffix)
    nested = root / "sub" / "deep" / ("nested" + suffix)
    for source in (top, nested):
        make_input(source, command)
        source.with_suffix(out_suffix).write_text("stale output")
    (root / "ignored.txt").write_text("ignored")

    assert main([command, str(root)]) == 0
    assert nested.with_suffix(out_suffix).read_text() == "stale output"
    assert main([command, str(root), "--recursive"]) == 0
    for source in (top, nested):
        assert read_output(source.with_suffix(out_suffix), command, tmp_path) == PAYLOAD

    output_root = tmp_path / "outputs"
    assert main([command, str(root), str(output_root), "-r"]) == 0
    output = output_root / "sub" / "deep" / ("nested" + out_suffix)
    assert read_output(output, command, tmp_path) == PAYLOAD


def test_explicit_single_file_and_batch_failure(tmp_path):
    source = tmp_path / "source.json"
    make_input(source, "pack")
    preset = tmp_path / "preset.SerumPreset"
    preset.write_text("stale")
    assert main(["pack", str(source), str(preset)]) == 0
    decoded = tmp_path / "result.json"
    assert main(["unpack", str(preset), str(decoded)]) == 0
    assert json.loads(decoded.read_text()) == PAYLOAD

    root = tmp_path / "batch"
    root.mkdir()
    (root / "a-invalid.json").write_text("{}")
    make_input(root / "b-valid.json", "pack")
    assert main(["pack", str(root), "--recursive"]) == 1
    assert read_output(root / "b-valid.SerumPreset", "pack", tmp_path) == PAYLOAD
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["unpack", str(empty)]) == 1
