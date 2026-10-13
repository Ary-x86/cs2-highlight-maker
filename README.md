# cs2-highlight-maker

Local, free replacement for Leetify-style highlight clips on Linux. It finds your multikills and clutches in CS2 demos, has CS2 render them frame by frame at a fixed framerate (no screen capture, so the output stays smooth whatever the PC load), and encodes per-highlight clips plus a per-match montage.

## How it works

1. **Detect**: [demoparser2](https://github.com/LaihoE/demoparser) reads the demo. Every Steam account that has logged in on this machine counts as "you". A highlight is a round with 3+ kills or a won 1v2+ clutch. Long gaps between kills get cut.
2. **Record**: the [CS Demo Manager](https://github.com/akiver/cs-demo-manager) CS2 server plugin runs console commands at given demo ticks. `cs2hl` writes that action list and launches CS2 with `-insecure`. CS2 seeks to each highlight, spectates you, and records with `startmovie … jpg wav` under `host_framerate`. Then it quits.
3. **Encode**: ffmpeg + NVENC turns the frames into H.264 MP4. Multi-part highlights are joined, and all clips of a match are stitched into a montage.

The upstream plugin stopped running tick actions after the 2026-08-19 CS2 update ([cs-demo-manager#1458](https://github.com/akiver/cs-demo-manager/issues/1458)). `plugin/frame-hook.patch` fixes this by hooking `FrameStageNotify` as soon as the client interface exists. `plugin/build.sh` builds the patched `libserver.so`.

## Setup

Needs: Steam with CS2, `uv`, `ffmpeg` (with NVENC for the default encoder), `g++`, `git`.

```sh
./setup.sh "/path/on/a/big/disk/cs2-highlights"
```

All heavy data lives in that folder, which is reached through `~/.local/share/cs2hl`: venv, plugin build, temporary frames and finished videos. `cs2hl` gets linked into `~/.local/bin`. The repo gets `workspace`, `clips`, `montages` and `replays` symlinks.

## Use

Download a match in CS2 (Watch → Your Matches → download), or drop any `.dem` into the replays folder. Then run:

```sh
cs2hl list               # show detected highlights, no rendering
cs2hl run                # render every demo not processed yet
cs2hl run --mode blur60  # motion-blur variant (240 fps blended to 60)
cs2hl watch              # keep running and render new demos as they appear
cs2hl clean              # undo the CS2 plugin install after a hard interruption
```

While rendering, CS2 opens and closes by itself. Leave it alone. The watcher waits whenever you are playing.

To start the watcher automatically when you log in:

```sh
mkdir -p ~/.config/systemd/user && cp systemd/cs2hl-watch.service ~/.config/systemd/user/
systemctl --user enable --now cs2hl-watch
```

## Modes

| mode | render | output | look |
|---|---|---|---|
| `smooth120` (default) | 120 fps | 1080p120 | crisp, smooth |
| `blur60` | 240 fps | 1080p60 | 4 frames blended per output frame, cinematic motion blur |

## Config

Optional `~/.local/share/cs2hl/config.toml`. Any field of `cs2hl/config.py:Config` can be set, for example:

```toml
mode = "blur60"
min_kills = 4
pre_seconds = 4.0
steamids = ["7656119..."]   # default: every account in Steam's loginusers.vdf
```

`cs2hl config` prints the effective settings.

## Output

- `clips/<date>_<map>_<id>/r03_4k_deagle.mp4`: one file per highlight, named by round, kills and weapon
- `montages/<date>_<map>_<id>.mp4`: every highlight of the match in order

Processed demos are tracked in `~/.local/share/cs2hl/state.json`. Use `run --force` to redo them.
