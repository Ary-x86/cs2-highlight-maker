#!/usr/bin/env bash
# CS2 Highlight Maker installer. Run from the repo:  ./install.sh [--workspace DIR] [--no-launch]
#
# The workspace holds the Python environment, the recording plugin, temporary frames (a few GB while
# rendering) and, by default, your videos. Put it on a disk with free space; it is reached through
# ~/.local/share/cs2hl so the path has no spaces (CS2 needs that).
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
LINK="$HOME/.local/share/cs2hl"
WORKSPACE=""
LAUNCH=1
while [ $# -gt 0 ]; do
    case "$1" in
        --workspace) WORKSPACE="$2"; shift 2 ;;
        --no-launch) LAUNCH=0; shift ;;
        *) echo "unknown option $1"; exit 1 ;;
    esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Linux ] || fail "Only Linux is supported (CS2 runs natively there)."

# --- system packages ----------------------------------------------------------------------------
missing=()
for tool in git g++ ffmpeg curl; do command -v "$tool" >/dev/null || missing+=("$tool"); done
if [ ${#missing[@]} -gt 0 ]; then
    bold "Missing programs: ${missing[*]}"
    pkgs=("${missing[@]/g++/gcc}")
    if command -v pacman >/dev/null; then cmd=(sudo pacman -S --needed "${pkgs[@]}")
    elif command -v apt-get >/dev/null; then cmd=(sudo apt-get install -y "${pkgs[@]/gcc/g++}")
    elif command -v dnf >/dev/null; then cmd=(sudo dnf install -y "${pkgs[@]/gcc/gcc-c++}")
    elif command -v zypper >/dev/null; then cmd=(sudo zypper install -y "${pkgs[@]/gcc/gcc-c++}")
    else fail "Install ${missing[*]} with your package manager, then run this again."; fi
    read -rp "Install them now with: ${cmd[*]} ? [Y/n] " ans
    [[ "${ans:-y}" =~ ^[Yy] ]] || fail "Install ${missing[*]} and run this again."
    "${cmd[@]}"
fi
if ! command -v uv >/dev/null && [ ! -x "$HOME/.local/bin/uv" ]; then
    bold "Installing uv (Python package manager) into ~/.local/bin"
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"

# --- workspace -------------------------------------------------------------------------------------
if [ -z "$WORKSPACE" ] && [ ! -e "$LINK" ] && [ -t 0 ]; then
    echo
    bold "Where should the workspace go?"
    echo "It needs ~1 GB plus a few GB free while rendering. Free space here: $(df -h --output=avail "$HOME" | tail -1 | tr -d ' ')"
    read -rp "Folder [$LINK]: " WORKSPACE
fi
if [ -n "$WORKSPACE" ]; then
    WORKSPACE="${WORKSPACE/#\~/$HOME}"
    mkdir -p "$WORKSPACE"
    WORKSPACE="$(cd "$WORKSPACE" && pwd)"
    if [ "$WORKSPACE" != "$LINK" ]; then
        [ -e "$LINK" ] && [ ! -L "$LINK" ] && fail "$LINK already exists as a real folder; move it first."
        mkdir -p "$(dirname "$LINK")"
        ln -sfn "$WORKSPACE" "$LINK"
    fi
fi
mkdir -p "$LINK"
WORK="$(readlink -f "$LINK")"
mkdir -p "$WORK"/{bin,tmp,demos}
bold "Workspace: $WORK"

# --- python environment ------------------------------------------------------------------------------
export UV_CACHE_DIR="$WORK/cache/uv"
[ -x "$WORK/venv/bin/python" ] || "$UV" venv -q "$WORK/venv" -p 3.12
VIRTUAL_ENV="$WORK/venv" "$UV" pip install -q -e "$REPO"

# --- recording plugin -------------------------------------------------------------------------------
if [ ! -f "$WORK/bin/libserver.so" ] || [ "$REPO/plugin/frame-hook.patch" -nt "$WORK/bin/libserver.so" ]; then
    bold "Building the CS2 recording plugin (one time, ~150 MB download)"
    CS2HL_WORK_DIR="$WORK" "$REPO/plugin/build.sh"
fi

# --- launcher ---------------------------------------------------------------------------------------
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
ln -sfn "$WORK/venv/bin/cs2hl" "$HOME/.local/bin/cs2hl"
cp "$REPO/cs2hl/gui/static/icon.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/cs2hl.svg"
cat > "$HOME/.local/share/applications/cs2hl.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=CS2 Highlight Maker
Comment=Turn your CS2 matches into highlight clips
Exec=$HOME/.local/bin/cs2hl gui
Icon=cs2hl
Terminal=false
Categories=AudioVideo;Video;Game;
StartupWMClass=cs2hl
EOF

# Convenience links inside the repo to the folders the app uses (ignored by git).
ln -sfn "$WORK" "$REPO/workspace"
CLIPS="$("$WORK/venv/bin/python" -c 'from cs2hl import config; c = config.load(); print(c.output_path)' 2>/dev/null || true)"
[ -n "$CLIPS" ] && ln -sfn "$CLIPS" "$REPO/videos"
REPLAYS="$("$WORK/venv/bin/python" -c 'from cs2hl import config; print(config.load().replays_dir)' 2>/dev/null || true)"
[ -n "$REPLAYS" ] && ln -sfn "$REPLAYS" "$REPO/replays"

echo
bold "Done! Open 'CS2 Highlight Maker' from your app menu, or run: cs2hl"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "(add ~/.local/bin to your PATH to use the cs2hl command)";; esac
[ "$LAUNCH" = 1 ] && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && (nohup "$HOME/.local/bin/cs2hl" gui >/dev/null 2>&1 &)
exit 0
