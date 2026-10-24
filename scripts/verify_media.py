"""Verify the separately built Windows media binary before packaging/installing it."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def verify(root: Path) -> dict:
    data = json.loads((root / "MEDIA_BUILD.json").read_text(encoding="utf-8"))
    exe = root / "bin" / "ffmpeg.exe"
    if not exe.exists():
        exe = root / "ffmpeg.exe"
    if hashlib.sha256(exe.read_bytes()).hexdigest() != data["binary"]["sha256"]:
        raise ValueError("Media executable SHA-256 mismatch")
    if (root / "sources").is_dir():
        for name in ("ffmpeg", "lame"):
            source = root / "sources" / data[name]["filename"]
            if hashlib.sha256(source.read_bytes()).hexdigest() != data[name]["sha256"]:
                raise ValueError(f"Corresponding source SHA-256 mismatch: {name}")
    result = subprocess.run([str(exe.resolve()), "-L"], capture_output=True, text=True,
                            check=True, timeout=30)
    text = result.stdout + result.stderr
    if "GNU Lesser General Public" not in text:
        raise ValueError("Expected an LGPL media component")
    if "--enable-gpl" in text or "--enable-nonfree" in text or "--enable-version3" in text:
        raise ValueError("Unexpected GPL/nonfree configuration")
    encoders = subprocess.run([str(exe.resolve()), "-encoders"], capture_output=True,
                             text=True, check=True, timeout=30)
    if "libmp3lame" not in encoders.stdout:
        raise ValueError("Required libmp3lame encoder unavailable")
    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    verified = verify(args.directory)
    print("Verified LGPL media component:", verified["binary"]["sha256"])
