# D-Rip

Linux-first desktop DVD title scanner and ripping queue. The UI is built with Python's standard-library Tkinter so it runs without an extra Python package

## Features

- Select an optical device, mounted disc, or `VIDEO_TS` folder.
- Scan title durations, chapters, audio streams, and subtitle streams using `lsdvd` (which uses libdvdread; CSS access is provided by libdvdcss when installed).
- Select multiple titles and queue MP4, MKV, or MPEG-TS outputs.
- Choose H.264 or H.265/HEVC when the installed FFmpeg provides the encoder, set video/audio bitrates, crop any frame edge in pixels, scale to a selected output height, and optionally deinterlace.
- Name output files with the disc volume label and title number, for example `DVD-X_title_02.mp4`.
- Use VLC's DVD input and stream-output path to make an H.264/MPEG-2 audio staging stream, then use FFmpeg to package MP4/MKV and encode AAC audio for MP4.
- Stop VLC at the scanned title duration, then trim the final file to that exact duration so a looping menu or next title is not included.
- Show live rip and packaging percentages in the queue, with elapsed time and estimated time remaining.
- Disable subtitle/SPU output during a rip because subtitle selection is not implemented yet; detected tracks remain visible in scan results.
- Use software decoding for DVD input to avoid GPU-driver-specific decoder behavior.
- Run jobs in the background, show process logs, and cancel the active job while the rest of the queue continues.
- Remove selected queue entries or clear the queue. Cancelled and failed titles can be added again.
- Avoid overwriting an existing output file.

The current MVP reports the available audio/subtitle tracks, lets VLC choose its default audio stream, and disables subtitle output during a rip. Per-track selection, embedded preview, job persistence, and Windows drive discovery are planned follow-ups.

## Linux prerequisites

Python 3.10+, Tkinter, VLC (`cvlc`), `lsdvd`, `ffprobe`, and DVD support in the installed VLC/libdvdread build. CSS-capable reads depend on libdvdcss being available to the system DVD stack. `ffprobe` (from FFmpeg) checks that finished files contain playable video and a nonzero duration. Exact package names vary by distribution.

On Debian/Ubuntu, the typical package set is:

```sh
sudo apt install python3-tk vlc lsdvd ffmpeg libdvd-pkg
```

Some distributions package `libdvdcss` directly; others require a separate repository or local package. `lsdvd` reports an error if CSS support is not available.

## Run

From the repository root:

```sh
python3 -m dvdrip.app
```

You can also launch the file directly, including from a folder whose path contains spaces:

```sh
python3 dvdrip/app.py
```

Or install the entry point in a virtual environment:

```sh
python3 -m pip install .
d-rip
```

## Test and coverage

```sh
python3 -m unittest discover -s tests -v
python3 -m coverage run --source=dvdrip -m unittest discover -s tests
python3 -m coverage report -m
```

The suite includes synthetic DVD metadata/process tests and a real VLC→FFmpeg smoke check using generated media; neither requires a physical DVD. A physical-disc integration check is still needed on the target Linux distribution.
