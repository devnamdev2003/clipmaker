"""
Viral Clips Studio - Setup Script
---------------------------------
Run once to install dependencies, check FFmpeg, and create a template .env file:
    python setup.py
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
PACKAGES = [
    "customtkinter",
    "yt-dlp",
    "python-dotenv",
    "google-genai",
]


def run(cmd):
    print(f"  > {' '.join(cmd)}")
    return subprocess.run(cmd, check=False).returncode


def main():
    print("=" * 56)
    print("         Viral Clips Studio - Automatic Setup")
    print("=" * 56)
    print(f"Python: {sys.version.split()[0]} ({sys.executable})\n")

    # 1. Upgrade pip & install required Python packages
    print("[1/3] Installing Python packages...")
    run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])
    code = run([sys.executable, "-m", "pip", "install", "--upgrade", *PACKAGES])
    if code != 0:
        print("\n[ERROR] Package installation failed. Check your internet connection and try again.")
        sys.exit(1)
    print("      Packages installed.\n")

    # 2. Check FFmpeg and ffprobe
    print("[2/3] Checking FFmpeg and ffprobe...")
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        print("      FFmpeg and ffprobe are already installed.\n")
    else:
        print("      FFmpeg/ffprobe not found on PATH.")
        if os.name == "nt" and shutil.which("winget"):
            print("      Attempting to install FFmpeg via winget...")
            rc = run([
                "winget", "install", "--id", "Gyan.FFmpeg", "-e",
                "--accept-source-agreements", "--accept-package-agreements",
            ])
            if rc == 0:
                print("      FFmpeg installed! Close and reopen PowerShell so PATH refreshes.\n")
            else:
                print("      [WARN] winget install failed. Install manually with:")
                print("             winget install Gyan.FFmpeg\n")
        else:
            print("      Install FFmpeg manually:")
            print("        Windows: winget install Gyan.FFmpeg")
            print("        macOS:   brew install ffmpeg")
            print("        Linux:   sudo apt install ffmpeg\n")

    # 3. Create template .env file if missing
    print("[3/3] Checking .env file...")
    if ENV_FILE.exists():
        print("      .env already exists (kept existing settings).\n")
    else:
        ENV_FILE.write_text(
            "GEMINI_API_KEY=\n"
            "GEMINI_MODEL=gemini-3.5-flash-lite\n"
            "COOKIES_BROWSER=None\n",
            encoding="utf-8",
        )
        print(f"      Created {ENV_FILE.name}. Add your Gemini API key via Settings in the app.\n")

    print("=" * 56)
    print("                    Setup Complete!")
    print("=" * 56)
    print("Next steps:")
    print("  1. (Optional) Put your exported YouTube cookies.txt next to main.py")
    print("     if YouTube asks for a sign-in check.")
    print("  2. Start the app:  python main.py")


if __name__ == "__main__":
    main()
