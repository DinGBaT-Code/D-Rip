"""Small process boundary around VideoLAN tools, designed for easy mocking."""

from __future__ import annotations

import shutil
import json
import re
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

from .models import JobState, RipJob, Title, Track


class BackendError(RuntimeError):
    pass


DVD_OUTPUT_DURATION_TOLERANCE_SECONDS = 1.0


def find_executable(name: str) -> Optional[str]:
    return shutil.which(name)


def available_video_encoders(ffmpeg: str = "ffmpeg") -> tuple[str, ...]:
    """Return encoders used when FFmpeg must transform the video."""
    executable = find_executable(ffmpeg)
    if not executable:
        return ()
    try:
        result = subprocess.run([executable, "-hide_banner", "-encoders"],
                                capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if result.returncode != 0:
        return ()
    encoders = set()
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        fields = line.split()
        if len(fields) >= 2 and "V" in fields[0]:
            encoders.add(fields[1])
    codecs = []
    if "libx264" in encoders:
        codecs.append("h264")
    if "libx265" in encoders:
        codecs.append("hevc")
    return tuple(codecs)


def available_video_codecs(ffmpeg: str = "ffmpeg") -> tuple[str, ...]:
    """Return output codecs, including copy-only H.264 from VLC's staging stream."""
    encoders = set(available_video_encoders(ffmpeg))
    codecs = ["h264"]
    if "hevc" in encoders:
        codecs.append("hevc")
    return tuple(codecs)


def _text(node: ET.Element, name: str, default: str = "") -> str:
    child = node.find(name)
    return (child.text or "").strip() if child is not None else default


def _int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _tracks(track_node: ET.Element, node_name: str) -> tuple[Track, ...]:
    output = []
    for node in track_node.findall(node_name):
        index = _int(_text(node, "ix"), len(output) + 1)
        language = _text(node, "language", "Unknown") or "Unknown"
        codec = _text(node, "format", "Unknown") or "Unknown"
        channels = _text(node, "channels")
        output.append(Track(index, language, codec, channels))
    return tuple(output)


def parse_lsdvd_xml(xml_text: str) -> list[Title]:
    """Parse `lsdvd -x -Ox` output into titles. Raises BackendError on bad data."""
    if not xml_text.strip():
        raise BackendError("lsdvd returned empty XML output.")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        # Some old lsdvd builds did not XML-escape disc metadata. Avoid silently
        # claiming a scan succeeded when the returned document cannot be parsed.
        raise BackendError(f"lsdvd returned invalid XML: {exc}") from exc
    nodes = root.findall("track")
    titles = []
    disc_label = _text(root, "title")
    for node in nodes:
        index = _int(_text(node, "ix"), len(titles) + 1)
        chapters = len(node.findall("chapter"))
        if chapters == 0:
            chapters = _int(_text(node, "chapter_count"))
        titles.append(Title(index, _float(_text(node, "length")), chapters,
                            _tracks(node, "audio"), _tracks(node, "subpicture"), disc_label))
    if not titles:
        raise BackendError("No DVD titles were found. Check that the source contains a readable DVD-Video disc.")
    return titles


def parse_lsdvd_json(json_text: str) -> list[Title]:
    """Parse lsdvd's newer `-Oj` output, which uses the same track fields."""
    if not json_text.strip():
        raise BackendError("lsdvd returned empty JSON output.")
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise BackendError(f"lsdvd returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise BackendError("lsdvd JSON root must be an object.")
    tracks = data.get("track", data.get("tracks", []))
    if not isinstance(tracks, list):
        raise BackendError("lsdvd JSON does not contain a title list.")
    titles = []
    disc_label = data.get("disc_title", data.get("title", ""))
    if not isinstance(disc_label, str):
        disc_label = ""
    for node in tracks:
        if not isinstance(node, dict):
            continue
        index = _int(node.get("ix", node.get("title", node.get("index", len(titles) + 1))), len(titles) + 1)
        chapters_data = node.get("chapter", node.get("chapters", []))
        chapters = len(chapters_data) if isinstance(chapters_data, list) else _int(node.get("chapter_count"))
        audio_data = node.get("audio", node.get("audiostreams", []))
        subtitle_data = node.get("subp", node.get("subpicture", node.get("subtitles", [])))

        def tracks_from_json(items):
            if not isinstance(items, list):
                return ()
            return tuple(Track(_int(item.get("ix", position + 1), position + 1),
                               str(item.get("language", "Unknown")),
                               str(item.get("format", "Unknown")),
                               str(item.get("channels", "")))
                         for position, item in enumerate(items) if isinstance(item, dict))

        titles.append(Title(index, _float(node.get("length", 0)), chapters,
                            tracks_from_json(audio_data), tracks_from_json(subtitle_data), disc_label))
    if not titles:
        raise BackendError("No DVD titles were found in lsdvd's JSON output.")
    return titles


def parse_lsdvd_human(text: str) -> list[Title]:
    """Parse lsdvd's human-readable format for older releases without JSON."""
    titles: list[Title] = []
    disc_label = ""
    for line in text.splitlines():
        disc_match = re.match(r"\s*Disc Title:\s*(.*?)\s*$", line)
        if disc_match:
            disc_label = disc_match.group(1).strip()
            continue
        title_match = re.match(
            r"\s*Title:\s*(\d+)\s*,\s*Length:\s*(\d{1,2}):(\d{2}):(\d{2}(?:\.\d+)?)\s*,?(.*)", line)
        if title_match:
            hours, minutes, seconds = title_match.group(2, 3, 4)
            duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            chapter_match = re.search(r"Chapters:\s*(\d+)", title_match.group(5))
            titles.append(Title(int(title_match.group(1)), duration,
                                int(chapter_match.group(1)) if chapter_match else 0,
                                disc_label=disc_label))
            continue
        if not titles:
            continue
        audio_match = re.match(r"\s*Audio:\s*(\d+)\s*,(.*)", line)
        subtitle_match = re.match(r"\s*(?:Subtitle|Subpicture):\s*(\d+)\s*,(.*)", line)
        match = audio_match or subtitle_match
        if not match:
            continue
        details = match.group(2)
        language_match = re.search(r"Language:\s*([^,]+)", details)
        format_match = re.search(r"Format:\s*([^,]+)", details)
        channels_match = re.search(r"Channels:\s*([^,]+)", details)
        language = language_match.group(1).strip() if language_match else "Unknown"
        codec = format_match.group(1).strip() if format_match else "Unknown"
        channels = channels_match.group(1).strip() if channels_match else ""
        track = Track(int(match.group(1)), language, codec, channels)
        if audio_match:
            titles[-1] = Title(titles[-1].index, titles[-1].duration_seconds, titles[-1].chapters,
                               titles[-1].audio_tracks + (track,), titles[-1].subtitles,
                               titles[-1].disc_label)
        else:
            titles[-1] = Title(titles[-1].index, titles[-1].duration_seconds, titles[-1].chapters,
                               titles[-1].audio_tracks, titles[-1].subtitles + (track,),
                               titles[-1].disc_label)
    if not titles:
        raise BackendError("lsdvd human-readable output contained no DVD titles.")
    return titles


def scan_disc(source: str, lsdvd: str = "lsdvd", timeout: int = 180) -> list[Title]:
    executable = find_executable(lsdvd)
    if not executable:
        raise BackendError("Could not find lsdvd. Install lsdvd plus libdvdread and libdvdcss, then try again.")
    if not source.strip():
        raise BackendError("Choose a DVD device or VIDEO_TS folder first.")
    source = normalize_dvd_source(source)
    try:
        result = subprocess.run([executable, "-x", "-O", "x", source], capture_output=True,
                                text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise BackendError("DVD scan timed out. Check the disc and drive, then scan again.") from exc
    except OSError as exc:
        raise BackendError(f"Could not start lsdvd: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise BackendError(detail or f"lsdvd exited with status {result.returncode}.")
    try:
        return parse_lsdvd_xml(result.stdout)
    except BackendError as xml_error:
        # Older lsdvd releases sometimes emit malformed XML for disc strings.
        # Newer releases provide JSON output with the same title/track data.
        try:
            json_result = subprocess.run([executable, "-x", "-O", "j", source], capture_output=True,
                                         text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            diagnostic = result.stderr.strip()
            if diagnostic:
                raise BackendError(f"{xml_error} lsdvd diagnostic: {diagnostic}") from exc
            raise xml_error from exc
        if json_result.returncode == 0:
            try:
                return parse_lsdvd_json(json_result.stdout)
            except BackendError as json_error:
                scan_error = json_error
        else:
            scan_error = BackendError(f"lsdvd JSON fallback exited with status {json_result.returncode}.")
        human_diagnostic = ""
        try:
            human_result = subprocess.run([executable, "-x", "-O", "h", source], capture_output=True,
                                          text=True, timeout=timeout, check=False)
            human_diagnostic = human_result.stderr.strip()
            if human_result.returncode == 0:
                try:
                    return parse_lsdvd_human(human_result.stdout)
                except BackendError as human_error:
                    scan_error = human_error
            else:
                scan_error = BackendError(f"lsdvd human-readable fallback exited with status {human_result.returncode}.")
        except subprocess.TimeoutExpired:
            scan_error = BackendError("DVD scan timed out while trying lsdvd's human-readable output.")
        except OSError as exc:
            scan_error = BackendError(f"Could not start lsdvd's human-readable fallback: {exc}")
        diagnostic = human_diagnostic or (json_result.stderr or result.stderr).strip()
        if "could not read enough bytes for title" in diagnostic.lower():
            raise BackendError(
                f"lsdvd could not read the disc header from '{source}'. If you selected a VIDEO_TS "
                "folder, select its parent DVD folder or the optical device (such as /dev/sr0). "
                f"Check that a readable disc is inserted. lsdvd diagnostic: {diagnostic}"
            )
        if diagnostic:
            raise BackendError(f"{scan_error} lsdvd diagnostic: {diagnostic}")
        raise BackendError(f"{scan_error} Check the selected DVD source and that lsdvd can read it.")


def _source_mrl(source: str, title_index: int) -> str:
    path = Path(normalize_dvd_source(source)).expanduser()
    if path.is_dir() or path.name.upper() == "VIDEO_TS":
        # Use the selected mounted filesystem path. This lets Linux perform
        # ordinary reads through the mount and avoids requiring the app to
        # open the optical block device directly.
        encoded = quote(str(path.resolve()), safe="/:@")
        return f"dvdsimple://{encoded}#{title_index}"
    if path.is_block_device() or str(path).startswith("/dev/"):
        mounted_root = _mounted_dvd_root_for_device(path)
        read_path = Path(mounted_root) if mounted_root else path
        encoded = quote(str(read_path.resolve()), safe="/:@")
        return f"dvdsimple://{encoded}#{title_index}"
    encoded = quote(source, safe="/:@")
    return f"dvd://{encoded}#{title_index}"


def _mounted_dvd_root_for_device(device: Path) -> Optional[str]:
    """Find a mounted DVD root for a selected optical block device."""
    try:
        device_real = device.resolve()
        with open("/proc/mounts", encoding="utf-8") as mounts:
            for line in mounts:
                fields = line.split()
                if len(fields) < 3 or fields[2].lower() not in {"iso9660", "udf"}:
                    continue
                if Path(fields[0]).resolve() != device_real:
                    continue
                mountpoint = re.sub(
                    r"\\([0-7]{3})",
                    lambda match: chr(int(match.group(1), 8)),
                    fields[1],
                )
                return mountpoint
    except OSError:
        return None
    return None


def normalize_dvd_source(source: str) -> str:
    """VLC and lsdvd expect the DVD root, which contains VIDEO_TS."""
    path = Path(source).expanduser()
    if path.name.upper() == "VIDEO_TS":
        return str(path.parent)
    return source


def escape_sout(value: str) -> str:
    """Escape VLC stream-output grammar delimiters in a path value."""
    return "".join("\\" + ch if ch in "\\{}," else ch for ch in value)


def build_vlc_transcode_command(input_mrl: str, destination: Path,
                               video_bitrate_kbps: int = 2500,
                               audio_bitrate_kbps: int = 192,
                               vlc: str = "cvlc", progress: bool = False,
                               stop_time_seconds: float = 0.0) -> list[str]:
    executable = find_executable(vlc) or vlc
    if video_bitrate_kbps <= 0 or audio_bitrate_kbps <= 0:
        raise BackendError("Audio and video bitrates must be positive.")
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise BackendError(f"Output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Use VLC's x264 module for H.264 encoding. The avcodec module inherits
    # legacy FFmpeg encoder options on some VLC builds (for example lmin and
    # qsquish) and logs them as unsupported even though the transcode may run.
    transcode = (f"#transcode{{vcodec=h264,venc=x264,vb={video_bitrate_kbps},"
                 f"acodec=mp2,ab={audio_bitrate_kbps},channels=2}}:"
                 f"std{{access=file,mux=ts,dst={escape_sout(str(destination))}}}")
    command = [executable, "--no-one-instance", "--ignore-config", "--no-media-library",
               "--intf=dummy", "--no-spu", "--no-sout-spu", "--avcodec-hw=none"]
    if stop_time_seconds > 0:
        # A DVD title can roll into another menu/title after its own end. Cap
        # VLC by the scanned title's media timestamp so it stops at the title
        # boundary even when DVD navigation does not exit cleanly.
        command.append(f"--stop-time={stop_time_seconds:g}")
    if progress:
        # VLC's old remote-control interface accepts get_time over stdin. The
        # time comes from the active title, so fast and slow rips report honestly.
        command.extend(["--extraintf=oldrc", "--rc-fake-tty"])
    command.extend(["--aout=dummy", "--no-video-title-show", input_mrl, f"--sout={transcode}",
                    "--sout-keep", "--play-and-exit"])
    return command


def build_vlc_command(job: RipJob, vlc: str = "cvlc", output_path: Optional[Path] = None,
                      progress: bool = False) -> list[str]:
    if job.container not in {"mp4", "mkv", "ts"}:
        raise BackendError(f"Unsupported output container: {job.container}")
    mrl = _source_mrl(job.source, job.title.index)
    return build_vlc_transcode_command(mrl, output_path or job.destination,
                                       job.video_bitrate_kbps, job.audio_bitrate_kbps, vlc, progress,
                                       job.title.duration_seconds)


def build_ffmpeg_command(job: RipJob, source_path: Path, ffmpeg: str = "ffmpeg",
                         progress: bool = False) -> list[str]:
    executable = find_executable(ffmpeg) or ffmpeg
    destination = job.destination.expanduser().resolve()
    if destination.exists():
        raise BackendError(f"Output already exists: {destination}")
    if job.video_codec not in {"h264", "hevc"}:
        raise BackendError(f"Unsupported video codec: {job.video_codec}")
    crop_values = (job.crop_left, job.crop_right, job.crop_top, job.crop_bottom)
    if any(not isinstance(value, int) or value < 0 for value in crop_values):
        raise BackendError("Crop values must be non-negative whole pixels.")
    valid_scale_heights = {0, 240, 360, 480, 576, 720, 1080}
    if job.scale_height not in valid_scale_heights:
        raise BackendError("Scale height must be Original, 240, 360, 480, 576, 720, or 1080 pixels.")
    if job.audio_bitrate_kbps <= 0 or job.video_bitrate_kbps <= 0:
        raise BackendError("Audio and video bitrates must be positive.")

    video_filters = []
    if job.deinterlace:
        video_filters.append("yadif")
    if any(crop_values):
        crop_width = crop_values[0] + crop_values[1]
        crop_height = crop_values[2] + crop_values[3]
        video_filters.append(
            f"crop=iw-{crop_width}:ih-{crop_height}:{job.crop_left}:{job.crop_top}"
        )
    if job.scale_height:
        video_filters.append(f"scale=-2:{job.scale_height}")
    video_filter = ",".join(video_filters)
    reencode_video = job.video_codec == "hevc" or bool(video_filter)

    encoder_by_codec = {"h264": "libx264", "hevc": "libx265"}
    if reencode_video and job.video_codec not in available_video_encoders(ffmpeg):
        encoder = encoder_by_codec[job.video_codec]
        raise BackendError(
            f"The installed FFmpeg does not provide the {encoder} video encoder. "
            "Install an FFmpeg build with that encoder, or choose an available codec."
        )

    command = [executable, "-hide_banner", "-nostdin", "-n", "-i", str(source_path)]
    if job.title.duration_seconds > 0:
        # VLC can emit beyond the DVD title boundary. Trim the final package to
        # the scanned duration so a looping menu or next title is not included.
        command.extend(["-t", f"{job.title.duration_seconds:g}"])
    command.extend(["-map", "0:v:0", "-map", "0:a?"])
    if video_filter:
        command.extend(["-vf", video_filter])
    if progress:
        command.extend(["-progress", "pipe:1", "-nostats"])
    if reencode_video:
        command.extend(["-c:v", encoder_by_codec[job.video_codec],
                        "-b:v", f"{job.video_bitrate_kbps}k", "-preset", "medium",
                        "-pix_fmt", "yuv420p"])
    else:
        command.extend(["-c:v", "copy"])
    if job.container == "mp4":
        command.extend(["-c:a", "aac", "-b:a", f"{job.audio_bitrate_kbps}k", "-movflags", "+faststart"])
    elif job.container == "mkv":
        command.extend(["-c:a", "copy"])
    elif job.container == "ts":
        command.extend(["-c:a", "copy"])
    else:
        raise BackendError(f"FFmpeg packaging is not supported for {job.container}.")
    command.append(str(destination))
    return command


def validate_output(path: Path, ffprobe: str = "ffprobe", timeout: int = 30,
                    expected_duration_seconds: float = 0.0) -> None:
    executable = find_executable(ffprobe)
    if not executable:
        raise BackendError("Could not find ffprobe (part of FFmpeg); install it to verify the encoded output.")
    try:
        result = subprocess.run([executable, "-v", "error", "-show_entries",
                                 "format=duration:stream=codec_type", "-of", "json", str(path)],
                                capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise BackendError("Timed out while verifying the output file.") from exc
    except OSError as exc:
        raise BackendError(f"Could not start ffprobe: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "ffprobe could not read the output file."
        raise BackendError(detail)
    try:
        metadata = json.loads(result.stdout)
        duration = float(metadata.get("format", {}).get("duration", 0))
        stream_types = {stream.get("codec_type") for stream in metadata.get("streams", [])}
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BackendError("ffprobe returned invalid output metadata.") from exc
    if duration <= 0 or "video" not in stream_types:
        raise BackendError("VLC produced an output with no playable video stream.")
    if expected_duration_seconds > 0:
        lower_tolerance = max(5.0, expected_duration_seconds * 0.05)
        if duration + lower_tolerance < expected_duration_seconds:
            raise BackendError(
                "The rip is shorter than the scanned DVD title "
                f"({duration:.1f}s output, {expected_duration_seconds:.1f}s expected). "
                "The disc read may have stopped at a timestamp or cell transition."
            )
        if duration > expected_duration_seconds + DVD_OUTPUT_DURATION_TOLERANCE_SECONDS:
            raise BackendError(
                "The final rip is longer than the scanned DVD title "
                f"({duration:.1f}s output, {expected_duration_seconds:.1f}s expected). "
                "The output could not be trimmed to the title boundary."
            )


def _run_command(command: list[str], cancel_event, on_log: Callable[[str], None],
                 output_path: Path,
                 on_progress: Optional[Callable[[str, float, float, Optional[float], bool], None]] = None,
                 duration_seconds: float = 0.0, phase: str = "Ripping",
                 progress_start: float = 0.0, progress_weight: float = 100.0,
                 progress_interval: float = 1.0, expected_size_bytes: int = 0) -> bool:
    """Run a media CLI, returning False when the user cancels it."""
    popen_options = {}
    if on_progress is not None and phase == "Ripping":
        popen_options["stdin"] = subprocess.PIPE
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, bufsize=1, universal_newlines=True, **popen_options)
    except OSError as exc:
        raise BackendError(f"Could not start {Path(command[0]).name}: {exc}") from exc

    try:
        assert process.stdout is not None
        # Read in a helper thread so cancellation remains responsive if VLC
        # temporarily stops emitting output.
        import queue
        import threading
        lines = queue.Queue()

        def collect_output():
            try:
                for output_line in process.stdout:
                    lines.put(output_line)
            finally:
                lines.put(None)

        reader = threading.Thread(target=collect_output, daemon=True)
        reader.start()
        finished_output = False
        started_at = time.monotonic()
        last_poll_at = started_at - max(0.05, progress_interval)
        last_emit_at = started_at
        awaiting_vlc_time = False
        vlc_response_seen = False
        vlc_poll_misses = 0
        vlc_poll_disabled = False
        last_packaging_time = -1.0
        last_progress_percent = progress_start
        last_rip_size = 0

        def report_progress(media_seconds: float, estimated: bool) -> None:
            nonlocal last_progress_percent
            elapsed = max(0.0, time.monotonic() - started_at)
            if duration_seconds > 0:
                fraction = min(0.995, max(0.0, media_seconds / duration_seconds))
                remaining_media = max(0.0, duration_seconds - media_seconds)
                rate = (media_seconds / elapsed) if elapsed > 0 else 0.0
                eta = remaining_media / rate if rate > 0 else None
            else:
                fraction = 0.0
                eta = None
            percent = min(99.5, max(progress_start, progress_start + fraction * progress_weight))
            # DVD title clocks can move backwards across navigation/cell
            # boundaries. Keep the queue's completion indicator monotonic.
            last_progress_percent = max(last_progress_percent, percent)
            on_progress(phase, last_progress_percent, elapsed, eta, estimated)

        while process.poll() is None or not finished_output or not lines.empty():
            if cancel_event.is_set():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                if output_path.exists():
                    output_path.unlink()
                return False
            now = time.monotonic()
            if (on_progress is not None and phase == "Ripping"
                    and now - last_poll_at >= max(0.05, progress_interval)):
                if awaiting_vlc_time:
                    vlc_poll_misses += 1
                last_poll_at = now
                if not vlc_poll_disabled:
                    try:
                        process.stdin.write("get_time\n")
                        process.stdin.flush()
                        awaiting_vlc_time = True
                    except (BrokenPipeError, OSError, AttributeError):
                        vlc_poll_disabled = True
                        vlc_poll_misses += 1
                if expected_size_bytes > 0 and duration_seconds > 0:
                    try:
                        current_size = output_path.stat().st_size
                    except OSError:
                        current_size = 0
                    if current_size > expected_size_bytes * 3:
                        try:
                            process.terminate()
                        except OSError:
                            pass
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        if output_path.exists():
                            output_path.unlink()
                        raise BackendError(
                            "VLC kept reading after the selected DVD title should have ended "
                            f"({current_size / 1_000_000:.1f} MB written; expected about "
                            f"{expected_size_bytes / 1_000_000:.1f} MB). The title may be "
                            "looping or its DVD duration may be incorrect."
                        )
                    if current_size > last_rip_size:
                        last_rip_size = current_size
                        fraction = min(0.995, last_rip_size / expected_size_bytes)
                        report_progress(duration_seconds * fraction, estimated=True)
                if (not vlc_response_seen and vlc_poll_misses and last_rip_size == 0
                        and duration_seconds > 0):
                    # With no media clock or output-size telemetry, only show
                    # a small startup estimate. Wall time alone must not make
                    # a stalled or slow rip appear nearly done.
                    report_progress(min(duration_seconds * 0.05, now - started_at), estimated=True)
            try:
                item = lines.get(timeout=0.1)
                if item is None:
                    finished_output = True
                    continue
                line = item.rstrip()
                if line:
                    if (on_progress is not None and phase == "Ripping"
                            and awaiting_vlc_time and re.fullmatch(r"\d+", line)):
                        position = float(line)
                        awaiting_vlc_time = False
                        vlc_response_seen = True
                        if position > 0 or time.monotonic() - started_at < 1.5:
                            report_progress(position, estimated=False)
                        continue
                    if on_progress is not None and phase == "Packaging":
                        key, separator, value = line.partition("=")
                        if separator and key in {"out_time_us", "out_time_ms"}:
                            try:
                                media_seconds = int(value) / 1_000_000
                            except ValueError:
                                media_seconds = -1
                            if media_seconds >= 0 and media_seconds > last_packaging_time:
                                last_packaging_time = media_seconds
                                report_progress(media_seconds, estimated=False)
                            continue
                        if separator and key in {
                                "frame", "fps", "stream_0_0_q", "bitrate", "total_size", "out_time",
                                "dup_frames", "drop_frames", "speed", "progress"}:
                            continue
                    on_log(line)
            except queue.Empty:
                continue
        reader.join(timeout=1)
        if process.returncode != 0:
            if output_path.exists():
                output_path.unlink()
            raise BackendError(f"{Path(command[0]).name} exited with status {process.returncode}. See the job log for details.")
        return True
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        stream = getattr(process, "stdout", None)
        if stream is not None and hasattr(stream, "close"):
            stream.close()
        stdin = getattr(process, "stdin", None)
        if stdin is not None and hasattr(stdin, "close"):
            stdin.close()


def run_job(job: RipJob, cancel_event, on_log: Callable[[str], None] = lambda _: None,
            vlc: str = "cvlc", ffmpeg: str = "ffmpeg",
            on_progress: Optional[Callable[[str, float, float, Optional[float], bool], None]] = None) -> None:
    """Read a title with VLC, package it with FFmpeg if requested, then verify it."""
    destination = job.destination.expanduser().resolve()
    if destination.exists():
        job.state = JobState.FAILED
        job.error = f"Output already exists: {destination}"
        raise BackendError(job.error)
    destination.parent.mkdir(parents=True, exist_ok=True)
    intermediate = destination.with_name(f"{destination.stem}.rip-stage-{uuid.uuid4().hex[:8]}.ts")
    job.state = JobState.RUNNING
    job_started_at = time.monotonic()
    job.progress_percent = 0.0
    job.progress_phase = "Ripping"
    job.elapsed_seconds = 0.0
    job.eta_seconds = job.title.duration_seconds if job.title.duration_seconds > 0 else None
    job.progress_estimated = True

    def report_progress(phase: str, percent: float, _stage_elapsed: float,
                        eta: Optional[float], estimated: bool) -> None:
        elapsed = max(0.0, time.monotonic() - job_started_at)
        job.progress_phase = phase
        job.progress_percent = percent
        job.elapsed_seconds = elapsed
        job.eta_seconds = eta
        job.progress_estimated = estimated
        if on_progress is not None:
            on_progress(phase, percent, elapsed, eta, estimated)

    def record_log(line: str) -> None:
        job.log.append(line)
        on_log(line)

    try:
        record_log(f"Writing temporary rip stream: {intermediate}")
        try:
            command = build_vlc_command(job, vlc, intermediate, progress=on_progress is not None)
        except BackendError as exc:
            job.state = JobState.FAILED
            job.error = str(exc)
            raise
        input_mrl = next((arg for arg in command if arg.startswith(("dvd://", "dvdsimple://"))), None)
        if input_mrl:
            record_log(f"DVD input: {input_mrl}")
        if job.title.duration_seconds > 0:
            record_log(
                f"VLC title stop time: {job.title.duration_seconds:.1f} seconds "
                "(from the DVD scan)"
            )
            record_log(
                f"Final output limit: {job.title.duration_seconds:.1f} seconds "
                "(scanned title duration)"
            )
        vlc_progress = {}
        if on_progress is not None:
            vlc_progress = {
                "on_progress": report_progress,
                "duration_seconds": job.title.duration_seconds,
                "phase": "Ripping",
                "progress_start": 0.0,
                "progress_weight": 90.0,
                "expected_size_bytes": int(
                    job.title.duration_seconds
                    * (job.video_bitrate_kbps + job.audio_bitrate_kbps)
                    * 1000 / 8 * 1.02
                ),
            }
        if not _run_command(command, cancel_event, record_log, intermediate, **vlc_progress):
            job.state = JobState.CANCELLED
            job.progress_phase = "Cancelled"
            return
        if not intermediate.exists() or intermediate.stat().st_size == 0:
            raise BackendError("VLC finished without producing an intermediate video stream.")
        # The VLC staging stream may include data past the DVD title boundary.
        # Validate that it is playable, then FFmpeg trims every container to
        # the scanned title duration.
        validate_output(intermediate)
        package_command = build_ffmpeg_command(job, intermediate, ffmpeg, progress=on_progress is not None)
        package_progress = {}
        if on_progress is not None:
            package_progress = {
                "on_progress": report_progress,
                "duration_seconds": job.title.duration_seconds,
                "phase": "Packaging",
                "progress_start": 90.0,
                "progress_weight": 9.0,
            }
        if not _run_command(package_command, cancel_event, record_log, destination, **package_progress):
            job.state = JobState.CANCELLED
            job.progress_phase = "Cancelled"
            return
        if not destination.exists() or destination.stat().st_size == 0:
            raise BackendError("The packaging step did not produce an output file.")
        validate_output(destination, expected_duration_seconds=job.title.duration_seconds)
        job.state = JobState.COMPLETE
        job.progress_phase = "Complete"
        job.progress_percent = 100.0
        job.elapsed_seconds = max(0.0, time.monotonic() - job_started_at)
        job.eta_seconds = 0.0
        job.progress_estimated = False
    except BackendError as exc:
        job.state = JobState.FAILED
        job.progress_phase = "Failed"
        job.error = str(exc)
        if destination.exists():
            destination.unlink()
        raise
    finally:
        if intermediate != destination and intermediate.exists():
            intermediate.unlink()
