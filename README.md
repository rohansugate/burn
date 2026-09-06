# Burn

**Project site:** [https://rohansugate.github.io/burn](https://rohansugate.github.io/burn)  
**Run in browser:** [https://rohansugate.github.io/burn/app.html](https://rohansugate.github.io/burn/app.html)

A web app that takes a video URL or a file, translates the speech to English, and burns those captions into the picture. You can try an in-browser demo on the project site, or run the full pipeline locally.

Paste a link, wait through verify → fetch → audio → translate → burn, then download the subtitled file.

## Run in the browser

Open **[https://rohansugate.github.io/burn/app.html](https://rohansugate.github.io/burn/app.html)**, upload a video, and Burn will:

1. Extract audio in your tab
2. Translate speech to English with Whisper (downloaded into the browser)
3. Burn captions onto the picture
4. Let you download the result

Your file stays on your computer — it is not uploaded to GitHub or any server. The first visit downloads a small model (~75 MB). Short clips work best.

Browsers cannot fetch most website video pages (CORS), so paste-a-link for YouTube-style URLs still needs the local app below.

## What it does

1. **Verify** the URL (http/https only; local and private hosts are blocked).
2. **Download** the video with [yt-dlp](https://github.com/yt-dlp/yt-dlp). If yt-dlp cannot resolve the page, a headless Chromium session (Playwright) looks for a playable media URL.
3. **Extract** audio.
4. **Transcribe and translate** speech to English with OpenAI Whisper (`base` model, CPU).
5. **Burn** the English subtitles into the video with FFmpeg (`libass`).
6. Let you **download** the finished file from `processed_videos/`.

You can also upload a local `.mp4`, `.mkv`, `.webm`, `.mov`, `.avi`, or `.m4v` file and skip the download step.

## Features

- URL download and local file upload
- Optional **Safe Download** tab that checks a URL with VirusTotal when `VT_API_KEY` is set
- Optional Netscape `cookies.txt` for sites that need a logged-in session
- Optional Chrome profile path so Playwright can reuse your real browser cookies (useful behind Cloudflare)
- Live progress in the UI while a single job runs (Whisper is memory-heavy, so jobs are queued one at a time)

## Requirements

- Python 3.11 or newer
- Git (or download the ZIP)
- FFmpeg **with libass** (needed to burn subtitles)
- Playwright Chromium (installed in the steps below)

## Get the code

**Option A — Git**

```bash
git clone https://github.com/rohansugate/burn.git
cd burn
```

**Option B — ZIP**

1. Open [https://github.com/rohansugate/burn](https://github.com/rohansugate/burn)
2. Click **Code → Download ZIP**
3. Unzip it and open that folder in a terminal (macOS/Linux) or PowerShell / Command Prompt (Windows)

Direct ZIP: [https://github.com/rohansugate/burn/archive/refs/heads/main.zip](https://github.com/rohansugate/burn/archive/refs/heads/main.zip)

## Install by operating system

### macOS

1. Install [Homebrew](https://brew.sh) if you don’t have it, then:

   ```bash
   brew install python git ffmpeg-full
   ```

   `ffmpeg-full` includes libass. Confirm with:

   ```bash
   ffmpeg -hide_banner -filters | grep subtitles
   ```

2. From the project folder:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   playwright install chromium
   ```

3. Start the app:

   ```bash
   chmod +x start.sh
   ./start.sh
   ```

   Or: `uvicorn backend.app:app --host 127.0.0.1 --port 8000`

### Linux

Works on Ubuntu/Debian, Fedora, Arch, and most other distros.

1. Install system packages.

   **Ubuntu / Debian**

   ```bash
   sudo apt update
   sudo apt install -y python3 python3-venv python3-pip git ffmpeg
   ```

   **Fedora**

   ```bash
   sudo dnf install -y python3 python3-pip git ffmpeg
   ```

   **Arch**

   ```bash
   sudo pacman -S --needed python python-pip git ffmpeg
   ```

   Distro FFmpeg builds usually include libass. Confirm with:

   ```bash
   ffmpeg -hide_banner -filters | grep subtitles
   ```

2. From the project folder:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   playwright install chromium
   ```

   If `playwright install chromium` fails on Linux, install the OS deps it prints, or run:

   ```bash
   playwright install-deps chromium
   ```

3. Start the app:

   ```bash
   chmod +x start.sh
   ./start.sh
   ```

### Windows

1. Install:
   - [Python 3.11+](https://www.python.org/downloads/) — check **Add python.exe to PATH**
   - [Git](https://git-scm.com/download/win) (skip if you used the ZIP)
   - FFmpeg with libass, using **winget** (Windows 11 / modern 10):

     ```powershell
     winget install -e --id Python.Python.3.12
     winget install -e --id Git.Git
     winget install -e --id Gyan.FFmpeg
     ```

     Or [Chocolatey](https://chocolatey.org): `choco install python git ffmpeg`

     Or download a **full** FFmpeg build from [https://www.gyan.dev/ffmpeg/builds/](https://www.gyan.dev/ffmpeg/builds/) and add its `bin` folder to PATH.

   Open a **new** PowerShell window after installing. Confirm:

   ```powershell
   python --version
   ffmpeg -hide_banner -filters | findstr subtitles
   ```

2. From the project folder:

   ```powershell
   py -3 -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   playwright install chromium
   ```

   If `py` is not found, use `python` instead of `py -3`.

3. Start the app:

   ```powershell
   .\start.cmd
   ```

   Or:

   ```powershell
   uvicorn backend.app:app --host 127.0.0.1 --port 8000
   ```

## Open the app

After the server starts, open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

`start.sh` (macOS/Linux) and `start.cmd` (Windows) create `.venv`, install Python packages if needed, and start Uvicorn.

Optional Chrome profile paths if a site needs your real browser cookies:

- macOS: `~/Library/Application Support/Google/Chrome/Default`
- Linux: `~/.config/google-chrome/Default`
- Windows: `%LOCALAPPDATA%\Google\Chrome\User Data\Default`

## Privacy

Cookies, your Chrome profile path, downloaded sources, and burned outputs stay on disk and are gitignored:

- `data/` — session cookies and browser profile path
- `tmp/` — in-progress downloads, audio, and subtitle files
- `processed_videos/` — finished videos

Do not commit those folders. They are local-only.

## Optional: VirusTotal

For the Safe Download tab, set:

```bash
export VT_API_KEY=your_virustotal_api_key
```

Without it, the safety check reports that no API key is configured.
