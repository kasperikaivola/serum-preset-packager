from pathlib import Path

from clean_unconverted_fixtures import clean_fixtures


def test_cleanup_preview_and_delete_preserve_native_pairs_and_loose_files(tmp_path, capsys):
    fixtures = tmp_path / "fixtures"
    native = fixtures / "Converted" / "nested" / "pair.SeRuMpReSeT"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"native preset")
    unconverted = fixtures / "._Unconverted with spaces"
    unconverted.mkdir()
    fxp = unconverted / "source.fxp"
    fxp.write_bytes(b"original copied FXP")
    loose = fixtures / "loose.fxp"
    loose.write_bytes(b"keep")
    assert clean_fixtures(fixtures) == 0
    assert "Would remove 1 folders; kept 1" in capsys.readouterr().out
    assert fxp.exists()
    assert clean_fixtures(fixtures, delete=True) == 0
    assert "Removed 1 folders; kept 1" in capsys.readouterr().out
    assert not unconverted.exists()
    assert native.read_bytes() == b"native preset"
    assert loose.read_bytes() == b"keep"


def test_cleanup_skips_linked_subtrees(tmp_path, monkeypatch):
    import clean_unconverted_fixtures as cleanup

    fixtures = tmp_path / "fixtures"
    nested = fixtures / "unconverted" / "linked subtree"
    nested.mkdir(parents=True)
    # Avoid Windows symlink privileges; exercise the junction/symlink guard.
    monkeypatch.setattr(cleanup, "_linked", lambda path: Path(path) == nested)
    assert clean_fixtures(fixtures, delete=True) == 0
    assert nested.exists()
