#!/usr/bin/env bash
# Сборка all_chats_download.plugin — zip-архива плагина для AyuGram.
set -euo pipefail
cd "$(dirname "$0")"

python3 - <<'PY'
import zipfile, pathlib
files = ["plugin.toml", "plugin.py", "icon.png", "README.txt"]
with zipfile.ZipFile("all_chats_download.plugin", "w", zipfile.ZIP_DEFLATED) as z:
    for f in files:
        p = pathlib.Path(f)
        if not p.exists():
            raise SystemExit("missing %s" % f)
        z.write(p, f)
print("built all_chats_download.plugin")
PY
