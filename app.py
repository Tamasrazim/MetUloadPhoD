#!/usr/bin/env python3
"""MetUloadPhoD: local MP4 60 FPS verification and timescale preparation."""
from __future__ import annotations

import json
import os
import queue
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from fractions import Fraction
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

APP_NAME = "MetUloadPhoD"
APP_VERSION = "0.1.0"
VIDEO_TIMESCALE = 15360
RESOLUTIONS = {
    "Keep original": None,
    "4K UHD · max 3840 px": 3840,
    "1440p · max 2560 px": 2560,
    "1080p · max 1920 px": 1920,
    "720p · max 1280 px": 1280,
}


def tool_path(name: str) -> str:
    exe = name + (".exe" if os.name == "nt" else "")
    roots = []
    if getattr(sys, "_MEIPASS", None):
        roots.append(Path(sys._MEIPASS))
    roots.append(Path(__file__).resolve().parent)
    for root in roots:
        item = root / exe
        if item.is_file():
            return str(item)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"{exe} is missing. Use the packaged build or install FFmpeg and add it to PATH.")


def capture(command: list[str], timeout: int = 900) -> str:
    result = subprocess.run(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "Media command failed.")[-6000:])
    return result.stdout


def parse_rate(value: Any) -> float | None:
    if not value or value in ("0/0", "N/A"):
        return None
    try:
        rate = float(Fraction(str(value)))
        return rate if rate > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def probe(path: Path) -> dict[str, Any]:
    data = json.loads(capture([
        tool_path("ffprobe"), "-v", "error", "-show_streams",
        "-show_format", "-of", "json", str(path),
    ]))
    videos = [
        s for s in data.get("streams", [])
        if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic", 0)
    ]
    if len(videos) != 1:
        raise ValueError("The source must contain exactly one ordinary video stream.")
    video = videos[0]
    if not video.get("width") or not video.get("height"):
        raise ValueError("Could not read the source dimensions.")
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    return {"video": video, "audio": audio, "format": data.get("format", {})}


def read_timestamps(path: Path) -> list[float]:
    data = json.loads(capture([
        tool_path("ffprobe"), "-v", "error", "-select_streams", "v:0",
        "-show_frames", "-show_entries", "frame=best_effort_timestamp_time",
        "-of", "json", str(path),
    ]))
    result = []
    for frame in data.get("frames", []):
        value = frame.get("best_effort_timestamp_time")
        if value not in (None, "N/A"):
            result.append(float(value))
    if len(result) < 2:
        raise ValueError("Could not inspect enough video frame timestamps.")
    return result


def cadence_report(stamps: list[float]) -> dict[str, Any]:
    intervals = [b - a for a, b in zip(stamps, stamps[1:])]
    if not intervals or any(x <= 0 for x in intervals):
        raise ValueError("The source has non-monotonic presentation timestamps.")
    median = statistics.median(intervals)
    max_dev = max(abs(x - median) for x in intervals)
    fps = 1.0 / median
    return {
        "frame_count": len(stamps),
        "fps_from_pts": fps,
        "median_interval_ms": median * 1000,
        "max_deviation_ms": max_dev * 1000,
        "near_60": 59.5 <= fps <= 60.5,
        "constant": max_dev <= 0.002,
    }


def output_dimensions(width: int, height: int, max_long_edge: int | None) -> tuple[int, int]:
    if max_long_edge is None or max(width, height) <= max_long_edge:
        return width, height
    factor = max_long_edge / max(width, height)
    w = max(2, int(round(width * factor / 2)) * 2)
    h = max(2, int(round(height * factor / 2)) * 2)
    return w, h


def source_is_hdr(video: dict[str, Any]) -> bool:
    return (
        video.get("color_transfer") in ("smpte2084", "arib-std-b67")
        or video.get("color_primaries") == "bt2020"
    )


