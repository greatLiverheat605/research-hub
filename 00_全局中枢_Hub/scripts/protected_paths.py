"""Prune protected trees before descending, including through directory aliases."""
import os
from pathlib import Path


def protected(path):
    return any(s in ('benchmark','冻结件') or s.startswith('冻结存档-') for s in Path(path).parts)


def walk(root):
    root = Path(root)
    if protected(root.resolve()) or root.is_symlink():
        return
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not protected(d) and not d.startswith('.') and d not in ('99_历史归档_Archive','node_modules','__pycache__') and not (Path(base)/d).is_symlink()]
        yield base, dirs, files
