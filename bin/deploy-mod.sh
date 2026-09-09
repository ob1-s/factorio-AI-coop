#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$PROJECT_ROOT/mod/FactorioCompanion"
DEST_DIR="${1:-$PROJECT_ROOT/.factorio-mods}"
VERSION=$(python3 -c "import json;print(json.load(open('$SRC/info.json'))['version'])")
NAME="FactorioCompanion_$VERSION"

mkdir -p "$DEST_DIR"
# Only remove versioned copies of this mod from the explicitly selected
# destination. Other mods and the destination's mod-list are preserved.
find "$DEST_DIR" -mindepth 1 -maxdepth 1 -type d -name 'FactorioCompanion_*' -exec rm -rf -- {} +
cp -r "$SRC" "$DEST_DIR/$NAME"

# Factorio expects an object with a "mods" array (not a bare array). Preserve
# an existing list while ensuring this mod is enabled for the test/development
# destination.
python3 - "$DEST_DIR/mod-list.json" <<'PY'
import json
import os
import sys

path = sys.argv[1]
if os.path.exists(path):
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
else:
    document = {"mods": []}

if not isinstance(document, dict):
    raise SystemExit("mod-list.json must contain an object")
mods = document.setdefault("mods", [])
if not isinstance(mods, list):
    raise SystemExit("mod-list.json mods must be an array")

for entry in mods:
    if isinstance(entry, dict) and entry.get("name") == "FactorioCompanion":
        entry["enabled"] = True
        break
else:
    mods.append({"name": "FactorioCompanion", "enabled": True})

with open(path, "w", encoding="utf-8") as handle:
    json.dump(document, handle, indent=2)
    handle.write("\n")
PY

echo "deployed $NAME to $DEST_DIR"
