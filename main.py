"""
Viral Clips Studio
------------------
Paste a YouTube link -> captions are fetched -> Gemini picks the 5 best moments
-> the clips are cut with FFmpeg.

If the Gemini API fails (bad key, quota, network, invalid JSON, wrong clips ...),
the app does NOT stop. It prepares a transcript file + prompt and asks you to run
it manually in the Gemini chat website, then paste the JSON answer back here.

Clip editor: after the AI picks the clips, an editor opens that plays the video straight from
YouTube (nothing is downloaded yet). For every clip you set start/end time and output width/height,
then press "Generate clips" - only then is the video downloaded and the clips are cut.
(Optional: pip install pywebview  ->  the editor opens in its own window instead of the browser.)

New in this version:
  * "Download the video first" (sidebar switch, or the button inside the editor): the video is downloaded
    and the editor plays the file from your computer. Use it for long videos that will not play from YouTube.
  * Titles & hashtags for ALL clips in one go (one Gemini call). They are written automatically after the
    clips are created, shown in one window with Copy buttons, and saved as posts.txt in the output folder.
  * Crop frame you can move: set width/height for a clip, then DRAG the frame on the video to choose
    which part is kept (or press "Center"). FFmpeg crops exactly that part.
  * SPLIT SCREEN for vertical clips: choose Layout -> "Split: top + bottom" on a clip. Two frames (TOP = blue,
    BOTTOM = gold) appear on the video; drag each one to a different part of the picture (zoom and split height
    are adjustable, a panel can also show the full video). FFmpeg stacks the two parts into one vertical video.
    With the downloaded video you also get a live preview of the finished clip.
  * ONLY THE CLIPS ARE DOWNLOADED - FOR LONG VIDEOS ONLY (20 minutes or more; LONG_VIDEO_SECONDS; sidebar switch, on
    by default). Shorter videos open the editor straight from YouTube and are downloaded in full when you press
    Generate. For long videos, instead of the whole video, only the part of
    each clip plus 10 seconds before and 10 seconds after it is downloaded (a clip at 21:00-21:20 -> 20:50-21:30,
    40 seconds). Clips that overlap share one download. The final clip is still exactly the start/end you chose.
    The parts are downloaded RIGHT AFTER the AI gives the clip times, BEFORE the editor opens. The editor then
    plays those small files (so you edit inside the tool, not on another site) and you can move the start/end
    anywhere inside the downloaded 10 s margins. Generate clips just cuts them - nothing more is downloaded.
    If you used \"Download video first\" (full video), that file is used instead.
  * TEXT ON THE VIDEO: press "+ Add text" on a clip. Type the text (Hindi works too), pick the font, size, colour,
    bold/italic, outline, an optional background box (colour, opacity, padding, rounded corners) and place it:
    drag it on the "Output preview", use the X / Y sliders or the Top / Middle / Bottom buttons. You can also set
    "from / to" seconds so the text appears only for part of the clip. Add as many texts as you like.
  * "Title & hashtags" button on every clip card: Gemini writes a YouTube title, YouTube description,
    Instagram caption and hashtags from what is said in the clip. Copy buttons for each part.

Install once:
    pip install customtkinter yt-dlp python-dotenv google-genai
Also needs FFmpeg + ffprobe on PATH.

.env file (next to main.py) - can also be edited from the Settings button:
    GEMINI_API_KEY=your_key
    GEMINI_MODEL=gemini-3.5-flash-lite
    COOKIES_BROWSER=None
"""

import base64
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
import importlib.util
import mimetypes
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
import tkinter as tk
from dataclasses import dataclass, replace, field
from pathlib import Path
from tkinter import filedialog, messagebox

try:
    import customtkinter as ctk
except ImportError:  # friendly message instead of a raw traceback
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror(
        "Missing package",
        "This app needs the 'customtkinter' package.\n\n"
        "Open a terminal and run:\n    pip install customtkinter\n\nThen start the app again.",
    )
    sys.exit(1)

import yt_dlp
from dotenv import load_dotenv
from google import genai
from google.genai import types
from yt_dlp.utils import download_range_func

try:  # optional: upload the finished clips to YouTube (all that code lives in youtube_uploader.py)
    from youtube_uploader import make_upload_panel_class
except ImportError:
    make_upload_panel_class = None


# --------------------------------------------------------------------------- #
# Paths, config, constants
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
OUTPUT_DIR = BASE_DIR / "output"
MANUAL_DIR = BASE_DIR / "manual_gemini"
PREVIEW_DIR = BASE_DIR / "previews"
ENV_FILE = BASE_DIR / ".env"

DEFAULT_MODEL = "gemini-3.5-flash-lite"
REQUIRED_CLIPS = 5  # clips asked from the AI for every transcript part
TRANSCRIPT_CHUNK_SECONDS = 35 * 60  # long transcripts go to the AI in parts of about this length (30-40 min)
MIN_LAST_CHUNK_SECONDS = 5 * 60  # a shorter last part is joined to the part before it
MAX_AI_ATTEMPTS = 3
CLIP_TOLERANCE = 1.0  # seconds of slack allowed around the chosen duration range
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
GEMINI_URL = "https://gemini.google.com/app"
SIZE_DEFAULT = "default"  # clip uses the sidebar "Output shape" setting
SECTION_PAD = 10  # seconds downloaded before and after every clip when only the clip parts are downloaded
LONG_VIDEO_SECONDS = 20 * 60  # videos at least this long: only the clips are downloaded; shorter ones play from YouTube
SECTION_MERGE_GAP = 1  # parts that overlap or are closer than this many seconds are downloaded as one

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

COOKIE_BROWSERS = ["None", "chrome", "edge", "firefox", "brave", "opera", "safari"]

DEFAULT_INSTRUCTIONS = "Prioritize funny moments with a strong punchline."

STEPS = ["Video info", "Transcript", "AI analysis", "Download", "Create clips", "Upload"]

FFMPEG_HELP = (
    "FFmpeg (and ffprobe) were not found on this computer.\n\n"
    "Windows: open PowerShell and run\n    winget install Gyan.FFmpeg\n"
    "then close and reopen PowerShell and this app so PATH refreshes.\n\n"
    "macOS: brew install ffmpeg\nLinux: sudo apt install ffmpeg"
)

# Palette: graphite panels, one periwinkle accent.
BG = "#16181d"
PANEL = "#1f2229"
RAISED = "#272b34"
LINE = "#343945"
TEXT = "#e8e6e1"
SOFT = "#c3c7d1"
MUTED = "#8d93a0"
ACCENT = "#8b9bff"
ACCENT_HOVER = "#7686f0"
ACCENT_TEXT = "#10131c"
GOOD = "#5fc9a0"
BAD = "#ef6a6a"
WARN = "#e8b04a"

UI_FONT = "Segoe UI" if os.name == "nt" else None
MONO_FONT = "Consolas" if os.name == "nt" else "Courier"


class Config:
    api_key = ""
    model = DEFAULT_MODEL
    cookies_browser = "None"

    @classmethod
    def load(cls):
        load_dotenv(ENV_FILE, override=True)
        cls.api_key = os.getenv("GEMINI_API_KEY", "").strip()
        cls.model = os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODEL
        cls.cookies_browser = os.getenv("COOKIES_BROWSER", "None").strip() or "None"


Config.load()


def save_env_values(values):
    """Update/insert KEY=VALUE lines in .env without touching other lines."""
    lines = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    remaining = dict(values)
    out = []
    for line in lines:
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Errors and helpers
# --------------------------------------------------------------------------- #
class JobCancelled(Exception):
    pass


class UserFacingError(Exception):
    """An error whose message is already written for the user."""


class Waiter:
    """Lets the worker thread wait for an answer from the UI thread."""

    def __init__(self):
        self.event = threading.Event()
        self.value = None

    def resolve(self, value):
        self.value = value
        self.event.set()


@dataclass
class Clip:
    rank: int
    title: str
    description: str
    start: float
    end: float
    size: object = SIZE_DEFAULT  # SIZE_DEFAULT | None (original) | (width, height)
    crop_x: float = 0.5  # where the crop frame sits: 0 = far left, 0.5 = centre, 1 = far right
    crop_y: float = 0.5  # 0 = top, 0.5 = centre, 1 = bottom
    layout: str = "single"  # "single" = one crop frame, "split" = two frames stacked top / bottom
    split: object = None  # for "split": {"ratio", "top": region, "bottom": region} (see normalize_split)
    texts: object = None  # text overlays: list of {"png": bytes, "x", "y", "start", "end"} (drawn by the editor)

    @property
    def length(self):
        return self.end - self.start

    def to_json(self):
        return {
            "rank": self.rank,
            "start_time": format_timestamp(self.start),
            "end_time": format_timestamp(self.end),
            "start_seconds": round(self.start, 2),
            "end_seconds": round(self.end, 2),
            "duration_seconds": round(self.length, 2),
            "title": self.title,
            "description": self.description,
        }


@dataclass
class Job:
    url: str
    quality: str
    ratio: object
    duration_range: tuple
    instructions: str
    auto_create: bool
    cancel: threading.Event
    download_first: bool = False  # download before editing, edit from the file
    auto_post: bool = True  # write titles/hashtags for all clips after they are created
    clip_parts_only: bool = True  # download only each clip (+ SECTION_PAD seconds either side), not the whole video
    video_duration: object = None  # length of the YouTube video in seconds (filled in when the video is read)
    chunks: list = field(default_factory=list)  # transcript parts, see split_transcript()


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def friendly_error(exc):
    """Turn technical exceptions into messages that say what to do next."""
    if isinstance(exc, UserFacingError):
        return str(exc)
    raw = ANSI_RE.sub("", str(exc)).strip()
    low = raw.lower()
    rules = [
        (("private video",),
         "This video is private. Use a public or unlisted video."),
        (("confirm your age", "age-restricted", "age restricted"),
         "This video is age-restricted. Open Settings, pick the browser where you are signed in to "
         "YouTube under 'Browser cookies', and try again."),
        (("not a bot", "sign in to confirm"),
         "YouTube asked for a sign-in check. Open Settings, pick the browser where you are signed in "
         "to YouTube under 'Browser cookies', and try again."),
        (("members-only", "members only", "join this channel"),
         "This video is for channel members only. Pick a public video, or use browser cookies from "
         "an account that is a member (Settings)."),
        (("not available in your country", "blocked it in your country", "geo restricted", "geo-restricted"),
         "This video is blocked in your country."),
        (("video unavailable", "this video is unavailable", "has been removed", "no longer available",
          "this video has been"),
         "This video is unavailable or was removed."),
        (("premieres in", "live event will begin"),
         "This is an upcoming premiere/live stream. Try again after it has finished."),
        (("unsupported url", "is not a valid url"),
         "That link is not supported. Paste a normal YouTube video link."),
        (("http error 429", "too many requests"),
         "YouTube is rate limiting this computer. Wait a few minutes, or use browser cookies in Settings."),
        (("getaddrinfo", "urlopen error", "timed out", "timeout", "connection reset", "connection aborted",
          "network is unreachable", "name or service not known", "failed to resolve", "temporary failure",
          "connection error", "max retries exceeded"),
         "Network problem. Check your internet connection and try again."),
        (("no space left", "disk full"),
         "The disk is full. Free some space and try again."),
        (("permission denied", "access is denied"),
         "Windows blocked access to a file or folder. Close any program using the clips and try again."),
        (("api key not valid", "api_key_invalid", "invalid api key", "api key expired"),
         "The Gemini API key is not valid. Check it in Settings."),
        (("permission_denied", "403"),
         "Gemini refused the request (permission denied). Check the API key and model in Settings."),
        (("resource_exhausted", "quota", "429"),
         "The Gemini quota or rate limit was reached."),
        (("not_found", "404", "is not found for api version", "models/"),
         f"Gemini could not find the model '{Config.model}'. Check the model name in Settings."),
        (("503", "unavailable", "overloaded"),
         "Gemini is overloaded right now."),
    ]
    for needles, message in rules:
        if any(n in low for n in needles):
            return message
    if len(raw) > 500:
        raw = raw[:500] + "…"
    return raw or exc.__class__.__name__


def is_fatal_ai_error(exc):
    """Errors that retrying cannot fix (wrong key, wrong model)."""
    low = str(exc).lower()
    return any(n in low for n in (
        "api key not valid", "api_key_invalid", "invalid api key", "permission_denied",
        "is not found for api version", "not_found", "api key expired",
    ))


def safe_name(value, max_length=100):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(value))
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:max_length].strip(" .") or "youtube_video"


def unique_path(path):
    if not path.exists():
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path


def format_timestamp(seconds):
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


_NUM = re.compile(r"\d+(?:\.\d+)?")


def parse_time_value(value):
    """Accepts 12, 12.5, '12', '01:05', '1:02:03.500', '01:05,250' -> seconds."""
    if isinstance(value, bool) or value is None:
        raise ValueError("empty time value")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    if _NUM.fullmatch(text):
        return float(text)
    parts = text.split(":")
    if len(parts) in (2, 3) and all(_NUM.fullmatch(p) for p in parts):
        nums = [float(p) for p in parts]
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    raise ValueError(f"cannot read time '{value}'")


