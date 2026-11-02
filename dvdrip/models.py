from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Track:
    index: int
    language: str = "Unknown"
    codec: str = "Unknown"
    channels: str = ""

    def label(self) -> str:
        details = " / ".join(part for part in (self.language, self.codec, self.channels)
                              if part and part.lower() != "unknown")
        return f"{self.index}: {details}" if details else str(self.index)


@dataclass(frozen=True)
class Title:
    index: int
    duration_seconds: float
    chapters: int = 0
    audio_tracks: tuple[Track, ...] = ()
    subtitles: tuple[Track, ...] = ()
    disc_label: str = ""

    @property
    def duration_label(self) -> str:
        total = max(0, int(self.duration_seconds))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02}:{minutes:02}:{seconds:02}"


class JobState(str, Enum):
    QUEUED = "Queued"
    RUNNING = "Running"
    COMPLETE = "Complete"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


@dataclass
class RipJob:
    source: str
    title: Title
    destination: Path
    container: str = "mp4"
    video_bitrate_kbps: int = 2500
    audio_bitrate_kbps: int = 192
    video_codec: str = "h264"
    scale_height: int = 0
    crop_left: int = 0
    crop_right: int = 0
    crop_top: int = 0
    crop_bottom: int = 0
    deinterlace: bool = False
    state: JobState = JobState.QUEUED
    error: Optional[str] = None
    log: list[str] = field(default_factory=list)
    progress_percent: float = 0.0
    progress_phase: str = "Queued"
    elapsed_seconds: float = 0.0
    eta_seconds: Optional[float] = None
    progress_estimated: bool = False
