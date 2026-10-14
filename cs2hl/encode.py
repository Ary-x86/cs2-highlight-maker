"""Turn recorded JPG frames + WAV into finished clips, and clips into montages."""

from __future__ import annotations

import subprocess
from pathlib import Path

CODEC_NAMES = {"h264_nvenc": "NVIDIA GPU (NVENC)", "h264_vaapi": "AMD/Intel GPU (VAAPI)", "libx264": "CPU (x264)"}


def _render_node() -> Path | None:
    return next(iter(sorted(Path("/dev/dri").glob("renderD*"))), None) if Path("/dev/dri").exists() else None


def codec_chain(choice: str) -> list[str]:
    """Encoders to try in order; the CPU encoder is always the last resort."""
    if choice == "auto":
        if Path("/proc/driver/nvidia/version").exists():
            choice = "h264_nvenc"
        elif _render_node():
            choice = "h264_vaapi"
        else:
            choice = "libx264"
    return [choice] if choice == "libx264" else [choice, "libx264"]


def find_take(raw_dir: Path, name: str) -> Path:
    """startmovie writes each take into its own timestamped folder."""
    matches = [p.parent for p in raw_dir.glob(f"*/{name}.wav")]
    if not matches:
        matches = [p.parent for p in raw_dir.glob(f"*/{name}00000000.jpg")]
    if not matches:
        raise FileNotFoundError(f"no frames recorded for {name} in {raw_dir}")
    return max(matches, key=lambda p: p.stat().st_mtime)


def _encoder_args(codec: str, quality: int) -> list[str]:
    if codec.endswith("_vaapi"):
        return ["-c:v", codec, "-rc_mode", "CQP", "-qp", str(quality)]
    if codec.endswith("_nvenc"):
        return ["-c:v", codec, "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", str(quality), "-b:v", "0",
                "-maxrate", "60M", "-bufsize", "120M",
                "-pix_fmt", "yuv420p"]
    return ["-c:v", codec, "-preset", "medium", "-crf", str(quality), "-pix_fmt", "yuv420p"]


def encode_clip(takes: list[tuple[Path, str]], out: Path, render_fps: int, blend: int,
                codec: str = "h264_nvenc", quality: int = 19, height: int = 0) -> None:
    """Concatenate one or more takes (folder, name) into a single video.

    blend > 1 averages that many consecutive rendered frames into each output frame, which turns a high
    render framerate into natural motion blur at render_fps / blend. height > 0 rescales (e.g. 1440p -> 1080p).
    """
    inputs: list[str] = []
    if codec.endswith("_vaapi"):
        inputs += ["-vaapi_device", str(_render_node() or "/dev/dri/renderD128")]
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
    chain = []
    if blend > 1:
        chain += [f"tmix=frames={blend}", f"fps={render_fps // blend}"]
    if height:
        chain.append(f"scale=-2:{height}:flags=lanczos")
    if codec.endswith("_vaapi"):
        chain += ["format=nv12", "hwupload"]
    graph += f"[v]{','.join(chain) or 'null'}[vo]"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
           "-filter_complex", graph, "-map", "[vo]", "-map", "[a]",
           *_encoder_args(codec, quality), "-c:a", "aac", "-b:a", "256k",
           "-movflags", "+faststart", str(out)]
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


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