def read_text_file(path):
    data = Path(path).read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def open_path(path, select=False):
    path = Path(path)
    try:
        if os.name == "nt":
            if select and path.is_file():
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                os.startfile(str(path if path.is_dir() else path.parent if select else path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)] if (select and path.is_file()) else ["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])
    except Exception as exc:
        messagebox.showerror("Cannot open", str(exc))


# --------------------------------------------------------------------------- #
# Transcript parsing
# --------------------------------------------------------------------------- #
CUE_RE = re.compile(r"(?P<start>(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?)\s*-->")
TS_ONLY_RE = re.compile(r"^\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?$")
TS_INLINE_RE = re.compile(r"^\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?\s*[-–—:]?\s+(.+)$")


def clean_caption_line(line):
    line = re.sub(r"<[^>]+>", "", line)
    line = line.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    line = line.replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"')
    return re.sub(r"\s+", " ", line).strip()


def parse_transcript_text(content):
    """VTT / SRT / '[mm:ss] text' / YouTube 'Show transcript' paste -> '[mm:ss] text' lines."""
    lines = content.splitlines()
    entries = []

    # 1) VTT / SRT cues (with de-duplication of YouTube's rolling captions)
    prev_lines = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        match = CUE_RE.search(line) if "-->" in line else None
        if match:
            try:
                start = parse_time_value(match.group("start"))
            except ValueError:
                i += 1
                continue
            i += 1
            cue_lines = []
            while i < len(lines) and lines[i].strip():
                cleaned = clean_caption_line(lines[i])
                if cleaned:
                    cue_lines.append(cleaned)
                i += 1
            fresh = [c for c in cue_lines if c not in prev_lines]
            if cue_lines:
                prev_lines = cue_lines
            text = " ".join(fresh).strip()
            if text:
                entries.append(f"[{format_timestamp(start)}] {text}")
        i += 1

    # 2) Plain text: '[01:23] text', '01:23 text', or timestamp on its own line
    if not entries:
        current_start = None
        buffer = []

        def flush():
            nonlocal buffer
            if current_start is not None and buffer:
                entries.append(f"[{format_timestamp(current_start)}] {' '.join(buffer)}")
            buffer = []

        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            only = TS_ONLY_RE.match(line)
            if only:
                flush()
                current_start = parse_time_value(only.group(1))
                continue
            inline = TS_INLINE_RE.match(line)
            if inline:
                flush()
                current_start = None
                entries.append(f"[{format_timestamp(parse_time_value(inline.group(1)))}] {inline.group(2).strip()}")
                continue
            if current_start is not None:
                buffer.append(line)
        flush()

    if not entries:
        if not content.strip():
            raise ValueError("The transcript is empty.")
        raise ValueError(
            "No timestamps were found in this transcript. Use a VTT or SRT file, or text where each "
            "line starts with a time like [01:23] or 01:23."
        )
    return "\n".join(entries)


# --------------------------------------------------------------------------- #
# Prompt, AI JSON parsing and validation
# --------------------------------------------------------------------------- #
def split_transcript(transcript_text, duration=None, chunk_seconds=TRANSCRIPT_CHUNK_SECONDS):
    """Cut the '[mm:ss] text' transcript into parts of about `chunk_seconds`, on caption lines.
    -> [{"index", "start", "end", "text"}]; start/end are the seconds of the video the part covers."""
    buckets = {}
    for line in (transcript_text or "").splitlines():
        match = _TS_LINE.match(line.strip())
        if not match:
            continue
        try:
            moment = parse_time_value(match.group(1))
        except ValueError:
            continue
        buckets.setdefault(int(moment // chunk_seconds), []).append(line.strip())
    chunks = []
    for number in sorted(buckets):
        chunks.append({"start": number * chunk_seconds, "end": (number + 1) * chunk_seconds,
                       "text": "\n".join(buckets[number])})
    if len(chunks) >= 2:  # a tiny tail is not worth a request of its own
        last = chunks[-1]
        end = min(last["end"], duration) if duration else last["end"]
        if end - last["start"] < MIN_LAST_CHUNK_SECONDS:
            chunks[-2]["text"] += "\n" + last["text"]
            chunks[-2]["end"] = last["end"]
            chunks.pop()
    for number, chunk in enumerate(chunks):
        if duration:
            chunk["end"] = min(chunk["end"], float(duration))
        chunk["index"] = number
    if chunks:  # no gaps: every part starts where the previous one ended
        chunks[0]["start"] = 0
        for before, after in zip(chunks, chunks[1:]):
            after["start"] = before["end"]
    return chunks


def chunk_label(chunk):
    return f"{format_timestamp(chunk['start'])}–{format_timestamp(chunk['end'])}"


def clips_wanted(chunk):
    """How many clips to ask for in one part (5, fewer for a short last part)."""
    return max(1, min(REQUIRED_CLIPS, int((chunk["end"] - chunk["start"]) // 180)))


def build_prompt(video_title, source_url, duration, rng, instructions,
                 transcript_text=None, feedback=None, part=None, count=REQUIRED_CLIPS):
    duration_text = f"{duration:.2f}" if duration else "unknown (do not go past the end of the transcript)"
    if transcript_text is None:
        transcript_block = (
            "The timestamped transcript is attached to this message as the file transcript.txt. "
            "Read the whole file before answering."
        )
    else:
        transcript_block = f"TIMESTAMPED TRANSCRIPT:\n{transcript_text}"
    correction = ""
    if feedback:
        correction = (
            "\nIMPORTANT CORRECTION: your previous answer was rejected because: "
            f"{feedback}\nFix this in your new answer.\n"
        )
    title_json = json.dumps(video_title, ensure_ascii=False)
    url_json = json.dumps(source_url, ensure_ascii=False)
    part_text = ""
    if part:
        part_text = (
            f"\nTRANSCRIPT PART: this is part {part['index'] + 1} of {part['total']} of a longer video. It covers "
            f"{format_timestamp(part['start'])} to {format_timestamp(part['end'])} "
            f"({part['start']:.0f}s to {part['end']:.0f}s). Choose moments ONLY from inside this range: every "
            "clip must start and end between those two times.\n")
    return f"""You are an expert short-form video editor. Analyze the timestamped transcript
and identify the {count} strongest short-form viral moments for Instagram Reels and YouTube Shorts.

VIDEO TITLE: {video_title}
SOURCE URL: {source_url}
FULL VIDEO DURATION IN SECONDS: {duration_text}
REQUIRED CLIP DURATION: {rng[0]} to {rng[1]} seconds per clip.
ADDITIONAL USER INSTRUCTIONS: {instructions or "None. Use your best editorial judgment."}
{part_text}{correction}
Requirements:
- Return exactly {count} distinct moments, ranked from strongest to weakest.
- Every clip MUST be between {rng[0]} and {rng[1]} seconds inclusive.
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
  "video_title": {title_json},
  "source_url": {url_json},
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

{transcript_block}
"""


def _pull_clip_list(obj):
    if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
        return obj
    if isinstance(obj, dict):
        value = obj.get("top_5_viral_clips")
        if isinstance(value, list):
            return value
        for value in obj.values():
            if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
                return value
    return None


def extract_raw_clips(text):
    """Find the clip list inside whatever Gemini returned (fences, chatter, list/dict)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("The response is empty.")
    text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    variants = [text]
    straight = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    if straight != text:
        variants.append(straight)

    decoder = json.JSONDecoder()
    last_error = None
    for variant in variants:
        try:
            clips = _pull_clip_list(json.loads(variant))
            if clips is not None:
                return clips
        except json.JSONDecodeError as exc:
            last_error = exc
        for index, ch in enumerate(variant):
            if ch in "{[":
                try:
                    obj, _ = decoder.raw_decode(variant[index:])
                except json.JSONDecodeError as exc:
                    last_error = exc
                    continue
                clips = _pull_clip_list(obj)
                if clips is not None:
                    return clips
    if last_error:
        raise ValueError(f"The text is not valid JSON ({last_error.msg} at position {last_error.pos}).")
    raise ValueError('No clip list was found. The JSON must contain "top_5_viral_clips".')


def _first_value(raw, *keys):
    for key in keys:
        value = raw.get(key)
        if value is not None and value != "":
            return value
    raise KeyError(keys[0])


def build_clips(raw_clips, video_duration, rng, tolerance=CLIP_TOLERANCE, required=REQUIRED_CLIPS, window=None):
    """Validate/repair clips. window=(start, end) keeps only clips inside that part of the video.
    Returns (valid_clips, problems)."""
    clips, problems = [], []
    min_len, max_len = rng
    for index, raw in enumerate(raw_clips, 1):
        label = f"Clip {index}"
        try:
            start = parse_time_value(_first_value(raw, "start_seconds", "start_time", "start"))
            try:
                end = parse_time_value(_first_value(raw, "end_seconds", "end_time", "end"))
            except KeyError:
                end = start + parse_time_value(_first_value(raw, "duration_seconds", "duration"))
        except (KeyError, ValueError):
            problems.append(f"{label} has no readable start/end time.")
            continue
        if start < 0 or end <= start:
            problems.append(f"{label} has an invalid time range ({start:.1f}s to {end:.1f}s).")
            continue
        if video_duration:
            if start >= video_duration:
                problems.append(f"{label} starts at {start:.1f}s, after the video ends ({video_duration:.1f}s).")
                continue
            if end > video_duration:
                if end - video_duration > 3:
                    problems.append(f"{label} ends at {end:.1f}s, beyond the video length ({video_duration:.1f}s).")
                    continue
                end = video_duration
        if window and (start < window[0] - 5 or end > window[1] + 5):
            problems.append(f"{label} ({start:.0f}s to {end:.0f}s) is outside this transcript part "
                            f"({window[0]:.0f}s to {window[1]:.0f}s).")
            continue
        length = end - start
        if length < min_len - tolerance or length > max_len + tolerance:
            problems.append(f"{label} is {length:.1f}s long, outside the {min_len}-{max_len}s range.")
            continue
        try:
            rank = int(raw.get("rank", index))
        except (TypeError, ValueError):
            rank = index
        title = str(raw.get("title") or f"Clip {rank}").strip()
        description = str(raw.get("description") or "").strip()
        clips.append(Clip(rank, title, description, start, end))

    clips.sort(key=lambda c: c.rank)
    clips = clips[:required]
    for new_rank, clip in enumerate(clips, 1):
        clip.rank = new_rank
    if len(raw_clips) < required:
        problems.append(f"Only {len(raw_clips)} clips were returned; exactly {required} are needed.")
    return clips, problems


def fit_to_duration(clips, duration):
    """After download we know the true duration: clamp or drop clips beyond it."""
    kept, dropped = [], []
    for clip in clips:
        if clip.start >= duration - 1:
            dropped.append(clip)
            continue
        end = min(clip.end, duration)
        if end - clip.start < 1:
            dropped.append(clip)
            continue
        kept.append(replace(clip, end=end))
    return kept, dropped


# --------------------------------------------------------------------------- #
# yt-dlp / ffmpeg / Gemini
# --------------------------------------------------------------------------- #
def ydl_base_options():
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "socket_timeout": 30,
    }
    cookies_file = BASE_DIR / "cookies.txt"
    if cookies_file.is_file():
        options["cookiefile"] = str(cookies_file)
    else:
        browser = Config.cookies_browser.strip().lower()
        if browser and browser != "none":
            options["cookiesfrombrowser"] = (browser,)
    return options


def fetch_video_info(url):
    options = ydl_base_options()
    options["skip_download"] = True
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise UserFacingError("Could not read any information from this link.")
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise UserFacingError("This link is an empty playlist. Paste a single video link.")
        info = entries[0]
    return info


def locate_download(info, ydl):
    candidates = []
    for item in info.get("requested_downloads", []) or []:
        if item.get("filepath"):
            candidates.append(Path(item["filepath"]))
    for key in ("filepath", "_filename"):
        if info.get(key):
            candidates.append(Path(info[key]))
    try:
        prepared = Path(ydl.prepare_filename(info))
        candidates.extend([prepared] + [prepared.with_suffix(s) for s in (".mp4", ".mkv", ".webm")])
    except Exception:
        pass

    video_exts = {".mp4", ".mkv", ".webm", ".mov"}
    seen = set()
    for path in candidates:
        try:
            path = path.resolve()
        except OSError:
            continue
        if path not in seen:
            seen.add(path)
            if path.is_file() and path.suffix.lower() in video_exts:
                return path

    video_id = info.get("id", "")
    files = [
        p for p in DOWNLOAD_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in video_exts and not p.name.endswith((".part", ".ytdl"))
    ]
    by_id = [p for p in files if video_id and video_id in p.name]
    pool = by_id or files
    if pool:
        return max(pool, key=lambda p: p.stat().st_mtime)
    raise FileNotFoundError("Could not find the downloaded video file.")


class TerminalLogger:
    """yt-dlp logger: prints EVERYTHING (including FFmpeg's output) to the terminal window,
    and passes the important lines on to the app's Log tab."""

    def __init__(self, log=None):
        self.log = log

    @staticmethod
    def echo(text):
        try:
            print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)
        except Exception:
            pass

    def debug(self, msg):
        if msg.startswith("[debug] "):
            return
        self.echo(msg)

    def info(self, msg):
        self.echo(msg)

    def warning(self, msg):
        self.echo(f"WARNING: {msg}")

    def error(self, msg):
        self.echo(f"ERROR: {msg}")
        if self.log:
            self.log(f"yt-dlp: {msg}", "error")


def start_heartbeat(label, status, cancel, folder=None):
    """Every 3 s: print how long it has been running and how big the files in `folder` are, so you can see
    that the download is alive even when FFmpeg reports no percentage. Returns a function that stops it."""
    stop = threading.Event()
    began = time.time()

    def run():
        while not stop.wait(3.0) and not cancel.is_set():
            elapsed = int(time.time() - began)
            size = 0
            if folder is not None:
                try:
                    size = sum(f.stat().st_size for f in Path(folder).glob("*") if f.is_file()
                               and f.stat().st_mtime >= began - 1)
                except OSError:
                    pass
            text = f"{label} - running for {elapsed // 60}:{elapsed % 60:02d}" + (f", {size / 1e6:.1f} MB written" if size else "")
            TerminalLogger.echo(text)
            status(text)

    threading.Thread(target=run, daemon=True).start()
    return stop.set


def download_video(url, quality, log, status, progress, cancel):
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    top = {"value": 30.0}

    def hook(data):
        if cancel.is_set():
            raise JobCancelled()
        if data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            done = data.get("downloaded_bytes", 0)
            if total:
                top["value"] = max(top["value"], 30 + min(done / total, 1) * 40)
                progress(top["value"])
                speed = (data.get("_speed_str") or "").strip()
                eta = (data.get("_eta_str") or "").strip()
                extra = f"  •  {speed}" if speed else ""
                extra += f"  •  ETA {eta}" if eta else ""
                status(f"Downloading video… {done / total * 100:.0f}%{extra}")
        elif data.get("status") == "finished":
            log("Video and audio downloaded. FFmpeg is merging them.")
            status("Merging video and audio…")

    options = ydl_base_options()
    options.update({
        "format": quality,
        "merge_output_format": "mp4",
        "outtmpl": str(DOWNLOAD_DIR / "%(title).150B_%(id)s.%(ext)s"),
        "progress_hooks": [hook],
        "quiet": False, "no_warnings": False, "logger": TerminalLogger(log),
    })
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            path = locate_download(info, ydl)
    except JobCancelled:
        raise
    except Exception:
        if cancel.is_set():
            raise JobCancelled()
        raise
    return path, info


def plan_sections(clips, duration=None, pad=SECTION_PAD):
    """The time ranges (whole seconds) to download: every clip plus `pad` seconds on both sides.
    Ranges that overlap or touch are merged, so each clip lies completely inside one range."""
    spans = []
    for clip in clips:
        begin = max(0, int(math.floor(clip.start - pad)))
        finish = int(math.ceil(clip.end + pad))
        if duration:
            finish = min(finish, int(math.ceil(duration)))
        if finish - begin >= 1:
            spans.append([begin, finish])
    spans.sort()
    merged = []
    for begin, finish in spans:
        if merged and begin <= merged[-1][1] + SECTION_MERGE_GAP:
            merged[-1][1] = max(merged[-1][1], finish)
        else:
            merged.append([begin, finish])
    return [(begin, finish) for begin, finish in merged]


def find_part(parts, clip):
    """The downloaded part that contains this clip (parts: list of {"start", "end", "path"})."""
    for part in parts:
        if part["start"] - 0.01 <= clip.start and clip.end <= part["end"] + 0.01:
            return part
    for part in parts:  # clip runs a little past the end of the video
        if part["start"] - 0.01 <= clip.start < part["end"]:
            return part
    return None


def parts_cover(parts, clip):
    """True when the clip lies completely inside one of the downloaded parts."""
    return any(part["start"] - 0.01 <= clip.start and clip.end <= part["end"] + 0.01 for part in parts)


def download_clip_parts(url, quality, ranges, log, status, progress, cancel):
    """Download only the given (start, end) ranges of the video. -> (list of {"start","end","path"}, info)"""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    total = len(ranges)
    state = {"files": [], "last": 0.0}
    if quality == QUALITY_OPTIONS["Best available"]:
        # Cutting a part means FFmpeg re-encodes it for an exact start. 4K/8K would take very long, and the clips
        # are made at 1080p-class sizes anyway, so 'Best available' is capped at 1080p for the parts only.
        quality = QUALITY_OPTIONS["1080p or lower"]
        TerminalLogger.echo("Clip parts: 'Best available' is limited to 1080p so the cutting stays fast.")

    def hook(data):
        if cancel.is_set():
            raise JobCancelled()
        if data.get("status") == "downloading":
            done = data.get("downloaded_bytes") or 0
            number = min(len(state["files"]) + 1, total)
            status(f"Downloading clip part {number} of {total}…" + (f"  {done / 1e6:.1f} MB" if done else ""))
            if time.time() - state["last"] >= 2:  # a progress line in the terminal every 2 seconds
                state["last"] = time.time()
                speed = (data.get("_speed_str") or "").strip()
                TerminalLogger.echo(f"part {number}/{total}: {done / 1e6:.1f} MB" + (f"  {speed}" if speed else ""))

    def finished(path):
        state["files"].append(path)
        progress(30 + len(state["files"]) / total * 40)
        status(f"Downloaded clip part {len(state['files'])} of {total}")
        TerminalLogger.echo(f"Finished part {len(state['files'])}/{total}: {Path(path).name}")
        log(f"  part {len(state['files'])} of {total} ready", "ok")

    options = ydl_base_options()
    options.update({
        "format": quality,
        "merge_output_format": "mp4",
        "outtmpl": str(DOWNLOAD_DIR / "part_%(id)s_%(section_start)d_%(section_end)d.%(ext)s"),
        "download_ranges": download_range_func(None, [(float(a), float(b)) for a, b in ranges]),
        "force_keyframes_at_cuts": True,  # exact start, not the nearest earlier keyframe
        "overwrites": True,
        "progress_hooks": [hook],
        "post_hooks": [finished],
        "quiet": False, "no_warnings": False, "logger": TerminalLogger(log),
    })
    TerminalLogger.echo(f"Downloading {total} clip part(s): " + ", ".join(
        f"{format_timestamp(a)}-{format_timestamp(b)}" for a, b in ranges))
    stop_heartbeat = start_heartbeat("Cutting the clip parts (FFmpeg)", status, cancel, DOWNLOAD_DIR)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except JobCancelled:
        raise
    except Exception:
        if cancel.is_set():
            raise JobCancelled()
        raise
    finally:
        stop_heartbeat()

    video_exts = {".mp4", ".mkv", ".webm", ".mov"}
    parts = []
    for number, (begin, finish) in enumerate(ranges):
        path = None
        if len(state["files"]) == total:  # yt-dlp downloads the ranges in the order they were given
            candidate = Path(state["files"][number])
            if candidate.is_file():
                path = candidate
        if path is None:
            found = [f for f in DOWNLOAD_DIR.glob(f"part_*_{begin}_{finish}.*")
                     if f.suffix.lower() in video_exts and not f.name.endswith((".part", ".ytdl"))]
            if found:
                path = max(found, key=lambda f: f.stat().st_mtime)
        if path is None:
            raise FileNotFoundError(f"Could not find the downloaded part {format_timestamp(begin)} to {format_timestamp(finish)}.")
        parts.append({"start": float(begin), "end": float(finish), "path": path})
    return parts, info


def pick_caption_languages(info):
    subs = [k for k in (info.get("subtitles") or {}).keys() if k != "live_chat"]
    autos = list((info.get("automatic_captions") or {}).keys())
    pool = subs + [a for a in autos if a not in subs]
    ordered = []
    for wanted in ("en", "en-US", "en-GB", "en-orig", "hi", "hi-IN", "hi-orig"):
        if wanted in pool and wanted not in ordered:
            ordered.append(wanted)
    for key in subs:
        if key not in ordered:
            ordered.append(key)
    for key in autos:  # '-orig' means the video's own spoken language, not a translation
        if key.endswith("-orig") and key not in ordered:
            ordered.append(key)
    return ordered[:4]


def download_transcript(url, info, log, cancel):
    """Try official subtitles, then YouTube auto captions. Returns a Path or None."""
    languages = pick_caption_languages(info)
    if not languages:
        log("This video has no published captions.", "warn")
        return None
    video_id = info.get("id", "")
    if not video_id:
        return None

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for language in languages:
        if cancel.is_set():
            raise JobCancelled()
        options = ydl_base_options()
        options.update({
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": [language],
            "subtitlesformat": "vtt/best",
            "outtmpl": str(DOWNLOAD_DIR / "%(title).150B_%(id)s.%(ext)s"),
            "ignoreerrors": True,
        })
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([url])
        except Exception as exc:
            log(f"Captions ({language}) failed: {friendly_error(exc)}", "warn")
            continue
        found = sorted(
            list(DOWNLOAD_DIR.glob(f"*{video_id}*.{language}.vtt")) +
            list(DOWNLOAD_DIR.glob(f"*{video_id}*.{language}.srt")),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if found:
            log(f"Captions downloaded ({language}).")
            return found[0]
    return None


def probe_duration(path, cancel=None):
    code, out, err = run_process(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        cancel=cancel, timeout=60,
    )
    if code != 0:
        raise RuntimeError(f"ffprobe failed: {(err or '').strip().splitlines()[-1:] or 'unknown error'}")
    return float(out.strip())


def run_process(cmd, cancel=None, timeout=None):
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
    )
    started = time.time()
    while True:
        try:
            out, err = proc.communicate(timeout=0.4)
            return proc.returncode, out, err
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                proc.kill()
                proc.communicate()
                raise JobCancelled()
            if timeout and time.time() - started > timeout:
                proc.kill()
                proc.communicate()
                raise RuntimeError("A tool took too long and was stopped.")


def clamp01(value, default=0.5):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return min(1.0, max(0.0, number))


def make_video_filter(output_size, crop_x=0.5, crop_y=0.5):
    """Scale the video so it fills the output size, then crop the part the user chose in the editor."""
    if output_size is None:
        return None
    width, height = output_size
    cx, cy = clamp01(crop_x), clamp01(crop_y)
    return (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:(in_w-out_w)*{cx:.4f}:(in_h-out_h)*{cy:.4f},setsar=1")


SPLIT_MIN, SPLIT_MAX = 0.25, 0.75  # share of the output height the top panel may take
ZOOM_MIN = 0.25  # smallest part of the video a panel may show (1 = the biggest part that fits)
VERTICAL_DEFAULT = (1080, 1920)


def normalize_region(raw, default_x):
    """One panel: x/y = where its frame sits (0..1), z = zoom (1 = widest part, smaller = zoomed in), fit = whole video."""
    raw = raw if isinstance(raw, dict) else {}
    return {
        "x": clamp01(raw.get("x"), default_x),
        "y": clamp01(raw.get("y")),
        "z": max(ZOOM_MIN, clamp01(raw.get("z"), 1.0)),
        "fit": bool(raw.get("fit")),
    }


def normalize_split(raw):
    raw = raw if isinstance(raw, dict) else {}
    ratio = min(SPLIT_MAX, max(SPLIT_MIN, clamp01(raw.get("ratio"), 0.5)))
    return {
        "ratio": ratio,
        "top": normalize_region(raw.get("top"), 0.0),  # default: left part of the picture
        "bottom": normalize_region(raw.get("bottom"), 1.0),  # default: right part of the picture
    }


def panel_heights(height, ratio):
    """Even heights (top, bottom) that add up to the output height."""
    top = int(height * ratio / 2 + 0.5) * 2
    top = max(2, min(height - 2, top))
    return top, height - top


def make_panel_filter(region, panel_w, panel_h):
    """Filter chain that turns the source into one panel (panel_w x panel_h)."""
    if region["fit"]:  # the whole video, letter-boxed inside the panel
        return (f"scale={panel_w}:{panel_h}:force_original_aspect_ratio=decrease,"
                f"pad={panel_w}:{panel_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
    pa = round(panel_w / panel_h, 5)
    base = f"min(iw,ih*{pa})*{region['z']:.4f}"  # biggest box with the panel's shape, times the zoom
    return (f"crop=w='{base}':h='{base}/{pa}':"
            f"x='(iw-out_w)*{region['x']:.4f}':y='(ih-out_h)*{region['y']:.4f}',"
            f"scale={panel_w}:{panel_h}:flags=lanczos,setsar=1")


def make_split_filter_complex(output_size, split):
    """Two different parts of the same video, stacked: top panel over bottom panel."""
    split = normalize_split(split)
    width, height = output_size
    top_h, bottom_h = panel_heights(height, split["ratio"])
    top = make_panel_filter(split["top"], width, top_h)
    bottom = make_panel_filter(split["bottom"], width, bottom_h)
    return f"[0:v]split=2[s0][s1];[s0]{top}[t];[s1]{bottom}[b];[t][b]vstack=inputs=2[v]"


PNG_DATA_PREFIX = "data:image/png;base64,"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_TEXTS_PER_CLIP = 10


def decode_png_data_url(value):
    """The editor draws every text into a transparent PNG (so fonts, Hindi, outline and box look exactly as in the editor)."""
    if not isinstance(value, str) or not value.startswith(PNG_DATA_PREFIX):
        return None
    try:
        raw = base64.b64decode(value[len(PNG_DATA_PREFIX):], validate=True)
    except ValueError:
        return None
    if len(raw) > 8_000_000 or not raw.startswith(PNG_SIGNATURE):
        return None
    return raw


def optional_seconds(value):
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number >= 0 else None


def overlay_expression(item):
    """FFmpeg overlay filter: puts the text picture where the user placed it (kept inside the frame)."""
    x, y = clamp01(item.get("x"), 0.5), clamp01(item.get("y"), 0.85)
    text = (f"overlay=x='max(0,min(main_w-overlay_w,main_w*{x:.4f}-overlay_w/2))':"
            f"y='max(0,min(main_h-overlay_h,main_h*{y:.4f}-overlay_h/2))'")
    start, end = item.get("start"), item.get("end")
    if start is not None and end is not None:
        text += f":enable='between(t,{start:.2f},{end:.2f})'"
    elif start is not None:
        text += f":enable='gte(t,{start:.2f})'"
    elif end is not None:
        text += f":enable='lte(t,{end:.2f})'"
    return text


def cut_clip(source_path, output_path, start, end, output_size, cancel, crop_x=0.5, crop_y=0.5, split=None, texts=None):
    texts = list(texts or [])
    if texts and not output_size:
        output_size = VERTICAL_DEFAULT
    with tempfile.TemporaryDirectory(prefix="viral_clips_text_") as tmp:
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-i", str(source_path),
        ]
        for number, item in enumerate(texts):
            picture = Path(tmp) / f"text{number}.png"
            picture.write_bytes(item["png"])
            command.extend(["-i", str(picture)])
        command.extend(["-t", f"{end - start:.3f}"])
        if texts:
            if split and output_size:
                graph = make_split_filter_complex(output_size, split)
            else:
                graph = f"[0:v]{make_video_filter(output_size, crop_x, crop_y)}[v]"
            last = "v"
            for number, item in enumerate(texts):
                label = f"o{number}"
                graph += f";[{last}][{number + 1}:v]{overlay_expression(item)}[{label}]"
                last = label
            command.extend(["-filter_complex", graph, "-map", f"[{last}]", "-map", "0:a:0?"])
        elif split and output_size:
            command.extend(["-filter_complex", make_split_filter_complex(output_size, split),
                            "-map", "[v]", "-map", "0:a:0?"])
        else:
            command.extend(["-map", "0:v:0", "-map", "0:a:0?"])
            video_filter = make_video_filter(output_size, crop_x, crop_y)
            if video_filter:
                command.extend(["-vf", video_filter])
        command.extend([
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(output_path),
        ])
        code, _, err = run_process(command, cancel=cancel, timeout=1800)
    if code != 0:
        lines = (err or "").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else "FFmpeg failed")
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("FFmpeg produced an empty file")


def make_preview(source_path, cancel, progress):
    """Small H.264 copy of the video that every browser can play (used only if the original cannot be played)."""
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    source_path = Path(source_path)
    target = PREVIEW_DIR / f"{source_path.stem}_preview.mp4"
    try:
        total = probe_duration(source_path, cancel)
    except JobCancelled:
        raise
    except Exception:
        total = 0
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-y", "-i", str(source_path),
        "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=-2:'min(480,ih)'",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", "-progress", "pipe:1", str(target),
    ]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    errors = []

    def read_out():
        for line in proc.stdout:
            if line.startswith(("out_time_us=", "out_time_ms=")) and total:
                try:
                    micro = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if micro > 0:
                    progress(min(99.0, micro / 1e6 / total * 100))

    def read_err():
        for line in proc.stderr:
            errors.append(line)

    threads = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
    for thread in threads:
        thread.start()
    while True:
        try:
            proc.wait(timeout=0.4)
            break
        except subprocess.TimeoutExpired:
            if cancel.is_set():
                proc.kill()
                proc.wait()
                target.unlink(missing_ok=True)
                raise JobCancelled()
    for thread in threads:
        thread.join(timeout=2)
    if proc.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        last = "".join(errors).strip().splitlines()
        raise RuntimeError(last[-1] if last else "FFmpeg could not make the preview.")
    return target


def call_gemini(prompt, temperature=0.25):
    client = genai.Client(api_key=Config.api_key, http_options=types.HttpOptions(timeout=180_000))
    response = client.models.generate_content(
        model=Config.model,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=temperature),
    )
    try:
        text = (response.text or "").strip()
    except Exception:
        text = ""
    if not text:
        raise ValueError("Gemini returned an empty answer (it may have been blocked).")
    return text


# --------------------------------------------------------------------------- #
# Post copy: title, caption and hashtags for Instagram / YouTube (all clips in one go)
# --------------------------------------------------------------------------- #
_TS_LINE = re.compile(r"^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.*)$")
YT_TITLE_MAX = 100
YT_HASHTAG_MAX = 5
IG_HASHTAG_MAX = 12

SOCIAL_RULES = """Rules:
- Base everything on what is really said or shown. Do not invent facts, names, numbers or quotes.
- Write in the english language and script as the spoken words (hashtags may mix in English).
- youtube_title: at most 80 characters, a strong hook, honest (no fake clickbait), no hashtags, at most one emoji.
- youtube_description: 1 or 2 short sentences about the clip, then a short call to subscribe. No hashtags in it.
- youtube_hashtags: 4 or 5 relevant hashtags (do not include #Shorts, it is added automatically).
- instagram_caption: a hook on the first line, then 1 to 3 short lines, at most two emojis,
  ending with a question or a call to comment/follow. No hashtags in the text.
- instagram_hashtags: 8 to 12 hashtags, a mix of broad and niche ones.
- Hashtags have no spaces.
- Make the posts different from each other: do not reuse the same hook or hashtags set for every clip.
- Return ONLY valid JSON, with no Markdown fences and no commentary, in exactly this structure:"""


def transcript_excerpt(transcript_text, start, end, max_chars=4000):
    """The words spoken between start and end, taken from the '[mm:ss] text' transcript."""
    words = []
    for line in (transcript_text or "").splitlines():
        match = _TS_LINE.match(line.strip())
        if not match:
            continue
        try:
            moment = parse_time_value(match.group(1))
        except ValueError:
            continue
        if start - 1 <= moment <= end:
            words.append(match.group(2))
    return " ".join(words).strip()[:max_chars]


def _clip_block(clip, transcript_text):
    spoken = transcript_excerpt(transcript_text, clip.start, clip.end, 1500) or \
        "(No transcript text was found for this part. Use the clip title and description only.)"
    return (f"CLIP {clip.rank}\nTitle: {clip.title}\nDescription: {clip.description or '-'}\n"
            f"Length: {clip.length:.0f} seconds\nWhat is said: {spoken}")


def build_social_prompt_all(video_title, clips, transcript_text):
    clip_text = "\n\n".join(_clip_block(c, transcript_text) for c in clips)
    return f"""You are a social media manager who writes posts for short vertical videos
(YouTube Shorts and Instagram Reels). Write the post copy for EACH of the {len(clips)} clips below.

ORIGINAL VIDEO TITLE: {video_title}

{clip_text}

{SOCIAL_RULES}

{{
  "posts": [
    {{
      "rank": 1,
      "youtube_title": "",
      "youtube_description": "",
      "youtube_hashtags": ["#example"],
      "instagram_caption": "",
      "instagram_hashtags": ["#example"]
    }}
  ]
}}

Return one entry in "posts" for every clip, using the same rank number as the clip.
"""


def build_social_prompt(video_title, clip, transcript_text):
    """Prompt for ONE clip (used only to fill in a clip that the all-clips answer missed)."""
    return f"""You are a social media manager who writes posts for short vertical videos
(YouTube Shorts and Instagram Reels). Write the post copy for ONE clip.

ORIGINAL VIDEO TITLE: {video_title}

{_clip_block(clip, transcript_text)}

{SOCIAL_RULES}

{{
  "youtube_title": "",
  "youtube_description": "",
  "youtube_hashtags": ["#example"],
  "instagram_caption": "",
  "instagram_hashtags": ["#example"]
}}
"""


def extract_json_value(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    decoder = json.JSONDecoder()
    for index, ch in enumerate(text):
        if ch in "{[":
            try:
                obj, _ = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, (dict, list)):
                return obj
    raise ValueError("no JSON answer was found in the text")


def clean_hashtags(value, limit):
    if isinstance(value, str):
        value = re.split(r"[\s,]+", value)
    if not isinstance(value, (list, tuple)):
        return []
    tags, seen = [], set()
    for item in value:
        word = re.sub(r"\W+", "", str(item))
        if len(word) < 2 or word.isdigit() or word.lower() in seen:
            continue
        seen.add(word.lower())
        tags.append("#" + word)
        if len(tags) >= limit:
            break
    return tags


def normalize_social_copy(data):
    title = " ".join(str(data.get("youtube_title") or "").split())[:YT_TITLE_MAX].strip()
    if not title:
        raise ValueError("the answer has no youtube_title")
    yt_tags = [t for t in clean_hashtags(data.get("youtube_hashtags"), YT_HASHTAG_MAX)
               if t.lower() != "#shorts"][:YT_HASHTAG_MAX - 1]
    return {
        "youtube_title": title,
        "youtube_description": str(data.get("youtube_description") or "").strip(),
        "youtube_hashtags": ["#Shorts"] + yt_tags,
        "instagram_caption": str(data.get("instagram_caption") or "").strip(),
        "instagram_hashtags": clean_hashtags(data.get("instagram_hashtags"), IG_HASHTAG_MAX),
    }


def parse_social_copy(text):
    value = extract_json_value(text)
    if not isinstance(value, dict):
        raise ValueError("the answer is not a JSON object")
    return normalize_social_copy(value)


def parse_social_copy_all(text, ranks):
    """Answer with a list of posts -> {rank: copy}. Posts without a usable rank are matched by order."""
    value = extract_json_value(text)
    items = value
    if isinstance(value, dict):
        items = value.get("posts")
        if not isinstance(items, list):
            items = next((v for v in value.values() if isinstance(v, list)), None)
    if not isinstance(items, list):
        raise ValueError("no list of posts was found")
    posts = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        try:
            rank = int(item.get("rank"))
        except (TypeError, ValueError):
            rank = None
        if rank not in ranks:
            rank = ranks[index] if index < len(ranks) else None
        if rank is None or rank in posts:
            continue
        try:
            posts[rank] = normalize_social_copy(item)
        except ValueError:
            continue
    if not posts:
        raise ValueError("no usable posts were found in the answer")
    return posts


def generate_social_copy_all(video_title, clips, transcript_text, temperature=0.7):
    """ONE Gemini call writes the posts of every clip. Clips it forgot are filled in one by one."""
    ranks = [c.rank for c in clips]
    prompt = build_social_prompt_all(video_title, clips, transcript_text)
    posts, last = {}, None
    for attempt in range(2):
        try:
            posts = parse_social_copy_all(call_gemini(prompt, temperature), ranks)
            break
        except Exception as exc:
            last = exc
            if is_fatal_ai_error(exc):
                raise
            time.sleep(2)
    else:
        raise last
    for clip in clips:
        if clip.rank not in posts:
            try:
                single = build_social_prompt(video_title, clip, transcript_text)
                posts[clip.rank] = parse_social_copy(call_gemini(single, temperature))
            except Exception:
                pass
    return posts


def format_post(copy, url=None, include_link=True):
    """-> (YouTube title, YouTube description with hashtags, Instagram caption with hashtags)."""
    def join(parts):
        return "\n\n".join(p for p in parts if p)

    description = [copy.get("youtube_description", "")]
    if include_link and url:
        description.append(f"Full video: {url}")
    if copy.get("youtube_hashtags"):
        description.append(" ".join(copy["youtube_hashtags"]))
    caption = [copy.get("instagram_caption", "")]
    if copy.get("instagram_hashtags"):
        caption.append(" ".join(copy["instagram_hashtags"]))
    return copy.get("youtube_title", ""), join(description), join(caption)


def post_text(clip, yt_title, yt_desc, ig):
    return (f"CLIP #{clip.rank}: {clip.title}\n\nYOUTUBE TITLE\n{yt_title}\n\n"
            f"YOUTUBE DESCRIPTION\n{yt_desc}\n\nINSTAGRAM CAPTION\n{ig}\n")


def save_post_files(folder, items):
    """items: (clip, video_path or None, yt_title, yt_desc, ig). Writes posts.txt and a .txt next to each video."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    parts = []
    for clip, video_path, yt_title, yt_desc, ig in items:
        text = post_text(clip, yt_title, yt_desc, ig)
        parts.append(text)
        if video_path:
            try:
                Path(video_path).with_suffix(".txt").write_text(text, encoding="utf-8")
            except OSError:
                pass
    target = folder / "posts.txt"
    target.write_text(("\n" + "=" * 60 + "\n\n").join(parts), encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# Clip editor (watch the video from YouTube, set time + size for every clip)
# --------------------------------------------------------------------------- #
def clip_summary(clip):
    text = f"{format_timestamp(clip.start)} to {format_timestamp(clip.end)}, {clip.length:.0f} seconds"
    if clip.size != SIZE_DEFAULT:
        text += f", {clip.size[0]}×{clip.size[1]}" if clip.size else ", original size"
        if clip.texts:
            text += f", {len(clip.texts)} text" + ("s" if len(clip.texts) > 1 else "")
        if clip.layout == "split":
            text += ", split screen (top + bottom)"
        elif clip.size and (abs(clip.crop_x - 0.5) > 0.005 or abs(clip.crop_y - 0.5) > 0.005):
            text += f", frame at {clip.crop_x * 100:.0f}% / {clip.crop_y * 100:.0f}%"
    return text


def clip_output_size(clip, default_size):
    """Per-clip size from the editor, otherwise the 'Output shape' chosen in the sidebar."""
    size = default_size if clip.size == SIZE_DEFAULT else clip.size
    if (clip.layout == "split" or clip.texts) and not size:  # split screen and text need a real output size
        size = VERTICAL_DEFAULT
    return size


class EditorSession:
    """Everything the editor page needs to know about the current video."""

    def __init__(self, video_id, title, duration, embeddable, clips, default_size, video_path=None, parts=None,
                 chunks=None):
        self.video_id = video_id
        self.title = title
        self.duration = duration
        self.embeddable = embeddable
        self.clips = clips
        self.default_size = default_size
        self.video_path = video_path  # the downloaded video (None until it is downloaded)
        self.preview_path = None  # small copy that every browser can play (only if needed)
        self.parts = list(parts or [])  # downloaded clip parts: {"start", "end", "path"[, "preview"]}
        self.version = 1 if (video_path or self.parts) else 0  # changes whenever the file to play changes
        self.task = {"name": "", "percent": 0, "message": "", "error": ""}
        self.clip_jobs = {}  # "download this clip" jobs: id -> {status, percent, message, error, path, name}
        self.chunks = list(chunks or [])  # transcript parts that the AI has not seen / has seen (see next_chunk)
        self.next_chunk = 0  # the part the "Find clips" button works on next
        self.pending = None  # (part number, clips) found by the AI but not yet added (e.g. download failed)
        self.more = {"busy": False, "message": "", "error": ""}  # state of the AI search for clips
        self.clips_version = 0  # changes whenever clips are added
        self.lock = threading.Lock()

    @property
    def play_path(self):
        return self.preview_path or self.video_path

    def part_path(self, index):
        """The file to play for downloaded part number `index` (the small preview copy if there is one)."""
        try:
            part = self.parts[int(index)]
        except (IndexError, TypeError, ValueError):
            return None
        return part.get("preview") or part["path"]

    def set_task(self, **values):
        self.task.update(values)

    def more_info(self):
        total = len(self.chunks)
        nxt = self.next_chunk
        return {"busy": bool(self.more.get("busy")), "message": self.more.get("message", ""),
                "error": self.more.get("error", ""), "next": nxt, "total": total, "done": nxt >= total,
                "label": chunk_label(self.chunks[nxt]) if nxt < total else "", "found": len(self.clips),
                "clips_version": self.clips_version}

    def state(self):
        return {"task": dict(self.task), "local": bool(self.parts or self.play_path), "version": self.version,
                "more": self.more_info()}

    def data(self):
        items = []
        with self.lock:
            clips = list(self.clips)
        for clip in clips:
            size = self.default_size if clip.size == SIZE_DEFAULT else clip.size
            items.append({
                "rank": clip.rank, "title": clip.title, "description": clip.description,
                "start": round(clip.start, 2), "end": round(clip.end, 2),
                "width": size[0] if size else None, "height": size[1] if size else None,
                "crop_x": clip.crop_x, "crop_y": clip.crop_y,
                "layout": clip.layout, "split": normalize_split(clip.split),
            })
        return {
            "video_id": self.video_id, "title": self.title, "duration": self.duration,
            "embeddable": self.embeddable, "clips": items,
            "parts": [{"start": part["start"], "end": part["end"]} for part in self.parts],
            "local": bool(self.parts or self.play_path), "version": self.version, "more": self.more_info(),
        }


EDITOR_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="strict-origin-when-cross-origin">
<title>Clip editor</title>
<style>
:root{--bg:#16181d;--panel:#1f2229;--raised:#272b34;--line:#343945;--text:#e8e6e1;--soft:#c3c7d1;--muted:#8d93a0;
--accent:#8b9bff;--accent-h:#7686f0;--good:#5fc9a0;--bad:#ef6a6a;--warn:#e8b04a;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 "Segoe UI",system-ui,sans-serif}
header{padding:16px 24px;border-bottom:1px solid var(--line)}
h1{font-size:18px;margin:0 0 2px}
.sub{color:var(--muted);font-size:12px}
main{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(340px,1fr);gap:18px;padding:18px 24px 130px}
@media(max-width:980px){main{grid-template-columns:1fr}}
.stage{position:sticky;top:14px;align-self:start}
.player-box{position:relative;aspect-ratio:16/9;background:#000;border-radius:12px;overflow:hidden;border:1px solid var(--line)}
#player,#player iframe{position:absolute;inset:0;width:100%;height:100%;border:0}
.crop{position:absolute;pointer-events:auto;cursor:grab;touch-action:none;border:2px solid var(--accent);border-radius:3px;
box-shadow:0 0 0 9999px rgba(10,12,16,.62);display:none}
.crop.drag{cursor:grabbing}
.crop::after{content:attr(data-label);position:absolute;left:50%;top:6px;transform:translateX(-50%);font-size:11px;color:#fff;
background:rgba(0,0,0,.6);padding:2px 9px;border-radius:10px;white-space:nowrap;pointer-events:none}
#cropB{border-color:var(--warn)}
.player-box.split .crop{box-shadow:none;background:rgba(139,155,255,.16)}
.player-box.split #cropB{background:rgba(232,176,74,.18)}
#pvwrap{flex:none;width:172px}
.stagetop{display:flex;gap:12px;align-items:flex-start}
.stagetop .player-box{flex:1;min-width:0}
#pvc{display:block;height:300px;width:auto;max-width:172px;border:1px solid var(--line);border-radius:8px;background:#000;margin-top:4px}
.splitctl{margin-top:4px;padding:6px 8px;border:1px dashed var(--line);border-radius:8px}
.splitctl input[type=range]{width:110px;accent-color:var(--accent);padding:0}
.splitctl .mode{max-width:140px}
.textctl{margin-top:6px;padding:8px;border:1px dashed var(--line);border-radius:8px}
.textctl textarea{width:100%;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:7px;padding:6px 8px;font:inherit;resize:vertical}
.textctl textarea:focus{outline:none;border-color:var(--accent)}
.textctl input[type=color]{width:34px;height:28px;padding:0;border:1px solid var(--line);border-radius:6px;background:none;cursor:pointer}
.textctl input[type=range]{width:90px;accent-color:var(--accent);padding:0}
.textctl .fontin{width:150px}
.chk{display:inline-flex;gap:4px;align-items:center;cursor:pointer;color:var(--soft);font-size:13px}
.chip.on{border-color:var(--accent);color:var(--accent)}
.danger{color:var(--bad)}
#pvc{touch-action:none}
#pvc.hasText{cursor:grab}
.stage{top:12px;max-height:calc(100vh - 150px);overscroll-behavior:contain;overflow-y:auto;overflow-x:hidden;padding:0 6px 8px 0;scrollbar-width:thin;scrollbar-color:var(--line) transparent}
.stage::-webkit-scrollbar{width:8px}.stage::-webkit-scrollbar-thumb{background:var(--line);border-radius:4px}
/* wide windows: video panel stays pinned on the left (it scrolls by itself only if taller than the window), only the clip list on the right scrolls */
/* narrow windows (one column): nothing is pinned, the whole page scrolls */
@media(max-width:980px){.stage{position:static!important;max-height:none!important;overflow:visible!important}}
@media(max-width:560px){.stagetop{flex-wrap:wrap}.stagetop .player-box{flex:1 1 100%}main{padding:14px 12px 150px}header{padding:12px 14px}footer{padding:10px 12px}}
/* clip (not hidden): hidden would turn the page into a scroll box and break the pinned left panel */
body{max-width:100%;overflow-x:clip}
.scrubrow{display:flex;gap:10px;align-items:center;margin-top:10px}
.scrubrow input[type=range]{flex:1;accent-color:var(--accent);padding:0}
#player video{position:absolute;inset:0;width:100%;height:100%;background:#000;object-fit:contain}
.localbar{margin-top:10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:10px 12px;border:1px dashed var(--line);border-radius:8px;font-size:13px;color:var(--soft)}
.localbar progress{width:160px;accent-color:var(--accent)}
.under{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-top:10px;color:var(--soft);font-size:13px;flex-wrap:wrap}
.under label{display:flex;gap:6px;align-items:center;cursor:pointer}
#now{font-variant-numeric:tabular-nums}
.banner{margin-top:10px;padding:10px 12px;border-radius:8px;background:#3a2f17;color:var(--warn);font-size:13px;display:none}
.hint{margin-top:10px;color:var(--muted);font-size:12px}
#list{display:flex;flex-direction:column;gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px;cursor:pointer}
.card.active{border-color:var(--accent)}
.card.off{opacity:.5}
.chead{display:flex;gap:8px;align-items:center}
.rank{background:var(--raised);color:var(--accent);border-radius:8px;padding:2px 8px;font-weight:700;font-size:13px}
.title{flex:1;min-width:0}
input,select{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:7px;padding:6px 8px;font:inherit}
input:focus,select:focus{outline:none;border-color:var(--accent)}
input[type=checkbox]{width:18px;height:18px;accent-color:var(--accent)}
.t{width:92px;font-variant-numeric:tabular-nums}
.n{width:78px}
.bar{height:6px;background:var(--raised);border-radius:3px;margin:10px 0 8px;position:relative;overflow:hidden}
.bar i{position:absolute;top:0;bottom:0;background:var(--accent);border-radius:3px}
.row{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:6px}
.row .lab{width:54px;color:var(--soft);font-size:12px}
button{font:inherit;cursor:pointer;border:1px solid var(--line);background:var(--raised);color:var(--text);border-radius:7px;padding:5px 9px}
button:hover{background:var(--line)}
.mini{padding:3px 7px;font-size:12px}
.primary{background:var(--accent);color:#10131c;border-color:var(--accent);font-weight:700;padding:10px 22px;font-size:15px;border-radius:9px}
.primary:hover{background:var(--accent-h)}
.primary:disabled{opacity:.5;cursor:default}
.len{margin-top:8px;font-size:12px;color:var(--muted)}
.err{font-size:12px;color:var(--bad);min-height:0}
#finder{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:12px}
#finder .fh{font-weight:700;font-size:15px;margin-bottom:4px}
#finder .fh.ok{color:var(--good)}
#finder .primary{padding:8px 16px;font-size:14px}
#finder .busy{margin-top:8px;color:var(--accent);font-size:13px}
.fbtns{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
footer{position:fixed;left:0;right:0;bottom:0;background:var(--panel);border-top:1px solid var(--line);
padding:12px 24px;display:flex;align-items:center;gap:10px 16px;justify-content:space-between;flex-wrap:wrap;z-index:5}
#msg{font-size:13px;color:var(--soft)}
#msg.bad{color:var(--bad)}
#done{position:fixed;inset:0;background:rgba(16,18,24,.96);display:none;align-items:center;justify-content:center;text-align:center;padding:30px}
#done h2{margin:0 0 8px}
/* wide windows: two panes. The page itself does not scroll: the LEFT panel and the RIGHT clip list each have their own scrollbar */
@media(min-width:981px){
body{height:100vh;display:flex;flex-direction:column;overflow:hidden}
header{flex:none}
main{flex:1 1 auto;min-height:0;overflow:hidden;padding:14px 24px 12px;grid-template-rows:minmax(0,1fr);align-items:stretch}
.stage{position:static!important;top:auto;height:100%;max-height:none!important;overflow-y:auto!important;overflow-x:hidden;padding:0 8px 12px 0;overscroll-behavior:auto}
.rcol{min-height:0;height:100%;overflow-y:auto;overflow-x:hidden;padding:0 8px 16px 0;scrollbar-width:thin;scrollbar-color:var(--accent) transparent}
.rcol::-webkit-scrollbar{width:10px}.rcol::-webkit-scrollbar-thumb{background:var(--line);border-radius:5px}.rcol::-webkit-scrollbar-thumb:hover{background:var(--accent)}
footer{position:static;flex:none}
}
</style></head>
<body>
<header><h1 id="vtitle">Clip editor</h1>
<div class="sub">Watch the video straight from YouTube, set each clip's time and size, then press Generate. The video is downloaded when you press Generate.</div></header>
<main>
 <section class="stage">
  <div class="stagetop"><div class="player-box"><div id="player"></div><div id="crop" class="crop" data-k="single" data-label="✥ drag to move"></div>
   <div id="cropT" class="crop" data-k="top" data-label="TOP ✥"></div><div id="cropB" class="crop" data-k="bottom" data-label="BOTTOM ✥"></div></div>
   <div id="pvwrap" style="display:none"><div class="sub">Output <span id="pvsize"></span></div><canvas id="pvc"></canvas><div class="sub" id="pvnote"></div></div></div>
  <div class="banner" id="banner"></div>
  <div class="localbar" id="localbar">
   <span id="lbtext">Long video that will not play here, or want smoother editing?</span>
   <button id="dl">⬇ Download the video first, then edit from the file</button>
   <button id="pv" style="display:none">Make a smaller preview copy</button>
   <progress id="dlp" max="100" value="0" style="display:none"></progress><span id="dlmsg"></span>
  </div>
  <div class="scrubrow"><button id="pp">⏯ Play / Pause</button><input type="range" id="scrub" min="0" max="1000" value="0"></div>
  <div class="under">
   <span>Now: <b id="now">00:00.0</b> / <span id="total">--:--</span></span>
   <label><input type="checkbox" id="showcrop" checked> Show the crop frame of the selected clip</label>
  </div>
  <datalist id="fontlist"></datalist>
  <div class="hint">Pause on the exact frame, then press <b>Set = now</b> on a clip. Click a clip to select it and see its crop frame.
  <b>Drag the bright frame on the video to move it</b> and choose which part is kept (use the Center button to reset).
  <b>Text:</b> press “＋ Add text” on a clip, then choose font, colours and background; drag the text on the Output preview (right of the video) to place it.
  <b>Split screen:</b> set Layout to “Split: top + bottom” on a clip to show two different parts of the video in one vertical clip - drag the blue TOP box and the gold BOTTOM box.
  Width and height must be even numbers; the video is scaled to fill the frame and the part inside your frame is cut out.</div>
 </section>
 <div class="rcol"><div id="finder"></div><section id="list"></section></div>
</main>
<footer>
 <div id="msg"></div>
 <div class="fbtns"><button id="more2" style="padding:10px 16px">Find clips</button><button class="primary" id="gen">Generate clips</button></div>
</footer>
<div id="done"><div><h2>Sent to the app</h2>
<div class="sub" style="font-size:14px">Your clips are being created.<br>Go back to Viral Clips Studio to watch the progress. You can close this window.</div></div></div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const $=s=>document.querySelector(s);
const defReg=k=>({x:k==='top'?0:1,y:.5,z:1,fit:false});
const mk=c=>{const s=c.split||{};
 return {...c,enabled:true,cx:c.crop_x==null?.5:c.crop_x,cy:c.crop_y==null?.5:c.crop_y,layout:c.layout==='split'?'split':'single',texts:[],ti:0,
  ratio:s.ratio==null?.5:s.ratio,top:{...defReg('top'),...(s.top||{})},bottom:{...defReg('bottom'),...(s.bottom||{})}}};
const clips=D.clips.map(mk);
const st0=()=>clips[active]?clips[active].start:0;
let active=0,player=null,ready=false,stopAt=null,duration=D.duration||0;
let ytPlayer=null,local=false,localVersion=0,SA=16/9;
let PARTS=D.parts||[];
if(PARTS.length)document.querySelector('header .sub').textContent="Set each clip's time and size, then press Generate. Only each clip (plus 10 s before and after) was downloaded, not the whole video.";let curPart=-1,pendingSeek=null,wantPlay=false;
function partFor(t){let best=0,bd=1e9;for(let i=0;i<PARTS.length;i++){const p=PARTS[i];
 if(t>=p.start-0.01&&t<=p.end+0.01)return i;const d=Math.min(Math.abs(t-p.start),Math.abs(t-p.end));if(d<bd){bd=d;best=i}}return best}
function inParts(a,b){return PARTS.some(p=>a>=p.start-0.01&&b<=p.end+0.01)}
function scrubSpan(){if(PARTS.length&&curPart>=0){const p=PARTS[curPart];return[p.start,p.end-p.start]}return[0,duration]}
const PRESETS=[["Original size",null,null],["Vertical 9:16 (1080×1920)",1080,1920],["Horizontal 16:9 (1920×1080)",1920,1080],
["Square 1:1 (1080×1080)",1080,1080],["Portrait 4:5 (1080×1350)",1080,1350],["Custom size",0,0]];

function esc(s){return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function fmt(t){t=Math.max(0,t||0);const h=Math.floor(t/3600),m=Math.floor(t%3600/60),s=(t%60).toFixed(1).padStart(4,'0');
 return (h?String(h).padStart(2,'0')+':':'')+String(m).padStart(2,'0')+':'+s}
function parse(v){v=String(v).trim().replace(',','.');if(/^\d+(\.\d+)?$/.test(v))return parseFloat(v);
 const p=v.split(':');if(p.length<2||p.length>3||!p.every(x=>/^\d+(\.\d+)?$/.test(x)))return NaN;
 return p.map(Number).reduce((a,b)=>a*60+b,0)}
function presetIndex(c){if(!c.width&&!c.height)return 0;
 const i=PRESETS.findIndex((p,k)=>k>0&&k<PRESETS.length-1&&p[1]===c.width&&p[2]===c.height);return i<0?PRESETS.length-1:i}

function cardHTML(c,i){
 const nudge=w=>[-1,-0.1,0.1,1].map(d=>`<button class="mini" data-act="${w}" data-d="${d}">${d>0?'+':'−'}${Math.abs(d)}s</button>`).join('');
 return `<div class="card" data-i="${i}">
 <div class="chead"><input type="checkbox" class="en" checked title="Include this clip">
  <span class="rank">#${c.rank}</span><input class="title" value="${esc(c.title)}">
  <button data-act="play">▶ Preview</button> <button data-act="dlclip" title="Cut this clip with all your edits and save it now">⬇ Download this clip</button></div>
 <div class="bar"><i></i></div>
 <div class="row"><span class="lab">Start</span><input class="t" data-f="start"> <button class="mini" data-act="now" data-w="start">Set = now</button>${nudge('s')}</div>
 <div class="row"><span class="lab">End</span><input class="t" data-f="end"> <button class="mini" data-act="now" data-w="end">Set = now</button>${nudge('e')}</div>
 <div class="row"><span class="lab">Size</span><select class="preset">${PRESETS.map((p,k)=>`<option value="${k}">${p[0]}</option>`).join('')}</select>
  <input class="n" type="number" min="100" max="4096" step="2" data-f="width" placeholder="width"> ×
  <input class="n" type="number" min="100" max="4096" step="2" data-f="height" placeholder="height"></div>
 <div class="row"><span class="lab">Layout</span><select class="layout"><option value="single">One frame</option><option value="split">Split: top + bottom</option></select></div>
 <div class="row framerow"><span class="lab">Frame</span><button class="mini" data-act="center">⌖ Center</button><span class="pos"></span>
  <span class="sub">drag the frame on the video to move it</span></div>
 <div class="splitctl">
  ${['top','bottom'].map(k=>`<div class="row"><span class="lab">${k==='top'?'Top':'Bottom'}</span>
   <select class="mode" data-r="${k}"><option value="crop">Part of video</option><option value="fit">Full video (fit)</option></select>
   <span class="sub">Zoom</span><input type="range" class="zoom" data-r="${k}" min="25" max="100" step="1"><span class="sub zv" data-r="${k}"></span>
   <button class="mini" data-act="rcenter" data-r="${k}" title="Centre this frame">⌖</button></div>`).join('')}
  <div class="row"><span class="lab">Split</span><input type="range" class="ratio" min="25" max="75" step="1"><span class="sub rv"></span>
   <span class="sub">height of the top part</span></div>
  <div class="sub">Drag the blue TOP box and the gold BOTTOM box on the video to choose what each part shows.</div>
 </div>
 <div class="row"><span class="lab">Text</span><button class="mini" data-act="addtext">＋ Add text</button><span class="tchips"></span></div>
 <div class="tpanel"></div>
 <div class="len"></div><div class="err"></div></div>`}

function validate(c){
 if(!(c.end>c.start+0.5))return'the end must be after the start';
 if(duration&&c.start>=duration)return'the start is after the end of the video';
  if(PARTS.length&&!inParts(c.start,c.end)){const p=PARTS[partFor((c.start+c.end)/2)];
   return'the clip must stay inside the downloaded part ('+fmt(p.start)+' to '+fmt(p.end)+')'}
 if(!!c.width!==!!c.height)return'set both width and height (or neither)';
 if(c.width&&(c.width<100||c.width>4096||c.height<100||c.height>4096))return'width and height must be between 100 and 4096';
 if(c.layout==='split'&&!c.width)return'a split screen needs a size (pick Vertical 9:16)';
 if(c.texts.length&&!c.width)return'text needs a size (pick Vertical 9:16)';
 for(const t of c.texts){if((t.start!=null&&t.end!=null)&&t.end<=t.start)return'a text must end after it starts'}
 return''}

function sync(i){
 const c=clips[i],el=$(`.card[data-i="${i}"]`);
 const set=(sel,v)=>{const n=el.querySelector(sel);if(document.activeElement!==n)n.value=v};
 const sp=c.layout==='split';
 el.querySelector('.layout').value=c.layout;
 el.querySelector('.splitctl').style.display=sp?'':'none';
 el.querySelector('.framerow').style.display=sp?'none':'';
 if(sp){['top','bottom'].forEach(k=>{const r=c[k];
  el.querySelector(`.mode[data-r=${k}]`).value=r.fit?'fit':'crop';
  set(`.zoom[data-r=${k}]`,Math.round(r.z*100));
  el.querySelector(`.zv[data-r=${k}]`).textContent=r.fit?'':Math.round(r.z*100)+'%'});
  set('.ratio',Math.round(c.ratio*100));
  el.querySelector('.rv').textContent=Math.round(c.ratio*100)+'% / '+(100-Math.round(c.ratio*100))+'%'}
 set('[data-f=start]',fmt(c.start));set('[data-f=end]',fmt(c.end));
 set('[data-f=width]',c.width||'');set('[data-f=height]',c.height||'');
 el.querySelector('.preset').value=presetIndex(c);
 el.querySelector('.pos').textContent=c.width?`X ${Math.round(c.cx*100)}% · Y ${Math.round(c.cy*100)}%`:'(set a size first)';
 const bar=el.querySelector('.bar i'),d=duration||Math.max(c.end,1);
 bar.style.left=Math.min(100,c.start/d*100)+'%';bar.style.width=Math.max(0.6,Math.min(100-c.start/d*100,(c.end-c.start)/d*100))+'%';
 el.querySelector('.len').textContent=`Length ${Math.max(0,c.end-c.start).toFixed(1)} s`+(c.width?`  ·  output ${c.width}×${c.height}`:'  ·  original size');
 el.querySelector('.err').textContent=validate(c);
 el.classList.toggle('off',!c.enabled);
 if(i===active)overlay()}

function boxAspect(pa,z){let w=1,h=1;if(pa<SA)w=pa/SA;else h=SA/pa;return{w:w*z,h:h*z}}
function frameBox(c){return boxAspect(c.width/c.height,1)}
function panelHeights(c){let t=Math.round(c.height*c.ratio/2)*2;t=Math.max(2,Math.min(c.height-2,t));return[t,c.height-t]}
function boxFor(c,k){if(k==='single')return frameBox(c);const [th,bh]=panelHeights(c);return boxAspect(c.width/(k==='top'?th:bh),c[k].z)}
function getPos(c,k){return k==='single'?{x:c.cx,y:c.cy}:{x:c[k].x,y:c[k].y}}
function setPos(c,k,x,y){if(k==='single'){c.cx=x;c.cy=y}else{c[k].x=x;c[k].y=y}}
function overlay(){
 const c=clips[active],box=document.querySelector('.player-box');
 const els={single:$('#crop'),top:$('#cropT'),bottom:$('#cropB')};
 const show=!!(c&&$('#showcrop').checked&&c.width&&c.height);
 box.classList.toggle('split',show&&c.layout==='split');
 const keys=!show?[]:c.layout==='split'?['top','bottom']:['single'];
 for(const k in els){const el=els[k];
  if(!keys.includes(k)||(k!=='single'&&c[k].fit)){el.style.display='none';continue}
  const b=boxFor(c,k),p=getPos(c,k);
  Object.assign(el.style,{display:'block',width:b.w*100+'%',height:b.h*100+'%',left:(1-b.w)*p.x*100+'%',top:(1-b.h)*p.y*100+'%'})}
 drawPreview()}

// ---- text on the video -------------------------------------------------
function fontOk(n){const g=document.createElement('canvas').getContext('2d'),s='mmmmmmmmmmlliWWWअआक';
 return ['monospace','serif','sans-serif'].some(b=>{g.font='72px '+b;const w0=g.measureText(s).width;g.font='72px "'+n+'",'+b;return g.measureText(s).width!==w0})}
const FONTS=['Arial','Arial Black','Segoe UI','Segoe UI Black','Bahnschrift','Impact','Verdana','Tahoma','Trebuchet MS','Georgia','Times New Roman',
 'Courier New','Comic Sans MS','Calibri','Cambria','Consolas','Franklin Gothic Medium','Nirmala UI','Mangal','Kokila','Helvetica','Helvetica Neue','Roboto','Open Sans','Montserrat','Poppins'].filter(fontOk).concat(['sans-serif','serif']);
$('#fontlist').innerHTML=FONTS.map(f=>`<option value="${esc(f)}">`).join('');
const TEXT_POS=[['Top',.12],['Middle',.5],['Bottom',.85]];
function newText(n){return{text:'Your text',font:FONTS.includes('Arial')?'Arial':FONTS[0],size:72,bold:true,italic:false,color:'#ffffff',align:'center',
 strokeW:0,strokeColor:'#000000',bg:true,bgColor:'#000000',bgOpacity:.6,pad:18,radius:14,x:.5,y:[.85,.15,.5][n%3],start:null,end:null}}
function roundRectPath(g,x,y,w,h,r){r=Math.max(0,Math.min(r,w/2,h/2));g.beginPath();g.moveTo(x+r,y);g.arcTo(x+w,y,x+w,y+h,r);g.arcTo(x+w,y+h,x,y+h,r);g.arcTo(x,y+h,x,y,r);g.arcTo(x,y,x+w,y,r);g.closePath()}
function hexRGBA(h,a){const n=parseInt(h.slice(1),16)||0;return`rgba(${n>>16&255},${n>>8&255},${n&255},${a})`}
const tcache=new Map();
function renderText(t,outW){
 const key=JSON.stringify([t.text,t.font,t.size,t.bold,t.italic,t.color,t.align,t.strokeW,t.strokeColor,t.bg,t.bgColor,t.bgOpacity,t.pad,t.radius,outW]);
 if(tcache.has(key))return tcache.get(key);
 const size=Math.max(8,Math.min(600,+t.size||72)),sw=Math.max(0,+t.strokeW||0),p=t.bg?Math.max(0,+t.pad||0):0;
 const font=`${t.italic?'italic ':''}${t.bold?'700':'400'} ${size}px "${t.font}", sans-serif`;
 const m=document.createElement('canvas').getContext('2d');m.font=font;
 const maxW=Math.max(60,outW*.94-2*p-2*sw),lines=[];
 String(t.text).split('\n').forEach(par=>{let cur='';par.split(' ').forEach(w=>{const test=cur?cur+' '+w:w;
  if(cur&&m.measureText(test).width>maxW){lines.push(cur);cur=w}else cur=test});lines.push(cur)});
 const lh=Math.round(size*1.25),tw=Math.ceil(Math.max(1,...lines.map(l=>m.measureText(l).width)));
 const W=tw+2*p+2*sw+2,H=lines.length*lh+2*p+2*sw,cv=document.createElement('canvas');cv.width=W;cv.height=H;
 const g=cv.getContext('2d');g.font=font;g.textBaseline='middle';g.lineJoin='round';
 if(t.bg){roundRectPath(g,sw,sw,W-2*sw,H-2*sw,+t.radius||0);g.fillStyle=hexRGBA(t.bgColor,Math.min(1,Math.max(0,t.bgOpacity)));g.fill()}
 g.textAlign=t.align;const tx=t.align==='left'?sw+p+1:t.align==='right'?W-sw-p-1:W/2;
 lines.forEach((l,n)=>{const y=sw+p+n*lh+lh/2;
  if(sw>0){g.lineWidth=sw*2;g.strokeStyle=t.strokeColor;g.strokeText(l,tx,y)}
  g.fillStyle=t.color;g.fillText(l,tx,y)});
 if(tcache.size>60)tcache.clear();tcache.set(key,cv);return cv}
function textRect(c,t){const cv=renderText(t,c.width),w=cv.width,h=cv.height;
 return{cv,w,h,left:Math.min(Math.max(0,t.x*c.width-w/2),Math.max(0,c.width-w)),top:Math.min(Math.max(0,t.y*c.height-h/2),Math.max(0,c.height-h))}}
function exportTexts(c){return c.texts.filter(t=>String(t.text).trim()).map(t=>({png:renderText(t,c.width).toDataURL('image/png'),x:t.x,y:t.y,start:t.start,end:t.end}))}
function chipsHTML(c){return c.texts.map((t,n)=>`<button class="mini chip${n===c.ti?' on':''}" data-act="seltext" data-n="${n}">${esc(String(t.text).trim().slice(0,10)||'Text '+(n+1))}</button>`).join('')}
function textPanel(c){const t=c.texts[c.ti];if(!t)return'';
 const num=(f,min,max,step)=>`<input class="n" type="number" data-tf="${f}" min="${min}" max="${max}" step="${step}" value="${t[f]}">`;
 const chk=(f,l)=>`<label class="chk"><input type="checkbox" data-tf="${f}"${t[f]?' checked':''}> ${l}</label>`;
 const opt=(v,l)=>`<option value="${v}"${t.align===v?' selected':''}>${l}</option>`;
 return`<div class="textctl">
 <textarea data-tf="text" rows="2" placeholder="Type your text here (Hindi and emoji work too)">${esc(t.text)}</textarea>
 <div class="row"><span class="lab">Font</span><input class="fontin" list="fontlist" data-tf="font" value="${esc(t.font)}" title="Pick from the list or type any font installed on this computer"> Size ${num('size',8,600,2)} ${chk('bold','Bold')} ${chk('italic','Italic')}</div>
 <div class="row"><span class="lab">Color</span><input type="color" data-tf="color" value="${t.color}"> Align <select data-tf="align">${opt('left','Left')}${opt('center','Center')}${opt('right','Right')}</select>
  Outline ${num('strokeW',0,30,1)} <input type="color" data-tf="strokeColor" value="${t.strokeColor}"></div>
 <div class="row"><span class="lab">Back</span>${chk('bg','Background box')} <input type="color" data-tf="bgColor" value="${t.bgColor}">
  Opacity <input type="range" data-tf="bgOpacity" data-div="100" min="0" max="100" value="${Math.round(t.bgOpacity*100)}"></div>
 <div class="row"><span class="lab"></span>Padding ${num('pad',0,200,2)} Round ${num('radius',0,200,2)}</div>
 <div class="row"><span class="lab">Position</span>X <input class="tx" type="range" data-tf="x" data-div="100" min="0" max="100" value="${Math.round(t.x*100)}">
  Y <input class="ty" type="range" data-tf="y" data-div="100" min="0" max="100" value="${Math.round(t.y*100)}">
  ${TEXT_POS.map(p=>`<button class="mini" data-act="tpos" data-y="${p[1]}">${p[0]}</button>`).join('')}</div>
 <div class="row"><span class="lab">Show</span>from <input class="n" type="number" data-tf="start" min="0" step="0.1" placeholder="0" value="${t.start==null?'':t.start}">
  to <input class="n" type="number" data-tf="end" min="0" step="0.1" placeholder="end" value="${t.end==null?'':t.end}"> sec
  <button class="mini danger" data-act="deltext">Delete this text</button></div>
 <div class="sub">Drag the text on the “Output preview” to move it. Leave “from / to” empty to show it for the whole clip.</div></div>`}
function refreshText(i){const c=clips[i],el=$(`.card[data-i="${i}"]`);if(!el)return;
 el.querySelector('.tchips').innerHTML=chipsHTML(c);el.querySelector('.tpanel').innerHTML=textPanel(c)}
function syncTextPos(i){const c=clips[i],t=c.texts[c.ti],el=$(`.card[data-i="${i}"]`);if(!t||!el)return;
 const a=el.querySelector('.tx'),b=el.querySelector('.ty');if(a)a.value=Math.round(t.x*100);if(b)b.value=Math.round(t.y*100)}
function textInput(c,i,t){const tx=c.texts[c.ti];if(!tx)return;const f=t.dataset.tf;let v;
 if(t.type==='checkbox')v=t.checked;
 else if(t.type==='number'||t.type==='range'){
  if(t.value===''){if(f==='start'||f==='end')v=null;else return}
  else{v=parseFloat(t.value);if(isNaN(v))return;if(t.dataset.div)v/=+t.dataset.div}}
 else v=t.value;
 tx[f]=v;
 if(f==='text'){const ch=$(`.card[data-i="${i}"] .tchips`);if(ch)ch.innerHTML=chipsHTML(c)}
 sync(i)}
(function(){const cv=$('#pvc');let drag=null;
 const pt=e=>{const r=cv.getBoundingClientRect();return{x:(e.clientX-r.left)*cv.width/r.width,y:(e.clientY-r.top)*cv.height/r.height}};
 cv.addEventListener('pointerdown',e=>{const c=clips[active];if(!c||!c.texts.length||!c._rects)return;const q=pt(e);
  for(let n=c.texts.length-1;n>=0;n--){const r=c._rects[n];
   if(r&&q.x>=r.x&&q.x<=r.x+r.w&&q.y>=r.y&&q.y<=r.y+r.h){
    drag={n,dx:q.x-(r.x+r.w/2),dy:q.y-(r.y+r.h/2),k:cv.height/c.height};
    if(c.ti!==n){c.ti=n;refreshText(active)}
    cv.setPointerCapture(e.pointerId);e.preventDefault();return}}});
 cv.addEventListener('pointermove',e=>{if(!drag)return;const c=clips[active],t=c.texts[drag.n],q=pt(e);
  t.x=clamp01((q.x-drag.dx)/(c.width*drag.k));t.y=clamp01((q.y-drag.dy)/(c.height*drag.k));syncTextPos(active);drawPreview()});
 const end=()=>{drag=null};cv.addEventListener('pointerup',end);cv.addEventListener('pointercancel',end)})();

function panelsOf(c){
 if(c.layout==='split'){const [th,bh]=panelHeights(c);return[{k:'top',y:0,h:th,r:c.top},{k:'bottom',y:th,h:bh,r:c.bottom}]}
 return[{k:'single',y:0,h:c.height,r:{x:c.cx,y:c.cy,z:1,fit:false}}]}
function drawPreview(){
 const wrap=$('#pvwrap'),c=clips[active];
 if(!c||!c.width||!c.height){wrap.style.display='none';return}
 wrap.style.display='';
 const cv=$('#pvc'),H=300,W=Math.max(40,Math.round(H*c.width/c.height));
 if(cv.width!==W||cv.height!==H){cv.width=W;cv.height=H}
 const ctx=cv.getContext('2d'),k=H/c.height,v=local?document.querySelector('#player video'):null;
 const ok=!!(v&&v.videoWidth&&v.readyState>=2);
 ctx.fillStyle='#000';ctx.fillRect(0,0,W,H);
 panelsOf(c).forEach(p=>{const py=p.y*k,ph=p.h*k;
  if(ok){const vw=v.videoWidth,vh=v.videoHeight;
   if(p.r.fit){const s=Math.min(W/vw,ph/vh),dw=vw*s,dh=vh*s;ctx.drawImage(v,(W-dw)/2,py+(ph-dh)/2,dw,dh)}
   else{const b=boxAspect(c.width/p.h,p.r.z);ctx.drawImage(v,(1-b.w)*p.r.x*vw,(1-b.h)*p.r.y*vh,b.w*vw,b.h*vh,0,py,W,ph)}}
  else{ctx.fillStyle=p.k==='bottom'?'#3a2f17':'#232845';ctx.fillRect(0,py,W,ph);
   ctx.fillStyle='#c3c7d1';ctx.font='13px sans-serif';ctx.textAlign='center';
   ctx.fillText(p.k==='single'?'clip':p.k.toUpperCase(),W/2,py+ph/2)}});
 if(c.layout==='split'){const y=panelHeights(c)[0]*k;ctx.fillStyle='#8b9bff';ctx.fillRect(0,y-1,W,2)}
 ctx.imageSmoothingQuality='high';c._rects=[];
 c.texts.forEach((t,n)=>{if(!String(t.text).trim()){c._rects[n]=null;return}
  const r=textRect(c,t);ctx.drawImage(r.cv,r.left*k,r.top*k,r.w*k,r.h*k);c._rects[n]={x:r.left*k,y:r.top*k,w:r.w*k,h:r.h*k};
  if(n===c.ti){ctx.save();ctx.strokeStyle='#8b9bff';ctx.setLineDash([4,3]);ctx.lineWidth=1;ctx.strokeRect(r.left*k-1.5,r.top*k-1.5,r.w*k+3,r.h*k+3);ctx.restore()}});
 cv.classList.toggle('hasText',c.texts.length>0);
 $('#pvsize').textContent=`(${c.width}×${c.height})`;
 $('#pvnote').textContent=ok?'':'Live picture: press “Download the video first”.'}
setInterval(drawPreview,70);
const clamp01=v=>Math.min(1,Math.max(0,v));

function select(i){active=i;document.querySelectorAll('.card').forEach(n=>n.classList.toggle('active',+n.dataset.i===i));overlay()}
function seek(t){if(ready){player.seekTo(Math.max(0,t),true);player.pauseVideo();stopAt=null}}
function setTime(i,w,v){const c=clips[i];if(isNaN(v))return;v=Math.max(0,duration?Math.min(v,duration):v);
 if(PARTS.length){const p=PARTS[partFor((c.start+c.end)/2)];v=Math.max(p.start,Math.min(v,p.end))}c[w]=Math.round(v*10)/10;sync(i);seek(w==='start'?c.start:c.end-0.3)}

$('#list').innerHTML=clips.map(cardHTML).join('');
clips.forEach((_,i)=>{sync(i);refreshText(i)});
$('#vtitle').textContent=D.title||'Clip editor';
select(0);

$('#list').addEventListener('click',e=>{
 const card=e.target.closest('.card');if(!card)return;const i=+card.dataset.i,c=clips[i];
 if(!e.target.closest('input[type=checkbox]'))select(i);
 const b=e.target.closest('button');if(!b)return;const a=b.dataset.act;
 if(a==='play'&&ready){player.seekTo(c.start,true);player.playVideo();stopAt=c.end}
 else if(a==='now'&&ready)setTime(i,b.dataset.w,player.getCurrentTime());
 else if(a==='s')setTime(i,'start',c.start+ +b.dataset.d);
 else if(a==='e')setTime(i,'end',c.end+ +b.dataset.d)
 else if(a==='dlclip')downloadClip(i,b)
 else if(a==='center'){c.cx=.5;c.cy=.5;sync(i)}
 else if(a==='rcenter'){const r=c[b.dataset.r];r.x=.5;r.y=.5;sync(i)}
 else if(a==='addtext'){if(c.texts.length>=10)return msg('Up to 10 texts per clip.',true);
  c.texts.push(newText(c.texts.length));c.ti=c.texts.length-1;if(!c.width){c.width=1080;c.height=1920}refreshText(i);sync(i)}
 else if(a==='seltext'){c.ti=+b.dataset.n;refreshText(i);drawPreview()}
 else if(a==='deltext'){c.texts.splice(c.ti,1);c.ti=Math.max(0,Math.min(c.ti,c.texts.length-1));refreshText(i);sync(i)}
 else if(a==='tpos'){const t=c.texts[c.ti];if(t){t.x=.5;t.y=+b.dataset.y;syncTextPos(i);drawPreview()}}});
$('#list').addEventListener('change',e=>{
 const card=e.target.closest('.card');if(!card)return;const i=+card.dataset.i,c=clips[i],t=e.target;
 if(t.classList.contains('en')){c.enabled=t.checked;sync(i)}
 else if(t.classList.contains('layout')){c.layout=t.value;if(c.layout==='split'&&!c.width){c.width=1080;c.height=1920}sync(i)}
 else if(t.classList.contains('mode')){c[t.dataset.r].fit=t.value==='fit';sync(i)}
 else if(t.classList.contains('title'))c.title=t.value;
 else if(t.classList.contains('preset')){const p=PRESETS[+t.value];if(+t.value<PRESETS.length-1){c.width=p[1];c.height=p[2]}else{c.width=c.width||1080;c.height=c.height||1920}sync(i)}
 else if(t.dataset.f==='start'||t.dataset.f==='end'){const v=parse(t.value);if(isNaN(v)){sync(i);$('#msg').textContent='Time format: 90, 1:30 or 1:02:03.5';return}setTime(i,t.dataset.f,v)}
 else if(t.dataset.f==='width'||t.dataset.f==='height'){const v=parseInt(t.value,10);c[t.dataset.f]=isNaN(v)?null:v;
  if(!c.width&&!c.height){c.width=c.height=null}sync(i)}});
$('#list').addEventListener('input',e=>{const t=e.target;
 const card=t.closest('.card');if(!card)return;const i=+card.dataset.i,c=clips[i];
 if(t.dataset.tf){textInput(c,i,t);return}
 if(t.type!=='range')return;
 if(t.classList.contains('zoom'))c[t.dataset.r].z=t.value/100;
 else if(t.classList.contains('ratio'))c.ratio=t.value/100;
 sync(i)});
$('#showcrop').addEventListener('change',overlay);

(function(){
 const box=document.querySelector('.player-box');let drag=null;
 document.querySelectorAll('.crop').forEach(el=>{const key=el.dataset.k;
  el.addEventListener('pointerdown',e=>{const c=clips[active];if(!c||!c.width)return;const p=getPos(c,key);
   drag={x:e.clientX,y:e.clientY,px:p.x,py:p.y,r:box.getBoundingClientRect(),b:boxFor(c,key)};
   el.setPointerCapture(e.pointerId);el.classList.add('drag');e.preventDefault()});
  el.addEventListener('pointermove',e=>{if(!drag)return;const c=clips[active];let {x,y}=getPos(c,key);
   if(drag.b.w<.999)x=clamp01(drag.px+(e.clientX-drag.x)/(drag.r.width*(1-drag.b.w)));
   if(drag.b.h<.999)y=clamp01(drag.py+(e.clientY-drag.y)/(drag.r.height*(1-drag.b.h)));
   setPos(c,key,x,y);sync(active)});
  const end=()=>{drag=null;el.classList.remove('drag')};
  el.addEventListener('pointerup',end);el.addEventListener('pointercancel',end)});
})();
let scrubbing=false;const scrub=$('#scrub');
$('#pp').addEventListener('click',()=>{if(!ready)return;if(player.getPlayerState()===1)player.pauseVideo();else player.playVideo()});
scrub.addEventListener('input',()=>{scrubbing=true;const[b,l]=scrubSpan();if(ready&&l){player.seekTo(b+scrub.value/1000*l,true);stopAt=null}});
scrub.addEventListener('change',()=>{scrubbing=false});

function banner(text){const b=$('#banner');b.textContent=text;b.style.display='block'}
if(D.embeddable===false)banner('YouTube does not allow this video to be played outside youtube.com, so the preview may stay black. Press “Download the video first” below to edit from the file, or type the times by hand.');
window.onYouTubeIframeAPIReady=()=>{ytPlayer=player=new YT.Player('player',{videoId:D.video_id,host:'https://www.youtube.com',
 playerVars:{rel:0,playsinline:1,modestbranding:1,origin:location.origin},
 events:{onReady:()=>{ready=true;duration=player.getDuration()||duration;$('#total').textContent=fmt(duration);clips.forEach((_,i)=>sync(i));seek(st0())},
 onError:()=>banner('The video could not be played from YouTube here. Press “Download the video first” below to edit from the file, or type the times by hand.')}})};
if(!D.local){const s=document.createElement('script');s.src='https://www.youtube.com/iframe_api';document.head.appendChild(s);
 setTimeout(()=>{if(!ready&&!local)banner('The YouTube preview is slow or not loading. Press “Download the video first” below to edit from the file instead.')},15000)}
setInterval(()=>{if(!ready)return;const t=player.getCurrentTime();$('#now').textContent=fmt(t);
 const[sb,sl]=scrubSpan();if(!scrubbing&&sl)scrub.value=Math.round(Math.max(0,Math.min(1,(t-sb)/sl))*1000);
 if(stopAt!==null&&t>=stopAt){player.pauseVideo();stopAt=null}},120);

function destroyYT(){if(ytPlayer){try{ytPlayer.destroy()}catch(e){}ytPlayer=null}}
function startLocal(){
 destroyYT();local=true;ready=false;player=null;
 const box=document.querySelector('.player-box'),old=$('#player');if(old)old.remove();
 const holder=document.createElement('div');holder.id='player';box.insertBefore(holder,box.firstChild);
 const v=document.createElement('video');v.controls=true;v.playsInline=true;v.preload='auto';holder.appendChild(v);
 $('#banner').style.display='none';
 v.addEventListener('loadedmetadata',()=>{
  if(v.videoWidth&&v.videoHeight){SA=v.videoWidth/v.videoHeight;box.style.aspectRatio=v.videoWidth+' / '+v.videoHeight;
   box.style.maxWidth='calc(78vh * '+SA+')';box.style.margin='0 auto'}
  if(PARTS.length){const first=!player;
   if(first)player={
    seekTo:t=>{const i=partFor(t),rel=Math.max(0,t-PARTS[i].start);
     if(i!==curPart){ready=false;pendingSeek=rel;curPart=i;v.src='/video'+location.search+'&part='+i+'&v='+Date.now()}else v.currentTime=rel},
    playVideo:()=>{if(ready)v.play();else wantPlay=true},
    pauseVideo:()=>{wantPlay=false;v.pause()},
    getCurrentTime:()=>(PARTS[curPart]?PARTS[curPart].start:0)+v.currentTime,
    getDuration:()=>duration,getPlayerState:()=>v.paused?2:1};
   ready=true;
   if(pendingSeek!==null){v.currentTime=pendingSeek;pendingSeek=null;if(wantPlay){wantPlay=false;v.play()}}
   if(first){duration=D.duration||duration;$('#total').textContent=fmt(duration);clips.forEach((_,i)=>sync(i));seek(st0())}
   return}
  player={seekTo:t=>{v.currentTime=t},playVideo:()=>v.play(),pauseVideo:()=>v.pause(),getCurrentTime:()=>v.currentTime,
   getDuration:()=>v.duration,getPlayerState:()=>v.paused?2:1};
  ready=true;duration=v.duration||duration;$('#total').textContent=fmt(duration);clips.forEach((_,i)=>sync(i));seek(st0())});
 v.addEventListener('error',()=>{banner('Your browser cannot play the downloaded file. Press “Make a smaller preview copy” (it can take a few minutes for a long video).');
  $('#pv').style.display='';$('#pv').disabled=false;$('#pv').textContent=PARTS.length?'Make smaller preview copies':'Make a smaller preview copy'});
 if(PARTS.length){curPart=partFor(st0());v.src='/video'+location.search+'&part='+curPart+'&v='+Date.now()}
 else v.src='/video'+location.search+'&v='+Date.now();
 $('#dl').style.display='none';
 $('#lbtext').textContent=PARTS.length?'Editing from the downloaded clip parts ✓  Each clip has 10 s extra before and after - move the start/end anywhere inside it.'
  :'Editing from the downloaded video file ✓  You can jump anywhere, even in a very long video.'}
function post(action){return fetch('/action'+location.search,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})}).then(r=>r.json())}
async function pollState(){
 try{const r=await fetch('/state'+location.search);const s=await r.json();const t=s.task||{},p=$('#dlp');
  if(t.name){p.style.display='';p.value=t.percent||0;$('#dlmsg').textContent=(t.message||'')+(t.percent?' '+Math.round(t.percent)+'%':'');setTimeout(pollState,900);return}
  p.style.display='none';
  if(t.error){$('#dlmsg').textContent=t.error;$('#dl').disabled=false;$('#pv').disabled=false;return}
  $('#dlmsg').textContent='';
  if(s.local&&s.version!==localVersion){localVersion=s.version;startLocal()}
 }catch(e){setTimeout(pollState,1500)}}
async function runAction(action,btn){btn.disabled=true;
 try{const j=await post(action);if(!j.ok){btn.disabled=false;return msg(j.error||'Could not start.',true)}pollState()}
 catch(e){btn.disabled=false;msg(e.message,true)}}
$('#dl').addEventListener('click',()=>runAction('download',$('#dl')));
$('#pv').addEventListener('click',()=>runAction('preview',$('#pv')));
if(D.local){localVersion=D.version;startLocal()}

function msg(t,bad){const m=$('#msg');m.textContent=t;m.className=bad?'bad':''}
function clipPayload(c){return {rank:c.rank,title:c.title,description:c.description,start:c.start,end:c.end,width:c.width||null,height:c.height||null,
 crop_x:c.cx,crop_y:c.cy,layout:c.layout,split:{ratio:c.ratio,top:c.top,bottom:c.bottom},texts:exportTexts(c)}}
async function downloadClip(i,btn){
 const c=clips[i],e=validate(c);if(e)return msg(`Clip #${c.rank}: ${e}.`,true);
 const label=btn.textContent;btn.disabled=true;msg(`Clip #${c.rank}: starting…`);
 try{
  const r=await fetch('/clip'+location.search,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({clips:[clipPayload(c)]})});
  const j=await r.json();if(!j.ok)throw new Error(j.error||'The app could not start this clip.');
  for(;;){
   await new Promise(res=>setTimeout(res,800));
   const st=await (await fetch('/clipstate'+location.search+'&id='+j.id)).json();
   if(st.error){throw new Error(st.error)}
   if(st.status==='done'){
    const a=document.createElement('a');a.href='/file'+location.search+'&id='+j.id;a.download=st.name||'clip.mp4';document.body.appendChild(a);a.click();a.remove();
    msg(`Clip #${c.rank} saved: ${st.path}`);break}
   btn.textContent='⏳ '+Math.round(st.percent||0)+'%';msg(`Clip #${c.rank}: ${st.message||'working…'}`)}
 }catch(err){msg(err.message,true)}
 btn.disabled=false;btn.textContent=label}
$('#gen').addEventListener('click',async()=>{
 if(MORE.busy)return msg('The AI is still looking for clips. Please wait.',true);
 if(!clips.length)return msg('No clips yet. Press “Find clips” first.',true);
 if(!MORE.done&&!confirm(`Only part ${MORE.next} of ${MORE.total} of the transcript has been analysed.\n\nGenerate the clips you have now? (This closes the editor.)`))return;
 const out=[];let bad='';
 clips.forEach(c=>{if(!c.enabled)return;const e=validate(c);if(e&&!bad)bad=`Clip #${c.rank}: ${e}.`;
  out.push({rank:c.rank,title:c.title,description:c.description,start:c.start,end:c.end,width:c.width||null,height:c.height||null,crop_x:c.cx,crop_y:c.cy,
  layout:c.layout,split:{ratio:c.ratio,top:c.top,bottom:c.bottom},texts:exportTexts(c)})});
 if(bad)return msg(bad,true);
 if(!out.length)return msg('Tick at least one clip.',true);
 const btn=$('#gen');btn.disabled=true;msg('Sending to the app…');
 try{const r=await fetch('/generate'+location.search,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({clips:out})});
  const j=await r.json();if(!j.ok)throw new Error(j.error||'The app rejected the clips.');
  if(ready)player.pauseVideo();$('#done').style.display='flex'}
 catch(err){btn.disabled=false;msg(err.message,true)}});

// ---- find clips part by part ----------------------------------------------------------------
let MORE=D.more||{busy:false,next:0,total:0,done:true,label:'',found:0,clips_version:0,error:'',message:''};
let clipsVer=MORE.clips_version||0,moreTimer=null;
function findLabel(){return MORE.next===0?`Find clips in part 1 (${MORE.label})`:`Find more clips from part ${MORE.next+1} (${MORE.label})`}
function renderFinder(){
 const m=MORE,fin=$('#finder'),b2=$('#more2');let h='';
 if(m.done&&m.total){h=`<div class="fh ok">✓ Transcript done</div><div class="sub">All ${m.total} part${m.total>1?'s':''} of the transcript have been analysed and ${clips.length} clip${clips.length===1?'':'s'} ${clips.length===1?'was':'were'} found. Edit them below, then press “Generate clips”.</div>`}
 else if(m.total){
  h=`<div class="fh">${m.next===0?'Find clips with the AI':'Find more clips'}</div>
  <div class="sub">Long transcripts are sent to the AI in parts. Part ${m.next+1} of ${m.total} covers ${esc(m.label)}.${m.next?` ${clips.length} clips found so far.`:''} You may end up with more than 5 clips.</div>
  <div class="row"><button class="primary" data-find="1"${m.busy?' disabled':''}>${esc(findLabel())}</button></div>`}
 else h=`<div class="sub">This video has no transcript parts to analyse.</div>`;
 if(m.busy)h+=`<div class="busy">⏳ ${esc(m.message||'Working…')}</div>`;
 if(m.error)h+=`<div class="err" style="margin-top:6px">${esc(m.error)}</div>`;
 fin.innerHTML=h;
 b2.style.display=(m.done||!m.total)?'none':'';b2.disabled=!!m.busy;b2.textContent=m.busy?'Finding clips…':findLabel();
 $('#gen').disabled=!!m.busy}
async function refreshClips(){
 const d=await (await fetch('/clips'+location.search)).json();
 PARTS=d.parts||[];
 const first=clips.length,add=(d.clips||[]).slice(first);
 if(add.length){
  add.forEach(c=>clips.push(mk(c)));
  $('#list').insertAdjacentHTML('beforeend',add.map((c,k)=>cardHTML(clips[first+k],first+k)).join(''));
  for(let i=first;i<clips.length;i++){sync(i);refreshText(i)}
  select(first);if(ready)seek(st0());
  const el=document.querySelector(`.card[data-i="${first}"]`);if(el)el.scrollIntoView({behavior:'smooth',block:'start'});
  msg(`${add.length} new clip${add.length===1?'':'s'} added - ${clips.length} in total.`)}
 clipsVer=d.more.clips_version;MORE=d.more}
async function pollMore(){
 clearTimeout(moreTimer);
 try{
  const s=await (await fetch('/state'+location.search)).json();
  const wasBusy=MORE.busy;MORE=s.more||MORE;
  if(MORE.clips_version!==clipsVer)await refreshClips();
  if(s.local&&s.version!==localVersion){localVersion=s.version;startLocal()}
  renderFinder();
  if(MORE.busy)moreTimer=setTimeout(pollMore,1000);
  else if(wasBusy&&MORE.error)msg(MORE.error,true);
 }catch(e){moreTimer=setTimeout(pollMore,1500)}}
async function findMore(){
 if(MORE.busy)return;
 MORE.busy=true;MORE.message='Starting…';MORE.error='';renderFinder();
 try{const j=await post('find');
  if(!j.ok){MORE.busy=false;MORE.error=j.error||'Could not start.';renderFinder();return}
  pollMore()}
 catch(e){MORE.busy=false;MORE.error=e.message;renderFinder()}}
$('#finder').addEventListener('click',e=>{if(e.target.closest('[data-find]'))findMore()});
$('#more2').addEventListener('click',findMore);
renderFinder();
msg(clips.length?`${clips.length} clips ready to edit.`:'Press “Find clips” to let the AI pick the best moments.');
if(MORE.busy)pollMore();
</script></body></html>
"""


class EditorServer:
    """Tiny local web server (127.0.0.1 only, random port, secret token) that serves the editor page."""

    def __init__(self, session, on_generate, on_action, on_clip=None):
        self.session = session
        self.on_generate = on_generate
        self.on_action = on_action
        self.on_clip = on_clip
        self.token = secrets.token_urlsafe(16)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body, ctype):
                raw = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def _authorised(self, parsed):
                return parse_qs(parsed.query).get("t", [""])[0] == outer.token

            def _send_video(self, parsed):
                if outer.session.parts:  # clip-parts mode: /video?part=N
                    path = outer.session.part_path(parse_qs(parsed.query).get("part", ["0"])[0])
                else:
                    path = outer.session.play_path
                try:
                    size = Path(path).stat().st_size
                except (TypeError, OSError):
                    size = 0
                if not size:
                    self._send(404, "No video", "text/plain; charset=utf-8")
                    return
                start, end, code = 0, size - 1, 200
                match = re.match(r"bytes=(\d*)-(\d*)$", (self.headers.get("Range") or "").strip())
                if match and (match.group(1) or match.group(2)):
                    first, last = match.groups()
                    if first == "":
                        start = max(0, size - int(last))
                    else:
                        start = int(first)
                        if last:
                            end = min(int(last), size - 1)
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    code = 206
                self.send_response(code)
                self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Cache-Control", "no-store")
                if code == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                try:
                    with open(path, "rb") as handle:
                        handle.seek(start)
                        left = end - start + 1
                        while left > 0:
                            chunk = handle.read(min(1 << 20, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                except OSError:  # the browser closed the connection (seeking) - normal
                    pass

            def do_GET(self):
                parsed = urlparse(self.path)
                if not self._authorised(parsed):
                    self._send(404, "Not found", "text/plain; charset=utf-8")
                elif parsed.path == "/":
                    data = json.dumps(outer.session.data(), ensure_ascii=False).replace("<", "\\u003c")
                    self._send(200, EDITOR_HTML.replace("__DATA__", data), "text/html; charset=utf-8")
                elif parsed.path == "/state":
                    self._send(200, json.dumps(outer.session.state()), "application/json; charset=utf-8")
                elif parsed.path == "/clips":  # the full list again (the page asks for it when clips were added)
                    self._send(200, json.dumps(outer.session.data(), ensure_ascii=False), "application/json; charset=utf-8")
                elif parsed.path == "/video":
                    self._send_video(parsed)
                elif parsed.path == "/clipstate":
                    job = outer.session.clip_jobs.get(parse_qs(parsed.query).get("id", [""])[0])
                    self._send(200 if job else 404, json.dumps(job or {"error": "Unknown clip."}),
                               "application/json; charset=utf-8")
                elif parsed.path == "/file":
                    job = outer.session.clip_jobs.get(parse_qs(parsed.query).get("id", [""])[0])
                    path = Path(job["path"]) if job and job.get("path") else None
                    if path is None or not path.is_file():
                        self._send(404, "Not found", "text/plain; charset=utf-8")
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", str(path.stat().st_size))
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(path.name))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    try:
                        with open(path, "rb") as handle:
                            shutil.copyfileobj(handle, self.wfile, 1 << 20)
                    except OSError:
                        pass
                else:
                    self._send(404, "Not found", "text/plain; charset=utf-8")

            def do_POST(self):
                parsed = urlparse(self.path)
                if parsed.path not in ("/generate", "/action", "/clip") or not self._authorised(parsed):
                    self._send(404, "Not found", "text/plain; charset=utf-8")
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length <= 0 or length > 60_000_000:  # text pictures can be a few MB
                        raise ValueError("Bad request size.")
                    data = json.loads(self.rfile.read(length).decode("utf-8"))
                    extra = {}
                    if parsed.path == "/generate":
                        outer.on_generate(data)
                    elif parsed.path == "/clip":
                        extra = outer.on_clip(data) if outer.on_clip else {}
                    else:
                        outer.on_action(str(data.get("action", "")) if isinstance(data, dict) else "")
                    reply = {"ok": True, **extra}
                    code = 200
                except (ValueError, json.JSONDecodeError) as exc:
                    reply, code = {"ok": False, "error": str(exc)}, 400
                except Exception as exc:  # never let the page hang on a server error
                    reply, code = {"ok": False, "error": f"Unexpected error: {exc}"}, 500
                self._send(code, json.dumps(reply), "application/json; charset=utf-8")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/?t={self.token}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass


def open_editor_window(url):
    """Own app window if 'pywebview' is installed, otherwise the default browser."""
    if importlib.util.find_spec("webview") is not None and not getattr(sys, "frozen", False):
        try:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--editor-window", url])
            return
        except OSError:
            pass
    webbrowser.open(url)


def run_editor_window(url):
    import webview  # pip install pywebview
    webview.create_window("Clip editor", url, width=1320, height=880)
    webview.start()


# --------------------------------------------------------------------------- #
# UI building blocks
# --------------------------------------------------------------------------- #
def font(size=13, weight="normal", mono=False):
    family = MONO_FONT if mono else UI_FONT
    return ctk.CTkFont(family=family, size=size, weight=weight)


def set_textbox(box, text):
    box.configure(state="normal")
    box.delete("1.0", "end")
    box.insert("1.0", text)
    box.configure(state="disabled")


def primary_button(parent, text, command, **kw):
    return ctk.CTkButton(
        parent, text=text, command=command, fg_color=ACCENT, hover_color=ACCENT_HOVER,
        text_color=ACCENT_TEXT, font=font(13, "bold"), height=38, corner_radius=8, **kw)


def quiet_button(parent, text, command, **kw):
    kw.setdefault("height", 34)
    return ctk.CTkButton(
        parent, text=text, command=command, fg_color=RAISED, hover_color=LINE,
        text_color=TEXT, font=font(13), corner_radius=8, border_width=1, border_color=LINE, **kw)


def option_menu(parent, values, variable):
    return ctk.CTkOptionMenu(
        parent, values=values, variable=variable, dynamic_resizing=False, height=34,
        fg_color=RAISED, button_color=LINE, button_hover_color=MUTED,
        text_color=TEXT, dropdown_fg_color=RAISED, dropdown_hover_color=LINE,
        dropdown_text_color=TEXT, font=font(13), dropdown_font=font(13), corner_radius=8)


def field_label(parent, text):
    return ctk.CTkLabel(parent, text=text, anchor="w", text_color=SOFT, font=font(12))


def attach_flow(frame, get_widgets, all_widgets, gap=8):
    """Lay widgets out left to right and wrap them onto new rows when the frame is too narrow.
    Returns relayout(): call it after the list of visible widgets changes."""
    state = {"width": -1}

    def layout(_event=None):
        width = frame.winfo_width()
        if width < 40 or width == state["width"]:
            return
        state["width"] = width
        for widget in all_widgets:
            widget.grid_forget()
        x = row = col = 0
        for widget in get_widgets():
            need = widget.winfo_reqwidth() + gap
            if col and x + need > width:
                row, col, x = row + 1, 0, 0
            widget.grid(row=row, column=col, padx=(0, gap), pady=(0, 6), sticky="w")
            x += need
            col += 1

    def relayout():
        state["width"] = -1
        layout()

    frame.bind("<Configure>", layout, add="+")
    return relayout


class ModalBase(ctk.CTkToplevel):
    def __init__(self, parent, title, width, height, on_done, resizable=True):
        super().__init__(parent)
        self._on_done = on_done
        self._closed = False
        self.title(title)
        self.configure(fg_color=BG)
        self.resizable(True, True)  # always resizable, so nothing is ever stuck off-screen
        parent.update_idletasks()
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width, height = min(width, screen_w - 40), min(height, screen_h - 110)  # never taller than the screen
        self.minsize(min(width, 480), min(height, 360))
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - width) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - height) // 3)
        x = max(0, min(x, screen_w - width - 10))
        y = max(0, min(y, screen_h - height - 70))
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", lambda: self.close(None))
        self.after(200, self._take_focus)

    def _take_focus(self):
        if self._closed:
            return
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except tk.TclError:
            pass

    def close(self, value=None):
        if self._closed:
            return
        self._closed = True
        try:
            self.grab_release()
        except tk.TclError:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass
        if self._on_done:
            self._on_done(value)

    def copy_to_clipboard(self, text, button=None, label="Copy"):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        if button is not None:
            button.configure(text="Copied")
            self.after(1500, lambda: button.winfo_exists() and button.configure(text=label))


class TranscriptDialog(ModalBase):
    """Asks for a transcript file or pasted transcript text."""

    def __init__(self, parent, reason, error, on_done):
        super().__init__(parent, "Transcript needed", 640, 600, on_done)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(self, text="Add a transcript", font=font(20, "bold"), text_color=TEXT,
                     anchor="w").grid(row=0, column=0, sticky="ew", padx=24, pady=(22, 4))
        ctk.CTkLabel(self, text=reason, wraplength=580, justify="left", anchor="w",
                     text_color=SOFT, font=font(13)).grid(row=1, column=0, sticky="ew", padx=24)

        self.error_label = ctk.CTkLabel(self, text=error or "", wraplength=580, justify="left",
                                        anchor="w", text_color=BAD, font=font(13))
        self.error_label.grid(row=2, column=0, sticky="ew", padx=24, pady=(8, 0))

        file_row = ctk.CTkFrame(self, fg_color="transparent")
        file_row.grid(row=3, column=0, sticky="ew", padx=24, pady=(14, 6))
        primary_button(file_row, "Choose a transcript file", self.choose_file).pack(side="left")
        ctk.CTkLabel(file_row, text="VTT, SRT or TXT with timestamps", text_color=MUTED,
                     font=font(12)).pack(side="left", padx=12)

        paste_frame = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=10)
        paste_frame.grid(row=4, column=0, sticky="nsew", padx=24, pady=(8, 8))
        paste_frame.grid_columnconfigure(0, weight=1)
        paste_frame.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            paste_frame, anchor="w", justify="left", text_color=SOFT, font=font(12), wraplength=560,
            text="Or paste the text. On YouTube: open the video, click ...more, then Show transcript, "
                 "select all the lines and copy.",
        ).grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
        self.paste_box = ctk.CTkTextbox(paste_frame, fg_color=BG, text_color=TEXT, font=font(12, mono=True),
                                        border_width=1, border_color=LINE, wrap="word")
        self.paste_box.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 12))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=5, column=0, sticky="ew", padx=24, pady=(4, 20))
        quiet_button(buttons, "Cancel job", lambda: self.close(None)).pack(side="left")
        primary_button(buttons, "Use pasted text", self.use_pasted).pack(side="right")

    def choose_file(self):
        selected = filedialog.askopenfilename(
            parent=self, title="Select timestamped transcript",
            filetypes=[("Transcript files", "*.vtt *.srt *.txt"), ("All files", "*.*")])
        if selected:
            self.close(("file", selected))

    def use_pasted(self):
        text = self.paste_box.get("1.0", "end").strip()
        if not text:
            self.error_label.configure(text="Paste the transcript text first, or choose a file.")
            return
        self.close(("text", text))


