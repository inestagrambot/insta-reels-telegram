#!/usr/bin/env python3
"""
Instagram Reels -> Telegram pipeline.

* Reads the target Instagram usernames from pages.json (managed by the Telegram
  Mini App).
* Fetches the newest Reels of every page: yt-dlp first, instaloader as a
  fallback (yt-dlp is generally the more resilient of the two).
* Keeps history.json so a Reel is never delivered twice across runs.
* Processes at most MAX_NEW_PER_PAGE new Reels per page per run to stay well
  inside Instagram's rate limits.

Delivery logic for each new Reel:
  1. Download the video. If it is smaller than the Telegram upload limit it is
     sent as a native Telegram video (streamable).
  2. If the download fails, times out, or the file is too large, a text message
     with the Reel URL is sent instead, so a run never dies on a single video.

Every page and every video is wrapped in its own try/except: one failure never
stops the rest of the run.

All credentials come from environment variables / GitHub Secrets. No token,
bot token or chat id is ever hard-coded in this file.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# --------------------------------------------------------------------------- #
# Configuration (environment driven)
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
PAGES_FILE = ROOT / "pages.json"
HISTORY_FILE = ROOT / "history.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
IG_SESSIONID = os.getenv("IG_SESSIONID", "").strip()
IG_USERNAME = os.getenv("IG_USERNAME", "").strip()

MAX_NEW_PER_PAGE = max(1, int(os.getenv("MAX_NEW_PER_PAGE", "4")))
TELEGRAM_MAX_MB = float(os.getenv("TELEGRAM_MAX_MB", "49"))
MAX_UPLOAD_BYTES = int(TELEGRAM_MAX_MB * 1024 * 1024)
HISTORY_LIMIT = max(50, int(os.getenv("HISTORY_LIMIT", "800")))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "180"))
SOCKET_TIMEOUT = int(os.getenv("SOCKET_TIMEOUT", "30"))
SUMMARY_MODE = os.getenv("SEND_RUN_SUMMARY", "new").strip().lower()

TG_API = "https://api.telegram.org/bot{token}/{method}"
GH_API = "https://api.github.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
SHORTCODE_RE = re.compile(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)")


# --------------------------------------------------------------------------- #
# Logging / small helpers
# --------------------------------------------------------------------------- #
def setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


log = logging.getLogger("insta-reels")


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        log.warning("%s not found - using default value", path.name)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Could not read %s (%s) - using default value", path.name, exc)
    return default


def save_json(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(path)


def normalize_handle(value: Any) -> Optional[str]:
    """Accept 'name', '@name' or a full instagram.com URL and return 'name'."""
    text = str(value or "").strip().lstrip("@")
    if "instagram.com" in text:
        match = re.search(r"instagram\.com/([^/?#]+)", text)
        if match:
            text = match.group(1)
    text = text.strip().strip("/")
    return text if HANDLE_RE.match(text) else None


def human_mb(num_bytes: int) -> str:
    return f"{num_bytes / 1048576:.1f} MB"


def load_pages() -> List[str]:
    raw = load_json(PAGES_FILE, ["instagram"])
    if isinstance(raw, dict):
        raw = raw.get("pages", [])
    if not isinstance(raw, list):
        log.error("pages.json must contain a JSON array of usernames")
        return []
    pages: List[str] = []
    for item in raw:
        handle = normalize_handle(item)
        if handle and handle.lower() not in {p.lower() for p in pages}:
            pages.append(handle)
        elif not handle:
            log.warning("Ignoring invalid entry in pages.json: %r", item)
    return pages


def build_cookie_file(tmpdir: Path) -> Optional[str]:
    """Turn the optional IG_SESSIONID secret into a Netscape cookie file."""
    if not IG_SESSIONID:
        return None
    path = tmpdir / "ig_cookies.txt"
    expiry = int(time.time()) + 365 * 86400
    row = "\t".join(
        [".instagram.com", "TRUE", "/", "TRUE", str(expiry), "sessionid", IG_SESSIONID]
    )
    path.write_text(
        "# Netscape HTTP Cookie File\n# generated by main.py\n\n" + row + "\n",
        encoding="utf-8",
    )
    log.info("IG_SESSIONID cookie loaded (authenticated scraping enabled)")
    return str(path)


# --------------------------------------------------------------------------- #
# Telegram delivery
# --------------------------------------------------------------------------- #
def tg_url(method: str) -> str:
    return TG_API.format(token=TELEGRAM_BOT_TOKEN, method=method)


def send_message(text: str, disable_preview: bool = False) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram credentials missing - cannot send message")
        return False
    try:
        resp = requests.post(
            tg_url("sendMessage"),
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text[:4096],
                "parse_mode": "HTML",
                "disable_web_page_preview": disable_preview,
            },
            timeout=30,
        )
        if resp.status_code == 200 and resp.json().get("ok"):
            return True
        log.error("sendMessage rejected: %s", resp.text[:300])
    except requests.RequestException as exc:
        log.error("sendMessage failed: %s", exc)
    return False


def send_video(path: Path, caption: str, width: Optional[int] = None,
               height: Optional[int] = None, duration: Optional[float] = None) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram credentials missing - cannot send video")
        return False
    data: Dict[str, Any] = {
        "chat_id": TELEGRAM_CHAT_ID,
        "caption": caption[:1024],
        "parse_mode": "HTML",
        "supports_streaming": "true",
    }
    if width:
        data["width"] = int(width)
    if height:
        data["height"] = int(height)
    if duration:
        data["duration"] = int(duration)
    try:
        with path.open("rb") as fh:
            files = {"video": (path.name, fh, "video/mp4")}
            resp = requests.post(tg_url("sendVideo"), data=data, files=files, timeout=900)
        if resp.status_code == 200 and resp.json().get("ok"):
            return True
        log.error("sendVideo rejected (%s): %s", resp.status_code, resp.text[:300])
    except requests.RequestException as exc:
        log.error("sendVideo failed: %s", exc)
    return False


def build_caption(user: str, title: str, url: str, downloaded: bool) -> str:
    head = "🎬 <b>@{}</b>".format(html.escape(user))
    body = html.escape(title.strip())[:250] if title.strip() else "New Reel"
    footer = "⬇️ downloaded" if downloaded else "🔗 source link"
    return f"{head}\n{body}\n\n{html.escape(url)}\n<i>{footer}</i>"


def build_fallback_message(user: str, title: str, url: str, reason: str) -> str:
    head = "🔗 <b>@{}</b> — new Reel (link only)".format(html.escape(user))
    body = html.escape(title.strip())[:250] if title.strip() else "New Reel"
    return f"{head}\n{body}\n\n{html.escape(url)}\n<i>{html.escape(reason)}</i>"


# --------------------------------------------------------------------------- #
# Reel discovery - yt-dlp (primary)
# --------------------------------------------------------------------------- #
def yt_dlp_list(user: str, cookie: Optional[str], limit: int) -> List[Dict[str, Any]]:
    from yt_dlp import YoutubeDL

    options: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": limit,
        "ignoreerrors": True,
        "socket_timeout": SOCKET_TIMEOUT,
    }
    if cookie:
        options["cookiefile"] = cookie

    url = f"https://www.instagram.com/{user}/reels/"
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

    reels: List[Dict[str, Any]] = []
    for entry in (info or {}).get("entries") or []:
        if not entry:
            continue
        webpage = str(entry.get("webpage_url") or entry.get("url") or "")
        reel_id = str(entry.get("id") or "")
        if not reel_id:
            match = SHORTCODE_RE.search(webpage)
            reel_id = match.group(1) if match else ""
        if not reel_id:
            continue
        reels.append(
            {
                "id": reel_id,
                "url": webpage if webpage.startswith("http") else f"https://www.instagram.com/reel/{reel_id}/",
                "title": str(entry.get("title") or entry.get("description") or "")[:250],
                "duration": entry.get("duration"),
                "width": entry.get("width"),
                "height": entry.get("height"),
                "source": "yt-dlp",
            }
        )
        if len(reels) >= limit:
            break
    return reels


def yt_dlp_download(reel: Dict[str, Any], dest_dir: Path, cookie: Optional[str]) -> Optional[Path]:
    """Download one reel; returns the file path or None. Never downloads a file
    bigger than the Telegram limit (yt-dlp aborts it for us)."""
    from yt_dlp import YoutubeDL

    options: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "best[ext=mp4]/best",
        "outtmpl": str(dest_dir / "reel_%(id)s.%(ext)s"),
        "max_filesize": MAX_UPLOAD_BYTES,
        "socket_timeout": SOCKET_TIMEOUT,
        "retries": 2,
        "fragment_retries": 2,
        "overwrites": True,
    }
    if cookie:
        options["cookiefile"] = cookie

    with YoutubeDL(options) as ydl:
        ydl.download([reel["url"]])

    files = [p for p in dest_dir.glob("reel_*") if p.is_file() and p.stat().st_size > 0]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_size)


# --------------------------------------------------------------------------- #
# Reel discovery - instaloader (fallback) + direct HTTP fetch
# --------------------------------------------------------------------------- #
def make_instaloader() -> Any:
    import instaloader

    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
        quiet=True,
        max_connection_attempts=2,
    )
    if IG_SESSIONID:
        try:
            loader.context._session.cookies.set("sessionid", IG_SESSIONID, domain=".instagram.com")
            if IG_USERNAME:
                loader.context.username = IG_USERNAME
            log.info("instaloader: session cookie attached")
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("instaloader: could not attach session cookie (%s)", exc)
    return loader


def instaloader_list(user: str, limit: int) -> List[Dict[str, Any]]:
    loader = make_instaloader()
    import instaloader

    profile = instaloader.Profile.from_username(loader.context, user)
    reels: List[Dict[str, Any]] = []
    for post in profile.get_posts():
        if not post.is_video:
            continue
        reels.append(
            {
                "id": post.shortcode,
                "url": f"https://www.instagram.com/reel/{post.shortcode}/",
                "title": str(post.caption or "")[:250],
                "duration": None,
                "width": None,
                "height": None,
                "_direct_url": post.video_url,
                "source": "instaloader",
            }
        )
        if len(reels) >= limit:
            break
    return reels


def http_download(url: str, dest: Path) -> Optional[Path]:
    """Streaming download with a hard size cap (used for instaloader CDN URLs)."""
    headers = {"User-Agent": UA}
    if IG_SESSIONID:
        headers["Cookie"] = f"sessionid={IG_SESSIONID}"
    try:
        with requests.get(url, headers=headers, stream=True, timeout=(10, DOWNLOAD_TIMEOUT)) as resp:
            resp.raise_for_status()
            declared = int(resp.headers.get("Content-Length") or 0)
            if declared and declared > MAX_UPLOAD_BYTES:
                log.info("Direct download skipped: %s exceeds the %s limit", human_mb(declared), human_mb(MAX_UPLOAD_BYTES))
                return None
            written = 0
            with dest.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=262144):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > MAX_UPLOAD_BYTES:
                        log.info("Direct download aborted: larger than %s", human_mb(MAX_UPLOAD_BYTES))
                        fh.close()
                        dest.unlink(missing_ok=True)
                        return None
                    fh.write(chunk)
    except (requests.RequestException, OSError) as exc:
        log.warning("Direct download failed: %s", exc)
        dest.unlink(missing_ok=True)
        return None
    return dest if dest.exists() and dest.stat().st_size > 0 else None


# --------------------------------------------------------------------------- #
# Per-reel processing
# --------------------------------------------------------------------------- #
def deliver_reel(user: str, reel: Dict[str, Any], tmpdir: Path, cookie: Optional[str]) -> str:
    """Returns 'video', 'link' or 'failed'."""
    reel_id = reel.get("id") or "unknown"
    url = reel.get("url") or f"https://www.instagram.com/reel/{reel_id}/"
    title = reel.get("title") or ""
    workdir = tmpdir / f"{user}_{reel_id}"
    workdir.mkdir(parents=True, exist_ok=True)

    path: Optional[Path] = None
    failure_reason = ""

    try:
        path = yt_dlp_download(reel, workdir, cookie)
        if path is None:
            failure_reason = "download returned no file"
    except Exception as exc:
        failure_reason = f"yt-dlp error: {exc}"
        log.warning("[%s/%s] download via yt-dlp failed: %s", user, reel_id, exc)

    if path is None and reel.get("_direct_url"):
        log.info("[%s/%s] retrying with the direct CDN url", user, reel_id)
        path = http_download(reel["_direct_url"], workdir / f"{reel_id}.mp4")
        if path is None and not failure_reason:
            failure_reason = "direct download failed"

    if path is not None and path.exists():
        size = path.stat().st_size
        if size <= MAX_UPLOAD_BYTES:
            if send_video(path, build_caption(user, title, url, True), reel.get("width"), reel.get("height"), reel.get("duration")):
                log.info("[%s/%s] sent video (%s)", user, reel_id, human_mb(size))
                return "video"
            failure_reason = "telegram refused the video upload"
            log.warning("[%s/%s] Telegram rejected the video, falling back to a link", user, reel_id)
        else:
            failure_reason = f"file too large ({human_mb(size)})"
            log.info("[%s/%s] %s -> link fallback", user, reel_id, failure_reason)

    reason = failure_reason or "video unavailable"
    if send_message(build_fallback_message(user, title, url, reason)):
        log.info("[%s/%s] sent link instead (%s)", user, reel_id, reason)
        return "link"
    log.error("[%s/%s] could not deliver the reel at all", user, reel_id)
    return "failed"


def process_user(user: str, tmpdir: Path, cookie: Optional[str],
                 history: Dict[str, Any], stats: Dict[str, int]) -> None:
    known = set(history.get(user) or [])
    reel_id_list = history.get(user) if isinstance(history.get(user), list) else []

    discovered: List[Dict[str, Any]] = []
    try:
        discovered = yt_dlp_list(user, cookie, MAX_NEW_PER_PAGE * 3)
        log.info("[%s] yt-dlp listed %d reel(s)", user, len(discovered))
    except Exception as exc:
        log.warning("[%s] yt-dlp listing failed: %s", user, exc)

    if not discovered:
        try:
            discovered = instaloader_list(user, MAX_NEW_PER_PAGE * 3)
            log.info("[%s] instaloader listed %d reel(s)", user, len(discovered))
        except Exception as exc:
            log.warning("[%s] instaloader listing failed: %s", user, exc)

    if not discovered:
        log.warning("[%s] no reels could be listed this run", user)
        return

    fresh = [r for r in discovered if r["id"] not in known][:MAX_NEW_PER_PAGE]
    if not fresh:
        log.info("[%s] no new reels (already processed)", user)
        return

    log.info("[%s] %d new reel(s) to deliver", user, len(fresh))
    for reel in fresh:
        try:
            outcome = deliver_reel(user, reel, tmpdir, cookie)
        except Exception as exc:
            log.exception("[%s/%s] unexpected error: %s", user, reel.get("id"), exc)
            outcome = "failed"

        stats["new"] += 1
        if outcome == "video":
            stats["videos"] += 1
        elif outcome == "link":
            stats["links"] += 1
        else:
            stats["failed"] += 1

        # Marked as processed even when only a link was sent, so the next run
        # moves on instead of retrying the same reel forever.
        reel_id_list.append(reel["id"])
        history[user] = reel_id_list[-HISTORY_LIMIT:]

        time.sleep(2)  # small courtesy delay between reels


# --------------------------------------------------------------------------- #
# Optional: push history.json straight from the script (GitHub Contents API)
# --------------------------------------------------------------------------- #
def maybe_commit_history() -> None:
    if str(os.getenv("AUTO_COMMIT", "1")).strip().lower() in {"0", "false", "no", "off"}:
        log.info("AUTO_COMMIT disabled - the workflow will commit history.json")
        return
    token = os.getenv("GITHUB_TOKEN", "").strip()
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if not token or not repo:
        log.info("No GITHUB_TOKEN/GITHUB_REPOSITORY - the workflow will commit history.json")
        return
    branch = os.getenv("GITHUB_REF_NAME", "main").strip() or "main"
    api = f"{GH_API}/repos/{repo}/contents/history.json"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        current = requests.get(api, headers=headers, params={"ref": branch}, timeout=30)
        sha = current.json().get("sha") if current.status_code == 200 else None
        payload: Dict[str, Any] = {
            "message": "chore: update history.json [skip ci]",
            "content": base64.b64encode(HISTORY_FILE.read_bytes()).decode(),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        resp = requests.put(api, headers=headers, json=payload, timeout=30)
        if resp.status_code in (200, 201):
            log.info("history.json committed to %s@%s via the GitHub API", repo, branch)
        else:
            log.error("GitHub API commit failed: %s %s", resp.status_code, resp.text[:200])
    except (requests.RequestException, OSError) as exc:
        log.error("GitHub API commit error: %s", exc)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    setup_logging()
    log.info("=== Instagram Reels -> Telegram run started ===")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set (add them as GitHub Secrets)")
        return 2

    pages = load_pages()
    log.info("Monitoring %d page(s): %s", len(pages), ", ".join(pages) or "-")

    history = load_json(HISTORY_FILE, {})
    if not isinstance(history, dict):
        log.error("history.json had an unexpected shape - starting from scratch")
        history = {}

    stats = {"pages": 0, "new": 0, "videos": 0, "links": 0, "failed": 0}

    with tempfile.TemporaryDirectory(prefix="reels_") as tmp:
        tmpdir = Path(tmp)
        try:
            cookie = build_cookie_file(tmpdir)
        except OSError as exc:
            log.warning("Could not write the cookie file (%s) - continuing anonymously", exc)
            cookie = None

        for user in pages:
            stats["pages"] += 1
            try:
                process_user(user, tmpdir, cookie, history, stats)
            except Exception as exc:
                log.exception("[%s] page failed, continuing with the next one: %s", user, exc)
                stats["failed"] += 1

    save_json(HISTORY_FILE, history)
    log.info("history.json updated (%d tracked page(s))", len(history))

    maybe_commit_history()

    summary = (
        "📊 <b>Scraper run finished</b>\n"
        f"Pages checked: {stats['pages']}\n"
        f"New reels: {stats['new']}  •  videos: {stats['videos']}  •  links: {stats['links']}\n"
        f"Errors: {stats['failed']}"
    )
    if SUMMARY_MODE == "always" or (SUMMARY_MODE == "new" and stats["new"] > 0):
        send_message(summary)

    log.info("=== run finished: %s ===", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
