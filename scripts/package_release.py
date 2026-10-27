"""Assemble a Windows release ZIP with corresponding sources and licenses."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from verify_media import verify

ROOT = Path(__file__).resolve().parents[1]


def zip_tree(root: Path, target: Path) -> None:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(root))


def assemble(app: Path, media: Path, output: Path) -> None:
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT).strip():
        raise RuntimeError("Release packaging requires a clean worktree")
    verify(media)
    version = (ROOT / "VERSION").read_text().strip()
    output.mkdir(parents=True, exist_ok=True)
    for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "README.md"):
        shutil.copy2(ROOT / name, app / name)
    shutil.copytree(ROOT / "licenses", app / "licenses", dirs_exist_ok=True)
    shutil.copytree(ROOT / "docs" / "images", app / "docs" / "images", dirs_exist_ok=True)
    shutil.copy2(ROOT / "docs" / "releasing.md", app / "docs" / "releasing.md")
    (app / "media").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "media" / "README.md", app / "media" / "README.md")
    dependency_root = app / "DEPENDENCY_LICENSES"
    dependency_root.mkdir(exist_ok=True)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("Python runtime license is missing from the build interpreter")
    shutil.copy2(python_license, dependency_root / "Python-LICENSE.txt")
    metadata = []
    for dist in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata["Name"]):
        name = dist.metadata["Name"]
        metadata.append({"name": name, "version": dist.version,
                         "license": dist.metadata.get("License-Expression") or dist.metadata.get("License")})
        for file in dist.files or []:
            if ".dist-info" not in str(file):
                continue
            if file.name == "METADATA" or any(token in file.name.lower() for token in ("license", "copying", "notice")):
                source = Path(dist.locate_file(file))
                if source.is_file():
                    target = dependency_root / name / Path(*file.parts[1:])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    (app / "DEPENDENCIES.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    info = {"version": version, "commit": sha, "dirty": False,
            "media_sha256": json.loads((media / "MEDIA_BUILD.json").read_text())["binary"]["sha256"]}
    (app / "BUILD_INFO.json").write_text(json.dumps(info, indent=2) + "\n")
    sources = media / "sources"
    with zipfile.ZipFile(app / "media-sources.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(ROOT / "media" / "README.md", "README.md")
        for name in ("sources.json", "toolchain-packages.txt"):
            archive.write(ROOT / "media" / name, "media/" + name)
        archive.write(ROOT / "scripts" / "build_media.sh", "scripts/build_media.sh")
        for path in sources.iterdir():
            if path.is_file():
                prefix = "upstream/" if path.name.endswith(".tar.gz") else "build-records/"
                archive.write(path, prefix + path.name)
        for path in (ROOT / "licenses").iterdir():
            archive.write(path, "licenses/" + path.name)
    zip_tree(app, output / f"DouyinDownloader-{version}-windows-x64.zip")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--media", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    assemble(args.app.resolve(), args.media.resolve(), args.output.resolve())
