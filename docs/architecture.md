# D-Rip architecture

## Current implementation

- `dvdrip.app` is the Tkinter desktop interface and background job queue.
- `dvdrip.backend` wraps command execution and keeps scanning/ripping independently testable.
- `lsdvd -x -Ox` enumerates DVD titles, chapters, and stream metadata through libdvdread. CSS-capable reads depend on libdvdcss being available to the system DVD stack.
- `cvlc` reads the chosen DVD title and transcodes it through VLC stream output to an H.264/MP2 MPEG-TS staging file.
- SPU/subpicture decoding and stream output are disabled for rip jobs until subtitle-track selection is implemented; scanning still reports available subtitle tracks.
- VLC hardware decoding is disabled during DVD input; DVD-resolution software decoding avoids dependencies on host GPU decoder drivers.
- FFmpeg trims the staging stream to the scanned title duration and writes MP4, MKV, or MPEG-TS. It can preserve the VLC H.264 video or re-encode with H.264/H.265 while applying crop, scale, and deinterlace filters.
- Encoding controls are stored on each queued job so later UI changes do not alter a job that is already waiting.
- During ripping, D-Rip polls VLC's current title time through its local remote-control interface and compares it with the scanned title duration. During packaging, it reads FFmpeg's progress stream. The queue shows per-title percent and ETA; the activity panel also shows elapsed time. ETA is marked as approximate.
- `ffprobe` checks for a video stream and a nonzero duration before a job is marked complete.
- `dvdrip.models` contains the title, stream, and job state data.

The app delegates to VideoLAN's CLI rather than linking LibVLC directly. This avoids a binding dependency and uses the installed VLC DVD modules, including available libdvdcss support. The VLC→FFmpeg packaging path is smoke-tested on generated media.

## Follow-up milestones

1. Add an optional LibVLC preview and explicit audio/subtitle selection.
2. Persist queue and preferences.
3. Package VLC, lsdvd, libdvdread, and CSS support for Windows, then add Windows optical-drive discovery.

Windows packaging needs a dependency strategy that matches the licenses and distribution rules of each bundled library. The current tested scope is Linux process orchestration; a physical-disc smoke test is needed for each target packaging environment.
