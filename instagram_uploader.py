"""
Instagram Reels uploader for Viral Clips Studio
===============================================

Standalone module, same idea as youtube_uploader.py: main.py embeds make_instagram_panel_class() as the
"Instagram" tab. Every selected clip is posted to your Instagram account as a Reel with:

  * caption + hashtags  -> taken from the "INSTAGRAM CAPTION" part of the Gemini post copy saved next to each
                           clip (the per-clip .txt file); falls back to the clip's own title
  * share to feed       -> on by default (configurable)

It uses the official Instagram Graph API (Reels publishing with resumable upload), so the video file is sent
straight from your computer - you do NOT need to host it anywhere.

It also works on its own from the command line:

    python instagram_uploader.py output/<Video Title>            # post every .mp4 in a folder
    python instagram_uploader.py clip1.mp4 clip2.mp4 --yes
    python instagram_uploader.py --check                         # test your token + account, then exit

IMPORTANT: unlike YouTube there is no "private" mode. A Reel is PUBLIC as soon as it is published.

One-time setup: see SETUP_HELP below (or INSTAGRAM_SETUP.md):
    pip install requests
    + an Instagram Business/Creator account, a Meta app, an access token and your Instagram user id in .env
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

try:  # python-dotenv is already a dependency of main.py; optional here
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_LOG_NAME = "instagram_uploads.json"  # written inside each output folder, so a clip is never posted twice
INSTAGRAM_URL = "https://www.instagram.com/"

CAPTION_MAX = 2200  # Instagram caption limit (characters)
HASHTAGS_MAX = 30  # Instagram allows at most 30 hashtags per post
DEFAULT_API_VERSION = "v23.0"  # change with INSTAGRAM_API_VERSION in .env if Meta retires this one
DEFAULT_GRAPH_HOST = "https://graph.facebook.com"  # use https://graph.instagram.com for "Instagram Login" tokens
PROCESS_TIMEOUT = 15 * 60  # seconds to wait for Instagram to finish processing one video
POLL_EVERY = 4  # seconds between status checks
MAX_RETRIES = 4
REQUEST_TIMEOUT = 60
UPLOAD_TIMEOUT = (30, 600)  # (connect, read) seconds for the actual video upload

DEPENDENCIES = {"requests": "requests"}  # import name -> pip name
PIP_COMMAND = "pip install requests"

SETUP_HELP = (
    "Instagram upload needs a one-time setup:\n\n"
    "1. Your Instagram account must be a Business or Creator account (Instagram app > Settings > Account type).\n"
    "2. Link it to a Facebook Page (Instagram app > Settings > Account Center, or Page settings > Linked accounts).\n"
    "3. Open https://developers.facebook.com/apps/ > Create app (type: Business) and add the product "
    "\"Instagram\" (API setup with Facebook login).\n"
    "4. In Graph API Explorer pick your app, add the permissions instagram_basic, instagram_content_publish and "
    "pages_show_list (plus pages_read_engagement), then Generate Access Token and log in with the account that "
    "manages the Page.\n"
    "5. Exchange it for a long-lived token (valid ~60 days). INSTAGRAM_SETUP.md shows the one URL for this.\n"
    "6. Find your Instagram user id (a long number): open\n"
    "   /me/accounts?fields=instagram_business_account in Graph API Explorer.\n"
    "7. Put both in the .env file next to this script:\n"
    "      INSTAGRAM_ACCESS_TOKEN=your_long_lived_token\n"
    "      INSTAGRAM_USER_ID=1784xxxxxxxxxxxxx\n\n"
    f"Folder: {BASE_DIR}\n\n"
    "Then press \"Test connection\". Reels are PUBLIC as soon as they are posted."
)

FRIENDLY_CODES = {  # Graph API error code -> message
    190: "The Instagram access token is invalid or expired. Generate a new long-lived token and update "
         "INSTAGRAM_ACCESS_TOKEN in .env.",
    102: "The Instagram access token expired. Generate a new one and update INSTAGRAM_ACCESS_TOKEN in .env.",
    10: "The token is missing a permission. It needs instagram_basic and instagram_content_publish.",
    200: "The token is missing a permission. It needs instagram_basic and instagram_content_publish.",
    4: "Instagram says you made too many requests. Wait a while and try again.",
    17: "Instagram says you made too many requests. Wait a while and try again.",
    32: "Instagram says you made too many requests. Wait a while and try again.",
    613: "Instagram says you made too many requests. Wait a while and try again.",
    9: "You have reached Instagram's publishing limit for now. Try again later (limit: about 100 posts per day).",
}


class UploaderError(Exception):
    """An error whose message can be shown to the user as is."""


class UploadLimitError(UploaderError):
    """Rate / publishing limit reached: no point trying the remaining clips."""


class UploadCancelled(Exception):
    pass


# --------------------------------------------------------------------------- #
# Settings and data
# --------------------------------------------------------------------------- #
def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class UploadSettings:
    access_token: str = ""
    user_id: str = ""
    api_version: str = DEFAULT_API_VERSION
    graph_host: str = DEFAULT_GRAPH_HOST
    share_to_feed: bool = True  # also show the Reel on your profile grid (feed)
    auto_after_youtube: bool = False  # post automatically when the YouTube upload has finished

    @classmethod
    def from_env(cls):
        return cls(
            access_token=os.environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip(),
            user_id=os.environ.get("INSTAGRAM_USER_ID", "").strip(),
            api_version=os.environ.get("INSTAGRAM_API_VERSION", DEFAULT_API_VERSION).strip() or DEFAULT_API_VERSION,
            graph_host=(os.environ.get("INSTAGRAM_GRAPH_HOST", DEFAULT_GRAPH_HOST).strip()
                        or DEFAULT_GRAPH_HOST).rstrip("/"),
            share_to_feed=_env_bool("INSTAGRAM_SHARE_TO_FEED", True),
            auto_after_youtube=_env_bool("INSTAGRAM_AUTO_AFTER_YOUTUBE", False),
        )

    def configured(self):
        return bool(self.access_token and self.user_id)


@dataclass
class UploadItem:
    path: Path
    caption: str
    selected: bool = True
    index: int = 0
    status: str = "Ready"
    media_id: str = ""
    permalink: str = ""
    error: str = ""

    @property
    def url(self):
        return self.permalink or (INSTAGRAM_URL if self.media_id else "")


# --------------------------------------------------------------------------- #
# Caption
# --------------------------------------------------------------------------- #
def clean_caption(caption, fallback=""):
    """Trim to Instagram's limits: 2200 characters and 30 hashtags."""
    text = str(caption or "").replace("\r\n", "\n").strip() or fallback
    seen = 0

    def keep(match):
        nonlocal seen
        seen += 1
        return match.group(0) if seen <= HASHTAGS_MAX else ""

    text = re.sub(r"#\w+", keep, text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if len(text) > CAPTION_MAX:
        text = text[:CAPTION_MAX].rstrip()
    return text


def is_vertical(path):
    """True if the video is taller than wide (or if that cannot be measured)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "csv=s=x:p=0", str(path)],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
        width, height = (int(v) for v in out.split("x")[:2])
        return height > width
    except Exception:
        return True


def read_caption_file(video_path):
    """Read the Instagram caption that Viral Clips Studio saved as <clip>.txt next to the video."""
    sidecar = Path(video_path).with_suffix(".txt")
    try:
        text = sidecar.read_text(encoding="utf-8")
    except OSError:
        return None
    mark = "INSTAGRAM CAPTION"
    pos = text.find(mark)
    if pos < 0:
        return None
    caption = text[pos + len(mark):].strip()
    return caption or None


def load_upload_log(folder):
    try:
        data = json.loads((Path(folder) / UPLOAD_LOG_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_upload(path, media_id, permalink=""):
    folder = Path(path).parent
    data = load_upload_log(folder)
    data[Path(path).name] = {"media_id": media_id, "url": permalink,
                             "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        (folder / UPLOAD_LOG_NAME).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def build_items(saved):
    """saved: list of (clip, mp4 path) tuples as made by main.py, or plain paths."""
    items = []
    for entry in saved:
        clip, path = (entry if isinstance(entry, (tuple, list)) else (None, entry))
        path = Path(path)
        if not path.is_file():
            continue
        caption = read_caption_file(path)
        if not caption:  # no Gemini post copy: use the clip's own title
            caption = getattr(clip, "title", "") or path.stem
        item = UploadItem(path=path, caption=clean_caption(caption), index=len(items))
        done = load_upload_log(path.parent).get(path.name)
        if done and done.get("media_id"):
            item.media_id, item.permalink = done["media_id"], done.get("url", "")
            item.selected, item.status = False, "Already uploaded"
        items.append(item)
    return items


# --------------------------------------------------------------------------- #
# Instagram Graph API
# --------------------------------------------------------------------------- #
def missing_dependencies():
    import importlib.util
    missing = []
    for module, pip_name in DEPENDENCIES.items():
        try:
            if importlib.util.find_spec(module) is None:
                missing.append(pip_name)
        except (ImportError, ValueError):
            missing.append(pip_name)
    return missing


def _api_url(settings, path):
    return f"{settings.graph_host}/{settings.api_version}/{path.lstrip('/')}"


def _error_from(response):
    """Turn a failed Graph API response into an UploaderError (UploadLimitError for rate limits)."""
    try:
        error = response.json().get("error", {})
    except Exception:
        error = {}
    code = error.get("code")
    message = error.get("error_user_msg") or error.get("message") or response.text[:300]
    text = FRIENDLY_CODES.get(code) or f"Instagram refused the request: {message}"
    if code in (4, 9, 17, 32, 613):
        return UploadLimitError(text)
    return UploaderError(text)


def _request(method, url, cancel=None, retriable=True, **kwargs):
    """One Graph API call with retries on temporary errors. Returns the parsed JSON."""
    import requests

    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    last = ""
    for attempt in range(MAX_RETRIES + 1):
        if cancel is not None and cancel.is_set():
            raise UploadCancelled()
        try:
            response = requests.request(method, url, **kwargs)
        except requests.RequestException as exc:
            last = f"network problem ({exc})"
        else:
            if response.status_code < 400:
                try:
                    return response.json()
                except ValueError:
                    raise UploaderError(f"Instagram sent an unreadable answer: {response.text[:200]}")
            if response.status_code in (500, 502, 503, 504):
                last = f"temporary Instagram error {response.status_code}"
            else:
                raise _error_from(response)
        if not retriable or attempt == MAX_RETRIES:
            break
        time.sleep(min(2 ** (attempt + 1), 20))
    raise UploaderError(f"Instagram did not answer after {MAX_RETRIES + 1} tries: {last}")


def check_connection(settings):
    """Returns the Instagram username for the saved token, or raises UploaderError."""
    if not settings.configured():
        raise UploaderError(SETUP_HELP)
    data = _request("GET", _api_url(settings, settings.user_id),
                    params={"fields": "username", "access_token": settings.access_token})
    return data.get("username") or settings.user_id


class _ProgressFile:
    """File wrapper that reports progress and honours cancel while requests streams it."""

    def __init__(self, path, on_progress, cancel):
        self._file = open(path, "rb")
        self._size = os.path.getsize(path)
        self._sent = 0
        self._on_progress, self._cancel = on_progress, cancel

    def __len__(self):
        return self._size

    def read(self, size=-1):
        if self._cancel.is_set():
            raise UploadCancelled()
        chunk = self._file.read(size)
        self._sent += len(chunk)
        if chunk:
            self._on_progress(min(1.0, self._sent / max(1, self._size)))
        return chunk

    def close(self):
        self._file.close()


def _send_video(settings, upload_uri, path, on_progress, cancel):
    import requests

    size = os.path.getsize(path)
    headers = {"Authorization": f"OAuth {settings.access_token}", "offset": "0", "file_size": str(size)}
    last = ""
    for attempt in range(MAX_RETRIES + 1):
        if cancel.is_set():
            raise UploadCancelled()
        body = _ProgressFile(path, on_progress, cancel)
        try:
            response = requests.post(upload_uri, headers=headers, data=body, timeout=UPLOAD_TIMEOUT)
        except UploadCancelled:
            raise
        except requests.exceptions.RequestException as exc:
            if isinstance(exc.__cause__, UploadCancelled) or isinstance(getattr(exc, "args", [None])[0], UploadCancelled):
                raise UploadCancelled()
            last = f"network problem ({exc})"
        else:
            if response.status_code < 400:
                return
            if response.status_code in (500, 502, 503, 504):
                last = f"temporary Instagram error {response.status_code}"
            else:
                raise _error_from(response)
        finally:
            body.close()
        if attempt == MAX_RETRIES:
            break
        time.sleep(min(2 ** (attempt + 1), 20))
    raise UploaderError(f"Video upload failed after {MAX_RETRIES + 1} tries: {last}")


def _wait_until_ready(settings, container_id, cancel, on_status):
    deadline = time.time() + PROCESS_TIMEOUT
    while True:
        if cancel.is_set():
            raise UploadCancelled()
        data = _request("GET", _api_url(settings, container_id), cancel=cancel,
                        params={"fields": "status_code,status", "access_token": settings.access_token})
        code = str(data.get("status_code", "")).upper()
        if code == "FINISHED":
            return
        if code in {"ERROR", "EXPIRED"}:
            detail = data.get("status") or code
            raise UploaderError(f"Instagram could not process the video: {detail}")
        on_status(code.title() or "Processing")
        if time.time() > deadline:
            raise UploaderError("Instagram took too long to process the video. Try again later.")
        time.sleep(POLL_EVERY)


def upload_one(settings, item, on_progress, on_status, cancel):
    """Create the Reel container, send the file, wait for processing, publish. Returns (media_id, permalink)."""
    caption = clean_caption(item.caption, fallback=item.path.stem)

    on_status("Creating Reel…")
    data = {"media_type": "REELS", "upload_type": "resumable", "caption": caption,
            "share_to_feed": "true" if settings.share_to_feed else "false",
            "access_token": settings.access_token}
    created = _request("POST", _api_url(settings, f"{settings.user_id}/media"), cancel=cancel, data=data)
    container_id = created.get("id")
    upload_uri = created.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{settings.api_version}/{container_id}"
    if not container_id:
        raise UploaderError(f"Instagram did not return a container id: {created}")

    _send_video(settings, upload_uri, item.path, on_progress, cancel)

    on_status("Instagram is processing…")
    _wait_until_ready(settings, container_id, cancel, on_status)

    on_status("Publishing…")
    published = _request("POST", _api_url(settings, f"{settings.user_id}/media_publish"), cancel=cancel,
                         data={"creation_id": container_id, "access_token": settings.access_token})
    media_id = published.get("id")
    if not media_id:
        raise UploaderError(f"Instagram did not return a media id: {published}")
    permalink = ""
    try:  # nice to have: the public link of the new Reel
        info = _request("GET", _api_url(settings, media_id), retriable=False,
                        params={"fields": "permalink", "access_token": settings.access_token})
        permalink = info.get("permalink", "")
    except Exception:
        pass
    record_upload(item.path, media_id, permalink)
    return media_id, permalink


def run_uploads(items, settings, notify, cancel=None):
    """Post every selected item. notify(kind, item, value) kinds:
    auth, start, progress, status, done, error, fatal, finished. Returns the number of clips uploaded."""
    cancel = cancel or threading.Event()
    todo = [i for i in items if i.selected and not i.media_id]
    uploaded = 0
    try:
        notify("auth", None, "Checking your Instagram login…")
        name = check_connection(settings)
        notify("auth", None, f"Signed in as @{name}")
    except Exception as exc:
        notify("fatal", None, str(exc) if isinstance(exc, UploaderError) else f"Instagram check failed: {exc}")
        notify("finished", None, 0)
        return 0
    for item in todo:
        if cancel.is_set():
            break
        notify("start", item, "Uploading…")
        try:
            item.media_id, item.permalink = upload_one(
                settings, item, lambda f, it=item: notify("progress", it, f),
                lambda text, it=item: notify("status", it, text), cancel)
            item.status = "Uploaded"
            uploaded += 1
            notify("done", item, item.url)
        except UploadCancelled:
            notify("error", item, "Cancelled")
            break
        except UploadLimitError as exc:
            item.error = str(exc)
            notify("error", item, item.error)
            notify("fatal", None, item.error)
            break
        except Exception as exc:
            item.error = str(exc) if isinstance(exc, UploaderError) else f"Upload failed: {exc}"
            notify("error", item, item.error)
    notify("finished", None, uploaded)
    return uploaded


# --------------------------------------------------------------------------- #
# Instagram tab shown inside Viral Clips Studio (customtkinter, same look as the app)
# --------------------------------------------------------------------------- #
BG, PANEL, RAISED, LINE = "#16181d", "#1f2229", "#272b34", "#343945"
TEXT, SOFT, MUTED = "#e8e6e1", "#c3c7d1", "#8d93a0"
ACCENT, ACCENT_HOVER, ACCENT_TEXT = "#8b9bff", "#7686f0", "#10131c"
GOOD, BAD, WARN = "#5fc9a0", "#ef6a6a", "#e8b04a"


def attach_flow(frame, get_widgets, all_widgets, gap=8):
    """Lay widgets out left to right and wrap them onto new rows when the frame is too narrow."""
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

    frame.bind("<Configure>", layout, add="+")


def make_instagram_panel_class():
    import tkinter as tk
    from tkinter import filedialog, messagebox
    import customtkinter as ctk

    ui_font = "Segoe UI" if os.name == "nt" else None

    def font(size=13, weight="normal", mono=False):
        family = ("Consolas" if os.name == "nt" else "monospace") if mono else ui_font
        return ctk.CTkFont(family=family, size=size, weight=weight)

    def button(parent, text, command, primary=False, **kw):
        if primary:
            return ctk.CTkButton(parent, text=text, command=command, font=font(13, "bold"), fg_color=ACCENT,
                                 hover_color=ACCENT_HOVER, text_color=ACCENT_TEXT, height=36, corner_radius=8, **kw)
        return ctk.CTkButton(parent, text=text, command=command, font=font(13), fg_color=RAISED, hover_color=LINE,
                             text_color=TEXT, height=36, corner_radius=8, **kw)

    class InstagramPanel(ctk.CTkScrollableFrame):
        """The 'Instagram' tab. Clips are only posted when the user presses the button, or - if they ticked
        'Post automatically after the YouTube upload' - right after the YouTube upload has finished."""

        def __init__(self, parent, on_log=None, initial_dir=None):
            super().__init__(parent, fg_color="transparent")
            self.on_log = on_log
            self.initial_dir = Path(initial_dir) if initial_dir else BASE_DIR
            self.settings = UploadSettings.from_env()
            self.items, self.rows, self.entries = [], [], []
            self.events = queue.Queue()
            self.cancel = threading.Event()
            self.generation = 0  # events of an abandoned worker carry an old number and are ignored
            self.running = False
            self.alive = True
            self.uploaded = self.total = 0
            self.fatal_shown = False
            self.milestones = {}
            self.grid_columnconfigure(0, weight=1)

            head = ctk.CTkFrame(self, fg_color="transparent")
            head.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 0))
            ctk.CTkLabel(head, text="Post your clips to Instagram Reels", font=font(18, "bold"), text_color=TEXT,
                         anchor="w").pack(anchor="w")
            self.intro = ctk.CTkLabel(
                head, text="Reels are public as soon as they are posted (Instagram has no private mode). Review the "
                           "captions, untick any clip you do not want, then press Upload.",
                font=font(12), text_color=MUTED, anchor="w", justify="left", wraplength=520)
            self.intro.pack(anchor="w", pady=(2, 0))

            setup = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=10)
            setup.grid(row=1, column=0, sticky="ew", padx=6, pady=(10, 0))
            setup.grid_columnconfigure(0, weight=1)
            self.setup_label = ctk.CTkLabel(setup, text="", anchor="w", justify="left", font=font(12),
                                            text_color=SOFT, wraplength=420)
            self.setup_label.grid(row=0, column=0, sticky="ew", padx=12, pady=10)
            self.help_btn = button(setup, "Setup help", self.show_setup_help, width=100)
            self.help_btn.configure(height=30)
            self.help_btn.grid(row=0, column=1, padx=(0, 10), pady=8)

            self.list_frame = ctk.CTkFrame(self, fg_color="transparent")
            self.list_frame.grid(row=2, column=0, sticky="ew", padx=0, pady=(10, 0))
            self.list_frame.grid_columnconfigure(0, weight=1)
            self.empty_label = ctk.CTkLabel(
                self.list_frame, text_color=MUTED, font=font(13), justify="center",
                text="No clips loaded yet.\nCreate clips first, or press “Load clips from folder”.")

            opts = ctk.CTkFrame(self, fg_color="transparent")
            opts.grid(row=3, column=0, sticky="ew", padx=6, pady=(4, 0))
            self.feed_var = tk.BooleanVar(value=self.settings.share_to_feed)
            self.feed_check = ctk.CTkCheckBox(
                opts, text="Also show on my profile grid", variable=self.feed_var, font=font(12), text_color=SOFT,
                fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=MUTED)
            self.auto_var = tk.BooleanVar(value=self.settings.auto_after_youtube)
            self.auto_check = ctk.CTkCheckBox(
                opts, text="Post automatically after the YouTube upload", variable=self.auto_var, font=font(12),
                text_color=SOFT, fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=MUTED)
            self.opt_widgets = [self.feed_check, self.auto_check]
            attach_flow(opts, lambda: self.opt_widgets, self.opt_widgets, gap=16)

            self.progress = ctk.CTkProgressBar(self, progress_color=ACCENT, fg_color=RAISED, height=8)
            self.progress.set(0)
            self.progress.grid(row=4, column=0, sticky="ew", padx=8, pady=(12, 0))
            self.summary = ctk.CTkLabel(self, text="", anchor="w", justify="left", wraplength=520, font=font(13),
                                        text_color=MUTED)
            self.summary.grid(row=5, column=0, sticky="ew", padx=8, pady=(6, 0))

            bar = ctk.CTkFrame(self, fg_color="transparent")
            bar.grid(row=6, column=0, sticky="ew", padx=6, pady=(8, 0))
            self.upload_btn = button(bar, "Upload to Instagram", self.start_manual, primary=True)
            self.cancel_btn = button(bar, "Cancel upload", self.cancel_upload)
            self.test_btn = button(bar, "Test connection", self.test_connection)
            self.folder_btn = button(bar, "Load clips from folder…", self.choose_folder)
            self.reload_btn = button(bar, "Reload captions", self.reload)
            self.open_btn = button(bar, "Open Instagram", lambda: webbrowser.open(INSTAGRAM_URL))
            self.buttons = [self.upload_btn, self.cancel_btn, self.test_btn, self.folder_btn, self.reload_btn,
                            self.open_btn]
            attach_flow(bar, lambda: self.buttons, self.buttons)
            self.cancel_btn.configure(state="disabled")

            ctk.CTkLabel(self, text="Upload log", font=font(12, "bold"), text_color=SOFT, anchor="w").grid(
                row=7, column=0, sticky="w", padx=8, pady=(12, 2))
            self.log_box = ctk.CTkTextbox(self, height=170, fg_color=BG, text_color=SOFT, font=font(12, mono=True),
                                          wrap="word", state="disabled", border_width=1, border_color=LINE)
            self.log_box.grid(row=8, column=0, sticky="ew", padx=6, pady=(0, 10))
            self.log_box.tag_config("warn", foreground=WARN)
            self.log_box.tag_config("error", foreground=BAD)
            self.log_box.tag_config("ok", foreground=GOOD)

            self.bind("<Configure>", self._resize, add="+")
            self.refresh_setup(log=True)
            self.log("Instagram tab ready. Clips from a finished job appear here.")
            self._show_empty()
            self.after(100, self._poll)

        # ---------------------------------------------------------- helpers
        def _resize(self, event):
            wrap = max(260, event.width - 60)
            for label in (self.intro, self.summary):
                label.configure(wraplength=wrap)
            self.setup_label.configure(wraplength=max(200, wrap - 120))

        def destroy(self):
            self.alive = False
            super().destroy()

        def log(self, message, level="info"):
            stamp = time.strftime("%H:%M:%S")
            tag = {"warn": "warn", "error": "error", "ok": "ok"}.get(level)
            try:
                self.log_box.configure(state="normal")
                if tag:
                    self.log_box.insert("end", f"{stamp}  {message}\n", tag)
                else:
                    self.log_box.insert("end", f"{stamp}  {message}\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
            except tk.TclError:
                return
            if self.on_log:
                self.on_log(f"[Instagram] {message}", level)

        def reload_settings(self):
            """Re-read .env so a new token works without restarting the app."""
            try:
                from dotenv import load_dotenv
                load_dotenv(BASE_DIR / ".env", override=True)
            except Exception:
                pass
            fresh = UploadSettings.from_env()
            fresh.share_to_feed = bool(self.feed_var.get())
            fresh.auto_after_youtube = bool(self.auto_var.get())
            self.settings = fresh

        def check_setup(self):
            missing = missing_dependencies()
            if missing:
                return False, f"Missing Python packages: {', '.join(missing)}.  Run:  {PIP_COMMAND}"
            if not self.settings.configured():
                return False, ("Instagram setup needed: INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID were not "
                               "found in .env. Press “Setup help” for the one-time steps.")
            return True, "Token and user id found in .env. Press “Test connection” to verify them."

        def refresh_setup(self, log=False):
            self.reload_settings()
            ok, text = self.check_setup()
            self.setup_label.configure(text=("✓ " if ok else "⚠ ") + text, text_color=GOOD if ok else WARN)
            if log:
                self.log(f"Setup check: {text}", "ok" if ok else "warn")
            return ok

        def show_setup_help(self):
            self.refresh_setup(log=True)
            messagebox.showinfo("Instagram setup", SETUP_HELP + f"\n\nPackages:  {PIP_COMMAND}")

        def test_connection(self):
            if self.running or not self.refresh_setup():
                return
            self.summary.configure(text="Testing the connection…", text_color=MUTED)
            generation = self.generation

            def work():
                try:
                    name = check_connection(self.settings)
                    self.events.put((generation, "test", None, (True, f"Connected as @{name}.")))
                except Exception as exc:
                    self.events.put((generation, "test", None, (False, str(exc))))

            threading.Thread(target=work, daemon=True).start()

        def _show_empty(self):
            self.empty_label.grid(row=0, column=0, pady=40)

        def _set_editable(self, editable):
            state = "normal" if editable else "disabled"
            for widget in (self.feed_check, self.auto_check):
                widget.configure(state=state)
            for row in self.rows:
                if row["item"].media_id:
                    row["check"].configure(state="disabled")
                    continue
                for key in ("check", "caption"):
                    row[key].configure(state=state)

        # ----------------------------------------------------------- loading
        def clear(self):
            """Forget the loaded clips (used by 'New video'). Does nothing while an upload is running."""
            if self.running:
                return
            self._populate([], [], announce=False)
            self.progress.set(0)
            self.summary.configure(text="")

        def load(self, saved, source=""):
            """saved = [(clip, mp4 path), ...] or plain paths. Rows are only filled in: nothing is uploaded."""
            if self.running:
                self.log("An upload is running right now; the new clips were not loaded here.", "warn")
                return
            entries = list(saved)
            self._populate(entries, build_items(entries), source=source)

        def reload(self):
            if self.running:
                return
            if not self.entries:
                self.log("Nothing to reload yet. Create clips or load a folder first.", "warn")
                return
            self.log("Reloading captions from the saved post files…")
            self.load(self.entries)

        def choose_folder(self):
            if self.running:
                return
            start = self.initial_dir if self.initial_dir.is_dir() else BASE_DIR
            folder = filedialog.askdirectory(parent=self.winfo_toplevel(), initialdir=str(start),
                                             title="Choose the folder with your .mp4 clips")
            if not folder:
                return
            paths = sorted(Path(folder).glob("*.mp4"))
            if not paths:
                self.log(f"No .mp4 files found in {folder}", "warn")
                self.summary.configure(text="That folder has no .mp4 files.", text_color=WARN)
                return
            self.load(paths, source=folder)

        def _populate(self, entries, items, announce=True, source=""):
            for row in self.rows:
                row["card"].destroy()
            self.rows, self.entries, self.items = [], entries, items
            self.progress.set(0)
            self.empty_label.grid_forget()
            for item in items:
                self._build_row(item)
            waiting = [i for i in items if i.selected]
            if not items:
                self._show_empty()
                self.upload_btn.configure(state="disabled")
                if announce:
                    self.summary.configure(text="No clips found to upload.", text_color=WARN)
                    self.log("No clips found to upload.", "warn")
                return
            self.upload_btn.configure(state="normal" if waiting else "disabled", text="Upload to Instagram")
            where = f" from {source}" if source else ""
            if waiting:
                text = (f"{len(waiting)} clip{'s' if len(waiting) != 1 else ''} ready to post. "
                        "Check the captions, then press Upload to Instagram.")
                self.summary.configure(text=text, text_color=SOFT)
            else:
                self.summary.configure(text="All clips in this list are already uploaded.", text_color=GOOD)
            self.log(f"Loaded {len(items)} clip{'s' if len(items) != 1 else ''}{where}: {len(waiting)} waiting, "
                     f"{len(items) - len(waiting)} already uploaded.", "ok")

        def _build_row(self, item):
            card = ctk.CTkFrame(self.list_frame, fg_color=PANEL, corner_radius=10)
            card.grid(row=len(self.rows), column=0, sticky="ew", pady=(0, 10), padx=(0, 4))
            card.grid_columnconfigure(0, weight=1)
            top = ctk.CTkFrame(card, fg_color="transparent")
            top.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
            top.grid_columnconfigure(1, weight=1)
            var = tk.BooleanVar(value=item.selected)
            check = ctk.CTkCheckBox(top, text="", variable=var, width=24, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                    border_color=MUTED)
            check.grid(row=0, column=0, sticky="w")
            ctk.CTkLabel(top, text=item.path.name, font=font(12, "bold"), text_color=SOFT, anchor="w").grid(
                row=0, column=1, sticky="w")
            status = ctk.CTkLabel(top, text=item.status, font=font(12), text_color=GOOD if item.media_id else MUTED,
                                  anchor="e", cursor="hand2" if item.media_id else "")
            status.grid(row=0, column=2, sticky="e", padx=(6, 0))
            if item.media_id:
                status.bind("<Button-1>", lambda _e, u=item.url: webbrowser.open(u))
            caption = ctk.CTkTextbox(card, height=110, font=font(12), fg_color=RAISED, border_color=LINE,
                                     border_width=1, text_color=TEXT, wrap="word")
            caption.insert("1.0", item.caption)
            caption.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
            self.rows.append({"item": item, "var": var, "check": check, "caption": caption,
                              "status": status, "card": card})

        # ------------------------------------------------------------ actions
        def waiting_count(self):
            return len([r for r in self.rows if not r["item"].media_id and r["var"].get()])

        def start_manual(self):
            if self.running or not self.waiting_count():
                self.start()  # shows the usual "tick at least one clip" message
                return
            n = self.waiting_count()
            if not messagebox.askyesno(
                    "Post to Instagram",
                    f"Post {n} Reel{'s' if n != 1 else ''} to Instagram now?\n\nReels are public immediately."):
                return
            self.start()

        def start_auto(self):
            """Called by main.py when the YouTube upload has finished and the auto box is ticked."""
            if self.running or not self.auto_var.get() or not self.waiting_count():
                return
            self.log("YouTube upload finished: starting the Instagram upload automatically.")
            self.start()

        def start(self):
            if self.running:
                return
            if not self.refresh_setup():
                self.summary.configure(text="Instagram is not set up yet. Press “Setup help”.", text_color=BAD)
                self.log("Cannot upload: " + self.setup_label.cget("text").lstrip("⚠ "), "error")
                return
            chosen = []
            for row in self.rows:
                item = row["item"]
                item.selected = bool(row["var"].get()) and not item.media_id
                if item.selected:
                    item.caption = clean_caption(row["caption"].get("1.0", "end"), item.path.stem)
                    chosen.append(item)
            if not chosen:
                self.summary.configure(text="Tick at least one clip to upload.", text_color=WARN)
                self.log("Nothing ticked: tick at least one clip to upload.", "warn")
                return
            for item in chosen:
                if not is_vertical(item.path):
                    self.log(f"{item.path.name} is not vertical (9:16). Instagram may crop it or refuse it.", "warn")
            self.running = True
            self.uploaded, self.total, self.fatal_shown = 0, len(chosen), False
            self.milestones = {}
            self.generation += 1
            self.cancel = threading.Event()
            self._set_editable(False)
            self.upload_btn.configure(state="disabled")
            self.cancel_btn.configure(state="normal")
            self.folder_btn.configure(state="disabled")
            self.reload_btn.configure(state="disabled")
            self.test_btn.configure(state="disabled")
            self.progress.set(0)
            self.summary.configure(text="Starting…", text_color=MUTED)
            self.log(f"Starting upload of {self.total} Reel{'s' if self.total != 1 else ''}.")
            threading.Thread(target=self._work, args=(self.generation, self.cancel, self.settings),
                             daemon=True).start()

        def _work(self, generation, cancel, settings):
            run_uploads(self.items, settings,
                        lambda kind, item, value: self.events.put((generation, kind, item, value)), cancel)

        def cancel_upload(self):
            if not self.running:
                return
            self.cancel.set()  # stops at the next chunk / status check
            self.cancel_btn.configure(state="disabled")
            self.summary.configure(text="Cancelling…", text_color=WARN)
            self.log("Cancel requested. Stopping as soon as possible.", "warn")
            generation = self.generation
            self.after(4000, lambda: self._force_stop(generation))

        def _force_stop(self, generation):
            if not self.alive or generation != self.generation or not self.running:
                return
            self.generation += 1
            self._end_run(cancelled=True)

        def _poll(self):
            if not self.alive:
                return
            try:
                while True:
                    generation, kind, item, value = self.events.get_nowait()
                    if generation == self.generation:
                        self._handle(kind, item, value)
            except queue.Empty:
                pass
            except tk.TclError:
                return
            self.after(100, self._poll)

        def _set_status(self, item, text, color):
            self.rows[item.index]["status"].configure(text=text, text_color=color)

        def _handle(self, kind, item, value):
            if kind == "test":
                ok, text = value
                self.summary.configure(text=text if ok else text.splitlines()[0], text_color=GOOD if ok else BAD)
                self.log(text, "ok" if ok else "error")
            elif kind == "auth":
                self.summary.configure(text=value, text_color=MUTED)
                self.log(str(value))
            elif kind == "start":
                self._set_status(item, "Uploading… 0%", ACCENT)
                self.summary.configure(
                    text=f"Uploading clip {self.uploaded + 1} of {self.total}: {item.path.name}", text_color=MUTED)
                size = item.path.stat().st_size / (1024 * 1024) if item.path.exists() else 0
                self.log(f"Uploading {item.path.name} ({size:.1f} MB)…")
            elif kind == "progress":
                self._set_status(item, f"Uploading… {int(value * 100)}%", ACCENT)
                self.progress.set((self.uploaded + value * 0.8) / max(1, self.total))
                mark = int(value * 4)
                if 0 < mark < 4 and mark > self.milestones.get(item.index, 0):
                    self.milestones[item.index] = mark
                    self.log(f"  {item.path.name}: {mark * 25}% sent")
            elif kind == "status":
                self._set_status(item, str(value), ACCENT)
                if value in ("Instagram is processing…", "Publishing…"):
                    self.log(f"  {item.path.name}: {value}")
            elif kind == "done":
                self.uploaded += 1
                self._set_status(item, "Posted - click to open", GOOD)
                self.rows[item.index]["status"].configure(cursor="hand2")
                self.rows[item.index]["status"].bind("<Button-1>", lambda _e, u=value: webbrowser.open(u))
                self.progress.set(self.uploaded / max(1, self.total))
                self.log(f"Posted {item.path.name}: {value}", "ok")
            elif kind == "error":
                self._set_status(item, str(value)[:90], BAD)
                self.log(f"FAILED {item.path.name}: {value}", "error")
            elif kind == "fatal":
                self.fatal_shown = True
                self.summary.configure(text=str(value).splitlines()[0], text_color=BAD)
                self.log(str(value), "error")
            elif kind == "finished":
                self._end_run(cancelled=self.cancel.is_set())

        def _end_run(self, cancelled=False):
            self.running = False
            failed = self.total - self.uploaded
            if cancelled:
                text = f"Upload cancelled. {self.uploaded} of {self.total} posted."
                self.summary.configure(text=text, text_color=WARN)
                self.log(text, "warn")
            elif self.uploaded and not failed:
                text = f"All {self.uploaded} Reel{'s' if self.uploaded != 1 else ''} posted to Instagram."
                self.summary.configure(text=text, text_color=GOOD)
                self.log(text, "ok")
            else:
                if not self.fatal_shown:
                    self.summary.configure(
                        text=f"Posted {self.uploaded} of {self.total}. See the red lines in the log.",
                        text_color=WARN if self.uploaded else BAD)
                self.log(f"Upload finished: {self.uploaded} of {self.total} posted, {failed} failed.",
                         "warn" if self.uploaded else "error")
            self.cancel_btn.configure(state="disabled")
            self.folder_btn.configure(state="normal")
            self.reload_btn.configure(state="normal")
            self.test_btn.configure(state="normal")
            self._set_editable(True)
            waiting = [r for r in self.rows if not r["item"].media_id]
            self.upload_btn.configure(state="normal" if waiting else "disabled",
                                      text="Retry remaining clips" if waiting and self.uploaded + failed else
                                      "Upload to Instagram")
            self.refresh_setup()

    return InstagramPanel


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def _collect_paths(args):
    paths = []
    for arg in args:
        p = Path(arg)
        if p.is_dir():
            paths.extend(sorted(p.glob("*.mp4")))
        elif p.is_file():
            paths.append(p)
        else:
            print(f"Not found: {arg}", file=sys.stderr)
    return paths


def cli(argv=None):
    parser = argparse.ArgumentParser(description="Post Viral Clips Studio clips to Instagram as Reels.")
    parser.add_argument("paths", nargs="*", help="folder(s) or .mp4 file(s)")
    parser.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--no-feed", action="store_true", help="do not also show the Reel on the profile grid")
    parser.add_argument("--check", action="store_true", help="only test the token and account, then exit")
    args = parser.parse_args(argv)

    missing = missing_dependencies()
    if missing:
        print(f"Missing packages: {', '.join(missing)}\nInstall with:\n  {PIP_COMMAND}")
        return 1
    settings = UploadSettings.from_env()
    if args.check:
        try:
            print(f"Connected as @{check_connection(settings)}")
            return 0
        except UploaderError as exc:
            print(exc)
            return 1
    if not args.paths:
        parser.print_help()
        return 1
    if args.no_feed:
        settings.share_to_feed = False
    items = build_items(_collect_paths(args.paths))
    waiting = [i for i in items if i.selected]
    for i in items:
        first = i.caption.splitlines()[0] if i.caption else ""
        print(f"  {'[skip]' if not i.selected else '[ ok ]'} {i.path.name}\n         caption: {first[:80]}")
    if not waiting:
        print("Nothing to upload.")
        return 0
    if not args.yes and input(f"\nPost {len(waiting)} Reel(s) PUBLICLY to Instagram? [y/N] ").strip().lower() != "y":
        return 0

    def notify(kind, item, value):
        if kind in ("auth", "fatal"):
            print(value)
        elif kind == "start":
            print(f"Uploading {item.path.name} ...")
        elif kind == "progress":
            print(f"\r  {int(value * 100)}%", end="", flush=True)
        elif kind == "status":
            print(f"\r  {value}          ")
        elif kind == "done":
            print(f"\r  Posted: {value}")
        elif kind == "error":
            print(f"\r  FAILED: {value}")

    count = run_uploads(items, settings, notify)
    print(f"\nDone: {count} of {len(waiting)} posted.")
    return 0 if count == len(waiting) else 2


if __name__ == "__main__":
    sys.exit(cli())
