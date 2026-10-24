"""Scan an isolated source snapshot with a checksum-verified Gitleaks release."""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "8.30.1"
ROOT = Path(__file__).resolve().parents[1]


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def bootstrap() -> Path:
    system = "windows" if sys.platform == "win32" else "linux"
    extension = "zip" if system == "windows" else "tar.gz"
    name = f"gitleaks_{VERSION}_{system}_x64.{extension}"
    base = f"https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}"
    cache = ROOT / ".tools" / f"gitleaks-{VERSION}"
    exe = cache / ("gitleaks.exe" if system == "windows" else "gitleaks")
    checksum = download(f"{base}/gitleaks_{VERSION}_checksums.txt").decode()
    expected = next(line.split()[0] for line in checksum.splitlines() if line.endswith(name))
    archive = cache / name
    cache.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        archive.write_bytes(download(f"{base}/{name}"))
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise ValueError("Gitleaks archive checksum mismatch")
    if system == "windows":
        with zipfile.ZipFile(archive) as source:
            exe.write_bytes(source.read("gitleaks.exe"))
    else:
        with tarfile.open(archive) as source:
            exe.write_bytes(source.extractfile("gitleaks").read())
        exe.chmod(0o755)
    return exe


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    exe = bootstrap()
    report = ROOT / "build" / "secret-scan.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    base = [str(exe), "--no-banner", "--redact=100", "--config", str(ROOT / ".gitleaks.toml"),
            "--report-format=json", "--report-path", str(report)]
    if args.history:
        return subprocess.call(base + ["git", str(ROOT), "--log-opts=--all"])
    if args.directory:
        return subprocess.call(base + ["dir", str(args.directory)])
    with tempfile.TemporaryDirectory(prefix="douyin-source-scan-") as folder:
        snapshot = Path(folder)
        names = subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT
        ).decode("utf-8").split("\0")
        for name in set(names):
            source = ROOT / name
            if not name or not source.is_file():
                continue
            target = snapshot / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return subprocess.call(base + ["dir", str(snapshot)], env=dict(os.environ))


if __name__ == "__main__":
    raise SystemExit(main())
