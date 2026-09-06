# Burn

**Project site:** [https://rohansugate.github.io/burn](https://rohansugate.github.io/burn)

A local web app that takes a video URL or a file from your computer, translates the speech to English, and burns those captions into the picture.

Paste a link, wait through verify → fetch → audio → translate → burn, then download the subtitled file. Nothing is uploaded to a hosted service; processing runs on your machine.

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
