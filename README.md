# MetUloadPhoD

**Windows desktop video-preparation tool · version 0.1.0**

MetUloadPhoD imports an original MP4/MOV/M4V file, verifies its presentation cadence, applies the project's **1/15360 MP4 video-track-timescale** method, and lets the user select a maximum output resolution.

The application processes files locally. It does not upload videos, connect to social accounts, or require an account or API key.

## What it does

1. Imports a local source video and inspects its codec, dimensions, duration, frame count and presentation timestamps.
2. Requires a constant-cadence source of approximately 60 FPS (including common 59.94 FPS material). It does not duplicate or invent frames to make a lower-frame-rate source appear to be 60 FPS.
3. Writes the video track using a **15360 ticks/second** MP4 timescale. At exactly 60 FPS, 256 ticks represent one frame: `256 / 15360 = 1 / 60 second`.
4. Offers resolution caps for Original, 4K UHD, 1440p, 1080p and 720p. The cap is applied to the longer image edge, so both portrait and landscape footage are supported. The application never upscales a smaller source.
5. Preserves the source frame count and presentation timestamps, then verifies the output dimensions, frame cadence and `1/15360` video timebase before accepting the result.
6. Writes a JSON verification report alongside the MP4.

## Important method distinction

This version implements the **video-track timescale experiment documented in the Social 60 FPS research paper**. It does **not** implement CompressBase's separate AAC-PCE `qualityFps(file)` method. Those are different container-level modifications and must not be treated as interchangeable.

The timescale change is a local MP4 representation change. It does not command TikTok, Facebook or Instagram to produce a particular rendition, preserve 4K, or finish transcoding faster. Those outcomes require controlled platform-side measurements.

## Resolution and encoding behavior

| Selection | Behavior |
| --- | --- |
| Keep original | Remuxes the original encoded video without re-encoding it. |
| 4K UHD | Keeps the source size if it is already below the cap; otherwise scales to a maximum long edge of 3840 px. |
| 1440p | Maximum long edge of 2560 px. |
| 1080p | Maximum long edge of 1920 px. |
| 720p | Maximum long edge of 1280 px. |

When a smaller resolution is requested, the video must be decoded, scaled and re-encoded. This is lossy, even with a high-quality CRF setting. The app uses a slow, quality-oriented H.264 or HEVC encoder and preserves source frame timestamps. The quality preset only affects this resize path; the Keep Original path copies the compressed video stream.

The current resizing path is SDR-only. HDR/BT.2020 files can be passed through with **Keep original**; resizing an HDR file is rejected rather than silently tone-mapped or converted incorrectly.

The first video stream and first audio stream are used. Global metadata and chapters are omitted from the output by default to avoid carrying unrelated or potentially private source tags. Subtitles, attachments and alternate audio tracks are not copied in this initial version.

## Download the Windows EXE

The GitHub Actions workflow builds a self-contained, x64 Windows executable and publishes it as an Actions artifact named `MetUloadPhoD-windows-x64`. Open the repository's **Actions → Build Windows EXE → latest successful run → Artifacts** and download it.

The executable bundles FFmpeg and FFprobe. No separate FFmpeg installation is required for the packaged build.

## Build from source

Requirements: Python 3.12+, Windows x64, FFmpeg/FFprobe, and PyInstaller.

```powershell
python -m pip install --upgrade pip pyinstaller pytest
pytest -q

# Put ffmpeg.exe, ffprobe.exe and their required DLLs in .\ffmpeg\bin\
$bin = (Resolve-Path .\ffmpeg\bin).Path
$args = @('--noconfirm', '--clean', '--onefile', '--windowed', '--name', 'MetUloadPhoD')
Get-ChildItem $bin -File | Where-Object { $_.Extension -in '.exe', '.dll' } | ForEach-Object {
    $args += @('--add-binary', "$($_.FullName);.")
}
$args += 'app.py'
pyinstaller @args
```

The executable will be created at `dist\MetUloadPhoD.exe`.

## Project status

This is the first desktop-app implementation. The local transform and output-verification pipeline are the deliverable; platform-level effectiveness remains a separate research question. The tool deliberately reports verified local properties rather than promising a social-platform result.

## License and third-party components

The MetUloadPhoD application source is licensed under the MIT License. Windows builds bundle FFmpeg/FFprobe from the Gyan.dev Essentials build distribution; see [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) and the FFmpeg/Gyan distribution terms for details.
