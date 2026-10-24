"""Install a verified local media archive for source development (no downloads)."""
from __future__ import annotations

import argparse
import shutil
import tempfile
import zipfile
from pathlib import Path

from verify_media import verify


def install(archive: Path, destination: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="douyin-media-") as folder:
        staging = Path(folder)
        with zipfile.ZipFile(archive) as source:
            if sum(item.file_size for item in source.infolist()) > 100 * 1024 * 1024:
                raise ValueError("Unexpectedly large media archive")
            for item in source.infolist():
                target = (staging / item.filename).resolve()
                if not target.is_relative_to(staging):
                    raise ValueError("Unsafe archive member")
            source.extractall(staging)
        verify(staging)
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staging / "bin" / "ffmpeg.exe", destination / "ffmpeg.exe")
        shutil.copy2(staging / "MEDIA_BUILD.json", destination / "MEDIA_BUILD.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    destination = Path(__file__).resolve().parents[1] / ".tools" / "ffmpeg"
    install(args.archive, destination)
    print("Installed verified media component in .tools/ffmpeg")