class ManualGeminiDialog(ModalBase):
    """Fallback used only when the code's Gemini call failed."""

    def __init__(self, parent, reason, folder, prompt, validator, on_done):
        super().__init__(parent, "Finish with Gemini chat", 780, 800, on_done, resizable=True)
        self.folder = folder
        self.prompt = prompt
        self.validator = validator
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 0))
        ctk.CTkLabel(head, text="The AI step failed. You can finish it by hand.",
                     font=font(20, "bold"), text_color=TEXT, anchor="w").pack(anchor="w")
        ctk.CTkLabel(head, text=reason, wraplength=720, justify="left", anchor="w",
                     text_color=WARN, font=font(13)).pack(anchor="w", pady=(6, 0))

        steps = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=10)
        steps.grid(row=1, column=0, sticky="ew", padx=24, pady=(14, 8))
        steps.grid_columnconfigure(1, weight=1)

        rows = [
            ("1", "Open Gemini in your browser and start a new chat.", "Open Gemini", self.open_gemini),
            ("2", f"Attach the transcript file  (transcript.txt in {folder.name}).",
             "Open folder", lambda: open_path(self.folder)),
            ("3", "Paste the prompt into the chat and send it.", "Copy prompt", None),
            ("4", "Copy Gemini's JSON answer and paste it in the box below.", "", None),
        ]
        for r, (num, text, btn_text, cmd) in enumerate(rows):
            ctk.CTkLabel(steps, text=num, width=26, height=26, corner_radius=13, fg_color=RAISED,
                         text_color=ACCENT, font=font(12, "bold")).grid(
                row=r, column=0, padx=(14, 8), pady=(12 if r == 0 else 5, 12 if r == len(rows) - 1 else 5))
            ctk.CTkLabel(steps, text=text, anchor="w", justify="left", wraplength=470, text_color=TEXT,
                         font=font(13)).grid(row=r, column=1, sticky="ew", pady=5)
            if btn_text:
                button = quiet_button(steps, btn_text, cmd, width=120)
                if btn_text == "Copy prompt":
                    self.copy_button = button
                    button.configure(command=lambda b=button: self.copy_to_clipboard(self.prompt, b, "Copy prompt"))
                button.grid(row=r, column=2, padx=(8, 14), pady=5)

        ctk.CTkLabel(self, text="Paste Gemini's JSON answer here", anchor="w", text_color=SOFT,
                     font=font(12)).grid(row=2, column=0, sticky="ew", padx=26, pady=(8, 0))
        self.json_box = ctk.CTkTextbox(self, fg_color=PANEL, text_color=TEXT, font=font(12, mono=True),
                                       border_width=1, border_color=LINE, wrap="word")
        self.json_box.grid(row=3, column=0, sticky="nsew", padx=24, pady=(4, 6))

        self.error_label = ctk.CTkLabel(self, text="", wraplength=720, justify="left", anchor="w",
                                        text_color=BAD, font=font(13))
        self.error_label.grid(row=4, column=0, sticky="ew", padx=26)

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=5, column=0, sticky="ew", padx=24, pady=(8, 20))
        quiet_button(buttons, "Cancel job", lambda: self.close(None)).pack(side="left")
        quiet_button(buttons, "Paste from clipboard", self.paste_clipboard).pack(side="left", padx=8)
        primary_button(buttons, "Use this JSON", self.submit).pack(side="right")

    def open_gemini(self):
        webbrowser.open(GEMINI_URL)

    def paste_clipboard(self):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            self.error_label.configure(text="The clipboard is empty.")
            return
        self.json_box.delete("1.0", "end")
        self.json_box.insert("1.0", text)
        self.error_label.configure(text="")

    def submit(self):
        text = self.json_box.get("1.0", "end").strip()
        if not text:
            self.error_label.configure(text="Paste Gemini's JSON answer first.")
            return
        result, error = self.validator(text, self)
        if result is None:
            self.error_label.configure(text=error or "That answer could not be used.")
            return
        self.close(result)


