"""Pure-Python implementation of Douyin's web security signature.

The web SDK calls this value ``x-secsdk-web-signature``.  It is an MD5 over
the exact query string sent after the normal ``a_bogus``/``X-Bogus`` signing
step.  Keeping the serializer here deliberately small makes it possible for
the HTTP client to sign and send one immutable query string.

Algorithm and salt ported from Evil0ctal/Douyin_TikTok_Download_API's native
``websign.py`` implementation (Apache-2.0).
"""

from __future__ import annotations

import hashlib
import time
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

# String #39 from Douyin's @byted/secsdk-strategy web bundle.
SALT = "A96D855A08C0A9707F8BEF0D9A527E4E"

# Douyin currently mints UIFID_TEMP, while older/browser-captured jars may
# expose the same value under one of the other spellings accepted by the SDK.
UIFID_COOKIE_NAMES = (
    "uifid",
    "uifid_temp",
    "uifidtemp",
    "UIFID",
    "UIFID_TEMP",
    "UIFIDTEMP",
)
VERIFY_FP_COOKIE = "s_v_web_id"

SIGNATURE_PARAM = "x-secsdk-web-signature"
UIFID_PARAM = "uifid"
TIMESTAMP_PARAM = "timestamp"
VERIFY_FP_PARAMS = ("verifyFp", "fp")
EXPIRE_HEADER = "x-secsdk-web-expire"

# Paths copied from the web SDK's webSign protection table.  The post endpoint
# is the monitored-creator path; the remaining entries keep detail/list calls
# coherent when they are made with the same authenticated session.
DOUYIN_SIGNED_PATHS = frozenset(
    {
        "/aweme/v1/web/aweme/detail/",
        "/aweme/v1/web/aweme/post/",
        "/aweme/v1/web/aweme/favorite/",
        "/aweme/v1/web/aweme/listcollection/",
        "/aweme/v1/web/mix/aweme/",
        "/aweme/v1/web/tab/feed/",
        "/aweme/v1/web/mix/list/",
        "/aweme/v1/web/music/aweme/",
        "/aweme/v1/web/music/list/",
        "/aweme/v1/web/mix/detail/",
        "/aweme/v1/web/mix/listcollection/",
        "/aweme/v1/web/music/detail/",
        "/aweme/v1/web/collects/list/",
        "/aweme/v1/web/collects/video/list/",
    }
)


def pick_uifid(cookies: Optional[Mapping[str, str]]) -> Optional[str]:
    """Return the first non-empty UIFID value from the current cookie jar."""

    for name in UIFID_COOKIE_NAMES:
        value = str((cookies or {}).get(name) or "").strip()
        if value:
            return value
    return None


def encode_pairs(pairs: Iterable[Tuple[str, str]]) -> str:
    """Serialize pairs exactly once, like the SDK's URLSearchParams string."""

    def encode(value: str) -> str:
        # ``urllib.parse.quote`` always leaves ``~`` unescaped, whereas the
        # application/x-www-form-urlencoded serializer used by
        # URLSearchParams escapes it.  Correct that one difference explicitly.
        return quote(str(value), safe="*-._").replace("~", "%7E")

    return "&".join(
        "{}={}".format(
            encode(key),
            encode(value),
        )
        for key, value in pairs
    )


def sign(
    pairs: Sequence[Tuple[str, str]],
    uifid: str,
    *,
    timestamp: Optional[int] = None,
) -> Tuple[str, str, Dict[str, str]]:
    """Return ``(query, signature, headers)`` for an already-complete query.

    ``pairs`` must be in the order used by the request.  The visitor id and
    timestamp are appended before hashing, and the signature itself is the
    final query parameter.  A pre-existing lowercase ``uifid`` is retained in
    place, matching the upstream SDK and avoiding a second copy in the
    preimage.
    """

    stamp = str(int(time.time() if timestamp is None else timestamp))
    covered = list(pairs)
    if not any(name == UIFID_PARAM for name, _ in covered):
        covered.append((UIFID_PARAM, str(uifid)))
    covered.append((TIMESTAMP_PARAM, stamp))
    query = encode_pairs(covered)
    signature = hashlib.md5(
        "{}_{}_{}_{}".format(str(uifid), stamp, SALT, query).encode("utf-8")
    ).hexdigest()
    headers = {
        UIFID_PARAM: str(uifid),
        SIGNATURE_PARAM: signature,
        EXPIRE_HEADER: stamp,
    }
    return "{}&{}={}".format(query, SIGNATURE_PARAM, signature), signature, headers


__all__ = [
    "EXPIRE_HEADER",
    "DOUYIN_SIGNED_PATHS",
    "SALT",
    "SIGNATURE_PARAM",
    "TIMESTAMP_PARAM",
    "UIFID_COOKIE_NAMES",
    "UIFID_PARAM",
    "VERIFY_FP_COOKIE",
    "VERIFY_FP_PARAMS",
    "encode_pairs",
    "pick_uifid",
    "sign",
]
