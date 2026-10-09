"""The default standalone route must not execute the investigated Serum binary."""
import ctypes
import json
import struct
from pathlib import Path

import pytest

from fxp_to_serumpreset import convert_file, translate_fxp, _wav_info, read_fxp, _text
from test_fixture_conversions import decode_preset
from cli import main


SOURCES = sorted(p for p in (Path(__file__).parent / 'fixtures').rglob('*.fxp')
                 if not p.name.startswith('._'))


@pytest.fixture(scope='module')
def asset_descriptors(tmp_path_factory):
    # Synthetic one-frame WAVs test dependency independence and container
    # integrity. Real asset descriptors are used by the native-pair comparisons.
    root = tmp_path_factory.mktemp('standalone-assets')
    for source in SOURCES:
        state = read_fxp(source)
        for osc in (0, 1):
            if struct.unpack_from('<I', state, 0x4968 + 4 * osc)[0]:
                continue  # Embedded tables do not need external descriptors.
            name = _text(state, 0x3c08 + osc * 512, 512).replace('\\', '/').lstrip('/')
            if not name:
                continue
            path = root / 'Tables' / name
            assert path.resolve().is_relative_to(root.resolve())
            path.parent.mkdir(parents=True, exist_ok=True)
            data_size = 2048 * 4
            header = b'RIFF' + struct.pack('<I', data_size + 36) + b'WAVEfmt ' + struct.pack('<IHHIIHH', 16, 3, 1, 44100, 176400, 4, 32) + b'data' + struct.pack('<I', data_size)
            with path.open('wb') as stream:
                stream.write(header)
                stream.truncate(len(header) + data_size)
    return root


@pytest.mark.parametrize('source', SOURCES, ids=lambda p: p.stem)
def test_all_fixtures_convert_without_a_plugin(source, tmp_path, monkeypatch, asset_descriptors):
    def forbidden(*args, **kwargs):
        pytest.fail('standalone conversion attempted to load an external binary')
    monkeypatch.setattr(ctypes, 'CDLL', forbidden)
    monkeypatch.setattr(ctypes, 'WinDLL', forbidden, raising=False)
    output = tmp_path / 'converted.SerumPreset'
    before = source.read_bytes()
    result = convert_file(source, output, serum_roots=[asset_descriptors])
    decoded = decode_preset(output)
    assert decoded == result
    assert decoded['metadata']['presetName'] == source.stem
    assert source.read_bytes() == before


@pytest.mark.parametrize('extra', ['padding', 'serum-size'])
def test_wav_metadata_uses_declared_chunks(tmp_path, extra):
    fmt = struct.pack('<HHIIHH', 1, 1, 44100, 88200, 2, 16)
    chunks = b'fmt ' + struct.pack('<I', len(fmt)) + fmt + b'data' + struct.pack('<I', 6) + bytes(6)
    riff = b'RIFF' + struct.pack('<I', 4 + len(chunks) + (4 if extra == 'serum-size' else 0)) + b'WAVE' + chunks
    if extra == 'padding':
        riff += b'unrelated allocator padding'
    path = tmp_path / 'asset.wav'
    path.write_bytes(riff)
    assert _wav_info(path) == dict(numChannels=1, sampleRate=44100, numFrames=3)


def test_cli_import_exports_packable_module_json(tmp_path, asset_descriptors, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('JSON import attempted to load a plugin')
    monkeypatch.setattr(ctypes, 'WinDLL', forbidden, raising=False)
    output = tmp_path / 'translated.json'
    assert main(['import-fxp', str(SOURCES[0]), str(output), '--serum-root', str(asset_descriptors)]) == 0
    preset = tmp_path / 'converted.SerumPreset'
    assert main(['pack', str(output), str(preset)]) == 0
    assert decode_preset(preset) == json.loads(output.read_text(encoding='utf-8'))


@pytest.mark.parametrize('name,path,expected', [
    ('11 S&H', ('LFO8', 'plainParams', 'kParamType'), 'RandomSH'),
    ('Lead 02 - PSYFORSERUM5 Zenhiser', ('ModSlot0', 'plainParams', 'kParamBypass'), 1),
    ('PD - Deckard [7 SKIES]', ('Oscillator0', 'WTOsc0', 'plainParams', 'kParamPhaseMemory'), 'kPerVoice'),
    ('LEAD++Can of Soup', ('FXRack0', 'FX', 5, 'FXDistortion', 'plainParams', 'kParamMode'), 'kDownsample'),
    ('Atmos__Prometheus', ('LFO0', 'plainParams', 'kParamAnchored'), 1),
    ('Lead__Cords', ('Global0', 'plainParams', 'kParamLegato'), 0),
    ('Seq__Made4Glitch', ('ModSlot9', 'plainParams', 'kParamAmount'), -1.0416686534881592),
    ('Seq__Made4Glitch', ('ModSlot11', 'plainParams', 'kParamAmount'), -2.166670560836792),
    ('SMGP2 - BA MidTempo 23', ('Oscillator0', 'WTOsc0', 'plainParams', 'kParamWarp'), .0001),
    ('POLY - Serenica Choir', ('Global0', 'plainParams', 'kParamPolyCount'), 32),
    ('PML - STP2 - PAD Apache', ('Global0', 'plainParams', 'kParamGlobalTuning'), 432.0000000298023),
    ('ACID - Classic Clean', ('lfoPointModAssignments',), [
        dict(busID=0, lfoID=0, lfoType=0, pointID=1, target=1),
        dict(busID=1, lfoID=1, lfoType=0, pointID=1, target=1)]),
])
def test_controls_from_native_import_observations(name, path, expected, asset_descriptors):
    # Frozen observations of Serum's importer, not calculated from our rules.
    sources = [p for p in SOURCES if p.stem == name]
    if not sources:
        pytest.skip(f'local research fixture missing: {name}')
    value = translate_fxp(sources[0], [asset_descriptors])['data']
    for key in path:
        value = value[key]
    if isinstance(expected, (int, float)):
        assert value == pytest.approx(expected, abs=1e-6)
    else:
        assert value == expected
