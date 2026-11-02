"""Tk desktop interface for scanning and ripping DVD titles."""

from __future__ import annotations

import os
import queue
import re
import shutil
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# Also allow launching this file directly (including from a path with spaces).
# In that mode Python does not assign the package name needed by relative imports.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "dvdrip"

from .backend import BackendError, available_video_encoders, run_job, scan_disc
from .models import JobState, RipJob, Title


VIDEO_CODEC_LABELS = {"h264": "H.264 (AVC)", "hevc": "H.265 (HEVC)"}
VIDEO_CODEC_VALUES = {label: codec for codec, label in VIDEO_CODEC_LABELS.items()}
SCALE_HEIGHTS = {"Original": 0, "240p": 240, "360p": 360, "480p": 480,
                 "576p": 576, "720p": 720, "1080p": 1080}


def discover_sources() -> list[str]:
    """Return likely Linux DVD devices and mounted optical volumes."""
    found: list[str] = []
    for path in ("/dev/dvd", "/dev/sr0", "/dev/sr1", "/dev/sr2", "/dev/sr3"):
        if os.path.exists(path):
            found.append(path)
    try:
        with open("/proc/mounts", encoding="utf-8") as mounts:
            for line in mounts:
                fields = line.split()
                if len(fields) > 2 and (fields[2].lower() in {"iso9660", "udf"}):
                    mountpoint = fields[1].replace("\\040", " ")
                    if mountpoint not in found:
                        found.append(mountpoint)
    except OSError:
        pass
    return found


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._")
    return cleaned or "dvd_title"


def disc_label_for_source(source: str) -> str:
    """Infer a useful volume label when lsdvd cannot report the disc title."""
    path = Path(source).expanduser()
    if path.name.upper() == "VIDEO_TS":
        path = path.parent
    if path.is_dir() and path.name:
        return safe_filename(path.name)
    if source.startswith("/dev/"):
        try:
            with open("/proc/mounts", encoding="utf-8") as mounts:
                source_real = os.path.realpath(source)
                for line in mounts:
                    fields = line.split()
                    if len(fields) > 2 and os.path.realpath(fields[0]) == source_real:
                        return safe_filename(Path(fields[1].replace("\\040", " ")).name)
        except OSError:
            pass
    return safe_filename(path.name) if path.name else "DVD"


def output_filename(disc_label: str, title_index: int, extension: str) -> str:
    return f"{safe_filename(disc_label)}_title_{title_index:02}.{extension}"


def remove_jobs(jobs: list[RipJob], selected_ids: set[int], active_job: RipJob | None = None) -> tuple[list[RipJob], int]:
    """Remove selected queue entries while preserving a job that is currently ripping."""
    kept = [job for job in jobs if id(job) not in selected_ids or job is active_job]
    return kept, len(jobs) - len(kept)


def retry_job(job: RipJob) -> bool:
    """Reset a cancelled or failed job when its destination can be safely reused."""
    if job.state not in {JobState.CANCELLED, JobState.FAILED, JobState.COMPLETE}:
        return False
    if Path(job.destination).exists():
        return False
    job.state = JobState.QUEUED
    job.error = None
    job.log.clear()
    job.progress_percent = 0.0
    job.progress_phase = "Queued"
    job.elapsed_seconds = 0.0
    job.eta_seconds = None
    job.progress_estimated = False
    return True


