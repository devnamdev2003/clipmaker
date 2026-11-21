"""
YouTube uploader for Viral Clips Studio
=======================================

Standalone module: main.py embeds make_upload_panel_class() as the "Upload" tab (step 6). Nothing is uploaded
automatically: the tab lists the finished clips, lets you review/edit title + description, and only uploads
when you press the Upload button. Every selected clip is uploaded to your channel with:

  * title + description + hashtags   -> taken from the Gemini post copy saved next to each clip
                                        (the per-clip .txt file); falls back to the clip's own title
  * tags                            -> built from the hashtags in the description
  * #Shorts                         -> added automatically for vertical clips
  * privacy, category, "made for kids" = No, notify-subscribers = off (all configurable)

It also works on its own from the command line:

    python youtube_uploader.py output/<Video Title>            # upload every .mp4 in a folder
    python youtube_uploader.py clip1.mp4 clip2.mp4 --privacy unlisted
    python youtube_uploader.py --login                         # only sign in to Google

One-time setup (see SETUP_HELP below):
    pip install google-api-python-client google-auth-oauthlib google-auth-httplib2
    + a Google Cloud OAuth "Desktop app" file saved next to this script as client_secret.json
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

try:  # python-dotenv is already a dependency of main.py; optional here
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / "youtube_token.json"
UPLOAD_LOG_NAME = "youtube_uploads.json"  # written inside each output folder, so a clip is never uploaded twice
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
STUDIO_URL = "https://studio.youtube.com/"

TITLE_MAX = 100
DESCRIPTION_MAX_BYTES = 4900  # YouTube limit is 5000 bytes
TAGS_MAX_CHARS = 450  # YouTube limit is 500 characters for all tags together
CHUNK_SIZE = 4 * 1024 * 1024  # must be a multiple of 256 KB
RETRIABLE_STATUS = {500, 502, 503, 504}
MAX_RETRIES = 8
PRIVACY_CHOICES = ["private", "unlisted", "public"]

DEPENDENCIES = {  # import name -> pip name
    "googleapiclient": "google-api-python-client",
    "google_auth_oauthlib": "google-auth-oauthlib",
    "google.auth": "google-auth",
    "httplib2": "httplib2",
}
PIP_COMMAND = "pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"

SETUP_HELP = (
    "YouTube upload needs a one-time setup:\n\n"
    "1. Open https://console.cloud.google.com/ and create (or pick) a project.\n"
    "2. APIs & Services > Library > enable \"YouTube Data API v3\".\n"
    "3. APIs & Services > OAuth consent screen: choose External, fill the app name and your email. "
    "Under \"Test users\" add the Google account that owns your YouTube channel.\n"
    "4. APIs & Services > Credentials > Create credentials > OAuth client ID > Application type: Desktop app.\n"
    "5. Download the JSON file and save it next to youtube_uploader.py as  client_secret.json\n\n"
    f"Folder: {BASE_DIR}\n\n"
    "Then try again. A browser window opens once so you can allow the upload; "
    "after that the login is remembered in youtube_token.json."
)

QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}
FRIENDLY_REASONS = {
    "uploadLimitExceeded": "Your channel has reached YouTube's upload limit for now. Try again later.",
    "youtubeSignupRequired": "The Google account you signed in with has no YouTube channel. "
                             "Create a channel on youtube.com first, then sign in again.",
    "forbidden": "YouTube refused the upload (forbidden). Check that this Google account is added as a "
                 "Test user on the OAuth consent screen and owns a channel.",
    "authError": "The saved Google login is no longer valid. Delete youtube_token.json and try again.",
    "invalidTitle": "YouTube rejected the title.",
    "invalidDescription": "YouTube rejected the description.",
    "invalidTags": "YouTube rejected the tags.",
}


class UploaderError(Exception):
    """An error whose message can be shown to the user as is."""


class UploadQuotaError(UploaderError):
    """Daily API quota used up: no point trying the remaining clips."""


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
    privacy: str = "private"  # private | unlisted | public
    category_id: str = "22"  # 22 People & Blogs, 24 Entertainment, 23 Comedy, 27 Education ...
    made_for_kids: bool = False
    notify_subscribers: bool = False
    language: str = ""  # e.g. "en" or "hi"; empty = let YouTube decide
    add_shorts_tag: bool = True  # add #Shorts to the description of vertical clips

    @classmethod
    def from_env(cls):
        privacy = os.environ.get("YOUTUBE_PRIVACY", "private").strip().lower()
        return cls(
            privacy=privacy if privacy in PRIVACY_CHOICES else "private",
            category_id=os.environ.get("YOUTUBE_CATEGORY_ID", "22").strip() or "22",
            made_for_kids=_env_bool("YOUTUBE_MADE_FOR_KIDS", False),
            notify_subscribers=_env_bool("YOUTUBE_NOTIFY_SUBSCRIBERS", False),
            language=os.environ.get("YOUTUBE_LANGUAGE", "").strip(),
            add_shorts_tag=_env_bool("YOUTUBE_ADD_SHORTS_TAG", True),
        )


@dataclass
class UploadItem:
    path: Path
    title: str
    description: str
    selected: bool = True
    index: int = 0
    status: str = "Ready"
    video_id: str = ""
    error: str = ""

    @property
    def url(self):
        return f"https://youtu.be/{self.video_id}" if self.video_id else ""


# --------------------------------------------------------------------------- #
# Metadata: title, description, tags
# --------------------------------------------------------------------------- #
def clean_title(title, fallback="Untitled clip"):
    title = re.sub(r"[<>]", "", str(title or ""))
    title = " ".join(title.split())
    if len(title) > TITLE_MAX:
        cut = title[:TITLE_MAX]
        if " " in cut[60:]:  # cut at a word boundary, but only if that does not lose too much
            cut = cut.rsplit(" ", 1)[0]
        title = cut.rstrip(" -,:;|")
    return title or fallback


def clean_description(description):
    text = re.sub(r"[<>]", "", str(description or "")).replace("\r\n", "\n").strip()
    raw = text.encode("utf-8")
    if len(raw) > DESCRIPTION_MAX_BYTES:
        text = raw[:DESCRIPTION_MAX_BYTES].decode("utf-8", errors="ignore").rstrip()
    return text


def extract_tags(description, limit=15):
    """Hashtags in the description become the video's search tags."""
    tags, seen, total = [], set(), 0
    for word in re.findall(r"#(\w{2,40})", description or ""):
        if word.isdigit() or word.lower() in seen:
            continue
        if total + len(word) + 1 > TAGS_MAX_CHARS or len(tags) >= limit:
            break
        seen.add(word.lower())
        tags.append(word)
        total += len(word) + 1
    return tags


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