def run_with_progress(
    command: list[str], duration: float, notify: Callable[[float, str], None]
) -> None:
    command = command[:1] + ["-progress", "pipe:1", "-nostats"] + command[1:]
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as error_log:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=error_log,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        last = -1.0
        try:
            assert process.stdout is not None
            for line in process.stdout:
                key, _, value = line.strip().partition("=")
                if key in ("out_time_us", "out_time_ms"):
                    try:
                        # FFmpeg reports this progress field in microseconds.
                        elapsed = int(value) / 1_000_000
                        percent = min(99.0, max(0.0, elapsed * 100 / max(duration, 0.001)))
                        if percent - last >= 0.5:
                            last = percent
                            notify(percent, f"Processing video · {percent:.0f}%")
                    except ValueError:
                        pass
            code = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        if code:
            error_log.seek(0)
            raise RuntimeError(error_log.read()[-6000:] or "FFmpeg failed.")
    notify(100, "Processing complete · verifying output")


def prepare_video(
    source: Path, destination: Path, resolution: str, crf: int,
    notify: Callable[[float, str], None],
) -> dict[str, Any]:
    if not source.is_file():
        raise FileNotFoundError("The selected source file no longer exists.")
    if source.resolve() == destination.resolve():
        raise ValueError("Choose an output path different from the source.")
    if destination.suffix.lower() != ".mp4":
        raise ValueError("The output file must use the .mp4 extension.")

    notify(0, "Inspecting source video")
    before = probe(source)
    video, audio = before["video"], before["audio"]
    source_stamps = read_timestamps(source)
    source_cadence = cadence_report(source_stamps)
    if not source_cadence["near_60"] or not source_cadence["constant"]:
        raise ValueError(
            f"Source cadence is {source_cadence['fps_from_pts']:.3f} FPS or variable. "
            "This tool preserves real frames; it does not invent or duplicate frames to label a source 60 FPS. "
            "Use a constant-cadence 59.5–60.5 FPS source."
        )

    max_edge = RESOLUTIONS[resolution]
    width, height = int(video["width"]), int(video["height"])
    out_w, out_h = output_dimensions(width, height, max_edge)
    resizing = (out_w, out_h) != (width, height)
    if resizing and source_is_hdr(video):
        raise ValueError("HDR/BT.2020 resizing is not enabled yet. Choose Keep original to preserve the source.")
    if resizing and video.get("codec_name") not in ("h264", "hevc"):
        raise ValueError("Resolution conversion currently supports H.264 and HEVC sources only.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_output = destination.with_name(f".{destination.stem}.{os.getpid()}.partial.mp4")
    temp_report = destination.with_suffix(destination.suffix + ".report.json.partial")
    temp_output.unlink(missing_ok=True)
    temp_report.unlink(missing_ok=True)

    command = [
        tool_path("ffmpeg"), "-hide_banner", "-y", "-nostdin", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-video_track_timescale", str(VIDEO_TIMESCALE),
        "-movflags", "+faststart",
    ]
    if resizing:
        command += [
            "-vf", f"scale={out_w}:{out_h}:flags=lanczos",
            "-fps_mode", "passthrough", "-enc_time_base:v", f"1:{VIDEO_TIMESCALE}",
            "-crf", str(crf), "-preset", "slow",
        ]
        if video.get("codec_name") == "hevc":
            command += ["-c:v", "libx265", "-tag:v", "hvc1"]
            command += ["-pix_fmt", "yuv420p10le" if "10" in video.get("pix_fmt", "") else "yuv420p"]
        else:
            command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-x264-params", "scenecut=0"]
    else:
        command += ["-c:v", "copy"]

    if audio:
        if audio.get("codec_name") in ("aac", "alac", "mp3", "ac3", "eac3"):
            command += ["-c:a", "copy"]
        else:
            command += ["-c:a", "aac", "-b:a", "320k"]
    else:
        command += ["-an"]
    command += ["-f", "mp4", str(temp_output)]

    duration_value = video.get("duration") or before["format"].get("duration") or 0
    try:
        duration = float(duration_value)
    except (TypeError, ValueError):
        duration = 0
    if duration <= 0:
        duration = max(source_stamps) - min(source_stamps)

    try:
        run_with_progress(command, duration, notify)
        notify(100, "Verifying frame cadence, timing and dimensions")
        after = probe(temp_output)
        output_video = after["video"]
        output_stamps = read_timestamps(temp_output)
        output_cadence = cadence_report(output_stamps)
        if output_video.get("time_base") != f"1/{VIDEO_TIMESCALE}":
            raise RuntimeError(
                f"Output timebase is {output_video.get('time_base')}; expected 1/{VIDEO_TIMESCALE}. Output rejected."
            )
        if (int(output_video["width"]), int(output_video["height"])) != (out_w, out_h):
            raise RuntimeError("Output dimensions do not match the selected resolution. Output rejected.")
        if len(source_stamps) != len(output_stamps):
            raise RuntimeError("Output frame count differs from the source. Output rejected.")

        src0, out0 = source_stamps[0], output_stamps[0]
        max_error = max(abs((a - src0) - (b - out0)) for a, b in zip(source_stamps, output_stamps))
        if max_error > 0.002:
            raise RuntimeError(f"Frame timestamps changed by up to {max_error * 1000:.2f} ms. Output rejected.")
        if not output_cadence["near_60"] or not output_cadence["constant"]:
            raise RuntimeError("Output did not pass constant-cadence 60 FPS verification. Output rejected.")

        report = {
            "application": APP_NAME,
            "version": APP_VERSION,
            "method": "MP4 video-track-timescale remux with optional spatial resize",
            "video_track_timescale": VIDEO_TIMESCALE,
            "source": str(source),
            "output": str(destination),
            "resolution_selection": resolution,
            "source_resolution": [width, height],
            "output_resolution": [out_w, out_h],
            "video_reencoded": resizing,
            "video_codec_source": video.get("codec_name"),
            "video_codec_output": output_video.get("codec_name"),
            "source_frame_count": len(source_stamps),
            "output_frame_count": len(output_stamps),
            "source_cadence": source_cadence,
            "output_cadence": output_cadence,
            "max_timestamp_error_ms": max_error * 1000,
            "video_bitstream_copied": not resizing,
            "platform_result_guaranteed": False,
            "notes": [
                "No frames were generated or duplicated.",
                "The timescale is a local MP4 representation change, not a command to a social platform.",
                "Resolution selections never upscale.",
                "Resized video is re-encoded and is not bit-for-bit lossless.",
                "Global metadata and chapters are omitted; only the first audio stream is retained.",
            ],
        }
        report_final = destination.with_suffix(destination.suffix + ".report.json")
        temp_report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        os.replace(temp_output, destination)
        os.replace(temp_report, report_final)
        notify(100, "Done · output verified")
        return report
    except BaseException:
        temp_output.unlink(missing_ok=True)
        temp_report.unlink(missing_ok=True)
        raise


class DesktopApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title(f"{APP_NAME} · {APP_VERSION}")
        root.geometry("820x600")
        root.minsize(740, 550)
        root.configure(bg="#0b0b0b")
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.source: Path | None = None
        self.info: dict[str, Any] | None = None
        self.cadence: dict[str, Any] | None = None
        self.busy = False

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#0b0b0b")
        style.configure("Panel.TFrame", background="#141414")
        style.configure("TLabel", background="#0b0b0b", foreground="#f1f1f1", font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background="#0b0b0b", foreground="#969696", font=("Segoe UI", 9))
        style.configure("Panel.TLabel", background="#141414", foreground="#f1f1f1", font=("Segoe UI", 10))
        style.configure("Title.TLabel", background="#0b0b0b", foreground="#fff", font=("Segoe UI", 23, "bold"))
        style.configure("Sub.TLabel", background="#0b0b0b", foreground="#a5a5a5", font=("Segoe UI", 9))
        style.configure("TButton", background="#ededed", foreground="#111", padding=(14, 9), font=("Segoe UI", 9, "bold"), borderwidth=0)
        style.map("TButton", background=[("active", "#fff"), ("disabled", "#404040")], foreground=[("disabled", "#888")])
        style.configure("TCombobox", fieldbackground="#202020", background="#202020", foreground="#fff", arrowcolor="#fff", padding=7)
        style.configure("Horizontal.TProgressbar", troughcolor="#252525", background="#eee", bordercolor="#252525")

        self.resolution = tk.StringVar(value="Keep original")
        self.quality = tk.StringVar(value="Maximum · CRF 14")
        self.file_text = tk.StringVar(value="No source video selected")
        self.details = tk.StringVar(value="Import a video to inspect its frame cadence.")
        self.status = tk.StringVar(value="READY")
        self.progress_value = tk.DoubleVar(value=0)
        self._build()
        root.after(100, self._poll)

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=(28, 24, 28, 22))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="METUPLOADPHOD", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="LOCAL VIDEO PREPARATION  /  60 FPS SOURCE PRESERVATION", style="Sub.TLabel").pack(anchor="w", pady=(3, 0))

        ttk.Label(outer, text="SOURCE VIDEO", style="Muted.TLabel").pack(anchor="w", pady=(25, 8))
        panel = ttk.Frame(outer, style="Panel.TFrame", padding=16)
        panel.pack(fill="x")
        ttk.Label(panel, textvariable=self.file_text, style="Panel.TLabel", wraplength=680).pack(anchor="w")
        ttk.Label(panel, textvariable=self.details, style="Panel.TLabel", wraplength=680).pack(anchor="w", pady=(7, 12))
        self.import_button = ttk.Button(panel, text="IMPORT ORIGINAL VIDEO", command=self._import)
        self.import_button.pack(anchor="w")

        options = ttk.Frame(outer)
        options.pack(fill="x", pady=(21, 0))
        left = ttk.Frame(options)
        left.pack(side="left", fill="x", expand=True, padx=(0, 10))
        right = ttk.Frame(options)
        right.pack(side="left", fill="x", expand=True, padx=(10, 0))
        ttk.Label(left, text="OUTPUT RESOLUTION", style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        self.res_combo = ttk.Combobox(left, textvariable=self.resolution, values=list(RESOLUTIONS), state="readonly")
        self.res_combo.pack(fill="x")
        ttk.Label(left, text="No upscaling · portrait and landscape supported", style="Muted.TLabel").pack(anchor="w", pady=(6, 0))
        ttk.Label(right, text="ENCODING QUALITY", style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        self.quality_combo = ttk.Combobox(
            right, textvariable=self.quality,
            values=["Maximum · CRF 14", "Very high · CRF 16", "High · CRF 18"],
            state="readonly",
        )
        self.quality_combo.pack(fill="x")
        ttk.Label(right, text="Only used when downscaling requires re-encoding", style="Muted.TLabel", wraplength=340).pack(anchor="w", pady=(6, 0))

        note = ttk.Frame(outer, style="Panel.TFrame", padding=14)
        note.pack(fill="x", pady=(21, 0))
        ttk.Label(note, text="IMPORTANT", style="Panel.TLabel", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ttk.Label(
            note,
            text="60 FPS is preserved, not manufactured. The input must already contain a constant ~60 FPS timeline. "
                 "The 1/15360 track timescale is a local MP4 timing representation; it cannot guarantee a platform's "
                 "transcoding or 4K output. This build uses the research timescale method, not the separate AAC-PCE method.",
            style="Panel.TLabel", wraplength=700, justify="left",
        ).pack(anchor="w", pady=(5, 0))

        self.progress = ttk.Progressbar(outer, maximum=100, variable=self.progress_value, mode="determinate")
        self.progress.pack(fill="x", pady=(22, 8))
        ttk.Label(outer, textvariable=self.status, style="Muted.TLabel").pack(anchor="w")
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(17, 0))
        self.process_button = ttk.Button(actions, text="PROCESS VIDEO", command=self._start)
        self.process_button.pack(side="left")
        ttk.Label(actions, text="Local processing · no account connection", style="Muted.TLabel").pack(side="right")
        ttk.Label(outer, text="METUPLOADPHOD  /  EXPERIMENTAL MEDIA TOOL", style="Muted.TLabel").pack(anchor="w", pady=(22, 0))

    def _busy(self, value: bool) -> None:
        self.busy = value
        state = "disabled" if value else "normal"
        self.import_button.configure(state=state)
        self.process_button.configure(state=state)
        self.res_combo.configure(state="disabled" if value else "readonly")
        self.quality_combo.configure(state="disabled" if value else "readonly")

    def _import(self) -> None:
        name = filedialog.askopenfilename(
            title="Choose source video",
            filetypes=[("Video files", "*.mp4 *.mov *.m4v"), ("All files", "*.*")],
        )
        if not name:
            return
        self.source = Path(name)
        self.file_text.set(self.source.name)
        self.details.set("Inspecting source file and frame timestamps…")
        self.status.set("INSPECTING SOURCE")
        self._busy(True)
        threading.Thread(target=self._inspect_worker, args=(self.source,), daemon=True).start()

    def _inspect_worker(self, path: Path) -> None:
        try:
            info = probe(path)
            stamps = read_timestamps(path)
            cadence = cadence_report(stamps)
            self.events.put(("inspected", (path, info, cadence)))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _start(self) -> None:
        if self.busy:
            return
        if not self.source or not self.info or not self.cadence:
            messagebox.showwarning(APP_NAME, "Import and inspect a source video first.")
            return
        if not self.cadence["near_60"] or not self.cadence["constant"]:
            messagebox.showerror(APP_NAME, "This source is not verified as constant ~60 FPS.")
            return
        output_name = filedialog.asksaveasfilename(
            title="Save prepared video", initialdir=str(self.source.parent),
            initialfile=self.source.stem + "_MetUloadPhoD.mp4",
            defaultextension=".mp4", filetypes=[("MP4 video", "*.mp4")],
        )
        if not output_name:
            return
        output = Path(output_name)
        if output.exists() and not messagebox.askyesno(APP_NAME, f"Replace existing file?\n{output.name}"):
            return
        crf = {"Maximum · CRF 14": 14, "Very high · CRF 16": 16, "High · CRF 18": 18}.get(self.quality.get(), 14)
        self.progress_value.set(0)
        self.status.set("PREPARING OUTPUT")
        self._busy(True)
        threading.Thread(
            target=self._process_worker,
            args=(self.source, output, self.resolution.get(), crf),
            daemon=True,
        ).start()

    def _process_worker(self, source: Path, destination: Path, resolution: str, crf: int) -> None:
        try:
            result = prepare_video(source, destination, resolution, crf, lambda p, s: self.events.put(("progress", (p, s))))
            self.events.put(("done", result))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    percent, label = payload
                    self.progress_value.set(percent)
                    self.status.set(label.upper())
                elif kind == "inspected":
                    path, info, cadence = payload
                    self.source, self.info, self.cadence = path, info, cadence
                    video = info["video"]
                    duration_value = video.get("duration") or info["format"].get("duration") or 0
                    try:
                        duration = float(duration_value)
                    except (TypeError, ValueError):
                        duration = 0
                    fps = cadence["fps_from_pts"]
                    codec = str(video.get("codec_name", "unknown")).upper()
                    verified = cadence["near_60"] and cadence["constant"]
                    label = "CONSTANT ~60 FPS VERIFIED" if verified else "60 FPS NOT VERIFIED"
                    self.details.set(
                        f"{int(video['width'])} × {int(video['height'])}  ·  {fps:.3f} FPS  ·  "
                        f"{cadence['frame_count']:,} frames  ·  {codec}  ·  {duration:.2f} sec\n{label}"
                    )
                    self.status.set("SOURCE READY" if verified else "SOURCE FPS CHECK FAILED")
                    self._busy(False)
                    if not verified:
                        messagebox.showwarning(APP_NAME, "This source is not constant ~60 FPS. No frames will be invented or duplicated.")
                elif kind == "done":
                    report = payload
                    w, h = report["output_resolution"]
                    self.progress_value.set(100)
                    self.status.set("OUTPUT VERIFIED")
                    self._busy(False)
                    messagebox.showinfo(
                        APP_NAME,
                        f"Video prepared and verified.\n\nResolution: {w} × {h}\n"
                        f"Frames: {report['output_frame_count']:,}\n"
                        f"FPS: {report['output_cadence']['fps_from_pts']:.3f}\n"
                        f"Video re-encoded: {'Yes' if report['video_reencoded'] else 'No — stream copied'}\n\n"
                        f"Report: {report['output']}.report.json",
                    )
                elif kind == "error":
                    self._busy(False)
                    self.status.set("OPERATION FAILED")
                    messagebox.showerror(APP_NAME, str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._poll)


def main() -> None:
    root = tk.Tk()
    DesktopApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
