#!/usr/bin/env bash
set -euo pipefail
SRC="/home/ob1/Projects/factory-companion/mod/FactorioCompanion"
DEST_DIR="${1:-/tmp/opencode/mods-test}"
VERSION=$(python3 -c "import json;print(json.load(open('$SRC/info.json'))['version'])")
NAME="FactorioCompanion_$VERSION"
mkdir -p "$DEST_DIR"
rm -rf "$DEST_DIR/FactorioCompanion_"* "$DEST_DIR/$NAME"
cp -r "$SRC" "$DEST_DIR/$NAME"
if [ ! -f "$DEST_DIR/mod-list.json" ]; then
  cat > "$DEST_DIR/mod-list.json" <<'EOF'
[
  { "name": "FactorioCompanion", "enabled": true }
]
EOF
fi
echo "deployed to $DEST_DIR"