class SettingsDialog(ModalBase):
    def __init__(self, parent, on_saved):
        super().__init__(parent, "Settings", 520, 520, None)
        self.on_saved = on_saved
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self, text="Settings", font=font(20, "bold"), text_color=TEXT,
                     anchor="w").grid(row=0, column=0, sticky="ew", padx=24, pady=(22, 2))
        ctk.CTkLabel(self, text=f"Saved to {ENV_FILE.name} next to the app.", text_color=MUTED,
                     anchor="w", font=font(12)).grid(row=1, column=0, sticky="ew", padx=24)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, sticky="ew", padx=24, pady=14)
        body.grid_columnconfigure(0, weight=1)

        field_label(body, "Gemini API key").grid(row=0, column=0, sticky="w")
        self.key_entry = ctk.CTkEntry(body, show="•", height=36, fg_color=PANEL, border_color=LINE,
                                      text_color=TEXT, font=font(13))
        self.key_entry.grid(row=1, column=0, sticky="ew", pady=(4, 2))
        self.key_entry.insert(0, Config.api_key)
        self.show_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(body, text="Show key", variable=self.show_var, command=self.toggle_key,
                        text_color=SOFT, font=font(12), checkbox_width=18, checkbox_height=18,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER).grid(row=2, column=0, sticky="w", pady=(2, 12))

        field_label(body, "Gemini model").grid(row=3, column=0, sticky="w")
        self.model_entry = ctk.CTkEntry(body, height=36, fg_color=PANEL, border_color=LINE,
                                        text_color=TEXT, font=font(13))
        self.model_entry.grid(row=4, column=0, sticky="ew", pady=(4, 12))
        self.model_entry.insert(0, Config.model)

        field_label(body, "Browser cookies (fixes age-restricted videos and 'not a bot' errors)").grid(
            row=5, column=0, sticky="w")
        self.cookie_var = tk.StringVar(value=Config.cookies_browser if Config.cookies_browser in COOKIE_BROWSERS
                                       else "None")
        option_menu(body, COOKIE_BROWSERS, self.cookie_var).grid(row=6, column=0, sticky="ew", pady=(4, 0))
        ctk.CTkLabel(body, text="Pick the browser where you are signed in to YouTube. Close that browser "
                                "first if downloads fail.", wraplength=460, justify="left", anchor="w",
                     text_color=MUTED, font=font(12)).grid(row=7, column=0, sticky="ew", pady=(6, 0))

        self.message = ctk.CTkLabel(self, text="", text_color=BAD, anchor="w", font=font(12))
        self.message.grid(row=3, column=0, sticky="ew", padx=24)

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=4, column=0, sticky="ew", padx=24, pady=(10, 20))
        quiet_button(buttons, "Cancel", lambda: self.close(None)).pack(side="left")
        primary_button(buttons, "Save settings", self.save).pack(side="right")

    def toggle_key(self):
        self.key_entry.configure(show="" if self.show_var.get() else "•")

    def save(self):
        key = self.key_entry.get().strip()
        model = self.model_entry.get().strip() or DEFAULT_MODEL
        if re.search(r"\s", key):
            self.message.configure(text="The API key must not contain spaces.")
            return
        try:
            save_env_values({
                "GEMINI_API_KEY": key,
                "GEMINI_MODEL": model,
                "COOKIES_BROWSER": self.cookie_var.get(),
            })
        except OSError as exc:
            self.message.configure(text=f"Could not write the .env file: {exc}")
            return
        Config.load()
        self.on_saved()
        self.close(True)


