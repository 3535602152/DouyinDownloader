from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def test_wheel_contains_the_cli_desktop_and_frontend(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE", "NOTICE",
                 "THIRD_PARTY_NOTICES.md", "desktop_app.py"):
        shutil.copy2(root / name, source / name)
    shutil.copytree(root / "licenses", source / "licenses")
    for name in ("cli", "common", "douyin", "engine", "gui", "services", "storage"):
        shutil.copytree(root / name, source / name, ignore=shutil.ignore_patterns("__pycache__"))
    output = tmp_path / "wheels"
    subprocess.run([sys.executable, "-c",
                    "from setuptools.build_meta import build_wheel; import sys; "
                    "build_wheel(sys.argv[1])", str(output)], cwd=source,
                   check=True, capture_output=True, text=True)
    wheel, = output.glob("*.whl")
    unpacked = tmp_path / "unpacked"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert any(name.endswith("/licenses/NOTICE") for name in names)
        assert any(name.endswith("/licenses/licenses/Apache-2.0.txt") for name in names)
        assert {"desktop_app.py", "cli/main.py", "services/app.py", "storage/db.py"} <= names
        for resource in (root / "gui" / "web").rglob("*"):
            if resource.is_file():
                assert resource.relative_to(root).as_posix() in names
        entry_points = next(name for name in names if name.endswith("/entry_points.txt"))
        assert "douyin-dl = cli.main:main" in archive.read(entry_points).decode()
        archive.extractall(unpacked)
    env = dict(os.environ, PYTHONPATH=str(unpacked))
    check = """
import pathlib, sys
sys.path.insert(0, str(pathlib.Path.cwd()))
import cli.main, desktop_app, gui.bridge, services.app
for module in (cli.main, desktop_app, gui.bridge, services.app):
    assert pathlib.Path.cwd() in pathlib.Path(module.__file__).resolve().parents
"""
    subprocess.run([sys.executable, "-c", check], cwd=unpacked, env=env,
                   check=True, capture_output=True, text=True)
    result = subprocess.run([sys.executable, "-c",
                             "import pathlib,sys,runpy; sys.path.insert(0,str(pathlib.Path.cwd())); "
                             "runpy.run_module('cli',run_name='__main__')", "--help"], cwd=unpacked, env=env,
                            check=True, capture_output=True, text=True)
    assert "douyin-dl" in result.stdout