def read_post_file(video_path):
    """Read the YouTube title + description that Viral Clips Studio saved as <clip>.txt next to the video."""
    sidecar = Path(video_path).with_suffix(".txt")
    try:
        text = sidecar.read_text(encoding="utf-8")
    except OSError:
        return None
    t_mark, d_mark, i_mark = "YOUTUBE TITLE", "YOUTUBE DESCRIPTION", "INSTAGRAM CAPTION"
    t_pos, d_pos = text.find(t_mark), text.find(d_mark)
    if t_pos < 0 or d_pos < t_pos:
        return None
    i_pos = text.find(i_mark, d_pos)
    title = text[t_pos + len(t_mark):d_pos].strip()
    description = text[d_pos + len(d_mark):(i_pos if i_pos >= 0 else len(text))].strip()
    return (title, description) if title else None


def load_upload_log(folder):
    try:
        data = json.loads((Path(folder) / UPLOAD_LOG_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_upload(path, video_id):
    folder = Path(path).parent
    data = load_upload_log(folder)
    data[Path(path).name] = {"video_id": video_id, "url": f"https://youtu.be/{video_id}",
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
        post = read_post_file(path)
        if post:
            title, description = post
        else:  # no Gemini post copy: use the clip's own title/description
            title = getattr(clip, "title", "") or path.stem
            description = getattr(clip, "description", "") or ""
        item = UploadItem(path=path, title=clean_title(title or path.stem), description=description.strip(),
                          index=len(items))
        done = load_upload_log(path.parent).get(path.name)
        if done and done.get("video_id"):
            item.video_id, item.selected, item.status = done["video_id"], False, "Already uploaded"
        items.append(item)
    return items


def build_body(item, settings, short):
    description = clean_description(item.description)
    if short and settings.add_shorts_tag and "#shorts" not in (item.title + " " + description).lower():
        description = (description + "\n\n#Shorts").strip()
    snippet = {"title": clean_title(item.title), "description": description, "categoryId": settings.category_id}
    tags = extract_tags(description)
    if tags:  # the API rejects an empty tag list
        snippet["tags"] = tags
    if settings.language:
        snippet["defaultLanguage"] = settings.language
        snippet["defaultAudioLanguage"] = settings.language
    return {"snippet": snippet,
            "status": {"privacyStatus": settings.privacy, "selfDeclaredMadeForKids": settings.made_for_kids,
                       "embeddable": True, "publicStatsViewable": True}}


# --------------------------------------------------------------------------- #
# Google login + upload
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


def find_client_secret():
    exact = BASE_DIR / "client_secret.json"
    if exact.exists():
        return exact
    matches = sorted(BASE_DIR.glob("client_secret*.json"))  # Google names the download client_secret_1234-abc.json
    return matches[0] if matches else None


def _save_token(creds):
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass


def get_credentials(log=print):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except Exception:
            creds = None
    if creds and creds.valid:
        return creds
    if creds and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except RefreshError:
            log("Your saved Google login expired. Signing in again…")
        except Exception as exc:  # network trouble: do not throw the login away
            raise UploaderError(f"Could not refresh the Google login ({exc}). Check your internet and try again.")
    secret = find_client_secret()
    if secret is None:
        raise UploaderError(SETUP_HELP)
    log("Opening your browser to sign in to Google (only needed once)…")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent",
                                  success_message="Signed in. You can close this tab and go back to Viral Clips Studio.")
    _save_token(creds)
    return creds


def build_service(creds):
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _http_reason(exc):
    try:
        data = json.loads(exc.content.decode("utf-8"))
        error = data.get("error", {})
        reason = (error.get("errors") or [{}])[0].get("reason", "")
        return reason, error.get("message", "")
    except Exception:
        return "", ""


def _resumable_upload(request, on_progress, cancel):
    import httplib2
    from googleapiclient.errors import HttpError

    response, retry = None, 0
    while response is None:
        if cancel.is_set():
            raise UploadCancelled()
        try:
            status, response = request.next_chunk()
            retry = 0
            if status:
                on_progress(status.progress())
        except HttpError as exc:
            reason, message = _http_reason(exc)
            if reason in QUOTA_REASONS:
                raise UploadQuotaError("Your YouTube API quota for today is used up. The remaining clips can be "
                                       "uploaded tomorrow (quota resets at midnight Pacific Time) - just run the "
                                       "upload again; finished clips are skipped.")
            if exc.resp.status in RETRIABLE_STATUS:
                error = f"temporary YouTube error {exc.resp.status}"
            else:
                raise UploaderError(FRIENDLY_REASONS.get(reason) or f"YouTube refused the upload: {message or exc}")
        except (OSError, httplib2.HttpLib2Error) as exc:
            error = f"network problem ({exc})"
        else:
            continue
        retry += 1
        if retry > MAX_RETRIES:
            raise UploaderError(f"Upload stopped after {MAX_RETRIES} retries: {error}")
        time.sleep(min(2 ** retry, 30) + random.random())
    if "id" not in response:
        raise UploaderError(f"YouTube did not return a video id: {response}")
    return response["id"]


def upload_one(service, item, settings, on_progress, cancel):
    from googleapiclient.http import MediaFileUpload

    body = build_body(item, settings, is_vertical(item.path))
    media = MediaFileUpload(str(item.path), mimetype="video/mp4", chunksize=CHUNK_SIZE, resumable=True)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media,
                                      notifySubscribers=settings.notify_subscribers)
    video_id = _resumable_upload(request, on_progress, cancel)
    record_upload(item.path, video_id)
    return video_id


def run_uploads(items, settings, notify, cancel=None):
    """Upload every selected item. notify(kind, item, value) kinds:
    auth, start, progress, done, error, fatal, finished. Returns the number of clips uploaded."""
    cancel = cancel or threading.Event()
    todo = [i for i in items if i.selected and not i.video_id]
    uploaded = 0
    try:
        notify("auth", None, "Signing in to Google…")
        creds = get_credentials(lambda text: notify("auth", None, text))
        service = build_service(creds)
    except Exception as exc:
        notify("fatal", None, str(exc) if isinstance(exc, UploaderError) else f"Google sign-in failed: {exc}")
        notify("finished", None, 0)
        return 0
    for item in todo:
        if cancel.is_set():
            break
        notify("start", item, "Uploading…")
        try:
            item.video_id = upload_one(service, item, settings, lambda f, it=item: notify("progress", it, f), cancel)
            item.status = "Uploaded"
            uploaded += 1
            notify("done", item, item.url)
        except UploadCancelled:
            notify("error", item, "Cancelled")
            break
        except UploadQuotaError as exc:
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
# Upload tab shown inside Viral Clips Studio (customtkinter, same look as the app)
# --------------------------------------------------------------------------- #
BG, PANEL, RAISED, LINE = "#16181d", "#1f2229", "#272b34", "#343945"
TEXT, SOFT, MUTED = "#e8e6e1", "#c3c7d1", "#8d93a0"
ACCENT, ACCENT_HOVER, ACCENT_TEXT = "#8b9bff", "#7686f0", "#10131c"
GOOD, BAD, WARN = "#5fc9a0", "#ef6a6a", "#e8b04a"


def attach_flow(frame, get_widgets, all_widgets, gap=8):
    """Lay widgets out left to right and wrap them onto new rows when the frame is too narrow.
    Returns relayout(): call it after the visible widgets change."""
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


def make_upload_panel_class():
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

    class UploadPanel(ctk.CTkScrollableFrame):
        """The 'Upload' tab. Nothing is uploaded until the user presses the Upload button.
        on_log(message, level) mirrors every line into the app's Log tab; on_step(state) drives the 6th step pill."""

        def __init__(self, parent, on_log=None, on_step=None, initial_dir=None):
            super().__init__(parent, fg_color="transparent")
            self.on_log, self.on_step = on_log, on_step
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
            ctk.CTkLabel(head, text="Upload your clips to YouTube", font=font(18, "bold"), text_color=TEXT,
                         anchor="w").pack(anchor="w")
            self.intro = ctk.CTkLabel(
                head, text="Nothing is uploaded automatically. Review the titles and descriptions, untick any clip "
                           "you do not want, then press Upload.", font=font(12), text_color=MUTED, anchor="w",
                justify="left", wraplength=520)
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
            ctk.CTkLabel(opts, text="Visibility", font=font(12), text_color=SOFT).pack(side="left")
            self.privacy_var = tk.StringVar(value=self.settings.privacy)
            self.privacy_menu = ctk.CTkOptionMenu(
                opts, values=PRIVACY_CHOICES, variable=self.privacy_var, width=120, font=font(12),
                fg_color=RAISED, button_color=RAISED, button_hover_color=LINE, text_color=TEXT)
            self.privacy_menu.pack(side="left", padx=(8, 16))
            self.notify_var = tk.BooleanVar(value=self.settings.notify_subscribers)
            self.notify_check = ctk.CTkCheckBox(
                opts, text="Notify subscribers", variable=self.notify_var, font=font(12), text_color=SOFT,
                fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=MUTED)
            self.notify_check.pack(side="left")

            self.progress = ctk.CTkProgressBar(self, progress_color=ACCENT, fg_color=RAISED, height=8)
            self.progress.set(0)
            self.progress.grid(row=4, column=0, sticky="ew", padx=8, pady=(12, 0))
            self.summary = ctk.CTkLabel(self, text="", anchor="w", justify="left", wraplength=520, font=font(13),
                                        text_color=MUTED)
            self.summary.grid(row=5, column=0, sticky="ew", padx=8, pady=(6, 0))

            bar = ctk.CTkFrame(self, fg_color="transparent")
            bar.grid(row=6, column=0, sticky="ew", padx=6, pady=(8, 0))
            self.upload_btn = button(bar, "Upload to YouTube", self.start, primary=True)
            self.cancel_btn = button(bar, "Cancel upload", self.cancel_upload)
            self.folder_btn = button(bar, "Load clips from folder…", self.choose_folder)
            self.reload_btn = button(bar, "Reload titles", self.reload)
            self.studio_btn = button(bar, "Open YouTube Studio", lambda: webbrowser.open(STUDIO_URL))
            self.buttons = [self.upload_btn, self.cancel_btn, self.folder_btn, self.reload_btn, self.studio_btn]
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
            self.log("Upload tab ready. Clips from a finished job appear here; nothing is uploaded until you press "
                     "Upload.")
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
                self.on_log(f"[Upload] {message}", level)

        def step(self, state):
            if self.on_step:
                self.on_step(state)

        def check_setup(self):
            missing = missing_dependencies()
            if missing:
                return False, f"Missing Python packages: {', '.join(missing)}.  Run:  {PIP_COMMAND}"
            if find_client_secret() is None and not TOKEN_FILE.exists():
                return False, ("Google setup needed: client_secret.json was not found next to the app. "
                               "Press “Setup help” for the one-time steps.")
            if TOKEN_FILE.exists():
                return True, "Google login saved. Ready to upload."
            return True, "client_secret.json found. A browser window opens to sign in the first time you upload."

        def refresh_setup(self, log=False):
            ok, text = self.check_setup()
            self.setup_label.configure(text=("✓ " if ok else "⚠ ") + text, text_color=GOOD if ok else WARN)
            if log:
                self.log(f"Setup check: {text}", "ok" if ok else "warn")
            return ok

        def show_setup_help(self):
            self.refresh_setup(log=True)
            messagebox.showinfo("YouTube setup", SETUP_HELP + f"\n\nPackages:  {PIP_COMMAND}")

        def _show_empty(self):
            self.empty_label.grid(row=0, column=0, pady=40)

        def _set_editable(self, editable):
            state = "normal" if editable else "disabled"
            for widget in (self.privacy_menu, self.notify_check):
                widget.configure(state=state)
            for row in self.rows:
                if row["item"].video_id:
                    row["check"].configure(state="disabled")
                    continue
                for key in ("check", "title", "desc"):
                    row[key].configure(state=state)

        # ----------------------------------------------------------- loading
        def clear(self):
            """Forget the loaded clips (used by 'New video'). Does nothing while an upload is running."""
            if self.running:
                return
            self._populate([], [], announce=False)
            self.progress.set(0)
            self.summary.configure(text="")
            self.step("pending")

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
            self.log("Reloading titles and descriptions from the saved post files…")
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
            self.upload_btn.configure(state="normal" if waiting else "disabled", text="Upload to YouTube")
            where = f" from {source}" if source else ""
            if waiting:
                text = (f"{len(waiting)} clip{'s' if len(waiting) != 1 else ''} ready to upload. "
                        "Check the titles, then press Upload to YouTube.")
                self.summary.configure(text=text, text_color=SOFT)
            else:
                self.summary.configure(text="All clips in this list are already uploaded.", text_color=GOOD)
            self.log(f"Loaded {len(items)} clip{'s' if len(items) != 1 else ''}{where}: {len(waiting)} waiting, "
                     f"{len(items) - len(waiting)} already uploaded.", "ok")
            for item in items:
                self.log(f"  {item.path.name}  |  {item.status}  |  “{item.title}”")
            self.step("pending")

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
            status = ctk.CTkLabel(top, text=item.status, font=font(12), text_color=GOOD if item.video_id else MUTED,
                                  anchor="e", cursor="hand2" if item.video_id else "")
            status.grid(row=0, column=2, sticky="e", padx=(6, 0))
            if item.video_id:
                status.bind("<Button-1>", lambda _e, u=item.url: webbrowser.open(u))
            title = ctk.CTkEntry(card, font=font(13), fg_color=RAISED, border_color=LINE, text_color=TEXT)
            title.insert(0, item.title)
            title.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 6))
            desc = ctk.CTkTextbox(card, height=84, font=font(12), fg_color=RAISED, border_color=LINE,
                                  border_width=1, text_color=TEXT, wrap="word")
            desc.insert("1.0", item.description)
            desc.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
            self.rows.append({"item": item, "var": var, "check": check, "title": title, "desc": desc,
                              "status": status, "card": card})

        # ------------------------------------------------------------ actions
        def start(self):
            if self.running:
                return
            if not self.refresh_setup():
                self.summary.configure(text="Upload is not set up yet. Press “Setup help”.", text_color=BAD)
                self.log("Cannot upload: " + self.setup_label.cget("text").lstrip("⚠ "), "error")
                return
            chosen = []
            for row in self.rows:
                item = row["item"]
                item.selected = bool(row["var"].get()) and not item.video_id
                if item.selected:
                    item.title = clean_title(row["title"].get(), item.path.stem)
                    item.description = row["desc"].get("1.0", "end").strip()
                    chosen.append(item)
            if not chosen:
                self.summary.configure(text="Tick at least one clip to upload.", text_color=WARN)
                self.log("Nothing ticked: tick at least one clip to upload.", "warn")
                return
            self.settings.privacy = self.privacy_var.get()
            self.settings.notify_subscribers = bool(self.notify_var.get())
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
            self.progress.set(0)
            self.summary.configure(text="Starting…", text_color=MUTED)
            self.step("active")
            self.log(f"Starting upload of {self.total} clip{'s' if self.total != 1 else ''} "
                     f"(visibility: {self.settings.privacy}, notify subscribers: "
                     f"{'yes' if self.settings.notify_subscribers else 'no'}).")
            threading.Thread(target=self._work, args=(self.generation, self.cancel), daemon=True).start()

        def _work(self, generation, cancel):
            run_uploads(self.items, self.settings,
                        lambda kind, item, value: self.events.put((generation, kind, item, value)), cancel)

        def cancel_upload(self):
            if not self.running:
                return
            self.cancel.set()  # stops after the chunk that is uploading right now
            self.cancel_btn.configure(state="disabled")
            self.summary.configure(text="Cancelling after the current chunk…", text_color=WARN)
            self.log("Cancel requested. Stopping after the current chunk.", "warn")
            generation = self.generation
            self.after(2500, lambda: self._force_stop(generation))

        def _force_stop(self, generation):
            # The worker could be waiting for a Google sign-in in the browser, which cannot be interrupted.
            # Never wait for it: let the UI go on and ignore anything that old worker reports later.
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
            if kind == "auth":
                self.summary.configure(text=value, text_color=MUTED)
                self.log(str(value))
            elif kind == "start":
                self._set_status(item, "Uploading… 0%", ACCENT)
                self.summary.configure(
                    text=f"Uploading clip {self.uploaded + 1} of {self.total}: {item.title}", text_color=MUTED)
                size = item.path.stat().st_size / (1024 * 1024) if item.path.exists() else 0
                self.log(f"Uploading {item.path.name} ({size:.1f} MB) as “{item.title}”…")
            elif kind == "progress":
                self._set_status(item, f"Uploading… {int(value * 100)}%", ACCENT)
                self.progress.set((self.uploaded + value) / max(1, self.total))
                mark = int(value * 4)
                if 0 < mark < 4 and mark > self.milestones.get(item.index, 0):
                    self.milestones[item.index] = mark
                    self.log(f"  {item.path.name}: {mark * 25}% sent")
            elif kind == "done":
                self.uploaded += 1
                self._set_status(item, "Uploaded - click to open", GOOD)
                self.rows[item.index]["status"].configure(cursor="hand2")
                self.rows[item.index]["status"].bind("<Button-1>", lambda _e, u=value: webbrowser.open(u))
                self.progress.set(self.uploaded / max(1, self.total))
                self.log(f"Uploaded {item.path.name}: {value}", "ok")
            elif kind == "error":
                self._set_status(item, str(value)[:90], BAD)
                self.log(f"FAILED {item.path.name}: {value}", "error")
            elif kind == "fatal":
                self.fatal_shown = True
                self.summary.configure(text=value, text_color=BAD)
                self.log(str(value), "error")
            elif kind == "finished":
                self._end_run(cancelled=self.cancel.is_set())

        def _end_run(self, cancelled=False):
            self.running = False
            failed = self.total - self.uploaded
            privacy = self.settings.privacy
            if cancelled:
                text = f"Upload cancelled. {self.uploaded} of {self.total} uploaded."
                self.summary.configure(text=text, text_color=WARN)
                self.log(text, "warn")
                self.step("pending" if not self.uploaded else "done")
            elif self.uploaded and not failed:
                note = " They are private: open YouTube Studio to make them public." if privacy == "private" else ""
                text = f"All {self.uploaded} clip{'s' if self.uploaded != 1 else ''} uploaded ({privacy}).{note}"
                self.summary.configure(text=text, text_color=GOOD)
                self.log(text, "ok")
                self.step("done")
            else:
                if not self.fatal_shown:
                    self.summary.configure(
                        text=f"Uploaded {self.uploaded} of {self.total}. See the red lines in the log.",
                        text_color=WARN if self.uploaded else BAD)
                self.log(f"Upload finished: {self.uploaded} of {self.total} uploaded, {failed} failed.",
                         "warn" if self.uploaded else "error")
                self.step("failed")
            self.cancel_btn.configure(state="disabled")
            self.folder_btn.configure(state="normal")
            self.reload_btn.configure(state="normal")
            self._set_editable(True)
            waiting = [r for r in self.rows if not r["item"].video_id]
            self.upload_btn.configure(state="normal" if waiting else "disabled",
                                      text="Retry remaining clips" if waiting and self.uploaded + failed else
                                      "Upload to YouTube")
            self.refresh_setup()

    return UploadPanel


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
    parser = argparse.ArgumentParser(description="Upload Viral Clips Studio clips to YouTube.")
    parser.add_argument("paths", nargs="*", help="folder(s) or .mp4 file(s)")
    parser.add_argument("--privacy", choices=PRIVACY_CHOICES, help="default: YOUTUBE_PRIVACY from .env, else private")
    parser.add_argument("--notify", action="store_true", help="notify subscribers")
    parser.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--login", action="store_true", help="only sign in to Google and exit")
    args = parser.parse_args(argv)

    missing = missing_dependencies()
    if missing:
        print(f"Missing packages: {', '.join(missing)}\nInstall with:\n  {PIP_COMMAND}")
        return 1
    if args.login:
        try:
            get_credentials(print)
            print("Signed in. Login saved in", TOKEN_FILE.name)
            return 0
        except UploaderError as exc:
            print(exc)
            return 1
    if not args.paths:
        parser.print_help()
        return 1
    settings = UploadSettings.from_env()
    if args.privacy:
        settings.privacy = args.privacy
    settings.notify_subscribers = settings.notify_subscribers or args.notify
    items = [i for i in build_items(_collect_paths(args.paths))]
    waiting = [i for i in items if i.selected]
    for i in items:
        print(f"  {'[skip]' if not i.selected else '[ ok ]'} {i.path.name}\n         title: {i.title}")
    if not waiting:
        print("Nothing to upload.")
        return 0
    if not args.yes and input(f"\nUpload {len(waiting)} clip(s) as '{settings.privacy}'? [y/N] ").strip().lower() != "y":
        return 0

    def notify(kind, item, value):
        if kind in ("auth", "fatal"):
            print(value)
        elif kind == "start":
            print(f"Uploading {item.path.name} ...")
        elif kind == "progress":
            print(f"\r  {int(value * 100)}%", end="", flush=True)
        elif kind == "done":
            print(f"\r  Uploaded: {value}")
        elif kind == "error":
            print(f"\r  FAILED: {value}")

    count = run_uploads(items, settings, notify)
    print(f"\nDone: {count} of {len(waiting)} uploaded.")
    return 0 if count == len(waiting) else 2


if __name__ == "__main__":
    sys.exit(cli())