class PostCopyDialog(ModalBase):
    """Titles, captions and hashtags for ALL clips at once (one Gemini call), ready to copy."""

    def __init__(self, app, on_done):
        super().__init__(app, "Titles & hashtags for your posts", 880, 900, on_done, resizable=True)
        self.app = app
        self.results = queue.Queue()
        self.busy = False
        self.blocks = {}
        self.link_var = tk.BooleanVar(value=True)
        cards = [c for c in app.cards if c.output_path] or list(app.cards)
        self.cards = {c.clip.rank: c for c in cards}
        self.clips = [c.clip for c in cards]
        context = app.post_context or {}
        self.video_title = context.get("title", "")
        self.url = context.get("url", "")
        self.transcript = context.get("transcript", "")
        self.prompt = build_social_prompt_all(self.video_title, self.clips, self.transcript)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=24, pady=(18, 4))
        ctk.CTkLabel(head, text="Titles & hashtags for all your clips", font=font(20, "bold"),
                     text_color=TEXT, anchor="w").pack(anchor="w")
        ctk.CTkLabel(head, text="Everything is written in one go. Edit anything you like, then press Copy and paste "
                                "it into YouTube or Instagram.", font=font(12), text_color=MUTED, anchor="w",
                     justify="left", wraplength=800).pack(anchor="w", pady=(2, 0))

        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.grid(row=1, column=0, sticky="nsew", padx=(18, 12), pady=(6, 0))
        self.scroll.grid_columnconfigure(0, weight=1)
        for i, clip in enumerate(self.clips):
            self._build_block(i, clip)

        options = ctk.CTkFrame(self, fg_color="transparent")
        options.grid(row=2, column=0, sticky="ew", padx=24, pady=(8, 0))
        ctk.CTkCheckBox(options, text="Add the link of the full video to the YouTube descriptions",
                        variable=self.link_var, command=self.refresh_descriptions, font=font(12), text_color=SOFT,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=MUTED).pack(anchor="w")

        self.status = ctk.CTkLabel(self, text="", anchor="w", justify="left", wraplength=820, font=font(13),
                                   text_color=MUTED)
        self.status.grid(row=3, column=0, sticky="ew", padx=26, pady=(8, 0))

        self.fallback = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=10)
        self.fallback.grid(row=4, column=0, sticky="ew", padx=24, pady=(8, 0))
        ctk.CTkLabel(self.fallback, text="Or use the Gemini chat website:", font=font(12), text_color=SOFT).pack(
            side="left", padx=(14, 8), pady=10)
        self.prompt_button = quiet_button(self.fallback, "Copy prompt", None, width=110, height=30)
        self.prompt_button.configure(command=lambda: self.copy_to_clipboard(self.prompt, self.prompt_button, "Copy prompt"))
        self.prompt_button.pack(side="left", padx=4)
        quiet_button(self.fallback, "Open Gemini", lambda: webbrowser.open(GEMINI_URL), width=110, height=30).pack(
            side="left", padx=4)
        quiet_button(self.fallback, "Paste answer", self.paste_answer, width=110, height=30).pack(side="left", padx=4)
        self.fallback.grid_remove()

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=5, column=0, sticky="ew", padx=24, pady=(10, 20))
        self.regen_button = quiet_button(buttons, "Write again (all clips)", lambda: self.generate(creative=True),
                                         width=190)
        self.regen_button.pack(side="left")
        quiet_button(buttons, "Save all as .txt", self.save_all, width=140).pack(side="left", padx=8)
        self.all_button = primary_button(buttons, "Copy everything", self.copy_everything, width=160)
        self.all_button.pack(side="right")

        self.after(250, self.start)
        self.after(150, self.poll)

    # layout ---------------------------------------------------------------
    def _build_block(self, row, clip):
        block = ctk.CTkFrame(self.scroll, fg_color=PANEL, corner_radius=12, border_width=1, border_color=LINE)
        block.grid(row=row, column=0, sticky="ew", pady=(0, 12), padx=(0, 6))
        block.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(block, text=f"#{clip.rank}   {clip.title}", font=font(14, "bold"), text_color=ACCENT,
                     anchor="w", justify="left", wraplength=760).grid(
            row=0, column=0, columnspan=2, sticky="ew", padx=14, pady=(12, 2))
        title = self._field(block, 1, "YouTube title", None)
        desc = self._field(block, 3, "YouTube description (with hashtags)", 100)
        caption = self._field(block, 5, "Instagram caption (with hashtags)", 100)
        caption.grid_configure(pady=(4, 14))
        self.blocks[clip.rank] = {"title": title, "desc": desc, "ig": caption}

    def _field(self, block, row, label, height):
        field_label(block, label).grid(row=row, column=0, sticky="w", padx=14, pady=(8, 0))
        if height is None:
            widget = ctk.CTkEntry(block, fg_color=BG, text_color=TEXT, border_color=LINE, font=font(14), height=34)
        else:
            widget = ctk.CTkTextbox(block, fg_color=BG, text_color=TEXT, font=font(13), border_width=1,
                                    border_color=LINE, wrap="word", height=height)
        widget.grid(row=row + 1, column=0, columnspan=2, sticky="ew", padx=14, pady=(4, 0))
        button = quiet_button(block, "Copy", None, width=76, height=26)
        button.configure(command=lambda w=widget, b=button: self._copy(w, b))
        button.grid(row=row, column=1, sticky="e", padx=14, pady=(8, 0))
        return widget

    @staticmethod
    def read(widget):
        if isinstance(widget, ctk.CTkEntry):
            return widget.get().strip()
        return widget.get("1.0", "end").strip()

    @staticmethod
    def write(widget, text):
        if isinstance(widget, ctk.CTkEntry):
            widget.delete(0, "end")
            widget.insert(0, text)
        else:
            widget.delete("1.0", "end")
            widget.insert("1.0", text)

    def _copy(self, widget, button):
        text = self.read(widget)
        if text:
            self.copy_to_clipboard(text, button)

    # content --------------------------------------------------------------
    def fill(self, posts):
        for rank, copy in posts.items():
            block = self.blocks.get(rank)
            if not block:
                continue
            self.app.posts[rank] = copy
            yt_title, yt_desc, ig = format_post(copy, self.url, self.link_var.get())
            self.write(block["title"], yt_title)
            self.write(block["desc"], yt_desc)
            self.write(block["ig"], ig)
        self.fallback.grid_remove()
        done = sum(1 for r in self.blocks if r in self.app.posts)
        self.status.configure(text=f"Ready: {done} of {len(self.blocks)} clips. Edit anything, then press Copy.",
                              text_color=GOOD if done == len(self.blocks) else WARN)

    def refresh_descriptions(self):
        for rank, block in self.blocks.items():
            copy = self.app.posts.get(rank)
            if copy:
                self.write(block["desc"], format_post(copy, self.url, self.link_var.get())[1])

    def all_items(self):
        items = []
        for clip in self.clips:
            block = self.blocks[clip.rank]
            card = self.cards.get(clip.rank)
            items.append((clip, card.output_path if card else None, self.read(block["title"]),
                          self.read(block["desc"]), self.read(block["ig"])))
        return items

    def copy_everything(self):
        text = ("\n" + "=" * 60 + "\n\n").join(post_text(c, t, d, i) for c, _p, t, d, i in self.all_items())
        self.copy_to_clipboard(text, self.all_button, "Copy everything")

    def show_error(self, text):
        self.status.configure(text=text, text_color=BAD)
        self.fallback.grid()

    # actions --------------------------------------------------------------
    def start(self):
        if self._closed:
            return
        if not self.clips:
            self.status.configure(text="There are no clips yet.", text_color=WARN)
            return
        self.fill({r: p for r, p in self.app.posts.items() if r in self.blocks})
        missing = [c for c in self.clips if c.rank not in self.app.posts]
        if missing:
            self.generate(missing)

    def generate(self, clips=None, creative=False):
        if self.busy or not self.clips:
            return
        if not Config.api_key:
            self.show_error("No Gemini API key is set. Add it in Settings, or use the Gemini chat website below.")
            return
        clips = clips or self.clips
        self.busy = True
        self.regen_button.configure(state="disabled")
        self.status.configure(text=f"Gemini is writing the posts for {len(clips)} clips…", text_color=ACCENT)
        temperature = 1.0 if creative else 0.7
        title, transcript = self.video_title, self.transcript

        def work():
            try:
                self.results.put(("ok", generate_social_copy_all(title, clips, transcript, temperature)))
            except Exception as exc:
                self.results.put(("error", f"Gemini could not write the posts. {friendly_error(exc)}"))

        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        if self._closed:
            return
        try:
            while True:
                kind, value = self.results.get_nowait()
                self.busy = False
                self.regen_button.configure(state="normal")
                if kind == "ok":
                    self.fill(value)
                else:
                    self.show_error(value)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        self.after(150, self.poll)

    def paste_answer(self):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            self.show_error("The clipboard is empty. Copy Gemini's answer first.")
            return
        try:
            self.fill(parse_social_copy_all(text, [c.rank for c in self.clips]))
        except ValueError as exc:
            self.show_error(f"That text could not be used: {exc}.")

    def save_all(self):
        paths = [c.output_path for c in self.cards.values() if c.output_path]
        if not paths:
            self.status.configure(text="Create the clips first. Then the text files are saved next to the videos.",
                                  text_color=WARN)
            return
        try:
            target = save_post_files(Path(paths[0]).parent, self.all_items())
        except OSError as exc:
            self.show_error(f"Could not save the files: {exc}")
            return
        self.status.configure(text=f"Saved {target.name} and one .txt next to each video in {target.parent}",
                              text_color=GOOD)


