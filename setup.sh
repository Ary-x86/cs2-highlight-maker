#!/usr/bin/env bash
# One-time setup. Usage: ./setup.sh [workspace-dir]
# The workspace (venv, plugin build, temp frames, clips) can live on any big disk; it is reached through
# ~/.local/share/cs2hl so nothing heavy lands on the root filesystem.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
LINK="$HOME/.local/share/cs2hl"
TARGET="${1:-}"

if [ -n "$TARGET" ]; then
    mkdir -p "$TARGET"
    ln -sfn "$(cd "$TARGET" && pwd)" "$LINK"
elif [ ! -e "$LINK" ]; then
    mkdir -p "$LINK"
fi
WORK="$(readlink -f "$LINK")"
mkdir -p "$WORK"/{bin,clips,montages,tmp,demos}

for tool in uv ffmpeg g++ git; do
    command -v "$tool" >/dev/null || { echo "missing: $tool"; exit 1; }
done

export UV_CACHE_DIR="$WORK/cache/uv"
[ -x "$WORK/venv/bin/python" ] || uv venv -q "$WORK/venv" -p 3.12
VIRTUAL_ENV="$WORK/venv" uv pip install -q -e "$REPO"

[ -f "$WORK/bin/libserver.so" ] || "$REPO/plugin/build.sh"

mkdir -p "$HOME/.local/bin"
ln -sfn "$WORK/venv/bin/cs2hl" "$HOME/.local/bin/cs2hl"

# Convenience links inside the repo to every outside folder the tool uses.
ln -sfn "$WORK" "$REPO/workspace"
ln -sfn workspace/clips "$REPO/clips"
ln -sfn workspace/montages "$REPO/montages"
REPLAYS="$("$WORK/venv/bin/python" -c 'from cs2hl import config; print(config.load().replays_dir)')"
ln -sfn "$REPLAYS" "$REPO/replays"

echo "done. try: cs2hl list"
