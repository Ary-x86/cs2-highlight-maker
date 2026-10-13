#!/usr/bin/env bash
# Build the CS:DM CS2 server plugin with frame-hook.patch applied.
# Upstream bug: https://github.com/akiver/cs-demo-manager/issues/1458 (tick actions never run since CS2 1.41.7.6).
set -euo pipefail

WORK_DIR="${CS2HL_WORK_DIR:-$HOME/.local/share/cs2hl}"
CSDM_COMMIT="b45a5f29283590c3fd0fc6526564b49567c0b931"
SRC="$WORK_DIR/src/csdm-src"
PATCH="$(cd "$(dirname "$0")" && pwd)/frame-hook.patch"

if [ ! -d "$SRC/.git" ]; then
    git clone --filter=blob:none --sparse https://github.com/akiver/cs-demo-manager.git "$SRC"
    git -C "$SRC" sparse-checkout set cs2-server-plugin
fi
git -C "$SRC" fetch --depth 1 origin "$CSDM_COMMIT"
git -C "$SRC" checkout -q --force "$CSDM_COMMIT"
for dep in easywsclient hl2sdk json; do
    git -C "$SRC" submodule update --init --depth 1 "cs2-server-plugin/cs2-server-plugin/deps/$dep"
done
git -C "$SRC" apply "$PATCH"

cd "$SRC/cs2-server-plugin/cs2-server-plugin"
g++ -std=c++17 -O2 -w -shared -fPIC \
    -DPOSIX=1 -DNO_MALLOC_OVERRIDE=1 -DCOMPILER_GCC=1 -DPLATFORM_POSIX=1 -DX64BITS=1 -DPLATFORM_64BITS=1 \
    -DLINUX=1 -Dstricmp=strcasecmp \
    -I./deps/json/include -I./deps/easywsclient -I./deps/hl2sdk/public -I./deps/hl2sdk/public/tier0 \
    -I./deps/hl2sdk/public/tier1 -I./deps/hl2sdk/public/mathlib \
    -o libserver.so main.cpp ./deps/easywsclient/easywsclient.cpp ./deps/hl2sdk/tier1/convar.cpp \
    -L./deps/hl2sdk/lib/linux64 -ldl -ltier0

mkdir -p "$WORK_DIR/bin"
mv libserver.so "$WORK_DIR/bin/libserver.so"
echo "built $WORK_DIR/bin/libserver.so"
