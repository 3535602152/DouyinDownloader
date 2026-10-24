# Third-party materials

The application's original modifications use MIT. Incorporated Apache-2.0 and
ISC/MIT materials retain their respective terms; the root MIT license does not
replace these licenses. Installed dependencies retain their own licenses.

| Material | Source | Terms and included notices |
| --- | --- | --- |
| Base project and logo; derived Windows icon | [jiji262/douyin-downloader](https://github.com/jiji262/douyin-downloader) | Root `LICENSE` (MIT). The logo matches upstream Git blob `83a5566ee6da16b89759a1e5db06b1c18bc81b9b`. |
| A-Bogus implementation, msToken configuration snapshot | [Johnserf-Seed/f2](https://github.com/Johnserf-Seed/f2) | Apache-2.0; `licenses/F2-LICENSE.txt`; original file attribution retained. |
| X-Bogus and web security signature implementation | [Evil0ctal/Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) | Apache-2.0; `licenses/Apache-2.0.txt`, `licenses/Evil0ctal-NOTICE.txt`. |
| Sun and moon decorative icons | [Lucide](https://lucide.dev/license) / Feather | ISC and MIT; `licenses/Lucide-ISC-Feather-MIT.txt`. |
| FFmpeg 9.0.2 standalone media component | [FFmpeg](https://github.com/FFmpeg/FFmpeg/tree/n9.0.2) | LGPL-2.1-or-later; `licenses/LGPL-2.1.txt`. Built without GPL/nonfree code. |
| LAME 3.100 MP3 encoder in the media component | [LAME](https://lame.sourceforge.io/) | LGPL; `licenses/LAME-COPYING.txt`. |
| MinGW-w64 and GCC runtime used by the media build | Ubuntu 24.04 MinGW-w64 / GCC 13 cross toolchain | `licenses/MinGW-w64-COPYING.txt`, `licenses/GCC-Runtime-Exception.txt` and `licenses/GPL-3.0.txt`; GCC's runtime exception applies to its runtime components. |

Adaptations to the signing implementations include relocation into
`douyin/signing`, Python import/type compatibility adjustments, and integration
with the application's exact-query request serializer. See the retained file
headers and `NOTICE`. No captured Douyin page markup or production stylesheet
is required by the application.

Release packages also contain `DEPENDENCY_LICENSES/` with the license files and
metadata of the Python distributions installed into the isolated build. The
media source archive contains the exact source tarballs, SHA-256 manifest,
configuration and toolchain records used to produce `ffmpeg.exe`, plus the
build recipe. The application does not impose restrictions on replacing that
separate executable with a compatible user-supplied build.
