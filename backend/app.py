"""
Local media download + English subtitle burn-in service.
Run from project root:
  uvicorn backend.app:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import re
import requests
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import tqdm as tqdm_module
import whisper
import yt_dlp
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from tqdm import tqdm as std_tqdm

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
PROCESSED_DIR = ROOT / "processed_videos"
TMP_DIR = ROOT / "tmp"
DATA_DIR = ROOT / "data"
COOKIES_PATH = DATA_DIR / "cookies.txt"
BROWSER_PROFILE_PATH_FILE = DATA_DIR / "browser_profile_path.txt"
FRONTEND_DIR = ROOT / "frontend"

for _d in (PROCESSED_DIR, TMP_DIR, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024 * 1024  # 10 GiB
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}


def _resolve_ffmpeg_bins() -> tuple[str, str]:
    """Prefer Homebrew ffmpeg-full (has libass/subtitles) over plain ffmpeg."""
    candidates = [
        Path("/opt/homebrew/opt/ffmpeg-full/bin"),
        Path("/usr/local/opt/ffmpeg-full/bin"),
    ]
    for base in candidates:
        ff = base / "ffmpeg"
        fp = base / "ffprobe"
        if ff.is_file() and fp.is_file():
            return str(ff), str(fp)
    return "ffmpeg", "ffprobe"


FFMPEG_BIN, FFPROBE_BIN = _resolve_ffmpeg_bins()

# Progress ranges (inclusive start, exclusive end of next stage)
# verify 0-5 | download 5-35 | extract 35-40 | translate 40-90 | burn 90-100

# ---------------------------------------------------------------------------
# Job store + single-worker queue (Whisper is memory-heavy; run one job at a time)
# ---------------------------------------------------------------------------

_jobs_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_job_queue: queue.Queue[tuple[str, str, str | None]] = queue.Queue()
_MAX_CONCURRENT_JOBS = 1
_worker_started = False
_worker_lock = threading.Lock()


def _new_job(url: str) -> str:
    job_id = uuid.uuid4().hex[:10]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "url": url,
            "status": "queued",
            "stage": "queued",
            "message": "Queued…",
            "percent": 0,
            "filename": None,
            "download_url": None,
            "error": None,
        }
    return job_id


def update_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.update(fields)


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Subtitle Burn-In Tool")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

whisper_model = None
_whisper_load_error: str | None = None


def get_whisper_model():
    """Load Whisper once, on first use (avoids crashing the whole server on SSL/download issues)."""
    global whisper_model, _whisper_load_error
    if whisper_model is not None:
        return whisper_model
    try:
        # CPU is more reliable than MPS with current torch+whisper on macOS
        whisper_model = whisper.load_model("base", device="cpu")
        _whisper_load_error = None
        return whisper_model
    except Exception as exc:  # noqa: BLE001
        _whisper_load_error = str(exc)
        raise RuntimeError(f"Failed to load speech model: {exc}") from exc


@app.on_event("startup")
def warm_whisper_model() -> None:
    """Best-effort preload; server still starts if download fails."""
    try:
        get_whisper_model()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class VideoRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)

    @field_validator("url")
    @classmethod
    def strip_url(cls, value: str) -> str:
        return value.strip()


class BrowserProfileRequest(BaseModel):
    path: str = Field(..., min_length=3, max_length=4096)


# ---------------------------------------------------------------------------
# URL / download safety
# ---------------------------------------------------------------------------

_BLOCKED_HOST_SUFFIXES = (
    ".onion",
    ".local",
    ".internal",
    ".localhost",
)


def _is_private_or_local_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


class PipelineError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def validate_media_url(url: str) -> str:
    """Reject non-http(s) URLs, local/private hosts, and malformed links."""
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise PipelineError("Only http and https URLs are allowed.")

    if not parsed.netloc or parsed.username or parsed.password:
        raise PipelineError("Invalid or unsafe URL.")

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise PipelineError("URL is missing a hostname.")

    if host == "localhost" or any(host.endswith(s) for s in _BLOCKED_HOST_SUFFIXES):
        raise PipelineError("Local or internal hosts are not allowed.")

    path_lower = (parsed.path or "").lower()
    dangerous_exts = (
        ".exe",
        ".bat",
        ".cmd",
        ".msi",
        ".scr",
        ".js",
        ".vbs",
        ".ps1",
        ".sh",
        ".apk",
        ".dmg",
        ".pkg",
    )
    if any(path_lower.endswith(ext) for ext in dangerous_exts):
        raise PipelineError("URL points to a disallowed file type.")

    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if _is_private_or_local_ip(host):
            raise PipelineError("Private or local IP addresses are not allowed.")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise PipelineError(f"Could not resolve host: {host}") from exc

    if not infos:
        raise PipelineError(f"Could not resolve host: {host}")

    for info in infos:
        ip_str = info[4][0]
        if _is_private_or_local_ip(ip_str):
            raise PipelineError("URL resolves to a private or local address and was blocked.")

    return url


def _cookies_opts() -> dict:
    if COOKIES_PATH.is_file() and COOKIES_PATH.stat().st_size > 0:
        return {"cookiefile": str(COOKIES_PATH)}
    return {}


def resolve_media_url(url: str) -> str:
    """
    Resolve a page URL to a direct media URL.

    Tries yt-dlp first; if the extractor reports 'Unsupported URL', falls
    back to a headless Chromium browser via Playwright to scrape the page
    for a <video>, <source>, <iframe>, or network-request media URL.

    If a browser profile path is configured, Playwright will launch with
    that profile so cookies/localStorage from your real browser are reused.
    """
    browser_profile = None
    if BROWSER_PROFILE_PATH_FILE.is_file():
        candidate = BROWSER_PROFILE_PATH_FILE.read_text(encoding="utf-8").strip()
        if candidate and Path(candidate).is_dir():
            browser_profile = candidate

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "noplaylist": True,
        "extractor_args": {"generic": {"impersonate": ["chrome"]}},
        **_cookies_opts(),
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info and info.get("url"):
            return info["url"]
        if info and info.get("formats"):
            for fmt in reversed(info["formats"]):
                if fmt.get("url"):
                    return fmt["url"]
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        if "Unsupported URL" not in msg and "Unsupported URL" not in str(exc):
            raise
    except Exception as exc:  # noqa: BLE001
        raise PipelineError(f"Failed to resolve media URL: {exc}") from exc

    from backend.browser_extractor import extract_video_url

    try:
        temp_cookie_path = TMP_DIR / f"browser_cookies_{uuid.uuid4().hex[:10]}.txt"
        resolved = extract_video_url(
            url,
            user_data_dir=browser_profile,
            export_cookies_to=temp_cookie_path,
        )
        if temp_cookie_path.is_file() and temp_cookie_path.stat().st_size > 0:
            COOKIES_PATH.write_bytes(temp_cookie_path.read_bytes())
        temp_cookie_path.unlink(missing_ok=True)
        return resolved
    except Exception as exc:  # noqa: BLE001
        raise PipelineError(
            f"Could not resolve media URL from page. "
            f"Content may be DRM-protected or require a supported platform. ({exc})"
        ) from exc


def probe_media_url(url: str) -> dict:
    """Use yt-dlp metadata only (no download) to confirm the URL is media."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "noplaylist": True,
        "extractor_args": {"generic": {"impersonate": ["chrome"]}},
        **_cookies_opts(),
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise PipelineError(f"Link could not be verified as downloadable media: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise PipelineError(f"Failed to verify link: {exc}") from exc

    if not info:
        raise PipelineError("No media metadata returned for this URL.")

    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if not entries:
            raise PipelineError("Playlist has no entries.")
        info = entries[0]

    duration = info.get("duration")
    if duration is not None and duration > 6 * 60 * 60:
        raise PipelineError("Media is longer than the 6-hour safety limit.")

    filesize = info.get("filesize") or info.get("filesize_approx")
    if filesize and filesize > MAX_DOWNLOAD_BYTES:
        raise PipelineError("Media exceeds the 10 GiB download limit.")

    return info


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[^\w\s\-.]", "", name, flags=re.UNICODE)
    name = re.sub(r"\s+", "_", name.strip())
    return (name[:80] or "video").strip("._")


def verify_downloaded_video(path: Path, job_id: str | None = None) -> None:
    if not path.is_file():
        raise PipelineError("Download did not produce a file.")

    if path.stat().st_size == 0:
        raise PipelineError("Downloaded file is empty.")

    if path.stat().st_size > MAX_DOWNLOAD_BYTES:
        path.unlink(missing_ok=True)
        raise PipelineError("Downloaded file exceeds size limit.")

    try:
        result = subprocess.run(
            [
                FFPROBE_BIN,
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise PipelineError("ffprobe not found. Install ffmpeg and ensure it is on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise PipelineError("Media verification timed out.") from exc

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "unknown error")[-300:]
        try:
            with path.open("rb") as f:
                header = f.read(16)
            if header.startswith((b"\x00\x00\x00\x18ftyp", b"\x00\x00\x00\x20ftyp", b"ftyp")):
                return path
            if header.startswith((b"RIFF", b"WEBP", b"matroska")):
                return path
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise PipelineError(f"Downloaded file could not be parsed as media and was discarded. {err}")

    try:
        probe = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        probe = {}

    streams = probe.get("streams", [])
    stream_types = {s.get("codec_type") for s in streams if s.get("codec_type")}
    if not stream_types:
        path.unlink(missing_ok=True)
        raise PipelineError("Downloaded file has no media streams and was discarded.")

    if "video" not in stream_types and "audio" not in stream_types:
        path.unlink(missing_ok=True)
        raise PipelineError("Downloaded file is not a valid media file and was discarded.")


def probe_duration_seconds(path: Path) -> float | None:
    try:
        result = subprocess.run(
            [
                FFPROBE_BIN,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    try:
        return float((result.stdout or "").strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Pipeline helpers
# ---------------------------------------------------------------------------


def download_video(url: str, work_dir: Path, job_id: str) -> Path:
    outtmpl = str(work_dir / "source.%(ext)s")

    def progress_hook(d: dict) -> None:
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                frac = max(0.0, min(1.0, done / total))
                pct = 5 + frac * 30
                update_job(
                    job_id,
                    status="running",
                    stage="downloading",
                    percent=round(pct, 1),
                    message=f"Downloading video… {int(frac * 100)}%",
                )
            else:
                update_job(
                    job_id,
                    status="running",
                    stage="downloading",
                    percent=10,
                    message="Downloading video…",
                )
        elif status == "finished":
            update_job(
                job_id,
                status="running",
                stage="downloading",
                percent=35,
                message="Download finished, preparing file…",
            )

    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "socket_timeout": 30,
        "progress_hooks": [progress_hook],
        "extractor_args": {"generic": {"impersonate": ["chrome"]}},
        **_cookies_opts(),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    candidates = sorted(work_dir.glob("source.*"))
    if not candidates:
        raise PipelineError("Download finished but no file was found.")
    return candidates[0]


def format_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    millis = int(round(seconds * 1000))
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def segments_to_srt(segments: list) -> str:
    lines: list[str] = []
    for i, seg in enumerate(segments, start=1):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = format_timestamp(float(seg["start"]))
        end = format_timestamp(float(seg["end"]))
        lines.append(str(i))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def write_srt(segments: list, srt_path: Path) -> None:
    srt_path.write_text(segments_to_srt(segments), encoding="utf-8")


def extract_audio_wav(video_path: Path, wav_path: Path, job_id: str) -> Path:
    update_job(
        job_id,
        status="running",
        stage="extracting",
        percent=36,
        message="Extracting audio…",
    )

    duration = probe_duration_seconds(video_path)
    if duration is not None and duration <= 0:
        raise PipelineError("Downloaded video has no usable duration; cannot extract audio.")

    try:
        result = subprocess.run(
            [
                FFPROBE_BIN,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,sample_rate,channels",
                "-of",
                "default=noprint_wrappers=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        result = None

    has_audio = False
    if result and result.returncode == 0:
        out = (result.stdout or "").lower()
        if "codec_name=" in out and "sample_rate=" in out:
            has_audio = True

    if not has_audio:
        raise PipelineError("Downloaded video has no usable audio track.")

    cmd = [
        FFMPEG_BIN,
        "-y",
        "-err_detect",
        "ignore_err",
        "-fflags",
        "+genpts+discardcorrupt",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-vsync",
        "0",
        "-async",
        "1",
        str(wav_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=600)
    except FileNotFoundError as exc:
        raise PipelineError("ffmpeg not found. Install ffmpeg and ensure it is on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise PipelineError("Audio extraction timed out.") from exc

    if result.returncode != 0 or not wav_path.is_file() or wav_path.stat().st_size < 1000:
        err = (result.stderr or result.stdout or "unknown error")[-800:]
        raise PipelineError(f"Could not extract audio from the video (no usable audio track?). {err}")

    update_job(
        job_id,
        status="running",
        stage="extracting",
        percent=40,
        message="Audio ready. Starting translation…",
    )
    return wav_path


def translate_audio(model, audio_path: Path, job_id: str) -> list:
    """Run Whisper translate; report progress via a tqdm subclass."""

    class JobTqdm(std_tqdm):
        def update(self, n: float = 1):  # type: ignore[override]
            result = super().update(n)
            total = self.total or 0
            if total:
                frac = max(0.0, min(1.0, float(self.n) / float(total)))
                pct = 40 + frac * 50
                update_job(
                    job_id,
                    status="running",
                    stage="translating",
                    percent=round(pct, 1),
                    message=f"Translating speech to English… {int(frac * 100)}%",
                )
            return result

    # whisper.transcribe does `import tqdm` then `tqdm.tqdm(...)`.
    # Do not use `import whisper.transcribe` — whisper.transcribe is the function.
    original_tqdm = tqdm_module.tqdm
    tqdm_module.tqdm = JobTqdm  # type: ignore[assignment]
    update_job(
        job_id,
        status="running",
        stage="translating",
        percent=40,
        message="Translating speech to English… 0%",
    )
    try:
        result = model.transcribe(
            str(audio_path),
            task="translate",
            fp16=False,
            condition_on_previous_text=False,
            temperature=0.0,
            verbose=False,
        )
    except RuntimeError as exc:
        raise PipelineError(f"Speech translation failed: {exc}") from exc
    finally:
        tqdm_module.tqdm = original_tqdm  # type: ignore[assignment]

    update_job(
        job_id,
        status="running",
        stage="translating",
        percent=90,
        message="Translation complete. Burning subtitles…",
    )
    return result.get("segments") or []


def ffmpeg_subtitles_filter(srt_path: Path) -> tuple[str, Path]:
    """Build a subtitles= filter using a space-free /tmp path (no quoting)."""
    # Copy to a space-free path — ffmpeg's subtitles filter breaks on spaces in paths.
    safe_srt = Path("/tmp") / f"burn_subs_{uuid.uuid4().hex[:10]}.srt"
    safe_srt.write_bytes(srt_path.read_bytes())
    # Bare path form works with libass/ffmpeg-full; avoid quotes which confuse the parser.
    return f"subtitles={safe_srt}", safe_srt


def burn_subtitles(video_path: Path, srt_path: Path, output_path: Path, job_id: str) -> None:
    duration = probe_duration_seconds(video_path) or 0
    vf, safe_srt = ffmpeg_subtitles_filter(srt_path)
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-i",
        str(video_path),
        "-vf",
        vf,
        "-c:a",
        "copy",
        "-progress",
        "pipe:1",
        "-nostats",
        str(output_path),
    ]
    update_job(
        job_id,
        status="running",
        stage="burning",
        percent=91,
        message="Burning subtitles into video… 0%",
    )
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        safe_srt.unlink(missing_ok=True)
        raise PipelineError("ffmpeg not found. Install ffmpeg and ensure it is on PATH.") from exc

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("out_time_ms=") or not duration:
                continue
            try:
                out_ms = int(line.split("=", 1)[1])
            except ValueError:
                continue
            frac = max(0.0, min(1.0, (out_ms / 1_000_000) / duration))
            pct = 90 + frac * 9
            update_job(
                job_id,
                status="running",
                stage="burning",
                percent=round(pct, 1),
                message=f"Burning subtitles into video… {int(frac * 100)}%",
            )
        returncode = proc.wait(timeout=3600)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        safe_srt.unlink(missing_ok=True)
        raise PipelineError("Subtitle burn-in timed out.") from exc

    stderr = ""
    if proc.stderr is not None:
        stderr = proc.stderr.read()[-800:]

    safe_srt.unlink(missing_ok=True)

    if returncode != 0 or not output_path.is_file():
        raise PipelineError(f"ffmpeg failed: {stderr or 'unknown ffmpeg error'}")


def cleanup_work_dir(work_dir: Path) -> None:
    if not work_dir.exists():
        return
    try:
        for child in work_dir.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)
        work_dir.rmdir()
    except OSError:
        pass


def run_pipeline(job_id: str, url: str, video_path: str | None = None) -> None:
    work_dir = TMP_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        if video_path:
            update_job(
                job_id,
                status="running",
                stage="verifying",
                percent=2,
                message="Upload received. Preparing…",
            )
            model = get_whisper_model()
            src_path = Path(video_path)
            verify_downloaded_video(src_path, job_id)
            title = sanitize_filename(src_path.stem)
            info = {"title": title}
        else:
            update_job(
                job_id,
                status="running",
                stage="verifying",
                percent=2,
                message="Verifying link…",
            )
            model = get_whisper_model()
            safe_url = validate_media_url(url)
            resolved_url = resolve_media_url(safe_url)
            info = probe_media_url(resolved_url)
            update_job(
                job_id,
                status="running",
                stage="verifying",
                percent=5,
                message="Link verified. Starting download…",
            )

            src_path = download_video(resolved_url, work_dir, job_id)
            verify_downloaded_video(src_path, job_id)

        title = sanitize_filename(info.get("title") or "video")
        output_name = f"{title}_{job_id}.mp4"
        output_path = PROCESSED_DIR / output_name
        srt_path = work_dir / "subs.srt"
        wav_path = work_dir / "audio.wav"

        extract_audio_wav(src_path, wav_path, job_id)

        segments = translate_audio(model, wav_path, job_id)
        if not segments:
            raise PipelineError("No speech detected to translate into subtitles.")

        write_srt(segments, srt_path)
        burn_subtitles(src_path, srt_path, output_path, job_id)

        update_job(
            job_id,
            status="done",
            stage="done",
            percent=100,
            message="Done. Your subtitled video is ready.",
            filename=output_name,
            download_url=f"/download/{output_name}",
            error=None,
        )
    except PipelineError as exc:
        update_job(
            job_id,
            status="error",
            stage="error",
            message=exc.message,
            error=exc.message,
        )
    except Exception as exc:  # noqa: BLE001
        update_job(
            job_id,
            status="error",
            stage="error",
            message=f"Conversion failed: {exc}",
            error=str(exc),
        )
    finally:
        cleanup_work_dir(work_dir)


def _enqueue_existing_job(job_id: str, url: str, video_path: str | None = None) -> None:
    """Put an already-created job on the single-worker queue."""
    queued_ahead = _job_queue.qsize()
    with _jobs_lock:
        busy = any(
            j.get("status") == "running" and j["id"] != job_id for j in _jobs.values()
        )
    if queued_ahead > 0:
        update_job(
            job_id,
            status="queued",
            stage="queued",
            percent=0,
            message=f"Queued — {queued_ahead} job(s) ahead…",
        )
    elif busy:
        update_job(
            job_id,
            status="queued",
            stage="queued",
            percent=0,
            message="Queued — waiting for the current job to finish…",
        )
    _ensure_worker()
    _job_queue.put((job_id, url, video_path))


def start_job(url: str, video_path: str | None = None) -> str:
    """Create and enqueue a conversion job. A single worker runs jobs one at a time."""
    job_id = _new_job(url)
    _enqueue_existing_job(job_id, url, video_path)
    return job_id


def _pipeline_worker() -> None:
    while True:
        job_id, url, video_path = _job_queue.get()
        try:
            run_pipeline(job_id, url, video_path)
        except Exception as exc:  # noqa: BLE001
            update_job(
                job_id,
                status="error",
                stage="error",
                message=f"Conversion failed: {exc}",
                error=str(exc),
            )
        finally:
            _job_queue.task_done()


def _ensure_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        for _ in range(_MAX_CONCURRENT_JOBS):
            thread = threading.Thread(target=_pipeline_worker, daemon=True, name="pipeline-worker")
            thread.start()
        _worker_started = True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    with _jobs_lock:
        running = sum(1 for j in _jobs.values() if j.get("status") == "running")
        queued = sum(1 for j in _jobs.values() if j.get("status") == "queued")
    return {
        "ok": True,
        "whisper_ready": whisper_model is not None,
        "cookies_configured": COOKIES_PATH.is_file() and COOKIES_PATH.stat().st_size > 0,
        "ffmpeg": FFMPEG_BIN,
        "ffprobe": FFPROBE_BIN,
        "max_concurrent_jobs": _MAX_CONCURRENT_JOBS,
        "jobs_running": running,
        "jobs_queued": queued,
    }


@app.post("/cookies/")
async def upload_cookies(file: UploadFile = File(...)) -> dict:
    """Upload a Netscape-format cookies.txt for authenticated downloads."""
    filename = (file.filename or "").lower()
    if filename and not (filename.endswith(".txt") or filename.endswith(".cookies")):
        raise HTTPException(
            status_code=400,
            detail="Upload a Netscape cookies .txt file (e.g. cookies.txt).",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Cookies file is empty.")
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Cookies file is too large.")

    text = raw.decode("utf-8", errors="replace")
    if "http" not in text.lower() and "#netscape" not in text.lower() and "\t" not in text:
        raise HTTPException(
            status_code=400,
            detail="File does not look like a Netscape cookies.txt export.",
        )

    COOKIES_PATH.write_bytes(raw)
    return {"ok": True, "message": "Cookies saved. They will be used for downloads."}


@app.delete("/cookies/")
def clear_cookies() -> dict:
    COOKIES_PATH.unlink(missing_ok=True)
    return {"ok": True, "message": "Cookies cleared."}


@app.get("/browser-profile/")
def get_browser_profile() -> dict:
    path = ""
    if BROWSER_PROFILE_PATH_FILE.is_file():
        candidate = BROWSER_PROFILE_PATH_FILE.read_text(encoding="utf-8").strip()
        if candidate and Path(candidate).is_dir():
            path = candidate
    return {"ok": True, "path": path}


@app.post("/browser-profile/")
def set_browser_profile(payload: BrowserProfileRequest) -> dict:
    path = payload.path.strip()
    if not path:
        BROWSER_PROFILE_PATH_FILE.unlink(missing_ok=True)
        return {"ok": True, "message": "Browser profile cleared. Using isolated profile.", "path": ""}
    p = Path(path)
    if not p.is_dir():
        raise HTTPException(status_code=400, detail="Path does not exist or is not a directory.")
    BROWSER_PROFILE_PATH_FILE.write_text(path, encoding="utf-8")
    return {"ok": True, "message": "Browser profile saved. Extraction will reuse your browser cookies.", "path": path}


@app.delete("/browser-profile/")
def clear_browser_profile() -> dict:
    BROWSER_PROFILE_PATH_FILE.unlink(missing_ok=True)
    return {"ok": True, "message": "Browser profile cleared."}


@app.post("/convert/")
def convert_video(payload: VideoRequest) -> dict:
    """Queue a conversion job and return a job id for progress polling."""
    try:
        validate_media_url(payload.url)
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    job_id = start_job(payload.url)
    return {
        "ok": True,
        "job_id": job_id,
        "status_url": f"/status/{job_id}",
    }


@app.post("/upload/")
async def upload_video(file: UploadFile = File(...)) -> dict:
    """Upload a local video file and queue it for subtitle burn-in."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename.")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_VIDEO_EXTENSIONS))}",
        )

    job_id = _new_job(f"upload:{file.filename}")
    work_dir = TMP_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    dest = work_dir / f"source{suffix}"

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(raw) > MAX_DOWNLOAD_BYTES:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Uploaded file exceeds the 10 GiB limit.")

    dest.write_bytes(raw)
    _enqueue_existing_job(job_id, file.filename, str(dest))
    return {
        "ok": True,
        "job_id": job_id,
        "status_url": f"/status/{job_id}",
    }


# ---------------------------------------------------------------------------
# Safety / safe-download
# ---------------------------------------------------------------------------

def _url_has_dangerous_extension(url: str) -> bool:
    url = url.lower()
    return any(url.endswith(ext) for ext in (".exe", ".bat", ".cmd", ".sh", ".apk", ".msi", ".js", ".vbs"))


def _basic_safety_checks(url: str) -> tuple[bool, str]:
    try:
        result = urlparse(url)
    except Exception as exc:  # noqa: BLE001
        return False, f"Invalid URL: {exc}"

    if result.scheme not in {"http", "https"}:
        return False, "Only http/https URLs are allowed."

    host = (result.hostname or "").lower()
    if not host:
        return False, "URL has no hostname."

    for suffix in (".onion", ".local", ".internal", ".localhost"):
        if host == suffix or host.endswith("." + suffix):
            return False, "Local/internal hosts are blocked."

    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False, "Private/reserved IP addresses are blocked."
    except ValueError:
        pass

    if _url_has_dangerous_extension(url):
        return False, "URL points to a potentially dangerous executable."

    return True, "Basic URL checks passed."


def _vt_headers() -> dict[str, str]:
    api_key = os.getenv("VT_API_KEY")
    if not api_key:
        return {}
    return {"x-apikey": api_key}


def _vt_check_url(url: str) -> dict:
    headers = _vt_headers()
    if not headers:
        return {"enabled": False}

    try:
        resp = requests.post(
            "https://www.virustotal.com/api/v3/urls",
            headers=headers,
            data={"url": url},
            timeout=20,
        )
        if resp.status_code != 200:
            return {"enabled": True, "error": f"VirusTotal API error: {resp.status_code}"}
        data = resp.json()
        analysis_id = data.get("data", {}).get("id")
        if not analysis_id:
            return {"enabled": True, "error": "No analysis id returned."}

        analysis_url = f"https://www.virustotal.com/api/v3/analyses/{analysis_id}"
        for _ in range(10):
            time.sleep(2)
            a_resp = requests.get(analysis_url, headers=headers, timeout=20)
            if a_resp.status_code != 200:
                continue
            a_data = a_resp.json()
            stats = a_data.get("data", {}).get("attributes", {}).get("stats", {})
            malicious = stats.get("malicious", 0)
            suspicious = stats.get("suspicious", 0)
            return {
                "enabled": True,
                "malicious": malicious,
                "suspicious": suspicious,
                "stats": stats,
            }
        return {"enabled": True, "error": "Analysis did not complete in time."}
    except Exception as exc:  # noqa: BLE001
        return {"enabled": True, "error": str(exc)}


@app.post("/safety-check/")
def safety_check(payload: VideoRequest) -> dict:
    """
    Lightweight safety check for a URL.

    Returns:
    - ok: true if basic checks pass
    - verdict: safe | risky | unsafe
    - checks: list of performed checks and results
    - virustotal: optional VirusTotal results (if VT_API_KEY is configured)
    """
    url = payload.url.strip()
    checks = []
    safe = True

    ok, msg = _basic_safety_checks(url)
    checks.append({"name": "basic", "ok": ok, "message": msg})
    if not ok:
        safe = False

    vt = _vt_check_url(url)
    checks.append({"name": "virustotal", "details": vt})

    if vt.get("enabled"):
        malicious = vt.get("malicious", 0)
        suspicious = vt.get("suspicious", 0)
        if malicious > 0 or suspicious > 0:
            safe = False
            checks[-1]["ok"] = False
            checks[-1]["message"] = f"VirusTotal flagged {malicious} malicious / {suspicious} suspicious."
        else:
            checks[-1]["ok"] = True
            checks[-1]["message"] = "VirusTotal did not flag this URL."
    else:
        checks[-1]["ok"] = True
        checks[-1]["message"] = "VirusTotal not configured; scan skipped."

    verdict = "unsafe" if any(not c.get("ok", True) for c in checks) else "safe"
    if verdict == "safe" and not vt.get("enabled"):
        verdict = "safe"

    return {
        "ok": safe,
        "verdict": verdict,
        "checks": checks,
        "message": "URL is safe to download." if safe else "URL is NOT safe to download.",
    }


@app.post("/safe-download/")
def safe_download(payload: VideoRequest) -> dict:
    """
    Run safety checks first; if the URL is safe, queue the normal download pipeline.
    """
    url = payload.url.strip()

    check_resp = safety_check(payload)
    if not check_resp.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=f"URL failed safety checks: {check_resp.get('message')}",
        )

    job_id = start_job(url)
    return {
        "ok": True,
        "job_id": job_id,
        "status_url": f"/status/{job_id}",
        "safety": check_resp,
    }


@app.post("/check-url/")
def check_url(payload: VideoRequest) -> dict:
    """
    Lightweight check: verify whether the given URL can be handled by the
    download pipeline without starting a job.

    Returns:
    - ok: true if the URL looks downloadable
    - supported: true if yt-dlp or the browser fallback can resolve it
    - message: human-readable status
    """
    url = payload.url.strip()

    try:
        validate_media_url(url)
    except PipelineError as exc:
        return {
            "ok": False,
            "supported": False,
            "message": exc.message,
        }

    try:
        resolved = resolve_media_url(url)
        return {
            "ok": True,
            "supported": True,
            "message": "URL is supported and ready for processing.",
        }
    except PipelineError as exc:
        return {
            "ok": False,
            "supported": False,
            "message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "supported": False,
            "message": f"Could not resolve this URL: {exc}",
        }


@app.get("/status/{job_id}")
def job_status(job_id: str) -> dict:
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return {
        "ok": True,
        "id": job["id"],
        "status": job["status"],
        "stage": job["stage"],
        "message": job["message"],
        "percent": job["percent"],
        "filename": job["filename"],
        "download_url": job["download_url"],
        "error": job["error"],
    }


@app.get("/download/{filename}")
def download_file(filename: str):
    safe_name = Path(filename).name
    if safe_name != filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")

    path = PROCESSED_DIR / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")

    return FileResponse(
        path,
        media_type="video/mp4",
        filename=safe_name,
    )


@app.get("/")
def index() -> FileResponse:
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.is_file():
        raise HTTPException(status_code=500, detail="Frontend is missing.")
    return FileResponse(index_path)
