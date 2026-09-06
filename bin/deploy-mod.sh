#!/usr/bin/env bash
set -euo pipefail
SRC="/home/ob1/Projects/factory-companion/mod/FactorioCompanion"
DEST_DIR="${1:-/tmp/opencode/mods-test}"
mkdir -p "$DEST_DIR"
rm -rf "$DEST_DIR/FactorioCompanion_0.1.0"
cp -r "$SRC" "$DEST_DIR/FactorioCompanion_0.1.0"
if [ ! -f "$DEST_DIR/mod-list.json" ]; then
  cat > "$DEST_DIR/mod-list.json" <<'EOF'
[
  { "name": "FactorioCompanion", "enabled": true }
]
EOF
fi
echo "deployed to $DEST_DIR"