class ClipCard(ctk.CTkFrame):
    def __init__(self, parent, clip, on_post=None):
        super().__init__(parent, fg_color=PANEL, corner_radius=12, border_width=1, border_color=LINE)
        self.clip = clip
        self.output_path = None
        self.post_copy = None
        self._wrap = 0
        self.var = tk.BooleanVar(value=True)
        self.grid_columnconfigure(2, weight=1)

        ctk.CTkCheckBox(self, text="", variable=self.var, width=24, checkbox_width=22, checkbox_height=22,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=MUTED).grid(
            row=0, column=0, rowspan=4, padx=(14, 4), pady=14, sticky="n")
        ctk.CTkLabel(self, text=f"#{clip.rank}", width=40, height=28, corner_radius=8, fg_color=RAISED,
                     text_color=ACCENT, font=font(13, "bold")).grid(
            row=0, column=1, rowspan=2, padx=(6, 12), pady=(14, 0), sticky="n")

        self.title_label = ctk.CTkLabel(self, text=clip.title, anchor="w", justify="left",
                                        font=font(15, "bold"), text_color=TEXT, wraplength=480)
        self.title_label.grid(row=0, column=2, sticky="ew", pady=(12, 0), padx=(0, 12))
        ctk.CTkLabel(
            self, anchor="w", text_color=MUTED, font=font(12),
            text=clip_summary(clip),
        ).grid(row=1, column=2, sticky="ew", padx=(0, 12))
        self.desc_label = ctk.CTkLabel(self, text=clip.description, anchor="w", justify="left",
                                       text_color=SOFT, font=font(13), wraplength=480)
        if clip.description:
            self.desc_label.grid(row=2, column=2, sticky="ew", padx=(0, 12), pady=(4, 0))

        self.status = ctk.CTkLabel(self, text="Waiting", anchor="w", text_color=MUTED, font=font(12))
        self.status.grid(row=3, column=2, sticky="w", pady=(6, 12))
        self.open_button = quiet_button(self, "Show file", self.show_file, width=90, height=28)
        self.open_button.grid(row=3, column=3, padx=(0, 14), pady=(6, 12))
        self.open_button.grid_remove()
        if on_post:
            quiet_button(self, "Title & hashtags", lambda: on_post(self), width=140, height=30).grid(
                row=0, column=3, rowspan=2, padx=(0, 14), pady=(12, 0), sticky="ne")
        self.bind("<Configure>", self._resize)

    def _resize(self, event):
        wrap = max(240, event.width - 150)
        if abs(wrap - self._wrap) > 6:
            self._wrap = wrap
            self.title_label.configure(wraplength=wrap)
            self.desc_label.configure(wraplength=wrap)

    def set_status(self, text, color, path=None):
        self.status.configure(text=text, text_color=color)
        if path:
            self.output_path = path
            self.open_button.grid()

    def show_file(self):
        if self.output_path:
            open_path(self.output_path, select=True)


