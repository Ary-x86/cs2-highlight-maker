"""Turn recorded JPG frames + WAV into finished clips, and clips into montages."""

from __future__ import annotations

import subprocess
from pathlib import Path


def find_take(raw_dir: Path, name: str) -> Path:
    """startmovie writes each take into its own timestamped folder."""
    matches = [p.parent for p in raw_dir.glob(f"*/{name}.wav")]
    if not matches:
        matches = [p.parent for p in raw_dir.glob(f"*/{name}00000000.jpg")]
    if not matches:
        raise FileNotFoundError(f"no frames recorded for {name} in {raw_dir}")
    return max(matches, key=lambda p: p.stat().st_mtime)


def _encoder_args(codec: str, quality: int) -> list[str]:
    if codec.endswith("_nvenc"):
        return ["-c:v", codec, "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", str(quality), "-b:v", "0",
                "-pix_fmt", "yuv420p"]
    return ["-c:v", codec, "-preset", "medium", "-crf", str(quality), "-pix_fmt", "yuv420p"]


def encode_clip(takes: list[tuple[Path, str]], out: Path, render_fps: int, blend: int,
                codec: str = "h264_nvenc", quality: int = 19) -> None:
    """Concatenate one or more takes (folder, name) into a single video.

    blend > 1 averages that many consecutive rendered frames into each output frame, which turns a high
    render framerate into natural motion blur at render_fps / blend.
    """
    inputs: list[str] = []
    graph = ""
    pairs = ""
    for i, (folder, name) in enumerate(takes):
        video, audio = 2 * i, 2 * i + 1
        inputs += ["-framerate", str(render_fps), "-i", str(folder / f"{name}%08d.jpg")]
        wav = folder / f"{name}.wav"
        if wav.exists():
            inputs += ["-i", str(wav)]
        else:
            inputs += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        # Cut audio to the take's video length so takes stay in sync after concat.
        seconds = len(list(folder.glob(f"{name}*.jpg"))) / render_fps
        graph += f"[{audio}:a]atrim=0:{seconds:.4f},asetpts=PTS-STARTPTS[a{i}];"
        pairs += f"[{video}:v][a{i}]"
    graph += f"{pairs}concat=n={len(takes)}:v=1:a=1[v][a];"
    if blend > 1:
        graph += f"[v]tmix=frames={blend},fps={render_fps // blend}[vo]"
    else:
        graph += "[v]null[vo]"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
           "-filter_complex", graph, "-map", "[vo]", "-map", "[a]",
           *_encoder_args(codec, quality), "-c:a", "aac", "-b:a", "256k",
           "-movflags", "+faststart", str(out)]
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd, check=True)


def montage(clips: list[Path], out: Path) -> None:
    """Stream-copy concat; every clip shares codec settings so no re-encode is needed."""
    out.parent.mkdir(parents=True, exist_ok=True)
    listing = out.with_suffix(".txt")
    listing.write_text("".join(f"file '{str(c).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
                               for c in clips))
    try:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                        "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(out)], check=True)
    finally:
        listing.unlink(missing_ok=True)
