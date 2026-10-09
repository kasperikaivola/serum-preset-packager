import copy
import struct
from pathlib import Path

import pytest

from cli import main
from fxp_to_serumpreset import _read_fxp
from serum1_json import pack_fxp, unpack_fxp


SOURCES = sorted(p for p in (Path(__file__).parent / 'fixtures').rglob('*.fxp') if not p.name.startswith('._'))


@pytest.mark.parametrize('source', SOURCES, ids=lambda p: p.stem)
def test_lossless_legacy_state_and_wavetable(source, tmp_path):
    obj = unpack_fxp(source)
    rebuilt = tmp_path / 'rebuilt.fxp'
    rebuilt.write_bytes(pack_fxp(obj))
    assert _read_fxp(source) == _read_fxp(rebuilt)
    assert source.read_bytes()[:4] == rebuilt.read_bytes()[:4]


def test_edit_parameter_and_cli_roundtrip(tmp_path):
    source = tmp_path / 'source.fxp'
    source.write_bytes(SOURCES[0].read_bytes())
    assert main(['unpack', str(source)]) == 0
    intermediate = tmp_path / 'source_serum1.json'
    assert main(['pack', str(intermediate)]) == 0
    assert _read_fxp(source) == _read_fxp(tmp_path / 'source_serum1.fxp')
    obj = unpack_fxp(source)
    edited = copy.deepcopy(obj)
    edited['data']['normalizedParameters'][0] = 0.25
    rebuilt = tmp_path / 'edited.fxp'
    rebuilt.write_bytes(pack_fxp(edited))
    state, tables = _read_fxp(rebuilt)
    original, original_tables = _read_fxp(source)
    assert struct.unpack_from('<f', state, 0x3460)[0] == 0.25
    assert state[:0x3460] == original[:0x3460]
    assert state[0x3464:] == original[0x3464:]
    assert tables == original_tables


def test_invalid_header_and_parameter_length():
    obj = unpack_fxp(SOURCES[0])
    obj['data']['normalizedParameters'].pop()
    with pytest.raises(ValueError, match='parameter count'):
        pack_fxp(obj)
