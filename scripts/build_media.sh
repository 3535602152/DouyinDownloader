#!/usr/bin/env bash
# Ubuntu 24.04 cross-build; all media inputs are checked before extraction.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:-$root/build/media}"
mkdir -p "$out/sources" "$out/work" "$out/prefix" "$out/bin"
out="$(cd "$out" && pwd)"
python3 - "$root/media/sources.json" "$out/sources" <<'PY'
import hashlib, json, pathlib, shutil, sys, urllib.request
manifest = pathlib.Path(sys.argv[1])
for item in json.loads(pathlib.Path(sys.argv[1]).read_text()).values():
    target = pathlib.Path(sys.argv[2]) / item['filename']
    bundled = manifest.parent.parent / 'upstream' / item['filename']
    if not target.exists() and bundled.exists():
        shutil.copy2(bundled, target)
    if not target.exists():
        with urllib.request.urlopen(item['url'], timeout=120) as response:
            target.write_bytes(response.read())
    assert hashlib.sha256(target.read_bytes()).hexdigest() == item['sha256'], target.name
PY
export SOURCE_DATE_EPOCH=1727740800
export TZ=UTC LC_ALL=C
tar -xzf "$out/sources/lame-3.100.tar.gz" -C "$out/work"
tar -xzf "$out/sources/ffmpeg-9.0.2.tar.gz" -C "$out/work"
export CC=x86_64-w64-mingw32-gcc-posix
export AR=x86_64-w64-mingw32-ar RANLIB=x86_64-w64-mingw32-ranlib
cd "$out/work/lame-3.100"
./configure --host=x86_64-w64-mingw32 --prefix="$out/prefix" \
  --disable-shared --enable-static --disable-frontend --disable-nasm
make -j2
make install
cd "$out/work/FFmpeg-946fcce07b6dcd0331c8cc609192aeff5e1924f8"
export PKG_CONFIG_LIBDIR="$out/prefix/lib/pkgconfig"
./configure --prefix="$out/prefix" --target-os=mingw32 --arch=x86_64 \
  --enable-cross-compile --cross-prefix=x86_64-w64-mingw32- --cc="$CC" \
  --pkg-config=pkg-config --pkg-config-flags=--static \
  --extra-cflags="-I$out/prefix/include -ffile-prefix-map=$out=/build" \
  --extra-ldflags="-L$out/prefix/lib -static -static-libgcc" \
  --disable-autodetect --disable-gpl --disable-nonfree --disable-version3 \
  --disable-shared --enable-static --disable-debug --disable-doc --disable-x86asm \
  --disable-network --disable-everything --disable-programs --enable-ffmpeg \
  --enable-libmp3lame --enable-encoder=libmp3lame --enable-muxer=mp3 \
  --enable-protocol=file,pipe --enable-demuxer=mov,mp3,wav,aac,ogg,flac,matroska \
  --enable-decoder=aac,mp3,mp3float,pcm_s16le,pcm_s24le,pcm_s32le,pcm_f32le,vorbis,flac,opus \
  --enable-parser=aac,mpegaudio,opus,vorbis \
  --enable-filter=anull,aresample,aformat
make -j2
cp ffmpeg.exe "$out/bin/ffmpeg.exe"
cp "$root/media/sources.json" "$out/sources/"
cp "$root/scripts/build_media.sh" "$out/sources/"
cp COPYING.LGPLv2.1 "$out/sources/FFmpeg-COPYING.LGPLv2.1"
cp "$out/work/lame-3.100/COPYING" "$out/sources/LAME-COPYING"
cp ffbuild/config.mak "$out/sources/ffmpeg-config.mak"
dpkg-query -W -f='${Package}=${Version}\n' \
  gcc-mingw-w64-x86-64-posix mingw-w64-x86-64-dev binutils-mingw-w64-x86-64 make pkg-config \
  > "$out/sources/toolchain-packages.txt"
"$CC" --version > "$out/sources/compiler.txt"
python3 - "$out" <<'PY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
exe = root / 'bin/ffmpeg.exe'
data = json.loads((root/'sources/sources.json').read_text())
data['binary'] = {'filename': 'ffmpeg.exe', 'sha256': hashlib.sha256(exe.read_bytes()).hexdigest(),
                  'license': 'LGPL-2.1-or-later', 'target': 'windows-x86_64'}
(root/'MEDIA_BUILD.json').write_text(json.dumps(data, indent=2)+'\n')
PY
echo "Media component built: $out/bin/ffmpeg.exe"
