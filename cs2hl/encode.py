"""Turn recorded frames + WAV into finished clips, and clips into montages.

Standard and Compact recordings are JPG frames. Clean recordings are lossless TGA frames, which are far too big to
keep around (11 MB each at 1440p), so FramePacker squeezes them into visually lossless video chunks while CS2 is
still recording and deletes them.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import system
from .config import WORK_DIR

CODEC_NAMES = {"h264_nvenc": "NVIDIA GPU (NVENC)", "h264_vaapi": "AMD/Intel GPU (VAAPI)",
               "h264_amf": "AMD GPU (AMF)", "h264_qsv": "Intel GPU (Quick Sync)", "libx264": "CPU (x264)"}

# Compact level -> (max output height, x264 CRF, audio kbit/s). Smallest files at 1, best looking at 5.
COMPACT_LEVELS = {1: (720, 30, 96), 2: (720, 27, 128), 3: (720, 24, 128), 4: (1080, 27, 160), 5: (1080, 24, 160)}
COMPACT_MAX_FPS = 60
CLEAN_QUALITY_BOOST = 6  # Clean encodes this much below the Quality slider (lower = sharper)
BT709 = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
PACK_BATCH = 48  # TGA frames converted per ffmpeg call while recording


def ffmpeg() -> str:
    """FFmpeg on PATH, else the copy the Windows setup downloads into the workspace."""
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    local = WORK_DIR / "bin" / ("ffmpeg.exe" if system.IS_WINDOWS else "ffmpeg")
    return str(local) if local.exists() else "ffmpeg"


def ffmpeg_available() -> bool:
    return shutil.which(ffmpeg()) is not None or Path(ffmpeg()).exists()


def _render_node() -> Path | None:
    return next(iter(sorted(Path("/dev/dri").glob("renderD*"))), None) if Path("/dev/dri").exists() else None


def codec_chain(choice: str) -> list[str]:
    """Encoders to try in order; the CPU encoder is always the last resort."""
    if choice == "auto" and system.IS_WINDOWS:
        # nvidia-smi ships with the NVIDIA driver; otherwise try AMD, then Intel.
        return ["h264_nvenc", "libx264"] if shutil.which("nvidia-smi") else ["h264_amf", "h264_qsv", "libx264"]
    if choice == "auto":
        if Path("/proc/driver/nvidia/version").exists():
            choice = "h264_nvenc"
        elif _render_node():
            choice = "h264_vaapi"
        else:
            choice = "libx264"
    return [choice] if choice == "libx264" else [choice, "libx264"]


@dataclass
class Take:
    """One recorded sequence: ffmpeg input arguments for its video, its frame count and its audio."""
    video: list[str]
    frames: int
    wav: Path
    image_sequence: bool = True  # numbered image files, which need the render framerate passed in


def find_take(raw_dir: Path, name: str, packer: FramePacker | None = None) -> Take:
    """startmovie writes each take into its own timestamped folder."""
    if packer:
        return packer.take(name)
    matches = [p.parent for p in raw_dir.glob(f"*/{name}.wav")]
    if not matches:
        matches = [p.parent for p in raw_dir.glob(f"*/{name}00000000.jpg")]
    if not matches:
        raise FileNotFoundError(f"no frames recorded for {name} in {raw_dir}")
    folder = max(matches, key=lambda p: p.stat().st_mtime)
    frames = len(list(folder.glob(f"{name}*.jpg")))
    return Take(["-i", str(folder / f"{name}%08d.jpg")], frames, folder / f"{name}.wav")


# --- Clean recordings: pack lossless frames while CS2 records ---------------------------------------

@dataclass
class _Pack:
    folder: Path
    name: str
    next: int | None = None  # number of the next TGA frame to convert
    frames: int = 0
    segments: list[Path] = field(default_factory=list)


class FramePacker:
    """Converts finished TGA frames into lossless-looking x264 4:4:4 chunks and deletes them.

    A frame counts as finished once the next one exists (CS2 writes them in order), or when the game has quit.
    Runs in a background thread during the recording; finish() converts whatever is left.
    """

    FRAME = re.compile(r"^(.+?)(\d{8})\.tga$")

    def __init__(self, raw_dir: Path, fps: int):
        self.raw_dir, self.fps = raw_dir, fps
        self.packs: dict[tuple[Path, str], _Pack] = {}
        self.packed = 0  # frames converted and deleted so far
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="frame-packer", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(0.5):
            try:
                self.sweep(final=False)
            except Exception as e:  # noqa: BLE001 - reported by finish()
                self.error = str(e)
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()

    def finish(self) -> None:
        """Stop the background thread and convert the remaining frames (call after CS2 quit)."""
        self.stop()
        if self.error:
            raise RuntimeError(self.error)
        self.sweep(final=True)

    def _frames(self) -> dict[tuple[Path, str], list[int]]:
        found: dict[tuple[Path, str], list[int]] = {}
        for take in os.scandir(self.raw_dir) if self.raw_dir.exists() else ():
            if not take.is_dir():
                continue
            for e in os.scandir(take.path):
                if m := self.FRAME.match(e.name):
                    found.setdefault((Path(take.path), m.group(1)), []).append(int(m.group(2)))
        return found

    def sweep(self, final: bool) -> None:
        for key, numbers in self._frames().items():
            numbers.sort()
            if not final:
                numbers = numbers[:-1]  # CS2 may still be writing the newest frame
            pack = self.packs.setdefault(key, _Pack(*key))
            while numbers:
                if pack.next not in numbers:
                    pack.next = numbers[0]  # first frame, or a gap
                run = [pack.next]
                while len(run) < PACK_BATCH and run[-1] + 1 in numbers:
                    run.append(run[-1] + 1)
                if len(run) < PACK_BATCH and not final:
                    break
                self._convert(pack, run[0], len(run))
                numbers = [n for n in numbers if n > run[-1]]
                pack.next = run[-1] + 1

    def _convert(self, pack: _Pack, first: int, count: int) -> None:
        seg = pack.folder / f"{pack.name}_part{len(pack.segments):04d}.mkv"
        cmd = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-framerate", str(self.fps),
               "-start_number", str(first), "-i", str(pack.folder / f"{pack.name}%08d.tga"), "-frames:v", str(count),
               "-vf", "scale=flags=accurate_rnd+full_chroma_int:out_color_matrix=bt709:out_range=tv,format=yuv444p",
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "4", *BT709, str(seg)]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                **system.BACKGROUND)
        system.lower_priority(proc.pid)
        _, err = proc.communicate()
        if proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, cmd, stderr=err)
        for n in range(first, first + count):
            (pack.folder / f"{pack.name}{n:08d}.tga").unlink(missing_ok=True)
        pack.segments.append(seg)
        pack.frames += count
        self.packed += count

    def take(self, name: str) -> Take:
        packs = [p for p in self.packs.values() if p.name == name and p.segments]
        if not packs:
            raise FileNotFoundError(f"no frames recorded for {name} in {self.raw_dir}")
        pack = max(packs, key=lambda p: p.folder.stat().st_mtime)
        listing = pack.folder / f"{name}_parts.txt"
        listing.write_text("".join(f"file '{s.name}'\n" for s in pack.segments), encoding="utf-8")
        return Take(["-f", "concat", "-safe", "0", "-i", str(listing)], pack.frames, pack.folder / f"{name}.wav",
                    image_sequence=False)


# --- final encode -----------------------------------------------------------------------------------

def _encoder_args(codec: str, quality: int, clean: bool = False, full_color: bool = False) -> list[str]:
    if codec.endswith("_vaapi"):
        return ["-c:v", codec, "-rc_mode", "CQP", "-qp", str(quality)]
    if codec.endswith("_amf"):
        q = str(quality)
        return ["-c:v", codec, "-quality", "quality", "-rc", "cqp", "-qp_i", q, "-qp_p", q, "-qp_b", q,
                "-pix_fmt", "yuv420p"]
    if codec.endswith("_qsv"):
        return ["-c:v", codec, "-global_quality", str(quality), "-pix_fmt", "nv12"]
    pix = "yuv444p" if full_color else "yuv420p"
    if codec.endswith("_nvenc"):
        args = ["-c:v", codec, "-preset", "p7" if clean else "p6", "-tune", "hq", "-rc", "vbr", "-cq", str(quality),
                "-b:v", "0", "-maxrate", "200M" if clean else "60M", "-bufsize", "400M" if clean else "120M"]
        if clean:
            # Look ahead and spend bits where the eye notices (flat walls, the crosshair) to stop blocking.
            args += ["-rc-lookahead", "32", "-spatial-aq", "1", "-temporal-aq", "1"]
        return args + ["-pix_fmt", pix] + (["-profile:v", "high444p"] if full_color else [])
    return ["-c:v", codec, "-preset", "slow" if clean else "medium", "-crf", str(quality), "-pix_fmt", pix]


def blend_frames(blend: int, strength: float) -> int:
    """Frames averaged per output frame. strength 1.0 covers exactly one output frame (a 360° shutter);
    more reaches back into the previous frame for a longer smear."""
    return max(blend, round(blend * strength)) if blend > 1 else 1


def encode_clip(takes: list[Take], out: Path, render_fps: int, blend: int, codec: str = "h264_nvenc",
                quality: int = 19, height: int = 0, *, profile: str = "standard", blur: float = 1.0,
                compact_level: int = 3, full_color: bool = False, source_height: int = 1080) -> None:
    """Concatenate one or more takes into a single video.

    blend > 1 averages consecutive rendered frames into each output frame, which turns a high render framerate into
    natural motion blur at render_fps / blend (blur > 1 widens the window for extra smear). height > 0 rescales
    (e.g. 1440p -> 1080p). profile: standard, clean (recorded lossless, sharper encode) or compact (small H.264
    for sharing; always encoded by x264 on the CPU, which makes the smallest files).
    """
    clean, compact = profile == "clean", profile == "compact"
    if compact:
        max_h, crf, audio_kbps = COMPACT_LEVELS[compact_level]
        codec, quality, full_color = "libx264", crf, False
        height = min(max_h, height or source_height)
        height = 0 if height >= source_height else height
    elif clean:
        quality = max(10, quality - CLEAN_QUALITY_BOOST)
        full_color = full_color and (codec.endswith("_nvenc") or codec == "libx264")
    else:
        full_color = False
    inputs: list[str] = []
    if codec.endswith("_vaapi"):
        inputs += ["-vaapi_device", str(_render_node() or "/dev/dri/renderD128")]
    graph = ""
    pairs = ""
    for i, take in enumerate(takes):
        video, audio = 2 * i, 2 * i + 1
        inputs += (["-framerate", str(render_fps)] if take.image_sequence else []) + take.video
        if take.wav.exists():
            inputs += ["-i", str(take.wav)]
        else:
            inputs += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        # Cut audio to the take's video length so takes stay in sync after concat.
        seconds = take.frames / render_fps
        graph += f"[{audio}:a]atrim=0:{seconds:.4f},asetpts=PTS-STARTPTS[a{i}];"
        pairs += f"[{video}:v][a{i}]"
    graph += f"{pairs}concat=n={len(takes)}:v=1:a=1[v][a];"
    chain = []
    fps = render_fps
    if blend > 1:
        chain += [f"tmix=frames={blend_frames(blend, blur)}", f"fps={render_fps // blend}"]
        fps = render_fps // blend
    if compact and fps > COMPACT_MAX_FPS:
        chain.append(f"fps={COMPACT_MAX_FPS}")
    # JPG frames are full-range BT.601; Clean chunks are already BT.709. Convert to standard BT.709 video levels,
    # which every player (phones, browsers, WhatsApp) shows with the right colours.
    src = "in_color_matrix=bt709:in_range=tv" if clean else "in_color_matrix=bt601:in_range=pc"
    size = f"-2:{height}:" if height else ""
    chain.append(f"scale={size}flags=lanczos+accurate_rnd+full_chroma_int:{src}:out_color_matrix=bt709:out_range=tv")
    chain.append(f"format={'yuv444p' if full_color else 'yuv420p'}")
    if codec.endswith("_vaapi"):
        chain += ["format=nv12", "hwupload"]
    graph += f"[v]{','.join(chain)}[vo]"
    if compact:
        venc = ["-c:v", "libx264", "-preset", "slow", "-crf", str(quality), "-pix_fmt", "yuv420p",
                "-profile:v", "high", "-level:v", "4.2", "-x264-params", "aq-mode=3"]
        aenc = ["-c:a", "aac", "-b:a", f"{audio_kbps}k"]
    else:
        venc = _encoder_args(codec, quality, clean, full_color)
        aenc = ["-c:a", "aac", "-b:a", "256k"]
    cmd = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *inputs,
           "-filter_complex", graph, "-map", "[vo]", "-map", "[a]", *venc, *BT709, *aenc,
           "-movflags", "+faststart", str(out)]
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, **system.NO_WINDOW)


def montage(clips: list[Path], out: Path) -> None:
    """Stream-copy concat; every clip shares codec settings so no re-encode is needed."""
    out.parent.mkdir(parents=True, exist_ok=True)
    listing = out.with_suffix(".txt")
    listing.write_text("".join(f"file '{str(c).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
                               for c in clips), encoding="utf-8")
    try:
        subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                        "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(out)], check=True,
                       **system.NO_WINDOW)
    finally:
        listing.unlink(missing_ok=True)
