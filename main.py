import json
import os
import queue
import re
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path

import yt_dlp
from dotenv import load_dotenv
from google import genai
from google.genai import types


BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
OUTPUT_DIR = BASE_DIR / "output"
ENV_FILE = BASE_DIR / ".env"

load_dotenv(ENV_FILE)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()

QUALITY_OPTIONS = {
    "Best available": "bestvideo+bestaudio/best",
    "1080p or lower": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "720p or lower": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "480p or lower": "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "360p or lower": "bestvideo[height<=360]+bestaudio/best[height<=360]",
}

RATIO_OPTIONS = {
    "Keep original ratio": None,
    "Vertical 9:16 (Reels / Shorts)": (1080, 1920),
    "Horizontal 16:9": (1920, 1080),
    "Square 1:1": (1080, 1080),
    "Portrait 4:5 (Instagram feed)": (1080, 1350),
}

DURATION_OPTIONS = {
    "20–25 seconds": (20, 25),
    "15–20 seconds": (15, 20),
    "25–30 seconds": (25, 30),
    "30–45 seconds": (30, 45),
    "45–60 seconds": (45, 60),
    "Any duration (10–60 seconds)": (10, 60),
}


def safe_name(value, max_length=100):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(value))
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value[:max_length].strip() or "youtube_video")


def locate_download(info, ydl):
    candidates = []
    for key in ("filepath", "_filename"):
        if info.get(key):
            candidates.append(Path(info[key]))

    try:
        prepared = Path(ydl.prepare_filename(info))
        candidates.extend([
            prepared,
            prepared.with_suffix(".mp4"),
            prepared.with_suffix(".mkv"),
            prepared.with_suffix(".webm"),
        ])
    except Exception:
        pass

    for item in info.get("requested_downloads", []):
        if item.get("filepath"):
            candidates.append(Path(item["filepath"]))

    seen = set()
    for path in candidates:
        try:
            path = path.resolve()
        except OSError:
            continue
        if path not in seen:
            seen.add(path)
            if path.is_file() and path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}:
                return path

    files = [
        p for p in DOWNLOAD_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
        and not p.name.endswith((".part", ".ytdl"))
    ]
    if files:
        return max(files, key=lambda p: p.stat().st_mtime)
    raise FileNotFoundError("Could not locate the downloaded merged video.")


def get_duration(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True, check=True
    )
    return float(result.stdout.strip())


def parse_timestamped_subtitles(path):
    """Read VTT/SRT captions and return timestamped lines for Gemini."""
    content = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = content.splitlines()
    entries = []
    i = 0
    timestamp_pattern = re.compile(
        r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)\s*-->\s*"
    )

    def to_seconds(value):
        value = value.replace(",", ".")
        parts = value.split(":")
        if len(parts) == 2:
            hours = 0
            minutes = int(parts[0])
            seconds = float(parts[1])
        else:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
        return hours * 3600 + minutes * 60 + seconds

    while i < len(lines):
        line = lines[i].strip()
        match = timestamp_pattern.search(line)
        if match:
            start_text = match.group("start")
            cue_text = []
            i += 1
            while i < len(lines) and lines[i].strip():
                candidate = lines[i].strip()
                if not candidate.startswith(("NOTE", "STYLE", "REGION")):
                    candidate = re.sub(r"<[^>]+>", "", candidate)
                    candidate = re.sub(r"&amp;", "&", candidate)
                    candidate = re.sub(r"&lt;", "<", candidate)
                    candidate = re.sub(r"&gt;", ">", candidate)
                    if candidate and not candidate.isdigit():
                        cue_text.append(candidate)
                i += 1
            text = " ".join(cue_text).strip()
            if text:
                seconds = to_seconds(start_text)
                timestamp = format_timestamp(seconds)
                entries.append(f"[{timestamp}] {text}")
        i += 1

    if not entries:
        # Support plain TXT transcripts if each line begins with a timestamp.
        for line in lines:
            line = line.strip()
            if re.match(r"^\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s+.+", line):
                entries.append(line)

    if not entries:
        raise ValueError(
            "The transcript has no readable timestamps. Please upload a timestamped "
            "VTT/SRT transcript, or a TXT file with timestamps on each line."
        )

    return "\n".join(entries)


