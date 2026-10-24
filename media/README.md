# Source-built media component

The separate FFmpeg process converts local soundtracks to MP3. FFmpeg 9.0.2 and
LAME 3.100 sources are fixed and SHA-256 checked in `sources.json`. GPL, nonfree,
version3, network support and unrelated external libraries are disabled. Live
photos are assembled by Python and need no video encoder.

On Ubuntu 24.04, install the versions in `toolchain-packages.txt`, then build:

```sh
sudo apt-get update
sudo apt-get install $(cat media/toolchain-packages.txt)
bash scripts/build_media.sh
```

Outputs include `bin/ffmpeg.exe`, `MEDIA_BUILD.json` and `sources/` with exact
source archives, checksums, licenses, compiler/configuration records and recipe.
There are no source patches. MinGW GCC uses its upstream runtime exception.

Windows validation and installation:

```powershell
python scripts/verify_media.py build/media
python scripts/prepare_media.py path\to\DouyinDownloader-media-windows-x64.zip
```

Resolution order: `IMAGEIO_FFMPEG_EXE`, bundled `ffmpeg/`, development
`.tools/ffmpeg/`, then PATH. Invalid explicit overrides fail. Application startup
never downloads components. Recipients may replace the separate executable.

The media component and complete corresponding-source archive accompany the
same draft release as the application and must remain available to every recipient
when the release is later published.