# --------------------------------------------------------------------------- #
# Main application
# --------------------------------------------------------------------------- #
class ViralClipsApp(ctk.CTk):
    STEP_COLORS = {
        "pending": (RAISED, MUTED),
        "active": (ACCENT, ACCENT_TEXT),
        "done": ("#245b45", "#d8f5e8"),
        "failed": ("#6d2b2b", "#ffd9d9"),
    }

    def __init__(self):
        super().__init__()
        self.title("Viral Clips Studio")
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width, height = min(1240, screen_w - 40), min(880, screen_h - 90)
        self.geometry(f"{width}x{height}+{max(0, (screen_w - width) // 2)}+{max(0, (screen_h - height) // 3)}")
        self.minsize(min(880, width), min(540, height))  # small screens: everything scrolls instead of being cut off
        self.configure(fg_color=BG)

        self.events = queue.Queue()
        self.app_state = "idle"  # idle | running | review | finished
        self.job = None
        self.cards = []
        self.found_clips = []  # every clip the AI found so far (the cards in the Clips tab)
        self.review_waiter = None
        self.editor_session = None
        self.editor_server = None
        self.active_dialog = None
        self.post_dialog = None
        self.post_context = None  # video title, link and transcript for the post copy
        self.posts = {}  # rank -> post copy written by Gemini
        self.video_cache = None  # the downloaded video, so it is never downloaded twice
        self.parts_cache = None  # the downloaded clip parts (when only the clips are downloaded)
        self.video_lock = threading.Lock()
        self.last_output_folder = None
        self._alive = True
        self._step_index = None
        self._step_state = None

        self.quality_var = tk.StringVar(value="720p or lower")
        self.ratio_var = tk.StringVar(value="Vertical 9:16 (Reels / Shorts)")
        self.duration_var = tk.StringVar(value="20–25 seconds")
        self.auto_var = tk.BooleanVar(value=False)  # editor is the default path
        self.status_var = tk.StringVar(value="Ready")
        self.download_first_var = tk.BooleanVar(value=False)  # download first, edit from the file
        self.clip_parts_var = tk.BooleanVar(value=True)  # download only the clips (+10 s either side)
        self.post_var = tk.BooleanVar(value=True)  # write titles & hashtags after the clips are created

        self.build_ui()
        self.refresh_key_status()
        self.refresh_buttons()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.process_events)

    # ------------------------------------------------------------------ UI
    def build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=22, pady=(18, 10))
        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.pack(side="left")
        ctk.CTkLabel(titles, text="Viral Clips Studio", font=font(24, "bold"), text_color=TEXT).pack(anchor="w")
        ctk.CTkLabel(titles, text="Turn one long YouTube video into five short clips.",
                     font=font(13), text_color=MUTED).pack(anchor="w")
        quiet_button(header, "Settings", self.open_settings, width=96).pack(side="right")
        quiet_button(header, "Output folder", self.open_output, width=120).pack(side="right", padx=8)

        # Sidebar
        side = ctk.CTkScrollableFrame(self, fg_color=PANEL, corner_radius=14, width=336)
        side.grid(row=1, column=0, sticky="ns", padx=(22, 10), pady=(0, 10))
        side.grid_columnconfigure(0, weight=1)

        field_label(side, "YouTube link").grid(row=0, column=0, sticky="w", padx=20, pady=(20, 0))
        url_row = ctk.CTkFrame(side, fg_color="transparent")
        url_row.grid(row=1, column=0, sticky="ew", padx=20, pady=(4, 12))
        url_row.grid_columnconfigure(0, weight=1)
        self.url_entry = ctk.CTkEntry(url_row, height=38, fg_color=BG, border_color=LINE, text_color=TEXT,
                                      placeholder_text="https://www.youtube.com/watch?v=…", font=font(13))
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.url_entry.bind("<Return>", lambda e: self.on_primary())
        quiet_button(url_row, "Paste", self.paste_url, width=64, height=38).grid(row=0, column=1, padx=(8, 0))

        field_label(side, "Download quality").grid(row=2, column=0, sticky="w", padx=20)
        option_menu(side, list(QUALITY_OPTIONS), self.quality_var).grid(
            row=3, column=0, sticky="ew", padx=20, pady=(4, 12))
        field_label(side, "Output shape").grid(row=4, column=0, sticky="w", padx=20)
        option_menu(side, list(RATIO_OPTIONS), self.ratio_var).grid(
            row=5, column=0, sticky="ew", padx=20, pady=(4, 12))
        field_label(side, "Clip length").grid(row=6, column=0, sticky="w", padx=20)
        option_menu(side, list(DURATION_OPTIONS), self.duration_var).grid(
            row=7, column=0, sticky="ew", padx=20, pady=(4, 12))

        field_label(side, "Instructions for Gemini (optional)").grid(row=8, column=0, sticky="w", padx=20)
        self.instructions_box = ctk.CTkTextbox(side, height=72, fg_color=BG, border_width=1, border_color=LINE,
                                               text_color=TEXT, font=font(13), wrap="word")
        self.instructions_box.grid(row=9, column=0, sticky="ew", padx=20, pady=(4, 12))
        self.instructions_box.insert("1.0", DEFAULT_INSTRUCTIONS)

        switches = [
            (f"Long videos ({LONG_VIDEO_SECONDS // 60} min+): download only the clips (+{SECTION_PAD}s)", self.clip_parts_var, None),
            ("Download video first, edit from the file", self.download_first_var, None),
            ("Auto-write titles & hashtags for all clips", self.post_var, None),
            ("Skip the editor (create clips right after analysis)", self.auto_var, self.refresh_buttons),
        ]
        for offset, (label, variable, command) in enumerate(switches):
            ctk.CTkSwitch(side, text=label, variable=variable, command=command, text_color=SOFT, font=font(13),
                          progress_color=ACCENT, button_color=TEXT, button_hover_color=SOFT).grid(
                row=10 + offset, column=0, sticky="w", padx=20, pady=(2, 10 if offset < len(switches) - 1 else 14))

        status_box = ctk.CTkFrame(side, fg_color=BG, corner_radius=10)
        status_box.grid(row=10 + len(switches), column=0, sticky="ew", padx=20, pady=(0, 20))
        status_box.grid_columnconfigure(0, weight=1)
        self.key_label = ctk.CTkLabel(status_box, text="", anchor="w", justify="left", wraplength=270,
                                      font=font(12), text_color=SOFT)
        self.key_label.grid(row=0, column=0, sticky="ew", padx=14, pady=12)

        # Main area
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=1, column=1, sticky="nsew", padx=(10, 22), pady=(0, 10))
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)

        step_bar = ctk.CTkFrame(main, fg_color="transparent")
        step_bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.step_labels = []
        for i, name in enumerate(STEPS):
            label = ctk.CTkLabel(step_bar, text=f"{i + 1}. {name}", height=30, corner_radius=15,
                                 fg_color=RAISED, text_color=MUTED, font=font(12, "bold"), padx=12)
            self.step_labels.append(label)
        attach_flow(step_bar, lambda: self.step_labels, self.step_labels)  # wraps on narrow windows
        self.step_labels[-1].configure(cursor="hand2")  # the Upload step opens the Upload tab
        self.step_labels[-1].bind("<Button-1>", lambda _e: self.tabs.set("Upload"))

        self.tabs = ctk.CTkTabview(
            main, fg_color=PANEL, corner_radius=14, segmented_button_fg_color=BG,
            segmented_button_selected_color=ACCENT, segmented_button_selected_hover_color=ACCENT_HOVER,
            segmented_button_unselected_color=BG, segmented_button_unselected_hover_color=RAISED,
            text_color=TEXT)
        self.tabs.grid(row=1, column=0, sticky="nsew")
        for name in ("Clips", "Upload", "Log", "JSON"):
            self.tabs.add(name)
        self.tabs._segmented_button.configure(font=font(13, "bold"), text_color=TEXT)

        clips_tab = self.tabs.tab("Clips")
        clips_tab.grid_columnconfigure(0, weight=1)
        clips_tab.grid_rowconfigure(1, weight=1)
        toolbar = ctk.CTkFrame(clips_tab, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ctk.CTkLabel(toolbar, text="Posts for Instagram and YouTube", font=font(13), text_color=MUTED).pack(
            side="left", padx=(4, 0))
        self.post_all_btn = primary_button(toolbar, "Titles & hashtags for all clips", self.open_post_copy,
                                           width=250)
        self.post_all_btn.configure(state="disabled")
        self.post_all_btn.pack(side="right")
        self.clips_frame = ctk.CTkScrollableFrame(clips_tab, fg_color="transparent")
        self.clips_frame.grid(row=1, column=0, sticky="nsew")
        self.clips_frame.grid_columnconfigure(0, weight=1)
        self.empty_label = ctk.CTkLabel(
            self.clips_frame, text_color=MUTED, font=font(14), justify="center",
            text="No clips yet.\nPaste a YouTube link on the left and press Analyze.")
        self.empty_label.grid(row=0, column=0, pady=90)

        upload_tab = self.tabs.tab("Upload")
        upload_tab.grid_columnconfigure(0, weight=1)
        upload_tab.grid_rowconfigure(0, weight=1)
        self.upload_panel = None
        if make_upload_panel_class:
            self.upload_panel = make_upload_panel_class()(
                upload_tab, on_log=self.log, on_step=lambda state: self.apply_step(len(STEPS) - 1, state),
                initial_dir=OUTPUT_DIR)
            self.upload_panel.grid(row=0, column=0, sticky="nsew")
        else:
            ctk.CTkLabel(upload_tab, text_color=WARN, font=font(13), justify="center",
                         text="youtube_uploader.py was not found next to main.py.\nPut it in the same folder and "
                              "restart the app to enable uploading.").grid(row=0, column=0, pady=80)

        log_tab = self.tabs.tab("Log")
        log_tab.grid_columnconfigure(0, weight=1)
        log_tab.grid_rowconfigure(0, weight=1)
        self.log_box = ctk.CTkTextbox(log_tab, fg_color=BG, text_color=SOFT, font=font(12, mono=True),
                                      wrap="word", state="disabled")
        self.log_box.grid(row=0, column=0, sticky="nsew")
        self.log_box.tag_config("warn", foreground=WARN)
        self.log_box.tag_config("error", foreground=BAD)
        self.log_box.tag_config("ok", foreground=GOOD)

        json_tab = self.tabs.tab("JSON")
        json_tab.grid_columnconfigure(0, weight=1)
        json_tab.grid_rowconfigure(0, weight=1)
        self.json_box = ctk.CTkTextbox(json_tab, fg_color=BG, text_color=SOFT, font=font(12, mono=True),
                                       wrap="none", state="disabled")
        self.json_box.grid(row=0, column=0, sticky="nsew")
        quiet_button(json_tab, "Copy JSON", self.copy_json, width=110).grid(row=1, column=0, sticky="e", pady=(8, 0))

        # Bottom bar
        bottom = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=14)
        bottom.grid(row=2, column=0, columnspan=2, sticky="ew", padx=22, pady=(0, 20))
        bottom.grid_columnconfigure(0, weight=1)

        progress_area = ctk.CTkFrame(bottom, fg_color="transparent")
        progress_area.grid(row=0, column=0, sticky="ew", padx=(20, 12), pady=14)
        progress_area.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(progress_area, textvariable=self.status_var, anchor="w", text_color=TEXT,
                     font=font(13)).grid(row=0, column=0, sticky="ew")
        self.progress = ctk.CTkProgressBar(progress_area, height=8, progress_color=ACCENT, fg_color=RAISED)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.progress.set(0)

        buttons = ctk.CTkFrame(bottom, fg_color="transparent")
        buttons.grid(row=0, column=1, padx=(0, 20), pady=14)
        self._bottom_wide = True

        def place_buttons(event):
            wide = event.width >= 1120
            if wide != self._bottom_wide:
                self._bottom_wide = wide
                if wide:
                    progress_area.grid_configure(padx=(20, 12))
                    buttons.grid_configure(row=0, column=1, columnspan=1, sticky="", padx=(0, 20), pady=14)
                else:
                    progress_area.grid_configure(padx=(20, 20))
                    buttons.grid_configure(row=1, column=0, columnspan=2, sticky="e", padx=(20, 20), pady=(0, 14))

        bottom.bind("<Configure>", place_buttons)
        self.reset_btn = quiet_button(buttons, "New video", self.reset, width=100, height=38)
        self.reset_btn.pack(side="left", padx=(0, 8))
        self.cancel_btn = ctk.CTkButton(buttons, text="Cancel", command=self.cancel_job, width=90, height=38,
                                        fg_color="#5a2a2e", hover_color="#733439", text_color="#ffd9d9",
                                        font=font(13), corner_radius=8)
        self.cancel_btn.pack(side="left", padx=(0, 8))
        self.edit_btn = primary_button(buttons, "Edit clips (time and size)", self.open_editor, width=190)
        self.create_btn = quiet_button(buttons, "Create selected clips", self.create_selected, width=156, height=38)
        self.create_btn.pack(side="left", padx=(0, 8))
        self.start_btn = primary_button(buttons, "Analyze and create clips", self.on_primary, width=190)
        self.start_btn.pack(side="left")

    def refresh_key_status(self):
        if Config.api_key:
            self.key_label.configure(
                text=f"Gemini is ready ({Config.model}). If it fails, you can finish by hand in the Gemini chat.",
                text_color=SOFT)
        else:
            self.key_label.configure(
                text="No Gemini API key yet. Add one in Settings, or continue and use the Gemini chat by hand.",
                text_color=WARN)

    def refresh_buttons(self):
        s = self.app_state
        idle = s in ("idle", "finished")
        self.start_btn.configure(
            state="normal" if idle else "disabled",
            text="Analyze and create clips" if self.auto_var.get() else "Analyze video")
        self.reset_btn.configure(state="normal" if idle else "disabled")
        self.cancel_btn.configure(state="normal" if s in ("running", "review") else "disabled")
        if s == "review":
            self.create_btn.configure(state="normal")
            self.create_btn.pack(side="left", padx=(0, 8), before=self.start_btn)
            self.edit_btn.pack(side="left", padx=(0, 8), before=self.create_btn)
        else:
            self.create_btn.pack_forget()
            self.edit_btn.pack_forget()

    # ------------------------------------------------------ thread-safe calls
    def log(self, message, level="info"):
        self.events.put(("log", (message, level)))

    def set_status(self, message):
        self.events.put(("status", message))

    def set_progress(self, value):
        self.events.put(("progress", value))

    def set_step(self, index, state):
        self._step_index, self._step_state = index, state
        self.events.put(("step", (index, state)))

    def wait_for(self, waiter):
        while not waiter.event.wait(0.25):
            if self.job.cancel.is_set():
                raise JobCancelled()
        if self.job.cancel.is_set():
            raise JobCancelled()
        return waiter.value

    def ask(self, kind, **payload):
        waiter = Waiter()
        self.events.put(("dialog", (kind, payload, waiter)))
        return self.wait_for(waiter)

    # ----------------------------------------------------------- user actions
    def paste_url(self):
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, text)

    def open_settings(self):
        if self.app_state in ("running", "review"):
            messagebox.showinfo("Please wait", "Settings can be changed after the current job finishes.")
            return
        SettingsDialog(self, self.refresh_key_status)

    def open_output(self):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        open_path(self.last_output_folder if self.last_output_folder and self.last_output_folder.exists()
                  else OUTPUT_DIR)

    def copy_json(self):
        text = self.json_box.get("1.0", "end").strip()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)

    def on_primary(self):
        if self.app_state not in ("idle", "finished"):
            return
        if self.uploading():
            messagebox.showinfo("Upload in progress", "Wait for the YouTube upload to finish (or cancel it in the "
                                                      "Upload tab) before analysing a new video.")
            return
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showwarning("Missing link", "Paste a YouTube video link first.")
            self.url_entry.focus_set()
            return
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, url)
        if not re.match(r"^https?://([\w-]+\.)?(youtube\.com|youtu\.be|youtube-nocookie\.com)/", url, re.I):
            if not messagebox.askyesno(
                    "Not a YouTube link",
                    "This does not look like a YouTube link. Captions may not be found automatically.\n\n"
                    "Try it anyway?"):
                return
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            messagebox.showerror("FFmpeg not found", FFMPEG_HELP)
            return
        if not Config.api_key:
            answer = messagebox.askyesnocancel(
                "No Gemini API key",
                "There is no Gemini API key, so the app cannot ask Gemini by itself.\n\n"
                "Yes: continue and finish the AI step by hand in the Gemini chat\n"
                "No: open Settings to add a key\n"
                "Cancel: go back")
            if answer is None:
                return
            if answer is False:
                self.open_settings()
                return

        self.job = Job(
            url=url,
            quality=QUALITY_OPTIONS[self.quality_var.get()],
            ratio=RATIO_OPTIONS[self.ratio_var.get()],
            duration_range=DURATION_OPTIONS[self.duration_var.get()],
            instructions=self.instructions_box.get("1.0", "end").strip(),
            auto_create=self.auto_var.get(),
            cancel=threading.Event(),
            download_first=self.download_first_var.get(),
            auto_post=self.post_var.get(),
            clip_parts_only=self.clip_parts_var.get(),
        )
        self.clear_results()
        self.app_state = "running"
        self.refresh_buttons()
        self.set_status_now("Starting…")
        threading.Thread(target=self.run_job, args=(self.job,), daemon=True).start()

    def create_selected(self):
        if self.app_state != "review" or not self.review_waiter:
            return
        selected = [i for i, card in enumerate(self.cards) if card.var.get()]
        if not selected:
            messagebox.showwarning("No clips selected", "Tick at least one clip to create.")
            return
        waiter, self.review_waiter = self.review_waiter, None
        self.app_state = "running"
        self.refresh_buttons()
        waiter.resolve(("selected", selected))

    def open_editor(self):
        if self.app_state != "review" or not self.editor_session:
            return
        try:
            if self.editor_server is None:
                self.editor_server = EditorServer(self.editor_session, self.editor_generate, self.editor_action, self.editor_clip)
            open_editor_window(self.editor_server.url)
            self.log("Clip editor opened. Set the times and sizes, then press 'Generate clips' there.")
        except Exception as exc:
            messagebox.showerror("Cannot open the editor", str(exc))

    def parse_editor_clips(self, data):
        """Turn the clips sent by the editor page into Clip objects (raises ValueError for bad input)."""
        items = data.get("clips") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            raise ValueError("No clips were sent.")
        clips = []
        for n, item in enumerate(items, 1):
            try:
                rank = int(item.get("rank", n))
                start, end = float(item["start"]), float(item["end"])
            except (KeyError, TypeError, ValueError, AttributeError):
                raise ValueError(f"Clip {n} has unreadable times.")
            if start < 0 or end - start < 0.5:
                raise ValueError(f"Clip #{rank}: the end must be after the start.")
            width, height = item.get("width"), item.get("height")
            size = None  # original size
            if width or height:
                try:
                    width, height = int(width), int(height)
                except (TypeError, ValueError):
                    raise ValueError(f"Clip #{rank}: set both width and height.")
                if not (100 <= width <= 4096 and 100 <= height <= 4096):
                    raise ValueError(f"Clip #{rank}: width and height must be between 100 and 4096.")
                size = (width - width % 2, height - height % 2)  # H.264 needs even sizes
            title = str(item.get("title") or f"Clip {rank}").strip()[:200]
            layout = "split" if item.get("layout") == "split" else "single"
            split = normalize_split(item.get("split")) if layout == "split" else None
            texts = []
            raw_texts = item.get("texts") if isinstance(item.get("texts"), list) else []
            for text_number, raw_text in enumerate(raw_texts[:MAX_TEXTS_PER_CLIP], 1):
                png = decode_png_data_url(raw_text.get("png")) if isinstance(raw_text, dict) else None
                if png is None:
                    raise ValueError(f"Clip #{rank}: text {text_number} could not be read.")
                texts.append({"png": png, "x": clamp01(raw_text.get("x"), 0.5), "y": clamp01(raw_text.get("y"), 0.85),
                              "start": optional_seconds(raw_text.get("start")),
                              "end": optional_seconds(raw_text.get("end"))})
            if (layout == "split" or texts) and size is None:
                size = VERTICAL_DEFAULT
            clips.append(Clip(rank, title, str(item.get("description") or "").strip(), start, end, size,
                              clamp01(item.get("crop_x")), clamp01(item.get("crop_y")), layout, split,
                              texts or None))
        return clips

    def editor_generate(self, data):
        """Called from the editor's web server thread when the user presses Generate clips."""
        clips = self.parse_editor_clips(data)
        waiter = self.review_waiter
        if waiter is None or self.app_state != "review":
            raise ValueError("The app is not waiting for clips right now.")
        if self.editor_session is not None and self.editor_session.more.get("busy"):
            raise ValueError("The AI is still looking for clips. Wait until it has finished.")
        self.review_waiter = None
        self.events.put(("state", "running"))
        waiter.resolve(("edited", clips))

    def editor_clip(self, data):
        """'Download this clip' in the editor: cut ONE clip with all its edits, now, while the editor stays open."""
        session, job = self.editor_session, self.job
        if session is None or job is None:
            raise ValueError("The editor is not ready.")
        clips = self.parse_editor_clips(data)
        if len(clips) != 1:
            raise ValueError("Send exactly one clip.")
        clip_id = secrets.token_hex(6)
        session.clip_jobs[clip_id] = {"status": "working", "percent": 0, "message": "Starting…", "error": "",
                                      "path": "", "name": ""}
        threading.Thread(target=self._editor_clip_worker, args=(session, job, clips[0], clip_id), daemon=True).start()
        return {"id": clip_id}

    def _editor_clip_worker(self, session, job, clip, clip_id):
        state = session.clip_jobs[clip_id]
        try:
            if job.video_duration:
                fitted, _dropped = fit_to_duration([clip], job.video_duration)
                if not fitted:
                    raise UserFacingError("This clip is outside the video length.")
                clip = fitted[0]
            cached = self.video_cache
            source, cut_start, cut_end = None, clip.start, clip.end
            if session.video_path:
                source = session.video_path
            elif cached and cached["key"] == (job.url, job.quality) and Path(cached["path"]).is_file():
                source = cached["path"]
            else:  # download just this clip (+ padding); parts that are already downloaded are reused
                state.update(message=f"Downloading this clip (+{SECTION_PAD}s before and after)…", percent=10)
                parts = self.ensure_parts(job, [clip])
                part = find_part(parts, clip)
                if part is None:
                    raise RuntimeError("This clip is outside the downloaded parts.")
                source, cut_start, cut_end = part["path"], clip.start - part["start"], clip.end - part["start"]
            state.update(message="Cutting the clip…", percent=60)
            folder = OUTPUT_DIR / safe_name(session.title or "clips", 80)
            folder.mkdir(parents=True, exist_ok=True)
            out_path = unique_path(folder / f"{clip.rank:02d}_{safe_name(clip.title, 70)}.mp4")
            cut_clip(source, out_path, cut_start, cut_end, clip_output_size(clip, job.ratio), job.cancel,
                     clip.crop_x, clip.crop_y, clip.split if clip.layout == "split" else None, clip.texts)
            state.update(status="done", percent=100, message="Saved", path=str(out_path), name=out_path.name)
            self.log(f"Clip #{clip.rank} downloaded from the editor: {out_path}", "ok")
        except JobCancelled:
            state["error"] = "Cancelled."
        except Exception as exc:
            state["error"] = friendly_error(exc)
            self.log(f"Download of clip #{clip.rank} failed: {friendly_error(exc)}", "error")

    def close_editor(self):
        server, self.editor_server = self.editor_server, None
        self.editor_session = None
        if server is not None:
            threading.Thread(target=server.close, daemon=True).start()

    def cancel_job(self):
        if self.app_state not in ("running", "review") or not self.job:
            return
        if not messagebox.askyesno("Cancel job", "Stop the current job?"):
            return
        self.job.cancel.set()
        self.close_editor()
        if self.active_dialog is not None:
            self.active_dialog.close(None)
        if self.review_waiter is not None:
            self.review_waiter.resolve(None)
        self.set_status_now("Cancelling…")

    def uploading(self):
        return bool(self.upload_panel and self.upload_panel.running)

    def reset(self):
        if self.app_state not in ("idle", "finished"):
            return
        if self.uploading():
            messagebox.showinfo("Upload in progress", "Wait for the YouTube upload to finish (or cancel it in the "
                                                      "Upload tab) before starting a new video.")
            return
        self.url_entry.delete(0, "end")
        self.quality_var.set("720p or lower")
        self.ratio_var.set("Vertical 9:16 (Reels / Shorts)")
        self.duration_var.set("20–25 seconds")
        self.instructions_box.delete("1.0", "end")
        self.instructions_box.insert("1.0", DEFAULT_INSTRUCTIONS)
        self.clear_results()
        self.app_state = "idle"
        self.set_status_now("Ready for the next video")
        self.refresh_buttons()
        self.url_entry.focus_set()

    def clear_results(self):
        self.post_context = None
        self.posts = {}
        self.post_all_btn.configure(state="disabled")
        for card in self.cards:
            card.destroy()
        self.cards = []
        self.empty_label.grid(row=0, column=0, pady=90)
        set_textbox(self.json_box, "")
        set_textbox(self.log_box, "")
        if self.upload_panel:
            self.upload_panel.clear()
        for i in range(len(STEPS)):
            self.apply_step(i, "pending")
        self.progress.set(0)
        self.tabs.set("Clips")

    def set_status_now(self, text):
        self.status_var.set(text)

    def on_close(self):
        if self.uploading():
            if not messagebox.askyesno("Quit", "A YouTube upload is still running. Stop it and quit?"):
                return
            self.upload_panel.cancel.set()
        if self.app_state in ("running", "review"):
            if not messagebox.askyesno("Quit", "A job is still running. Stop it and quit?"):
                return
            if self.job:
                self.job.cancel.set()
        self._alive = False
        self.close_editor()
        self.destroy()

    # --------------------------------------------------------- event handling
    def apply_step(self, index, state):
        bg, fg = self.STEP_COLORS[state]
        mark = {"done": "✓ ", "failed": "✕ "}.get(state, "")
        self.step_labels[index].configure(fg_color=bg, text_color=fg, text=f"{mark}{index + 1}. {STEPS[index]}")

    def append_log(self, message, level):
        stamp = time.strftime("%H:%M:%S")
        tag = {"warn": "warn", "error": "error", "ok": "ok"}.get(level)
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{stamp}  {message}\n", tag) if tag else \
            self.log_box.insert("end", f"{stamp}  {message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def show_clips(self, clips):
        for card in self.cards:
            card.destroy()
        self.cards = []
        self.empty_label.grid_remove()
        for i, clip in enumerate(clips):
            card = ClipCard(self.clips_frame, clip)
            card.grid(row=i, column=0, sticky="ew", pady=(0, 10), padx=(0, 6))
            self.cards.append(card)
        self.post_all_btn.configure(state="normal" if self.cards else "disabled")
        self.tabs.set("Clips")

    def open_post_copy(self):
        if not self.post_context or not self.cards:
            messagebox.showinfo("Not ready", "The clips are not ready yet. Try again in a moment.")
            return
        if self.post_dialog is not None:
            try:
                self.post_dialog.lift()
                self.post_dialog.focus_force()
            except tk.TclError:
                self.post_dialog = None
            else:
                return

        def done(_value):
            self.post_dialog = None

        self.post_dialog = PostCopyDialog(self, done)

    def open_dialog(self, kind, payload, waiter):
        def finish(value):
            self.active_dialog = None
            waiter.resolve(value)

        if kind == "transcript":
            self.active_dialog = TranscriptDialog(self, payload["reason"], payload.get("error"), finish)
        elif kind == "manual_ai":
            self.active_dialog = ManualGeminiDialog(
                self, payload["reason"], payload["folder"], payload["prompt"], payload["validator"], finish)

    def process_events(self):
        if not self._alive:
            return
        try:
            while True:
                kind, value = self.events.get_nowait()
                try:
                    self.handle_event(kind, value)
                except Exception:
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.after(100, self.process_events)

    def handle_event(self, kind, value):
        if kind == "log":
            self.append_log(*value)
        elif kind == "status":
            self.status_var.set(value)
        elif kind == "progress":
            self.progress.set(max(0.0, min(1.0, value / 100)))
        elif kind == "step":
            self.apply_step(*value)
        elif kind == "clips":
            self.show_clips(value)
        elif kind == "json":
            set_textbox(self.json_box, value)
        elif kind == "posts":
            self.posts.update(value)
        elif kind == "state":
            self.app_state = value
            self.refresh_buttons()
        elif kind == "dialog":
            self.open_dialog(*value)
        elif kind == "clip_status":
            index, text, color, path = value
            if index < len(self.cards):
                self.cards[index].set_status(text, color, path)
        elif kind == "open_editor":
            self.open_editor()
        elif kind == "sync_cards":
            clips, waiter = value
            self.show_clips(clips)
            waiter.resolve(True)
        elif kind == "finished":
            self.finish_job(value)

    def finish_job(self, result):
        self.close_editor()
        self.app_state = "finished"
        self.review_waiter = None
        self.refresh_buttons()
        if result.get("folder"):
            self.last_output_folder = result["folder"]
        if result.get("ok"):
            self.progress.set(1)
            self.set_status_now("Done")
            hint = ""
            if self.upload_panel and result.get("saved"):
                self.upload_panel.load(result["saved"], source=str(result.get("folder", "")))
                self.log("Clips are listed in the Upload tab (step 6). Nothing is uploaded until you press Upload.")
                hint = "\n\nWant them on YouTube? Open the Upload tab (step 6)."
            if messagebox.askyesno("Clips ready", result["message"] + hint + "\n\nOpen the output folder now?"):
                open_path(result["folder"])
            if result.get("open_posts"):
                self.open_post_copy()
        elif result.get("cancelled"):
            self.set_status_now("Cancelled")
        else:
            self.set_status_now("Stopped. See the message for what to do next.")
            messagebox.showerror("Something went wrong", result["message"])

    # --------------------------------------------------------------- pipeline
    def run_job(self, job):
        try:
            title, duration, transcript_text, info = self.step_info(job)
            transcript_text = self.step_transcript(job, info)
            self.post_context = {"title": title, "url": job.url, "transcript": transcript_text}
            job.chunks = split_transcript(transcript_text, duration)
            if not job.chunks:
                raise UserFacingError("The transcript has no timestamped lines the AI can read.")
            self.log(f"The transcript is sent to the AI in {len(job.chunks)} part{'s' if len(job.chunks) != 1 else ''}: "
                     + ", ".join(chunk_label(c) for c in job.chunks) + ".")
            if job.auto_create:
                clips = self.step_ai_all(job, title, duration, job.chunks)
            else:
                answer = self.step_review(job, info, title, duration, job.chunks)
                if not answer:
                    raise JobCancelled()
                kind, value = answer
                if kind == "selected":
                    clips = [self.found_clips[i] for i in value]
                else:  # clips edited in the editor
                    clips = value
                    self.sync_cards(clips)
            folder, ok, failed, saved = self.step_download_and_cut(job, title, clips)

            lines = [f"Clips created: {ok}"]
            if failed:
                lines.append(f"Failed: {failed} (see the Log tab)")
            lines.append(f"\nSaved in:\n{folder}")
            open_posts = False
            if ok and job.auto_post:
                open_posts, note = self.step_post_copy(job, title, folder, saved, transcript_text)
                lines.append(f"\n{note}")
            result = {"ok": ok > 0, "folder": folder, "message": "\n".join(lines), "open_posts": open_posts,
                      "saved": saved}
            if ok == 0:
                result["message"] = "No clip could be created. Open the Log tab to see the FFmpeg messages."
            self.events.put(("finished", result))
        except JobCancelled:
            self.mark_step_failed()
            self.log("Job cancelled.", "warn")
            self.events.put(("finished", {"cancelled": True}))
        except Exception as exc:
            self.mark_step_failed()
            message = friendly_error(exc)
            self.log(f"ERROR: {message}", "error")
            if not isinstance(exc, UserFacingError):
                self.log(traceback.format_exc(), "error")
            self.events.put(("finished", {"ok": False, "message": message}))

    def mark_step_failed(self):
        if self._step_index is not None and self._step_state == "active":
            self.set_step(self._step_index, "failed")

    # step 1 ---------------------------------------------------------------
    def step_info(self, job):
        self.set_step(0, "active")
        self.set_status("Reading video information…")
        self.set_progress(2)
        info = fetch_video_info(job.url)
        if job.cancel.is_set():
            raise JobCancelled()

        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise UserFacingError("This is a live stream or an upcoming premiere. Wait until it has "
                                  "finished, then paste the link again.")
        title = info.get("title") or "YouTube video"
        duration = info.get("duration")
        job.video_duration = duration
        if duration and job.clip_parts_only and duration < LONG_VIDEO_SECONDS:
            job.clip_parts_only = False  # short video: edit straight from YouTube, download only after Generate
            self.log(f"Short video (under {LONG_VIDEO_SECONDS // 60} min): the editor plays it from YouTube, "
                     "nothing is downloaded until you press Generate.")
        self.log(f"Video: {title}")
        if duration:
            self.log(f"Length: {format_timestamp(duration)}")
            if duration < job.duration_range[0]:
                raise UserFacingError(
                    f"This video is only {duration:.0f} seconds long, shorter than the minimum clip length "
                    f"({job.duration_range[0]}s). Choose a shorter clip length.")
            if duration > 4 * 3600:
                self.log("This is a very long video. Download and AI analysis may take a while.", "warn")
        self.set_step(0, "done")
        self.set_progress(8)
        return title, duration, None, info

    # step 2 ---------------------------------------------------------------
    def step_transcript(self, job, info):
        self.set_step(1, "active")
        self.set_status("Looking for captions…")
        path = None
        try:
            path = download_transcript(job.url, info, self.log, job.cancel)
        except JobCancelled:
            raise
        except Exception as exc:
            self.log(f"Automatic caption download failed: {friendly_error(exc)}", "warn")

        if path:
            try:
                text = parse_transcript_text(read_text_file(path))
                self.log(f"Transcript ready: {path.name} ({len(text.splitlines())} lines)", "ok")
                self.set_step(1, "done")
                self.set_progress(18)
                return text
            except (ValueError, OSError) as exc:
                self.log(f"Downloaded captions could not be used: {exc}", "warn")

        self.set_status("Waiting for a transcript from you…")
        reason = ("YouTube captions could not be downloaded automatically. Choose a timestamped transcript "
                  "file, or paste the transcript text.")
        error = None
        while True:
            answer = self.ask("transcript", reason=reason, error=error)
            if not answer:
                raise UserFacingError("No transcript was provided, so the job was stopped.")
            kind, value = answer
            try:
                raw = read_text_file(value) if kind == "file" else value
                text = parse_transcript_text(raw)
                break
            except (ValueError, OSError) as exc:
                error = str(exc)
                self.log(f"Transcript rejected: {error}", "warn")
        self.log(f"Transcript ready ({len(text.splitlines())} lines).", "ok")
        self.set_step(1, "done")
        self.set_progress(18)
        return text

    # step 3 ---------------------------------------------------------------
    def find_clips(self, job, title, duration, chunk, total):
        """The AI step for ONE transcript part: Gemini first, the manual Gemini chat as the fallback.
        -> clips ranked 1..n"""
        part = {"index": chunk["index"], "total": total, "start": chunk["start"], "end": chunk["end"]}
        required = clips_wanted(chunk)
        reason = None
        if not Config.api_key:
            reason = "No Gemini API key is set, so Gemini could not be called."
            self.log(reason, "warn")
        else:
            try:
                return self.ask_gemini(job, title, duration, chunk["text"], part, required)
            except JobCancelled:
                raise
            except Exception as exc:
                reason = f"Gemini could not produce valid clips. {friendly_error(exc)}"
                self.log(f"AI step failed: {reason}", "error")
        return self.manual_gemini(job, title, duration, chunk["text"], reason, part, required)

    def step_ai_all(self, job, title, duration, chunks):
        """'Skip the editor' mode: every transcript part, one after the other, without waiting for the user."""
        self.set_step(2, "active")
        clips = []
        for chunk in chunks:
            if job.cancel.is_set():
                raise JobCancelled()
            self.log(f"Transcript part {chunk['index'] + 1} of {len(chunks)} ({chunk_label(chunk)}).")
            new = self.find_clips(job, title, duration, chunk, len(chunks))
            for number, clip in enumerate(new, 1):
                clip.rank = len(clips) + number
            clips += new
            self.log(f"Part {chunk['index'] + 1}: {len(new)} clips found, {len(clips)} in total.", "ok")
        self.publish_clips(job, title, clips)
        self.log(f"Transcript done: {len(chunks)} part{'s' if len(chunks) != 1 else ''} analysed, "
                 f"{len(clips)} clips.", "ok")
        self.set_step(2, "done")
        self.set_progress(30)
        return clips

    def publish_clips(self, job, title, clips):
        """Show the clips found so far in the Clips and JSON tabs."""
        self.found_clips = list(clips)
        payload = {
            "video_title": title,
            "source_url": job.url,
            "viral_clips": [c.to_json() for c in clips],
        }
        self.events.put(("json", json.dumps(payload, ensure_ascii=False, indent=2)))
        self.events.put(("clips", list(clips)))

    def ask_gemini(self, job, title, duration, transcript_text, part=None, required=REQUIRED_CLIPS):
        rng = job.duration_range
        window = (part["start"], part["end"]) if part else None
        where = f" (part {part['index'] + 1} of {part['total']})" if part else ""
        feedback = None
        last_error = None
        for attempt in range(1, MAX_AI_ATTEMPTS + 1):
            if job.cancel.is_set():
                raise JobCancelled()
            self.set_status(f"Asking Gemini… (attempt {attempt} of {MAX_AI_ATTEMPTS})")
            self.set_progress(20 + attempt * 2)
            self.log(f"Sending the transcript{where} to Gemini ({Config.model}), attempt {attempt}.")
            prompt = build_prompt(title, job.url, duration, rng, job.instructions,
                                  transcript_text=transcript_text, feedback=feedback, part=part, count=required)
            try:
                text = call_gemini(prompt)
                raw_clips = extract_raw_clips(text)
                clips, problems = build_clips(raw_clips, duration, rng, required=required, window=window)
                if len(clips) == required:
                    return clips
                feedback = " ".join(problems[:4]) or f"Only {len(clips)} usable clips were returned."
                raise ValueError(feedback)
            except JobCancelled:
                raise
            except Exception as exc:
                last_error = exc
                self.log(f"Attempt {attempt} failed: {friendly_error(exc)}", "warn")
                if is_fatal_ai_error(exc):
                    break
                if attempt < MAX_AI_ATTEMPTS:
                    delay = 3 * attempt
                    self.log(f"Retrying in {delay}s…")
                    if job.cancel.wait(delay):
                        raise JobCancelled()
        raise last_error or RuntimeError("Gemini did not return usable clips.")

    def manual_gemini(self, job, title, duration, transcript_text, reason, part=None, required=REQUIRED_CLIPS):
        """Fallback: save transcript + prompt, let the user run Gemini chat, paste JSON back.
        With a transcript part, only that part is saved (folder '<title> - part N')."""
        rng = job.duration_range
        window = (part["start"], part["end"]) if part else None
        name = safe_name(title, 60) + (f" - part {part['index'] + 1}" if part else "")
        folder = MANUAL_DIR / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "transcript.txt").write_text(transcript_text, encoding="utf-8")
        prompt = build_prompt(title, job.url, duration, rng, job.instructions, transcript_text=None,
                              part=part, count=required)
        (folder / "prompt.txt").write_text(prompt, encoding="utf-8")
        self.log(f"Manual Gemini files saved in {folder}", "warn")

        def validator(text, parent):
            try:
                raw_clips = extract_raw_clips(text)
            except ValueError as exc:
                return None, str(exc)
            clips, problems = build_clips(raw_clips, duration, rng, required=required, window=window)
            if len(clips) == required:
                return clips, None
            if not clips:
                return None, "No usable clips in that answer.\n" + "\n".join(problems[:5])
            if messagebox.askyesno(
                    "Some clips are not usable",
                    f"{len(clips)} of {required} clips are usable.\n\n" + "\n".join(problems[:5]) +
                    "\n\nContinue with the usable clips only?\n"
                    "Choose No to ask Gemini again and paste a new answer.",
                    parent=parent):
                return clips, None
            return None, "\n".join(problems[:5])

        self.set_status("Waiting for Gemini JSON from you…")
        clips = self.ask("manual_ai", reason=reason or "Gemini could not be used.", folder=folder,
                         prompt=prompt, validator=validator)
        if not clips:
            raise UserFacingError("No clips were provided, so the job was stopped.")
        return clips

    # review ---------------------------------------------------------------
    def step_review(self, job, info, title, duration, chunks):
        """Open the editor with NO clips yet: the AI is asked from inside the editor, one transcript part at a time."""
        video_path = None
        if job.download_first:  # download now, so the editor can play the file instead of YouTube
            self.events.put(("state", "running"))
            self.set_step(3, "active")
            self.set_status("Downloading the video so you can edit from the file…")
            video_path, _info = self.ensure_video(job)
            self.set_step(3, "done")
            self.log("Editing from the downloaded video.", "ok")
            try:
                duration = probe_duration(video_path, job.cancel)
            except JobCancelled:
                raise
            except Exception:
                pass
        waiter = Waiter()
        self.review_waiter = waiter
        self.found_clips = []
        self.editor_session = EditorSession(
            info.get("id", ""), title, duration, info.get("playable_in_embed") is not False,
            [], job.ratio, video_path, None, chunks)
        self.events.put(("state", "review"))
        self.events.put(("open_editor", None))
        self.set_status("In the editor press 'Find clips' to send the first part of the transcript to the AI, "
                        "then 'Find more clips' for the next parts. Press 'Generate clips' there when you are done.")
        self.log("Waiting for you in the clip editor.")
        return self.wait_for(waiter)

    def ensure_video(self, job, on_status=None, on_progress=None):
        """Download the video once. Later callers (editor button, create clips) reuse the same file."""
        key = (job.url, job.quality)
        with self.video_lock:
            cached = self.video_cache
            if cached and cached["key"] == key and Path(cached["path"]).is_file():
                return cached["path"], cached["info"]
            self.log("Starting video download.")
            self.set_status("Downloading video…")

            def status(text):
                self.set_status(text)
                if on_status:
                    on_status(text)

            def progress(value):
                self.set_progress(value)
                if on_progress:
                    on_progress(max(0, min(100, (value - 30) / 40 * 100)))

            path, info = download_video(job.url, job.quality, self.log, status, progress, job.cancel)
            self.log(f"Video ready: {path.name}", "ok")
            self.video_cache = {"key": key, "path": path, "info": info}
            return path, info

    def ensure_parts(self, job, clips):
        """Download only the clip parts (each clip + SECTION_PAD seconds either side). Reused if already downloaded."""
        usable = fit_to_duration(clips, job.video_duration)[0] if job.video_duration else list(clips)
        ranges = plan_sections(usable, job.video_duration)
        if not ranges:
            raise UserFacingError("None of the clips fit inside the video.")
        key = (job.url, job.quality, tuple(ranges))
        with self.video_lock:
            cached = self.parts_cache
            if (cached and cached["key"][:2] == (job.url, job.quality)
                    and all(Path(p["path"]).is_file() for p in cached["parts"])
                    and all(parts_cover(cached["parts"], clip) for clip in usable)):
                return cached["parts"]  # already downloaded: the clips (even edited ones) fit inside
            wanted = sum(b - a for a, b in ranges)
            whole = f" instead of the whole {format_timestamp(job.video_duration)}" if job.video_duration else ""
            self.log(f"Downloading only the clips: {len(ranges)} part(s), {format_timestamp(wanted)} in total{whole} "
                     f"(each clip with {SECTION_PAD}s before and after).")
            for begin, finish in ranges:
                self.log(f"  part {format_timestamp(begin)} to {format_timestamp(finish)}")
            self.set_status("Downloading the clip parts…")
            parts, _info = download_clip_parts(job.url, job.quality, ranges, self.log, self.set_status,
                                               self.set_progress, job.cancel)
            self.log("Clip parts ready.", "ok")
            self.parts_cache = {"key": key, "parts": parts}
            return parts

    def editor_action(self, action):
        """Called from the editor's web server thread: 'download' the video or 'preview' (smaller copy)."""
        session, job = self.editor_session, self.job
        if session is None or job is None or self.app_state != "review":
            raise ValueError("The app is not waiting for the editor right now.")
        if session.task.get("name"):
            raise ValueError("Please wait, the app is still working on the video.")
        if action == "find":
            if session.more.get("busy"):
                raise ValueError("The AI is already looking for clips.")
            if session.next_chunk >= len(session.chunks):
                raise ValueError("The whole transcript has been analysed already.")
            session.more.update(busy=True, message="Starting…", error="")
            threading.Thread(target=self._editor_find_worker, args=(session, job), daemon=True).start()
            return
        if action == "download":
            if session.video_path or session.parts:
                raise ValueError("The video is already downloaded.")
            target = self._editor_download
        elif action == "preview":
            if not (session.video_path or session.parts):
                raise ValueError("Download the video first.")
            target = self._editor_preview
        else:
            raise ValueError("Unknown action.")
        session.set_task(name=action, percent=0, message="Starting…", error="")
        threading.Thread(target=target, args=(session, job), daemon=True).start()

    def _editor_find_worker(self, session, job):
        """'Find clips' in the editor: send the NEXT transcript part to the AI, then add the clips to the editor."""
        index = session.next_chunk
        chunk, total = session.chunks[index], len(session.chunks)
        label = chunk_label(chunk)
        try:
            self.set_step(2, "active")
            self.log(f"Transcript part {index + 1} of {total} ({label}) goes to the AI.")
            if session.pending and session.pending[0] == index:  # found before, only the download failed
                new = session.pending[1]
            else:
                session.more.update(message=f"Asking the AI about part {index + 1} of {total} ({label})…")
                new = self.find_clips(job, session.title, job.video_duration or session.duration, chunk, total)
                for number, clip in enumerate(new, 1):
                    clip.rank = len(session.clips) + number
                session.pending = (index, new)
            parts = None
            cached = self.video_cache
            have_full = bool(session.video_path or (cached and cached["key"] == (job.url, job.quality)
                                                    and Path(cached["path"]).is_file()))
            if job.clip_parts_only and not have_full:  # long video: download only these clips (+ padding)
                session.more.update(message="Downloading only these clips (+10 s before and after)…")
                self.set_step(3, "active")
                parts = self.add_parts(job, session, new)
                self.set_step(3, "done")
            with session.lock:
                session.clips.extend(new)
                if parts is not None:
                    session.parts = parts
                    session.version += 1
                session.next_chunk = index + 1
                session.clips_version += 1
                session.pending = None
            self.publish_clips(job, session.title, session.clips)
            finished = session.next_chunk >= total
            self.log(f"Part {index + 1} of {total}: {len(new)} clips added, {len(session.clips)} in the editor."
                     + (" Transcript done." if finished else ""), "ok")
            self.set_step(2, "done")
            session.more.update(busy=False, message="", error="")
            self.set_status("Transcript done. Edit the clips, then press 'Generate clips' in the editor."
                            if finished else
                            f"{len(session.clips)} clips so far. Press 'Find more clips' in the editor for the next part.")
        except JobCancelled:
            session.more.update(busy=False, message="", error="Cancelled.")
        except Exception as exc:
            message = friendly_error(exc)
            self.log(f"Finding clips in part {index + 1} failed: {message}", "error")
            self.set_step(2, "failed")
            session.more.update(busy=False, message="", error=message + " Press the button to try again.")

    def add_parts(self, job, session, new_clips):
        """Download only the parts of the video that the NEW clips need (+SECTION_PAD s each side) and add them to
        the parts that are already downloaded. -> the complete, sorted list of parts"""
        usable = fit_to_duration(new_clips, job.video_duration)[0] if job.video_duration else list(new_clips)
        old = list(session.parts)
        missing = [clip for clip in usable if not parts_cover(old, clip)]
        ranges = plan_sections(missing, job.video_duration)
        if usable and not ranges and not old:
            raise UserFacingError("None of the new clips fit inside the video.")
        added = []
        if ranges:
            wanted = sum(b - a for a, b in ranges)
            self.log(f"Downloading only the new clips: {len(ranges)} part(s), {format_timestamp(wanted)} in total "
                     f"(each clip with {SECTION_PAD}s before and after).")
            for begin, finish in ranges:
                self.log(f"  part {format_timestamp(begin)} to {format_timestamp(finish)}")
            self.set_status("Downloading the clip parts…")
            added, _info = download_clip_parts(job.url, job.quality, ranges, self.log, self.set_status,
                                               self.set_progress, job.cancel)
            self.log("Clip parts ready.", "ok")
        merged = sorted(old + added, key=lambda part: part["start"])
        with self.video_lock:
            self.parts_cache = {"key": (job.url, job.quality, tuple((p["start"], p["end"]) for p in merged)),
                                "parts": merged}
        return merged

    def _editor_download(self, session, job):
        try:
            path, _info = self.ensure_video(
                job, lambda text: session.set_task(message=text), lambda p: session.set_task(percent=int(p)))
            try:
                session.duration = probe_duration(path, job.cancel)
            except JobCancelled:
                raise
            except Exception:
                pass
            session.video_path = path
            session.version += 1
            session.set_task(name="", percent=100, message="", error="")
            self.log("Video downloaded. The editor now plays the file.", "ok")
            self.set_status("Video downloaded. Keep editing in the editor, then press 'Generate clips'.")
        except JobCancelled:
            session.set_task(name="", message="", error="Cancelled.")
        except Exception as exc:
            session.set_task(name="", message="", error=friendly_error(exc))
            self.log(f"Download for the editor failed: {friendly_error(exc)}", "error")

    def _editor_preview(self, session, job):
        try:
            session.set_task(message="Making a smaller preview copy…")
            if session.parts:  # one preview copy per downloaded clip part
                count = len(session.parts)
                for number, part in enumerate(session.parts):
                    session.set_task(message=f"Making a smaller preview copy ({number + 1} of {count})…")
                    part["preview"] = make_preview(
                        part["path"], job.cancel,
                        lambda p, n=number: session.set_task(percent=int((n + p / 100) / count * 100)))
            else:
                session.preview_path = make_preview(
                    session.video_path, job.cancel, lambda p: session.set_task(percent=int(p)))
            session.version += 1
            session.set_task(name="", percent=100, message="", error="")
            self.log("Preview copy ready.", "ok")
        except JobCancelled:
            session.set_task(name="", message="", error="Cancelled.")
        except Exception as exc:
            session.set_task(name="", message="", error=friendly_error(exc))
            self.log(f"Preview copy failed: {friendly_error(exc)}", "error")

    def sync_cards(self, clips):
        """Rebuild the clip cards on the UI thread so they match the edited clips."""
        waiter = Waiter()
        self.events.put(("sync_cards", (clips, waiter)))
        self.wait_for(waiter)

    # steps 4 and 5 ---------------------------------------------------------
    def step_download_and_cut(self, job, title, clips):
        self.events.put(("state", "running"))
        self.set_step(3, "active")
        cached = self.video_cache
        have_full = bool(cached and cached["key"] == (job.url, job.quality) and Path(cached["path"]).is_file())
        parts = None
        if job.clip_parts_only and clips and not have_full:
            parts = self.ensure_parts(job, clips)  # only the clips (+ padding), not the whole video
            video_path, dl_info = None, {}
        else:
            video_path, dl_info = self.ensure_video(job)
        self.set_step(3, "done")
        self.set_progress(70)

        if parts is not None:
            true_duration = job.video_duration
        else:
            try:
                true_duration = probe_duration(video_path, job.cancel)
            except JobCancelled:
                raise
            except Exception as exc:
                self.log(f"Could not measure the video length ({exc}). Using the length from YouTube.", "warn")
                true_duration = float(dl_info.get("duration") or 0) or None

        # Map clip -> its card index (cards were built from the full list)
        index_by_rank = {card.clip.rank: i for i, card in enumerate(self.cards)}
        if true_duration:
            fitted, dropped = fit_to_duration(clips, true_duration)
            for clip in dropped:
                self.log(f"Skipped '{clip.title}': it is outside the video length.", "warn")
                self.events.put(("clip_status", (index_by_rank.get(clip.rank, 0), "Skipped (outside video)",
                                                 WARN, None)))
            clips = fitted
        if not clips:
            raise UserFacingError("None of the clips fit inside the downloaded video.")

        self.set_step(4, "active")
        folder = OUTPUT_DIR / safe_name(title, 80)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise UserFacingError(f"Cannot create the output folder:\n{folder}\n{exc}")

        ok = failed = 0
        saved = []  # (clip, mp4 path) of every clip that was created
        for position, clip in enumerate(clips, 1):
            if job.cancel.is_set():
                raise JobCancelled()
            card_index = index_by_rank.get(clip.rank, position - 1)
            clip_title = safe_name(clip.title, 70)
            out_path = unique_path(folder / f"{clip.rank:02d}_{clip_title}.mp4")
            self.set_status(f"Creating clip {position} of {len(clips)}: {clip.title}")
            self.events.put(("clip_status", (card_index, "Creating…", ACCENT, None)))
            self.log(f"[{position}/{len(clips)}] {clip.title} ({clip.start:.1f}s to {clip.end:.1f}s)")
            try:
                source, cut_start, cut_end = video_path, clip.start, clip.end
                if parts is not None:  # times inside the downloaded part start at 0
                    part = find_part(parts, clip)
                    if part is None:
                        raise RuntimeError("This clip is outside the downloaded parts.")
                    source, cut_start, cut_end = part["path"], clip.start - part["start"], clip.end - part["start"]
                cut_clip(source, out_path, cut_start, cut_end,
                         clip_output_size(clip, job.ratio), job.cancel, clip.crop_x, clip.crop_y,
                         clip.split if clip.layout == "split" else None, clip.texts)
                ok += 1
                self.log(f"  Saved {out_path.name}", "ok")
                saved.append((clip, out_path))
                self.events.put(("clip_status", (card_index, "Saved", GOOD, out_path)))
            except JobCancelled:
                raise
            except Exception as exc:
                failed += 1
                self.log(f"  Failed: {exc}", "error")
                self.events.put(("clip_status", (card_index, f"Failed: {exc}"[:90], BAD, None)))
                try:
                    out_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self.set_progress(70 + position / len(clips) * 30)

        self.set_step(4, "done")
        self.set_status("Completed")
        return folder, ok, failed, saved

    # step 6 (optional): titles and hashtags for every clip, one Gemini call ---
    def step_post_copy(self, job, title, folder, saved, transcript_text):
        """-> (open the post window?, one line for the final message)"""
        clips = [clip for clip, _path in saved]
        self.set_status("Writing titles & hashtags for all clips…")
        self.log(f"Writing titles and hashtags for {len(clips)} clips (one Gemini call).")
        try:
            if not Config.api_key:
                raise UserFacingError("No Gemini API key is set.")
            posts = generate_social_copy_all(title, clips, transcript_text)
            items = []
            for clip, path in saved:
                copy = posts.get(clip.rank)
                if copy:
                    items.append((clip, path) + format_post(copy, job.url))
            if not items:
                raise UserFacingError("Gemini returned no usable posts.")
            self.events.put(("posts", posts))
            target = save_post_files(folder, items)
        except JobCancelled:
            raise
        except Exception as exc:
            self.log(f"Titles & hashtags were not written: {friendly_error(exc)}", "warn")
            return False, ("Titles & hashtags: not written yet. Use the button in the Clips tab to try again "
                           "or to finish by hand in the Gemini chat.")
        self.log(f"Titles & hashtags ready. Saved {target.name} in the output folder.", "ok")
        self.set_status("Completed")
        return True, "Titles & hashtags for all clips are ready (also saved as posts.txt)."


def main():
    if "--editor-window" in sys.argv:  # child process started by open_editor_window()
        run_editor_window(sys.argv[sys.argv.index("--editor-window") + 1])
        return
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    app = ViralClipsApp()
    app.mainloop()


if __name__ == "__main__":
    main()