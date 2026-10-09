"""Focused checks for the opt-in native route and its private ABI boundary."""
import ctypes
import os

import pytest

import native_serum
from fxp_to_serumpreset import convert_file


def test_native_bool_ignores_cpp_padding():
    value = (ctypes.c_uint64 * 2)(4, 0xffffffffffffff00)
    assert native_serum.decode(ctypes.addressof(value)) is False
    value[1] |= 1
    assert native_serum.decode(ctypes.addressof(value)) is True


@pytest.mark.skipif(os.name != 'nt', reason='Windows worker ABI')
def test_wrong_binary_is_rejected_before_loading(tmp_path, monkeypatch):
    plugin = tmp_path / 'unverified.vst3'
    plugin.write_bytes(b'not the investigated binary')
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a: pytest.fail('unverified binary executed'))
    with pytest.raises(ValueError, match='verified Serum'):
        native_serum._worker(tmp_path / 'source.fxp', tmp_path / 'output.json', plugin)


def test_native_route_keeps_existing_output_without_starting_worker(tmp_path, monkeypatch):
    source = tmp_path / 'source.fxp'
    source.write_bytes(b'source')
    output = tmp_path / 'output.SerumPreset'
    output.write_bytes(b'existing')
    monkeypatch.setattr(native_serum, 'native_import', lambda *a, **k: pytest.fail('worker started'))
    with pytest.raises(FileExistsError):
        convert_file(source, output, backend='native')
    assert source.read_bytes() == b'source'
    assert output.read_bytes() == b'existing'
