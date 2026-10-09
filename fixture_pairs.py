"""Shared discovery and safe naming for native preset comparison folders."""
from pathlib import Path
import os

FIXTURES = Path(__file__).resolve().parent / "test" / "fixtures"


def linked(path: Path) -> bool:
    return path.is_symlink() or getattr(os.path, "isjunction", lambda p: False)(path)


def fixture_folders(root: Path):
    if linked(root):
        raise ValueError("fixtures root must not be linked")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("fixtures must be a directory")
    def onerror(error):
        raise error

    for directory, children, _ in os.walk(root, followlinks=False, onerror=onerror):
        children[:] = sorted(name for name in children if not linked(Path(directory) / name))
        folder = Path(directory)
        if folder != root:
            yield folder


def preset_files(folder: Path):
    files = sorted(p for p in folder.iterdir() if p.is_file() and not linked(p))
    sources = [p for p in files if p.suffix.lower() == ".fxp" and not p.name.startswith("._")]
    native = [p for p in files if p.suffix.lower() == ".serumpreset"
              and not p.name.lower().startswith("test_")]
    return sources, native


def rename_folder(folder: Path, root: Path, prefix: str) -> Path:
    root = root.resolve(strict=True)
    if linked(folder):
        raise ValueError("linked fixture folder")
    folder = folder.resolve(strict=True)
    if folder == root or not folder.is_relative_to(root):
        raise ValueError("folder must be inside fixtures")
    name = folder.name
    if name.lower().startswith("checked_"):
        return folder
    if name.lower().startswith("pair_"):
        name = name[5:]
    target = folder.with_name(prefix + name)
    if target == folder:
        return folder
    if target.exists() or linked(target):
        raise FileExistsError(f"rename target already exists: {target}")
    if not target.resolve().is_relative_to(root):
        raise ValueError("rename target outside fixtures")
    folder.rename(target)
    return target
