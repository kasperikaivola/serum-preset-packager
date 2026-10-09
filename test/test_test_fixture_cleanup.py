from clean_test_fixtures import clean_test_files


def test_recursive_cleanup_and_preview(tmp_path, monkeypatch):
    import clean_test_fixtures as cleanup

    root = tmp_path / "fixtures"
    nested = root / "nested" / "deeper"
    nested.mkdir(parents=True)
    generated = [root / "test_patch.json", nested / "TEST_patch.SerumPreset"]
    preserved = [nested / "patch.json", root / "conversion-test-report.json"]
    linked = root / "linked"
    linked.mkdir()
    preserved.append(linked / "test_external.json")
    for path in generated + preserved:
        path.write_text("content")
    monkeypatch.setattr(cleanup, "_linked", lambda path: path == linked)

    assert clean_test_files(root, dry_run=True) == 0
    assert all(path.exists() for path in generated + preserved)
    assert clean_test_files(root) == 0
    assert all(not path.exists() for path in generated)
    assert all(path.read_text() == "content" for path in preserved)
    assert nested.is_dir()
