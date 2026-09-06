"""
Headless browser fallback for sites unsupported by yt-dlp.
Uses Playwright + Chromium with stealth plugin to extract a direct media URL from the page.

Caveats:
- DRM-protected content (Widevine/FairPlay) will NOT work with this approach.
- Sites using blob URLs or heavily obfuscated players may require additional
  network-request inspection to locate the real .m3u8/.mp4 URL.
- Aggressive anti-bot measures may still block extraction; stealth plugins
  (e.g. playwright-stealth) are used but not guaranteed.
- This increases processing time and memory usage compared to yt-dlp.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright
from playwright_stealth.stealth import Stealth

_TRACKING_HOSTS = {
    "snaptrckr.fun", "snaptrckr.com", "googletagmanager.com", "google-analytics.com",
    "facebook.com", "fbcdn.net", "doubleclick.net", "googlesyndication.com",
    "amazon-adsystem.com", "adsafeprotected.com", "moatads.com", "ads.twitter.com",
}

def _is_tracking_url(url: str) -> bool:
    try:
        host = url.split("/")[2].lower()
        return any(host == h or host.endswith("." + h) for h in _TRACKING_HOSTS)
    except Exception:  # noqa: BLE001
        return False


def _looks_like_media(url: str) -> bool:
    url = url.lower()
    return any(ext in url for ext in (".m3u8", ".mp4", ".webm", ".mov", ".avi", ".mkv"))


def _looks_like_player(url: str) -> bool:
    url = url.lower()
    return any(h in url for h in ("player", "embed", "video", "stream", "play", "watch"))


def _default_chrome_profile() -> Optional[str]:
    home = Path.home()
    candidates = [
        home / "Library/Application Support/Google/Chrome/Default",
        home / "Library/Application Support/Google/Chrome/Profile 1",
        home / ".config/google-chrome/Default",
        home / ".config/google-chrome/Profile 1",
    ]
    for path in candidates:
        if path.is_dir():
            return str(path)
    return None


def export_netscape_cookies(cookies: list[dict], output_path: Path) -> None:
    lines = ["# Netscape HTTP Cookie File", ""]
    for c in cookies:
        domain = c.get("domain", "")
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path", "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = str(int(c.get("expires", 0) or 0))
        name = c.get("name", "")
        value = c.get("value", "")
        if not name:
            continue
        lines.append("\t".join([domain, flag, path, secure, expires, name, value]))
    output_path.write_text("\n".join(lines), encoding="utf-8")


def extract_video_url(
    page_url: str,
    *,
    timeout_ms: int = 60_000,
    _depth: int = 0,
    user_data_dir: Optional[str] = None,
    export_cookies_to: Optional[Path] = None,
) -> str:
    """
    Launch a headless Chromium instance, load the page, and try to find a
    direct media URL from <video>, <source>, <iframe>, or network requests.

    If an iframe points to another player page, follow it recursively up to
    ``_MAX_EMBED_DEPTH`` levels.
    """
    _MAX_EMBED_DEPTH = 3

    with sync_playwright() as p:
        common_args = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=site-per-process",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
            ],
        }

        if user_data_dir:
            context = p.chromium.launch_persistent_context(
                user_data_dir,
                **common_args,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 720},
                locale="en-US",
                timezone_id="America/New_York",
            )
        else:
            browser = p.chromium.launch(**common_args)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 720},
                locale="en-US",
                timezone_id="America/New_York",
            )

        page = context.new_page()
        Stealth().apply_stealth_sync(page)

        media_urls: set[str] = set()
        hls_playlist_urls: set[str] = set()

        def _on_request(request):
            url = request.url.lower()
            if url.endswith(".m3u8"):
                hls_playlist_urls.add(request.url)
            elif any(ext in url for ext in (".mp4", ".webm", ".mov", ".avi", ".mkv")):
                media_urls.add(request.url)

        page.on("request", _on_request)

        try:
            page.goto(page_url, wait_until="networkidle", timeout=timeout_ms)
        except Exception:  # noqa: BLE001
            try:
                page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as exc:  # noqa: BLE001
                context.close()
                raise RuntimeError(f"Failed to load page: {exc}") from exc

        time.sleep(1)

        if export_cookies_to:
            try:
                cookies = context.cookies()
                export_netscape_cookies(cookies, export_cookies_to)
            except Exception:  # noqa: BLE001
                pass

        video = page.query_selector("video")
        if video:
            src = video.get_attribute("src")
            if src and _looks_like_media(src) and not _is_tracking_url(src):
                context.close()
                return urljoin(page_url, src)

        source = page.query_selector("video source")
        if source:
            src = source.get_attribute("src")
            if src and _looks_like_media(src) and not _is_tracking_url(src):
                context.close()
                return urljoin(page_url, src)

        iframes = page.query_selector_all("iframe")
        player_iframe = None
        for iframe in iframes:
            src = iframe.get_attribute("src")
            if not src:
                continue
            if _is_tracking_url(src):
                continue
            if _looks_like_media(src):
                context.close()
                return urljoin(page_url, src)
            if _looks_like_player(src) and player_iframe is None:
                player_iframe = src

        for url in hls_playlist_urls:
            if not _is_tracking_url(url):
                context.close()
                return url

        for url in media_urls:
            if not _is_tracking_url(url):
                context.close()
                return url

        if player_iframe and _depth < _MAX_EMBED_DEPTH:
            context.close()
            return extract_video_url(
                urljoin(page_url, player_iframe),
                timeout_ms=timeout_ms,
                _depth=_depth + 1,
                user_data_dir=user_data_dir,
                export_cookies_to=export_cookies_to,
            )

        context.close()
        raise RuntimeError(
            "No direct media URL found. "
            "Content may be DRM-protected, use blob URLs, or require additional parsing."
        )
