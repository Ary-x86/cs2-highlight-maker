"""Errors that carry a plain-language explanation and a fix, shown as-is in the CLI and GUI."""

from __future__ import annotations


class FriendlyError(Exception):
    def __init__(self, title: str, why: str, fix: str):
        super().__init__(title)
        self.title = title
        self.why = why
        self.fix = fix

    def __str__(self) -> str:
        return f"{self.title}\n  Why: {self.why}\n  Fix: {self.fix}"

    def to_dict(self) -> dict:
        return {"title": self.title, "why": self.why, "fix": self.fix}


def explain(exc: BaseException) -> FriendlyError:
    """Best-effort translation of unexpected exceptions into something actionable."""
    if isinstance(exc, FriendlyError):
        return exc
    text = str(exc)
    if isinstance(exc, FileNotFoundError) and "ffmpeg" in text:
        return FriendlyError("FFmpeg is not installed", "Clips are encoded with FFmpeg.",
                             "Install it with your package manager (e.g. `sudo pacman -S ffmpeg`).")
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return FriendlyError("Disk is full", "Recording writes thousands of frames before encoding them.",
                             "Free some space or pick an output/workspace folder on a bigger disk in Settings.")
    if isinstance(exc, PermissionError):
        return FriendlyError("Permission denied", text,
                             "Make sure the folder in the message is writable by your user.")
    if "nvenc" in text.lower() or "No capable devices found" in text:
        return FriendlyError("NVIDIA hardware encoder unavailable", text,
                             "Set Encoder to 'CPU (libx264)' in Settings; it is slower but works everywhere.")
    return FriendlyError("Something went wrong", f"{type(exc).__name__}: {text}",
                         "Check the log above. If it keeps happening, run `cs2hl clean` and try again.")