def format_progress_time(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


def job_progress_summary(job: RipJob) -> str:
    if job.state == JobState.QUEUED:
        return "Waiting"
    if job.state == JobState.COMPLETE:
        return "Complete"
    if job.state == JobState.CANCELLED:
        return f"Cancelled at {job.progress_percent:.0f}%"
    if job.state == JobState.FAILED:
        return f"Failed at {job.progress_percent:.0f}%"
    estimate_mark = "~" if job.progress_estimated else ""
    summary = f"{job.progress_phase} · {estimate_mark}{job.progress_percent:.0f}%"
    if job.eta_seconds is not None:
        summary += f" · ~{format_progress_time(job.eta_seconds)} left"
    return summary


def job_progress_detail(job: RipJob) -> str:
    if job.state == JobState.QUEUED:
        return "Waiting in queue"
    if job.state == JobState.COMPLETE:
        return f"Complete · elapsed {format_progress_time(job.elapsed_seconds)}"
    estimate_mark = "~" if job.progress_estimated else ""
    detail = (f"{job.progress_phase} · {estimate_mark}{job.progress_percent:.0f}% · "
              f"elapsed {format_progress_time(job.elapsed_seconds)}")
    if job.eta_seconds is not None and job.state == JobState.RUNNING:
        detail += f" · about {format_progress_time(job.eta_seconds)} remaining"
    return detail


class DiscRipperApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("D-Rip")
        self.root.geometry("1120x800")
        self.root.minsize(900, 700)
        self.titles: list[Title] = []
        self.jobs: list[RipJob] = []
        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.scan_worker: threading.Thread | None = None
        self.active_job: RipJob | None = None
        self.progress_high_water: dict[int, tuple] = {}
        self.closing = False
        self.jobs_lock = threading.Lock()
        self._build_ui()
        self._poll_events()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="D-Rip", font=("TkDefaultFont", 18, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Scan a DVD, choose titles, then add them to the encoding queue.").pack(anchor="w", pady=(2, 10))

        source_frame = ttk.LabelFrame(outer, text="Disc source", padding=8)
        source_frame.pack(fill="x", pady=(0, 8))
        self.source_var = tk.StringVar()
        self.source_box = ttk.Combobox(source_frame, textvariable=self.source_var)
        self.source_box.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.source_box["values"] = discover_sources()
        self.browse_source_button = ttk.Button(source_frame, text="Browse VIDEO_TS…", command=self._browse_source)
        self.browse_source_button.pack(side="left", padx=(0, 6))
        self.scan_button = ttk.Button(source_frame, text="Scan", command=self._scan)
        self.scan_button.pack(side="left")
        self.scan_status_var = tk.StringVar(value="")
        ttk.Label(source_frame, textvariable=self.scan_status_var, width=18, anchor="w").pack(side="left", padx=(8, 0))

        title_frame = ttk.LabelFrame(outer, text="Titles", padding=8)
        title_frame.pack(fill="both", expand=True, pady=(0, 8))
        self.title_count_var = tk.StringVar(value="No scan yet")
        ttk.Label(title_frame, textvariable=self.title_count_var, anchor="e").pack(fill="x", pady=(0, 4))
        title_hscroll = ttk.Scrollbar(title_frame, orient="horizontal")
        title_hscroll.pack(side="bottom", fill="x")
        title_table = ttk.Frame(title_frame)
        title_table.pack(fill="both", expand=True)
        columns = ("title", "duration", "chapters", "audio", "subtitles")
        self.title_tree = ttk.Treeview(title_table, columns=columns, show="headings", selectmode="extended", height=6)
        for column, label, width in (("title", "Title", 65), ("duration", "Duration", 95),
                                     ("chapters", "Chapters", 75), ("audio", "Audio tracks", 260),
                                     ("subtitles", "Subtitles", 260)):
            self.title_tree.heading(column, text=label)
            self.title_tree.column(column, width=width, minwidth=55, stretch=column in {"audio", "subtitles"}, anchor="w")
        self.title_tree.pack(side="left", fill="both", expand=True)
        title_scroll = ttk.Scrollbar(title_table, orient="vertical", command=self.title_tree.yview)
        title_scroll.pack(side="right", fill="y")
        self.title_tree.configure(yscrollcommand=title_scroll.set)
        title_hscroll.configure(command=self.title_tree.xview)
        self.title_tree.configure(xscrollcommand=title_hscroll.set)

        settings = ttk.LabelFrame(outer, text="Output and encoding", padding=8)
        settings.pack(fill="x", pady=(0, 8))
        ttk.Label(settings, text="Folder").grid(row=0, column=0, sticky="w")
        default_output = Path.home() / "Videos"
        if not default_output.is_dir():
            default_output = Path.home()
        self.output_var = tk.StringVar(value=str(default_output))
        ttk.Entry(settings, textvariable=self.output_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(settings, text="Browse…", command=self._browse_output).grid(row=0, column=2)
        ttk.Label(settings, text="Format").grid(row=1, column=0, sticky="w", pady=(7, 0))
        self.format_var = tk.StringVar(value="MP4")
        ttk.Combobox(settings, textvariable=self.format_var, state="readonly",
                     values=("MP4", "MKV", "MPEG-TS"), width=12).grid(
                         row=1, column=1, sticky="w", padx=6, pady=(7, 0))
        ttk.Label(settings, text="Video codec").grid(row=1, column=2, sticky="e", padx=(6, 0), pady=(7, 0))
        self.supported_video_encoders = available_video_encoders()
        available_codecs = ("h264",) + (("hevc",) if "hevc" in self.supported_video_encoders else ())
        self.supported_video_codecs = available_codecs
        codec_labels = tuple(VIDEO_CODEC_LABELS[codec] for codec in available_codecs)
        self.codec_var = tk.StringVar(value=codec_labels[0] if codec_labels else "H.264 (AVC)")
        self.codec_box = ttk.Combobox(settings, textvariable=self.codec_var, state="readonly",
                                      values=codec_labels, width=16)
        self.codec_box.grid(row=1, column=3, sticky="w", padx=6, pady=(7, 0))
        ttk.Label(settings, text="Video kb/s").grid(row=1, column=4, sticky="e", padx=(6, 0), pady=(7, 0))
        self.bitrate_var = tk.StringVar(value="2500")
        ttk.Entry(settings, textvariable=self.bitrate_var, width=7).grid(row=1, column=5, sticky="w", padx=6, pady=(7, 0))
        ttk.Label(settings, text="Audio kb/s").grid(row=1, column=6, sticky="e", padx=(6, 0), pady=(7, 0))
        self.audio_bitrate_var = tk.StringVar(value="192")
        ttk.Entry(settings, textvariable=self.audio_bitrate_var, width=6).grid(
            row=1, column=7, sticky="w", padx=6, pady=(7, 0))
        settings.columnconfigure(1, weight=1)
        self.queue_button = ttk.Button(settings, text="Add selected titles to queue", command=self._queue_selected)
        self.queue_button.grid(row=1, column=8, sticky="e", padx=(8, 0), pady=(7, 0))

        ttk.Label(settings, text="Scale height").grid(row=2, column=0, sticky="w", pady=(7, 0))
        self.scale_var = tk.StringVar(value="Original")
        ttk.Combobox(settings, textvariable=self.scale_var, state="readonly",
                     values=tuple(SCALE_HEIGHTS), width=12).grid(
                         row=2, column=1, sticky="w", padx=6, pady=(7, 0))
        crop_labels = (("Crop L", "crop_left_var"), ("R", "crop_right_var"),
                       ("T", "crop_top_var"), ("B", "crop_bottom_var"))
        self.crop_vars = {}
        for offset, (label, attribute) in enumerate(crop_labels):
            label_column = 2 + offset * 2
            variable = tk.StringVar(value="0")
            setattr(self, attribute, variable)
            self.crop_vars[attribute] = variable
            ttk.Label(settings, text=label).grid(row=2, column=label_column, sticky="e", padx=(4, 0), pady=(7, 0))
            ttk.Entry(settings, textvariable=variable, width=5).grid(
                row=2, column=label_column + 1, sticky="w", padx=(3, 4), pady=(7, 0))
        self.deinterlace_var = tk.StringVar(value="No")
        ttk.Checkbutton(settings, text="Deinterlace", variable=self.deinterlace_var,
                        onvalue="Yes", offvalue="No").grid(row=2, column=10, sticky="w", padx=(6, 0), pady=(7, 0))
        ttk.Label(settings, text="Crop removes pixels from each edge; scale preserves aspect ratio.").grid(
            row=3, column=0, columnspan=11, sticky="w", pady=(3, 0))

        queue_frame = ttk.LabelFrame(outer, text="Rip queue", padding=8)
        queue_frame.pack(fill="both", expand=True, pady=(0, 8))
        buttons = ttk.Frame(queue_frame)
        self.queue_controls = buttons
        buttons.pack(side="bottom", fill="x", pady=(6, 0))
        self.start_button = ttk.Button(buttons, text="Start queue", command=self._start_queue)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(buttons, text="Cancel current job", command=self._cancel_current, state="disabled")
        self.cancel_button.pack(side="left", padx=6)
        self.remove_button = ttk.Button(buttons, text="Remove selected", command=self._remove_selected_jobs)
        self.remove_button.pack(side="left", padx=(0, 6))
        self.clear_button = ttk.Button(buttons, text="Clear queue", command=self._clear_queue)
        self.clear_button.pack(side="left")
        job_hscroll = ttk.Scrollbar(queue_frame, orient="horizontal")
        job_hscroll.pack(side="bottom", fill="x")
        queue_table = ttk.Frame(queue_frame)
        queue_table.pack(fill="both", expand=True)
        self.job_tree = ttk.Treeview(queue_table, columns=("file", "state", "progress"), show="headings", height=3,
                                     selectmode="extended")
        self.job_tree.heading("file", text="Output file")
        self.job_tree.heading("state", text="Status")
        self.job_tree.heading("progress", text="Progress")
        self.job_tree.column("file", width=560, minwidth=220, stretch=True)
        self.job_tree.column("state", width=110, minwidth=90, stretch=False)
        self.job_tree.column("progress", width=300, minwidth=200, stretch=False)
        self.job_tree.pack(side="left", fill="both", expand=True)
        job_scroll = ttk.Scrollbar(queue_table, orient="vertical", command=self.job_tree.yview)
        job_scroll.pack(side="right", fill="y")
        self.job_tree.configure(yscrollcommand=job_scroll.set)
        job_hscroll.configure(command=self.job_tree.xview)
        self.job_tree.configure(xscrollcommand=job_hscroll.set)

        log_frame = ttk.LabelFrame(outer, text="Activity", padding=8)
        log_frame.pack(fill="x")
        self.progress_status_var = tk.StringVar(value="")
        ttk.Label(log_frame, textvariable=self.progress_status_var, anchor="w").pack(fill="x", pady=(0, 4))
        self.progress = ttk.Progressbar(log_frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 6))
        self.log_text = tk.Text(log_frame, height=3, wrap="word", state="disabled")
        self.log_text.pack(fill="x")
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(outer, textvariable=self.status_var, relief="sunken", anchor="w").pack(fill="x", pady=(8, 0))

    def _browse_source(self):
        path = filedialog.askdirectory(title="Choose a DVD or VIDEO_TS folder")
        if path:
            video_ts = Path(path)
            if video_ts.name.lower() != "video_ts" and (video_ts / "VIDEO_TS").is_dir():
                video_ts = video_ts / "VIDEO_TS"
            self.source_var.set(str(video_ts))

    def _browse_output(self):
        path = filedialog.askdirectory(title="Choose output folder")
        if path:
            self.output_var.set(path)

    def _scan(self):
        source = self.source_var.get().strip()
        if not source:
            messagebox.showerror("Source required", "Choose a DVD device or VIDEO_TS folder first.")
            return
        self.scan_button.configure(state="disabled", text="Scanning…")
        self.start_button.configure(state="disabled")
        self.progress.configure(mode="indeterminate")
        self.progress_status_var.set("Scanning disc…")
        self.progress.start(12)
        self.status_var.set("Scanning DVD…")
        self.scan_status_var.set("Scanning…")
        self._log(f"Scanning {source}")

        def work():
            try:
                found = scan_disc(source)
                self.events.put(("scan_ok", found))
            except BackendError as exc:
                self.events.put(("scan_error", str(exc)))
            except Exception as exc:
                self.events.put(("scan_error", f"Unexpected scanner error ({type(exc).__name__}): {exc}"))

        try:
            self.scan_worker = threading.Thread(target=work, daemon=True, name="d-rip-scan")
            self.scan_worker.start()
        except Exception as exc:
            self.events.put(("scan_error", f"Could not start the DVD scanner: {exc}"))

    def _finish_scan(self):
        self.progress.stop()
        self.progress_status_var.set("")
        self.scan_button.configure(state="normal", text="Scan")
        self.start_button.configure(state="normal")

    def _queue_selected(self):
        selected = self.title_tree.selection()
        if not selected:
            messagebox.showinfo("Select titles", "Select one or more titles to add to the queue.")
            return
        folder = Path(self.output_var.get()).expanduser()
        if not folder.is_dir():
            messagebox.showerror("Output folder", "Choose an existing output folder.")
            return
        try:
            video_bitrate = int(self.bitrate_var.get())
            if not 100 <= video_bitrate <= 50000:
                raise ValueError
        except ValueError:
            messagebox.showerror("Bitrate", "Enter a video bitrate from 100 to 50000 kb/s.")
            return
        try:
            audio_bitrate = int(self.audio_bitrate_var.get())
            if not 64 <= audio_bitrate <= 512:
                raise ValueError
        except ValueError:
            messagebox.showerror("Audio bitrate", "Enter an audio bitrate from 64 to 512 kb/s.")
            return
        video_codec = VIDEO_CODEC_VALUES.get(self.codec_var.get())
        if video_codec not in self.supported_video_codecs:
            messagebox.showerror("Video codec", "The selected video encoder is not available in FFmpeg.")
            return
        scale_height = SCALE_HEIGHTS.get(self.scale_var.get())
        if scale_height is None:
            messagebox.showerror("Scale height", "Choose Original, 240p, 360p, 480p, 576p, 720p, or 1080p.")
            return
        try:
            crop = {
                "crop_left": int(self.crop_left_var.get()),
                "crop_right": int(self.crop_right_var.get()),
                "crop_top": int(self.crop_top_var.get()),
                "crop_bottom": int(self.crop_bottom_var.get()),
            }
            if any(value < 0 for value in crop.values()):
                raise ValueError
            if crop["crop_left"] + crop["crop_right"] >= 720:
                raise ValueError
            if crop["crop_top"] + crop["crop_bottom"] >= 480:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Crop pixels",
                "Enter whole, non-negative crop values that leave at least 1 pixel of the DVD frame.",
            )
            return
        deinterlace = self.deinterlace_var.get() == "Yes"
        requires_encoder = video_codec == "hevc" or bool(scale_height or deinterlace or any(crop.values()))
        if requires_encoder and video_codec not in self.supported_video_encoders:
            messagebox.showerror(
                "Video encoder",
                f"The selected settings require FFmpeg's {VIDEO_CODEC_LABELS[video_codec]} encoder, "
                "which is not available.",
            )
            return
        extension = {"MP4": "mp4", "MKV": "mkv", "MPEG-TS": "ts"}.get(self.format_var.get())
        if extension is None:
            messagebox.showerror("Output format", "Choose MP4, MKV, or MPEG-TS.")
            return
        encoding_settings = {
            "video_bitrate_kbps": video_bitrate,
            "audio_bitrate_kbps": audio_bitrate,
            "video_codec": video_codec,
            "scale_height": scale_height,
            **crop,
            "deinterlace": deinterlace,
        }
        source = self.source_var.get().strip()
        added = 0
        retried = 0
        for item in selected:
            index = int(item)
            title = next((title for title in self.titles if title.index == index), None)
            if title is None:
                continue
            disc_label = title.disc_label.strip()
            if disc_label.casefold().replace(" ", "_") in {
                    "unknown", "unknown_title", "unknown_dvd", "dvd", "dvd_video"}:
                disc_label = ""
            disc_label = disc_label or disc_label_for_source(source)
            destination = folder / output_filename(disc_label, title.index, extension)
            error_destination = None
            with self.jobs_lock:
                existing = next((job for job in self.jobs if Path(job.destination) == destination), None)
                if existing is not None:
                    if existing is self.active_job or existing.state in {JobState.QUEUED, JobState.RUNNING}:
                        continue
                    if retry_job(existing):
                        for name, value in encoding_settings.items():
                            setattr(existing, name, value)
                        retried += 1
                    elif Path(existing.destination).exists():
                        error_destination = existing.destination
                    else:
                        continue
                elif destination.exists():
                    error_destination = destination
                else:
                    self.jobs.append(RipJob(source, title, destination, extension, **encoding_settings))
                    added += 1
            if error_destination is not None:
                messagebox.showerror("Output already exists", f"The output file already exists:\n{error_destination}")

        self._refresh_job_tree()
        if added or retried:
            self.status_var.set(f"{added} added, {retried} returned to queue ({len(self.jobs)} total)")
        else:
            self.status_var.set(f"{len(self.jobs)} job(s) in queue")

    def _start_queue(self):
        with self.jobs_lock:
            pending = [job for job in self.jobs if job.state == JobState.QUEUED]
        if not pending:
            messagebox.showinfo("Queue empty", "Add one or more titles to the queue first.")
            return
        self.progress_high_water = {
            id(job): (0.0, 0.0, job.title.duration_seconds or None, True)
            for job in pending
        }
        self.cancel_event.clear()
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100, value=0)
        self.progress_status_var.set("Starting queue…")
        self.start_button.configure(state="disabled")
        self.scan_button.configure(state="disabled")
        self.cancel_button.configure(state="disabled")
        self.status_var.set("Encoding queue…")

        def work():
            for job in pending:
                with self.jobs_lock:
                    if self.closing:
                        break
                    if job not in self.jobs or job.state != JobState.QUEUED:
                        continue
                    self.active_job = job
                    job.state = JobState.RUNNING
                    job.progress_percent = 0.0
                    job.progress_phase = "Ripping"
                    job.elapsed_seconds = 0.0
                    job.eta_seconds = job.title.duration_seconds if job.title.duration_seconds > 0 else None
                    job.progress_estimated = True
                self.events.put(("job_update", job))
                self.events.put(("log", f"Starting title {job.title.index}: {job.destination.name}"))

                def report_progress(phase, percent, elapsed, eta, estimated):
                    self.events.put(("progress", (job, phase, percent, elapsed, eta, estimated)))

                try:
                    run_job(job, self.cancel_event, lambda line: self.events.put(("log", line)),
                            on_progress=report_progress)
                except BackendError as exc:
                    job.error = str(exc)
                except Exception as exc:
                    job.state = JobState.FAILED
                    job.error = f"Unexpected rip failure: {exc}"
                with self.jobs_lock:
                    if self.active_job is job:
                        self.active_job = None
                    if not self.closing:
                        # The cancel button applies to one rip; later queue entries can continue.
                        self.cancel_event.clear()
                self.events.put(("job_update", job))
            self.events.put(("queue_done", None))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _refresh_job_tree(self):
        self.job_tree.delete(*self.job_tree.get_children())
        with self.jobs_lock:
            jobs = list(self.jobs)
        for index, job in enumerate(jobs):
            self.job_tree.insert("", "end", iid=str(index),
                                 values=(str(job.destination), job.state.value, job_progress_summary(job)))

    def _remove_selected_jobs(self):
        selected_ids = set()
        with self.jobs_lock:
            for iid in self.job_tree.selection():
                try:
                    job = self.jobs[int(iid)]
                except (ValueError, IndexError):
                    continue
                selected_ids.add(id(job))
        if not selected_ids:
            self.status_var.set("Select one or more queue items to remove")
            return
        with self.jobs_lock:
            before = len(self.jobs)
            self.jobs, removed = remove_jobs(self.jobs, selected_ids, self.active_job)
            active_kept = self.active_job is not None and id(self.active_job) in selected_ids
        self._refresh_job_tree()
        message = f"Removed {removed} queue item(s)"
        if active_kept:
            message += "; the active rip is still running"
        self.status_var.set(message)

    def _clear_queue(self):
        with self.jobs_lock:
            active_job = self.active_job
            before = len(self.jobs)
            self.jobs = [job for job in self.jobs if job is active_job] if active_job else []
            removed = before - len(self.jobs)
        self._refresh_job_tree()
        if active_job:
            self.status_var.set(f"Cleared {removed} queue item(s); the active rip remains")
        else:
            self.status_var.set(f"Cleared {removed} queue item(s)")

    def _cancel_current(self):
        with self.jobs_lock:
            job = self.active_job
            if job is None:
                self.status_var.set("No rip is currently running")
                return
            self.cancel_event.set()
        self.status_var.set(f"Cancelling {job.destination.name}…")

    def _log(self, line: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "scan_ok":
                    try:
                        self.titles = payload
                        self.title_tree.delete(*self.title_tree.get_children())
                        for title in self.titles:
                            audios = "; ".join(track.label() for track in title.audio_tracks) or "Not reported"
                            subtitles = "; ".join(track.label() for track in title.subtitles) or "None reported"
                            self.title_tree.insert("", "end", iid=str(title.index), values=(
                                title.index, title.duration_label, title.chapters, audios, subtitles))
                        if self.titles:
                            main_title = max(self.titles, key=lambda title: title.duration_seconds)
                            self.title_tree.selection_set(str(main_title.index))
                            title_message = (
                                f"{len(self.titles)} title(s) found · likely main feature: "
                                f"Title {main_title.index} ({main_title.duration_label})"
                            )
                            self.status_var.set(title_message)
                        else:
                            title_message = "No titles found"
                            self.status_var.set("No DVD titles found")
                        self.title_count_var.set(title_message)
                        self.scan_status_var.set(f"{len(self.titles)} title(s) found" if self.titles else title_message)
                        log_message = f"Scan complete: {len(self.titles)} title(s) found"
                        if self.titles:
                            log_message += (
                                f". Likely main feature: title {main_title.index} "
                                f"({main_title.duration_label}); selected by default"
                            )
                        self._log(log_message)
                    except Exception as exc:
                        self.titles = []
                        error = f"Could not display DVD scan results ({type(exc).__name__}): {exc}"
                        self.status_var.set("Could not display scan results")
                        self.scan_status_var.set("Scan results failed")
                        self.title_count_var.set("Unable to display titles")
                        self._log(error)
                        messagebox.showerror("DVD scan failed", error)
                    finally:
                        self._finish_scan()
                elif kind == "scan_error":
                    self.status_var.set("Scan failed")
                    self.scan_status_var.set("Scan failed")
                    self.title_count_var.set("Scan failed")
                    self._log(payload)
                    messagebox.showerror("DVD scan failed", payload)
                    self._finish_scan()
                elif kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    job, phase, percent, elapsed, eta, estimated = payload
                    if not hasattr(self, "progress_high_water"):
                        self.progress_high_water = {}
                    previous = self.progress_high_water.get(id(job))
                    if previous is not None and float(percent) < previous[0]:
                        percent, elapsed, eta, estimated = previous
                    else:
                        percent = float(percent)
                        self.progress_high_water[id(job)] = (percent, elapsed, eta, estimated)
                    job.progress_phase = phase
                    job.progress_percent = percent
                    job.elapsed_seconds = elapsed
                    job.eta_seconds = eta
                    job.progress_estimated = estimated
                    try:
                        index = self.jobs.index(job)
                    except ValueError:
                        index = -1
                    if index >= 0:
                        iid = str(index)
                        if self.job_tree.exists(iid):
                            self.job_tree.set(iid, "progress", job_progress_summary(job))
                    self.progress.configure(mode="determinate", maximum=100, value=percent)
                    self.progress_status_var.set(job_progress_detail(job))
                    self.status_var.set(f"{job.destination.name}: {phase} {percent:.0f}%")
                elif kind == "job_update":
                    job = payload
                    if not hasattr(self, "progress_high_water"):
                        self.progress_high_water = {}
                    if job.state == JobState.COMPLETE:
                        job.progress_phase = "Complete"
                        job.progress_percent = 100.0
                        self.progress_high_water[id(job)] = (100.0, job.elapsed_seconds, 0.0, False)
                        job.eta_seconds = 0.0
                        job.progress_estimated = False
                    elif job.state == JobState.RUNNING:
                        self.progress_high_water.setdefault(
                            id(job), (0.0, 0.0, job.title.duration_seconds or None, True))
                    elif job.state == JobState.FAILED:
                        job.progress_phase = "Failed"
                    elif job.state == JobState.CANCELLED:
                        job.progress_phase = "Cancelled"
                    if job.state == JobState.RUNNING:
                        self.cancel_button.configure(state="normal")
                    else:
                        self.cancel_button.configure(state="disabled")
                    try:
                        index = self.jobs.index(job)
                    except ValueError:
                        index = -1
                    if index >= 0:
                        iid = str(index)
                        if self.job_tree.exists(iid):
                            self.job_tree.set(iid, "state", job.state.value)
                            self.job_tree.set(iid, "progress", job_progress_summary(job))
                        self.progress.configure(mode="determinate", maximum=100,
                                                value=job.progress_percent)
                        self.progress_status_var.set(job_progress_detail(job))
                        self.status_var.set(f"{job.destination.name}: {job.state.value}")
                    if job.error:
                        self._log(job.error)
                elif kind == "queue_done":
                    self.progress.stop()
                    self.progress_status_var.set("Queue finished")
                    self.start_button.configure(state="normal")
                    self.scan_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    self.status_var.set("Queue finished" if not self.closing else "Stopping application…")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _close(self):
        self.closing = True
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.status_var.set("Stopping active job…")
            self.root.after(250, self._close_when_stopped)
        else:
            self.root.destroy()

    def _close_when_stopped(self):
        if self.worker and self.worker.is_alive():
            self.root.after(250, self._close_when_stopped)
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    DiscRipperApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
