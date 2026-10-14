# CS2 Highlight Maker

Free, local alternative to Leetify-style highlight clips, for Linux. Log in with Steam, click **Make highlights of my latest match**, and get a clip of every multi-kill and clutch plus a montage of the whole match.

- **Smooth recording.** CS2 itself renders every frame at a fixed frame rate (120 fps, or 240 fps blended to 60 fps for motion blur). It isn't screen capture, so a busy PC can't make it stutter.
- **Downloads your matches for you.** Ranked Competitive, Premier, Rush, Wingman and other replays come from your Steam match history and are deleted after rendering. A replay you already downloaded yourself is used instead.
- **You pick what counts.** 3k / 4k / ace / clutch rounds, plus optional special shots: flicks, sniper flicks, spray transfers, long range kills, no-scopes, wallbangs, kills through smoke, jump shots and blind kills.
- **1080p or 1440p, normal or Very High graphics.** Record at 1440p and save at 1080p for extra sharpness. Graphics are only raised while recording.
- **Crosshairs.** Use donk's, a classic one, or build your own, with a live preview.
- **Explains what happened.** You get live progress, time left and a log. Errors say what went wrong and how to fix it.

## Install (one command)

```sh
git clone https://github.com/<you>/cs2-highlight-maker && cd cs2-highlight-maker && ./install.sh
```

The installer:
- installs anything that's missing (ffmpeg, a C++ compiler, uv), after asking you first
- asks where to keep the workspace; pick a disk with a few GB free
- builds the recording plugin
- adds **CS2 Highlight Maker** to your app menu and opens it

Requirements: Linux, Steam with CS2 installed, a GPU that runs CS2. Encoding uses NVIDIA NVENC or AMD/Intel VAAPI when available and falls back to the CPU. GPU encoding is tested on an RTX 3070 only; other GPUs are a work in progress.

## Using it

1. **Steam account** page: scan the QR code with the Steam mobile app, or log in with your password and Steam Guard code. The login is stored only on your PC.
2. **Highlights** page: click **Make highlights of my latest match**, or tick matches and press **+ Add to queue**. **Demos on this PC** lists `.dem` / `.dem.bz2` files from your CS2 replays folder. **Open on Steam ↗** opens the Steam page with the *Download Replay* buttons; put a replay you download there in the replays folder (or leave it in `~/Downloads`) and the app uses it instead of downloading again.
3. Watch the progress panel. CS2 opens, records, and closes by itself; don't touch it while it works. A match with four highlights takes about 15 minutes in Smooth mode and about 30 with motion blur.
4. **My videos** page: play the clips, or open the folder they're in. Click **✔ made** on a match to jump to its videos.

Everything is explained on the in-app **Help** page.

### Video styles

| style | how | result |
|---|---|---|
| Smooth · 120 fps (default) | every game frame is kept | crisp; can be slowed 2× in an editor |
| Motion blur · 60 fps | 240 fps rendered, 4 frames averaged per output frame | soft, cinematic blur on fast flicks; about 2× slower |

### Crosshairs

- **donk** (default): tiny cyan static cross
- **Classic**: white four lines with a centre dot and outline
- **Custom**: build your own in Settings

Your own CS2 settings are never changed. Anything the recording session touches (volume, HUD, crosshair) is set back to your values before CS2 quits, and your video settings (display mode, resolution, graphics) are restored from a copy once CS2 has closed.

## How it works

1. [demoparser2](https://github.com/LaihoE/demoparser) finds the rounds and kills you picked in Settings (by default 3k, 4k, aces and won 1v2+ clutches). Flicks are measured from your view angles in the 0.2 s before the first hit; spray transfers from continuous `weapon_fire` events. Long pauses between kills are cut out.
2. The [CS Demo Manager](https://github.com/akiver/cs-demo-manager) CS2 server plugin runs console commands at given demo ticks. The app loads the plugin into CS2 (`-insecure`, so no VAC risk), and CS2 then seeks to each highlight, spectates you and records with `startmovie … jpg wav` at a locked `host_framerate`.
3. ffmpeg encodes the frames (NVENC / VAAPI, or x264 as fallback), joins multi-part highlights and builds the montage. Encoding takes seconds per clip; most of the time is CS2 rendering (roughly 10–15 frames per second).

The upstream plugin stopped running tick actions after the 2026-08-19 CS2 update ([cs-demo-manager#1458](https://github.com/akiver/cs-demo-manager/issues/1458)). `plugin/frame-hook.patch` fixes that, and `plugin/build.sh` builds the patched plugin. The installer runs it for you.

Match history is read from Steam's *Personal Game Data* pages (the ones with **Download Replay** buttons). The replay links point at Valve's own servers over plain HTTP, which is why browsers call them unsafe.

## Command line

```sh
cs2hl               # open the app
cs2hl latest        # render your latest Steam match
cs2hl run [demo…]   # render new demos in the CS2 replays folder (or the given files)
cs2hl list [demo…]  # show detected highlights without rendering
cs2hl watch         # keep running, render demos as they appear in the replays folder
cs2hl clean         # remove the plugin from CS2 after a crash
cs2hl config        # print settings
```

Settings live in `~/.local/share/cs2hl/config.toml`. The GUI writes this file; you can also edit it by hand. To run the watcher at login:

```sh
mkdir -p ~/.config/systemd/user && cp systemd/cs2hl-watch.service ~/.config/systemd/user/
systemctl --user enable --now cs2hl-watch
```

## Development

```sh
~/.local/share/cs2hl/venv/bin/python -m unittest discover -s tests
```
