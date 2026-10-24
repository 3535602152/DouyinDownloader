"""Static release contracts shared by local validation and CI."""
from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
version = (ROOT / "VERSION").read_text().strip()
assert version == project["version"]
assert f'__version__ = "{version}"' in (ROOT / "__init__.py").read_text()
assert version == json.loads((ROOT / "package.json").read_text())["version"]
assert project["requires-python"] == ">=3.12,<3.15"
for file in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "licenses/Apache-2.0.txt",
             "licenses/LGPL-2.1.txt", "licenses/LAME-COPYING.txt"):
    assert (ROOT / file).is_file(), file
for path in (".accounts.json", ".cookies.json", "browser_profile/Default/Network/Cookies",
             "webview/Default/Network/Cookies", ".env", "private.pem"):
    assert subprocess.run(["git", "check-ignore", "-q", "--no-index", "--", path],
                          cwd=ROOT).returncode == 0, path
names = subprocess.check_output(["git", "ls-files"], cwd=ROOT).decode().splitlines()
for name in names:
    assert not re.search(r"(?:^|/)(?:\.accounts\.json|\.cookies\.json|config\.yml)$|\.(?:db|har|bundle)$", name), name
print("Release contracts passed")
