# Burn

<p align="center">
  <a href="https://rohansugate.github.io/burn/app.html"><img src="https://img.shields.io/badge/▶_Run_in_browser-live-e07a3a?style=for-the-badge" alt="Run in browser" /></a>
  <a href="https://rohansugate.github.io/burn"><img src="https://img.shields.io/badge/Project_site-rohansugate.github.io-1c1814?style=for-the-badge" alt="Project site" /></a>
</p>

**Run it now:** [https://rohansugate.github.io/burn/app.html](https://rohansugate.github.io/burn/app.html)

A web app that takes a video URL or a file, translates the speech to English, and burns those captions into the picture. You can try an in-browser demo on the project site, or run the full pipeline locally.

Paste a link, wait through verify → fetch → audio → translate → burn, then download the subtitled file.

## Run in the browser

Open **[https://rohansugate.github.io/burn/app.html](https://rohansugate.github.io/burn/app.html)** and pick **Upload**, **URL**, or **Safe Download**. Safe Download checks the link first (scheme, host, dangerous file types, and a public malware list when the browser allows it), then burns captions if it passes.

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

- Python 3.11+
- FFmpeg with subtitle support (`libass`). On macOS with Homebrew:

  ```bash
  brew install ffmpeg-full
  ```

- Playwright’s Chromium browser (installed after Python deps):

  ```bash
  .venv/bin/playwright install chromium
  ```

## Run

From the project root:

```bash
chmod +x start.sh
./start.sh
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

`start.sh` creates a virtualenv, installs `requirements.txt` if needed, and starts Uvicorn.

To run the server yourself:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

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