def format_timestamp(seconds):
    seconds = max(0, int(seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def download_video(url, quality, log, status, progress):
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    def hook(info):
        if info.get("status") == "downloading":
            total = info.get("total_bytes") or info.get("total_bytes_estimate")
            downloaded = info.get("downloaded_bytes", 0)
            if total:
                pct = min(35, downloaded / total * 35)
                progress(pct)
                status(f"Downloading video... {downloaded / total * 100:.1f}%")
        elif info.get("status") == "finished":
            log("Video/audio streams downloaded; FFmpeg is merging them.")

    options = {
        "format": quality,
        "merge_output_format": "mp4",
        "outtmpl": str(DOWNLOAD_DIR / "%(title).150B_%(id)s.%(ext)s"),
        "noplaylist": True,
        "retries": 3,
        "progress_hooks": [hook],
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        path = locate_download(info, ydl)
    return path, info


def download_transcript(url, video_info, log):
    """Try official subtitles first, then YouTube auto captions."""
    subtitles = video_info.get("subtitles") or {}
    auto_captions = video_info.get("automatic_captions") or {}

    available = list(subtitles.keys()) + [
        key for key in auto_captions.keys() if key not in subtitles
    ]
    if not available:
        return None

    preferred = []
    for wanted in ("en", "en-US", "en-GB", "hi", "hi-IN", "en-orig", "hi-orig"):
        if wanted in available and wanted not in preferred:
            preferred.append(wanted)

    # If no preferred language exists, try the first available caption language.
    if not preferred:
        preferred = [available[0]]

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for language in preferred:
        options = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": [language],
            "subtitlesformat": "vtt/best",
            "outtmpl": str(DOWNLOAD_DIR / "%(title).150B_%(id)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([url])
            video_id = video_info.get("id", "")
            candidates = sorted(
                DOWNLOAD_DIR.glob(f"*{video_id}*.vtt"),
                key=lambda p: p.stat().st_mtime,
                reverse=True
            )
            if candidates:
                log(f"Transcript downloaded automatically ({language}).")
                return candidates[0]
        except Exception as exc:
            log(f"Caption attempt for {language} did not work: {exc}")

    return None


def generate_top_clips_with_gemini(transcript_text, video_title, source_url, duration, clip_duration_range, user_instructions):
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is missing. Add GEMINI_API_KEY and GEMINI_MODEL to the .env file."
        )

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
You are an expert short-form video editor. Analyze the timestamped transcript below
and identify the five strongest short-form viral moments for Instagram Reels and
YouTube Shorts.

VIDEO TITLE: {video_title}
SOURCE URL: {source_url}
FULL VIDEO DURATION IN SECONDS: {duration:.2f}
REQUIRED CLIP DURATION: {clip_duration_range[0]} to {clip_duration_range[1]} seconds per clip.
ADDITIONAL USER INSTRUCTIONS: {user_instructions or "None. Use your best editorial judgment."}

Requirements:
- Return exactly 5 distinct moments, ranked from strongest to fifth strongest.
- Every clip MUST be between {clip_duration_range[0]} and {clip_duration_range[1]} seconds inclusive.
- Prioritize the requested duration range over other editorial preferences.
- Choose self-contained moments with a clear hook, funny/emotional/surprising payoff,
  useful insight, or strong audience reaction.
- Never exceed 60 seconds, and never return a clip outside the required duration range.
- Use timestamps from the transcript. Do not invent, guess, or shift timestamps.
- start_seconds and end_seconds must be numeric seconds from the beginning of the video.
- Each clip must have end_seconds greater than start_seconds and be within the video duration.
- Include enough lead-in for the setup and enough time for the punchline/payoff.
- Avoid overlapping clips unless there is a compelling reason; prefer distinct moments.
- Titles should be short, engaging, and accurate. Descriptions should briefly explain the moment.
- Preserve the language and meaning of what is said. Do not fabricate dialogue.
- Return ONLY valid JSON, with no Markdown fences or commentary.
- Use exactly this JSON structure and field names:

{{
  "video_title": "{video_title}",
  "source_url": "{source_url}",
  "top_5_viral_clips": [
    {{
      "rank": 1,
      "start_time": "MM:SS",
      "end_time": "MM:SS",
      "start_seconds": 0,
      "end_seconds": 30,
      "duration_seconds": 30,
      "title": "Clip title",
      "description": "Short description"
    }}
  ]
}}

TIMESTAMPED TRANSCRIPT:
{transcript_text}
"""

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.25,
        ),
    )

    text = (response.text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gemini returned invalid JSON: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("top_5_viral_clips"), list):
        raise ValueError('Gemini response did not contain "top_5_viral_clips".')

    if len(data["top_5_viral_clips"]) != 5:
        raise ValueError(
            f"Gemini returned {len(data['top_5_viral_clips'])} clips instead of 5. "
            "Try again or check the transcript."
        )

    return data


def validate_clips(clips, video_duration, clip_duration_range):
    valid = []
    for index, clip in enumerate(clips, 1):
        if not isinstance(clip, dict):
            raise ValueError(f"Clip #{index} is not a JSON object.")
        try:
            start = float(clip["start_seconds"])
            end = float(clip["end_seconds"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"Clip #{index} has invalid start_seconds/end_seconds.")
        if start < 0 or end <= start:
            raise ValueError(f"Clip #{index} has an invalid time range.")
        if end > video_duration:
            raise ValueError(
                f"Clip #{index} ends at {end:.1f}s, beyond the video duration "
                f"({video_duration:.1f}s)."
            )
        clip_length = end - start
        min_length, max_length = clip_duration_range
        if clip_length < min_length or clip_length > max_length:
            raise ValueError(
                f"Clip #{index} is {clip_length:.1f}s long, outside the selected "
                f"{min_length}–{max_length}s range. Try again or choose a wider range."
            )
        valid.append((clip, start, end))
    return valid


def make_video_filter(output_size):
    if output_size is None:
        return None
    width, height = output_size
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1"
    )


def cut_clip(source_path, output_path, start, end, output_size):
    command = [
        "ffmpeg", "-hide_banner", "-y",
        "-ss", f"{start:.3f}",
        "-i", str(source_path),
        "-t", f"{end - start:.3f}",
        "-map", "0:v:0",
        "-map", "0:a:0?",
    ]

    video_filter = make_video_filter(output_size)
    if video_filter:
        command.extend(["-vf", video_filter])

    command.extend([
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ])
    subprocess.run(command, capture_output=True, text=True, check=True)


class ViralClipsApp:
    def __init__(self, root):
        self.root = root
        self.root.title("YouTube Viral Clips Generator")
        self.root.geometry("960x900")
        self.root.minsize(820, 760)

        self.events = queue.Queue()
        self.is_running = False
        self.transcript_waiter = None
        self.transcript_result = None

        self.url_var = tk.StringVar()
        self.quality_var = tk.StringVar(value="720p or lower")
        self.ratio_var = tk.StringVar(value="Vertical 9:16 (Reels / Shorts)")
        self.duration_var = tk.StringVar(value="20–25 seconds")
        self.status_var = tk.StringVar(value="Ready")

        self.build_ui()
        self.root.after(120, self.process_events)

    def build_ui(self):
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer, text="YouTube Viral Clips Generator",
            font=("Segoe UI", 19, "bold")
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text="Choose clip length and editing instructions; Gemini finds moments and creates clips.",
            font=("Segoe UI", 10)
        ).pack(anchor="w", pady=(4, 14))

        settings = ttk.LabelFrame(outer, text="1. Video settings", padding=12)
        settings.pack(fill="x", pady=(0, 12))

        ttk.Label(settings, text="YouTube video URL").grid(row=0, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.url_var).grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(4, 10)
        )

        ttk.Label(settings, text="Download quality").grid(row=2, column=0, sticky="w")
        ttk.Label(settings, text="Output aspect ratio").grid(row=2, column=1, sticky="w")

        ttk.Combobox(
            settings, textvariable=self.quality_var,
            values=list(QUALITY_OPTIONS.keys()), state="readonly"
        ).grid(row=3, column=0, sticky="ew", padx=(0, 8), pady=(4, 0))

        ttk.Combobox(
            settings, textvariable=self.ratio_var,
            values=list(RATIO_OPTIONS.keys()), state="readonly"
        ).grid(row=3, column=1, sticky="ew", padx=(8, 0), pady=(4, 0))

        ttk.Label(settings, text="Clip duration").grid(row=4, column=0, sticky="w", pady=(10, 0))
        ttk.Combobox(
            settings, textvariable=self.duration_var,
            values=list(DURATION_OPTIONS.keys()), state="readonly"
        ).grid(row=5, column=0, sticky="ew", padx=(0, 8), pady=(4, 0))

        ttk.Label(settings, text="Additional instructions for Gemini (optional)").grid(
            row=4, column=1, sticky="w", pady=(10, 0)
        )
        self.instructions_text = tk.Text(settings, height=3, wrap="word", font=("Segoe UI", 9))
        self.instructions_text.grid(row=5, column=1, sticky="ew", padx=(8, 0), pady=(4, 0))
        self.instructions_text.insert(
            "1.0",
            "Prioritize funny moments with a strong punchline. Keep the full setup and reaction."
        )

        settings.columnconfigure(0, weight=1)
        settings.columnconfigure(1, weight=1)

        workflow = ttk.LabelFrame(outer, text="2. Automatic transcript + Gemini AI", padding=12)
        workflow.pack(fill="x", pady=(0, 12))
        ttk.Label(
            workflow,
            text=(
                "The app first tries to download YouTube captions automatically. "
                "If captions are unavailable, it will ask you to select a timestamped "
                "VTT, SRT, or TXT transcript."
            ),
            wraplength=850
        ).pack(anchor="w")
        ttk.Label(
            workflow,
            text=f"Gemini model: {GEMINI_MODEL}",
            foreground="#2878b5"
        ).pack(anchor="w", pady=(7, 0))

        output_frame = ttk.LabelFrame(outer, text="3. AI-generated top 5 moments (preview)", padding=10)
        output_frame.pack(fill="both", expand=True, pady=(0, 12))

        text_frame = ttk.Frame(output_frame)
        text_frame.pack(fill="both", expand=True)
        self.result_text = tk.Text(
            text_frame, height=11, wrap="none", font=("Consolas", 9),
            state="disabled"
        )
        y_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.result_text.yview)
        x_scroll = ttk.Scrollbar(text_frame, orient="horizontal", command=self.result_text.xview)
        self.result_text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.result_text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(0, 10))
        self.start_button = ttk.Button(
            controls, text="Download, Analyze & Generate Clips", command=self.start
        )
        self.start_button.pack(side="left")
        self.reset_button = ttk.Button(
            controls, text="Reset for Next Video", command=self.reset, state="disabled"
        )
        self.reset_button.pack(side="left", padx=(8, 0))
        ttk.Button(
            controls, text="Open Output Folder", command=self.open_output
        ).pack(side="right")

        progress_frame = ttk.LabelFrame(outer, text="4. Progress", padding=10)
        progress_frame.pack(fill="x")
        ttk.Label(progress_frame, textvariable=self.status_var).pack(anchor="w")
        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(
            progress_frame, variable=self.progress_var, maximum=100
        ).pack(fill="x", pady=(7, 0))

        self.log_text = tk.Text(
            outer, height=6, wrap="word", state="disabled", font=("Consolas", 9)
        )
        self.log_text.pack(fill="x", pady=(10, 0))

    def log(self, message):
        self.events.put(("log", message))

    def set_status(self, message):
        self.events.put(("status", message))

    def set_progress(self, value):
        self.events.put(("progress", value))

    def set_result_json(self, data):
        self.events.put(("json", json.dumps(data, ensure_ascii=False, indent=2)))

    def start(self):
        if self.is_running:
            return

        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Please enter a YouTube video URL.")
            return
        if not GEMINI_API_KEY:
            messagebox.showerror(
                "Gemini API key missing",
                "Add GEMINI_API_KEY and GEMINI_MODEL to the .env file beside main.py, then restart the app."
            )
            return

        self.is_running = True
        self.start_button.configure(state="disabled")
        self.progress_var.set(0)
        self.status_var.set("Starting...")
        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", "end")
        self.result_text.configure(state="disabled")
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

        args = (
            url,
            QUALITY_OPTIONS[self.quality_var.get()],
            RATIO_OPTIONS[self.ratio_var.get()],
            DURATION_OPTIONS[self.duration_var.get()],
            self.instructions_text.get("1.0", "end").strip(),
        )
        threading.Thread(target=self.worker, args=args, daemon=True).start()

    def worker(self, url, quality, ratio, clip_duration_range, user_instructions):
        try:
            if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
                raise RuntimeError(
                    "FFmpeg/ffprobe not found in PATH. Restart PowerShell/Windows and verify ffmpeg -version."
                )

            self.set_status("Downloading YouTube video...")
            self.log("Starting video download.")
            video_path, info = download_video(
                url, quality, self.log, self.set_status, self.set_progress
            )
            self.log(f"Video downloaded and merged: {video_path.name}")
            self.set_progress(40)

            self.set_status("Trying to download transcript...")
            self.log("Checking YouTube subtitles and automatic captions.")
            transcript_path = download_transcript(url, info, self.log)

            if transcript_path is None:
                self.log("Automatic transcript download was unavailable.")
                self.set_status("Please select a transcript file.")
                waiter = threading.Event()
                self.transcript_waiter = waiter
                self.transcript_result = None
                self.events.put(("ask_transcript", None))
                waiter.wait()
                transcript_path = self.transcript_result
                self.transcript_waiter = None

                if not transcript_path:
                    raise RuntimeError("No transcript was selected. Processing cancelled.")

            transcript_text = parse_timestamped_subtitles(Path(transcript_path))
            self.log(f"Transcript ready: {Path(transcript_path).name}")
            self.set_progress(50)

            self.set_status("Analyzing transcript with Gemini AI...")
            self.log(f"Sending timestamped transcript to Gemini ({GEMINI_MODEL}).")
            duration = get_duration(video_path)
            video_title = info.get("title") or video_path.stem

            data = generate_top_clips_with_gemini(
                transcript_text, video_title, url, duration,
                clip_duration_range, user_instructions
            )
            clips = validate_clips(
                data["top_5_viral_clips"], duration, clip_duration_range
            )
            self.set_result_json(data)
            self.log("Gemini generated the top 5 moments.")
            self.set_progress(60)

            output_folder = OUTPUT_DIR / safe_name(data.get("video_title", video_title))
            output_folder.mkdir(parents=True, exist_ok=True)

            successful = 0
            failed = 0
            for index, (clip, start, end) in enumerate(clips, 1):
                try:
                    rank = int(clip.get("rank", index))
                except (TypeError, ValueError):
                    rank = index
                clip_title = safe_name(clip.get("title", f"clip_{rank}"), 80)
                output_path = output_folder / f"{rank:02d}_{clip_title}.mp4"

                self.set_status(f"Creating clip {index} of {len(clips)}: {clip_title}")
                self.log(f"[{index}/5] Cutting {clip_title} ({start:.1f}s–{end:.1f}s)")
                try:
                    cut_clip(video_path, output_path, start, end, ratio)
                    successful += 1
                    self.log(f"  DONE: {output_path.name}")
                except subprocess.CalledProcessError as exc:
                    failed += 1
                    detail = (exc.stderr or str(exc)).strip().splitlines()
                    self.log(f"  FAILED: {detail[-1] if detail else 'FFmpeg error'}")

                self.set_progress(60 + index / len(clips) * 40)

            summary = (
                f"Processing finished!\n\n"
                f"Clips created: {successful}\n"
                f"Failed: {failed}\n\n"
                f"Output folder:\n{output_folder}"
            )
            self.set_status("Completed")
            self.log(summary.replace("\n", " | "))
            self.events.put(("done", True))
            self.events.put(("info", summary))

        except Exception as exc:
            self.log(f"ERROR: {exc}")
            self.set_status("Stopped due to an error")
            self.events.put(("done", False))
            self.events.put(("error", str(exc)))

    def process_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", value + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "status":
                    self.status_var.set(value)
                elif kind == "progress":
                    self.progress_var.set(value)
                elif kind == "json":
                    self.result_text.configure(state="normal")
                    self.result_text.delete("1.0", "end")
                    self.result_text.insert("1.0", value)
                    self.result_text.configure(state="disabled")
                elif kind == "done":
                    self.is_running = False
                    self.start_button.configure(state="normal")
                    self.reset_button.configure(state="normal")
                    if value:
                        self.progress_var.set(100)
                elif kind == "error":
                    messagebox.showerror("Processing error", value)
                elif kind == "info":
                    messagebox.showinfo("Clips generated", value)
                elif kind == "ask_transcript":
                    selected = filedialog.askopenfilename(
                        title="Select timestamped transcript",
                        filetypes=[
                            ("Transcript files", "*.vtt *.srt *.txt"),
                            ("WebVTT subtitles", "*.vtt"),
                            ("SubRip subtitles", "*.srt"),
                            ("Text files", "*.txt"),
                            ("All files", "*.*"),
                        ],
                    )
                    self.transcript_result = selected or None
                    if self.transcript_waiter:
                        self.transcript_waiter.set()
        except queue.Empty:
            pass
        self.root.after(120, self.process_events)

    def reset(self):
        if self.is_running:
            messagebox.showinfo("Please wait", "Wait for the current job to finish before resetting.")
            return

        self.url_var.set("")
        self.quality_var.set("720p or lower")
        self.ratio_var.set("Vertical 9:16 (Reels / Shorts)")
        self.duration_var.set("20–25 seconds")

        self.instructions_text.delete("1.0", "end")
        self.instructions_text.insert(
            "1.0",
            "Prioritize funny moments with a strong punchline. Keep the full setup and reaction."
        )

        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", "end")
        self.result_text.configure(state="disabled")

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

        self.progress_var.set(0)
        self.status_var.set("Ready for the next video")
        self.reset_button.configure(state="disabled")
        self.start_button.configure(state="normal")

    def open_output(self):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(str(OUTPUT_DIR))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(OUTPUT_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(OUTPUT_DIR)])
        except Exception as exc:
            messagebox.showerror("Cannot open folder", str(exc))


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    ViralClipsApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
