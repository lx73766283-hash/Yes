"""
YesOfficer Telegram Extractor + Uploader Bot.

Modes (inline buttons on /start):

    Extractor   -- paste cookies + a course URL; bot crawls every folder
                   (and all loose items at the root), fetches every video's
                   signed CDN URL and every PDF link, then sends back a
                   single .txt file in oliveboard style.

    Uploader    -- send a .txt back; bot asks for a batch name / handle /
                   optional thumbnail / starting entry; then downloads
                   video -> pdf -> pdf2 for each entry in order and
                   forwards them to the current chat.

Extra commands:
    /queue      -- reply to any .txt file with /queue to line it up
                   after the currently-running batch.
    /status     -- show current batch progress and queue length.
    /cancel     -- abort the current batch.
    /clean      -- owner only; wipes the downloads directory.

Ban-safe: per-session jittered pacing on all API calls, exponential backoff
on 429/5xx, and the browser's Appx headers (Client-Service/Auth-Key/Source)
are spoofed exactly.

This script is deliberately a single file so you can drop it on Kaggle
(or any VPS) with `pip install -r requirements.txt && python bot.py`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import string
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import unquote, urlparse

import aiohttp
from Crypto.Cipher import AES
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)


# =============================================================================
# CONFIG
# =============================================================================


def _load_env() -> None:
    """Minimal .env loader (no python-dotenv required)."""
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_env()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


API_ID = _env_int("API_ID", 0)
API_HASH = os.environ.get("API_HASH") or ""
BOT_TOKEN = os.environ.get("BOT_TOKEN") or ""

ALLOWED_USER_IDS: set[int] = {
    int(x) for x in (os.environ.get("ALLOWED_USER_IDS") or "").split(",") if x.strip()
}
OWNER_ID = _env_int("OWNER_ID", next(iter(ALLOWED_USER_IDS), 0))

# Where to send a silent copy of each file before forwarding to the target
# chat.  This lets us delete local files immediately after Telegram has them.
# Set to 0 (or unset) to disable -- files stay on disk until after delivery.
LOG_CHANNEL = _env_int("LOG_CHANNEL", 0)

DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR") or "./downloads").resolve()
DEFAULT_THUMB = os.environ.get("DEFAULT_THUMB") or "default_thumb.jpg"
FILENAME_SUFFIX = os.environ.get("FILENAME_SUFFIX") or ""

# Remux downloaded .mkv into .mp4 so desktop Telegram plays it.  Lossless
# (-c copy).  Turn off with REMUX_MP4=false if you ever want to ship the
# raw .mkv (e.g. a user device is mis-rendering the remuxed .mp4).
REMUX_MP4 = (os.environ.get("REMUX_MP4") or "true").strip().lower() not in (
    "0", "false", "no", "off")

MIN_DELAY = _env_float("MIN_DELAY", 0.8)
MAX_DELAY = _env_float("MAX_DELAY", 1.8)

# Extractor runs a lot of API calls back-to-back (one per video).  A slower,
# more human-like pace and a periodic "user went to read / switch tab" pause
# makes 200-300-item courses look like normal browsing to Appx risk controls.
EXTRACT_MIN_DELAY = _env_float("EXTRACT_MIN_DELAY", 1.2)
EXTRACT_MAX_DELAY = _env_float("EXTRACT_MAX_DELAY", 2.5)
EXTRACT_LONG_PAUSE_EVERY = _env_int("EXTRACT_LONG_PAUSE_EVERY", 40)
EXTRACT_LONG_PAUSE_MIN = _env_float("EXTRACT_LONG_PAUSE_MIN", 20.0)
EXTRACT_LONG_PAUSE_MAX = _env_float("EXTRACT_LONG_PAUSE_MAX", 45.0)

MAX_PARALLEL_DOWNLOADS = _env_int("MAX_PARALLEL_DOWNLOADS", 2)
MAX_PARALLEL_UPLOADS = _env_int("MAX_PARALLEL_UPLOADS", 2)
CONCURRENT_HTTP = _env_int("CONCURRENT_HTTP", 32)

# Multi-connection (aria2-style) download tuning.  The Appx CDN throttles
# each TCP connection to ~1.5 MB/s, so we split large files into N Range
# chunks and fetch them in parallel to saturate bandwidth.
#   MULTI_CONN        -- chunks per file (cap ~8, beyond that diminishing
#                        returns + higher ban-flag surface)
#   MULTI_MIN_SIZE    -- only trigger multi-connection when Content-Length
#                        is at least this many bytes (default 16 MB)
#   MULTI_CHUNK_IO    -- buffered-read size per chunk stream (bytes)
MULTI_CONN = _env_int("MULTI_CONN", 8)
MULTI_MIN_SIZE = _env_int("MULTI_MIN_SIZE", 16 * 1024 * 1024)
MULTI_CHUNK_IO = _env_int("MULTI_CHUNK_IO", 1 << 16)

PROGRESS_INTERVAL = _env_float("PROGRESS_INTERVAL", 4.0)
MIN_FREE_DISK_BYTES = _env_int("MIN_FREE_DISK_BYTES", 2 * 1024 ** 3)  # 2 GB
DISK_WAIT_SECS = _env_int("DISK_WAIT_SECS", 15)

DB_PATH = Path(__file__).with_name("bot_state.db")

if not API_ID or not API_HASH or not BOT_TOKEN:
    print(
        "ERROR: API_ID, API_HASH and BOT_TOKEN must be set "
        "(copy .env.example -> .env and fill in the values).",
        file=sys.stderr,
    )
    sys.exit(1)

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("yesofficer-bot")


# =============================================================================
# CRYPTO + COOKIE PARSING
# =============================================================================


API_BASE = "https://yesofficerapi.cloudflare.net.in"
FRONTEND_BASE = "https://www.yesofficer.com"

# AES-CBC key + IV extracted from the frontend bundle (`DECRYPTION_KEYS.VALUE`).
_AES_KEY = b"638udh3829162018"
_AES_IV_ASCII = b"fedcba9876543210"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _unpad_pkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def decrypt_appx(value: str) -> str:
    """AES-256-CBC + PKCS7 decrypt of an Appx-encrypted field.

    Format:  <base64(ciphertext)>:<base64(iv_ascii)>.
    """
    if not value or ":" not in value:
        return value
    ct_b64, iv_b64 = value.split(":", 1)
    try:
        ct = base64.b64decode(ct_b64)
        iv = base64.b64decode(iv_b64)
    except Exception:
        return value
    if len(iv) != 16:
        iv = _AES_IV_ASCII
    try:
        cipher = AES.new(_AES_KEY, AES.MODE_CBC, iv)
        pt = _unpad_pkcs7(cipher.decrypt(ct))
        return pt.decode("utf-8", errors="replace")
    except Exception:
        return value


def parse_cookie_blob(blob: str) -> dict[str, str]:
    """Parse a user-pasted cookie blob.  Accepts any reasonable shape."""
    out: dict[str, str] = {}
    normalised = blob.replace("\r", "\n")
    for chunk in re.split(r"[\n;]+", normalised):
        s = chunk.strip()
        if not s or "=" not in s:
            continue
        if s.lower().startswith(("cookie:", "set-cookie:")):
            s = s.split(":", 1)[1].strip()
        k, _, v = s.partition("=")
        out[k.strip()] = unquote(v.strip())
    return out


def _course_id_from_url(s: str) -> str:
    s = s.strip()
    if s.isdigit():
        return s
    m = re.search(r"/(?:new-courses|course)/(\d+)", s)
    return m.group(1) if m else ""


# =============================================================================
# API CLIENT
# =============================================================================


@dataclass
class ApiSession:
    jwt: str
    user_id: str

    def build_headers(self) -> dict[str, str]:
        return {
            "Authorization": self.jwt,
            "User-ID": self.user_id,
            "Client-Service": "Appx",
            "Auth-Key": "appxapi",
            "Source": "website",
            "Origin": FRONTEND_BASE,
            "Referer": FRONTEND_BASE + "/",
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        }


class YesOfficerApi:
    """Thin async wrapper around the yesofficer / Appx REST endpoints."""

    def __init__(
        self,
        session: ApiSession,
        http_session: aiohttp.ClientSession,
        rate_limit_min: float = MIN_DELAY,
        rate_limit_max: float = MAX_DELAY,
        long_pause_every: int = 0,
        long_pause_min: float = 0.0,
        long_pause_max: float = 0.0,
        on_long_pause: Optional[Callable[[int, float], Awaitable[None]]] = None,
    ) -> None:
        self._s = session
        self._http = http_session
        self._last_call = 0.0
        self._min = rate_limit_min
        self._max = rate_limit_max
        self._lock = asyncio.Lock()
        self._calls = 0
        self._long_every = long_pause_every
        self._long_min = long_pause_min
        self._long_max = long_pause_max
        self._on_long_pause = on_long_pause

    async def _pace(self) -> None:
        long_pause_secs = 0.0
        async with self._lock:
            self._calls += 1
            # Every N calls, insert a human-like "tab away" pause.
            if (self._long_every and self._calls > 1
                    and self._calls % self._long_every == 0
                    and self._long_max > 0):
                long_pause_secs = random.uniform(self._long_min, self._long_max)
            delay = random.uniform(self._min, self._max)
            now = time.monotonic()
            wait = (self._last_call + delay) - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
        if long_pause_secs > 0:
            if self._on_long_pause:
                try:
                    await self._on_long_pause(self._calls, long_pause_secs)
                except Exception:
                    pass
            await asyncio.sleep(long_pause_secs)
            async with self._lock:
                self._last_call = time.monotonic()

    async def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        backoff = 2.0
        for attempt in range(5):
            await self._pace()
            try:
                async with self._http.request(
                    method,
                    f"{API_BASE}{path}",
                    headers=self._s.build_headers(),
                    timeout=aiohttp.ClientTimeout(total=30),
                    **kw,
                ) as r:
                    if r.status == 429 or r.status >= 500:
                        log.warning("%s %s -> %s, backing off", method, path, r.status)
                        await asyncio.sleep(backoff + random.random())
                        backoff *= 2
                        continue
                    r.raise_for_status()
                    return await r.json(content_type=None) or {}
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("%s %s attempt %s failed: %s", method, path, attempt + 1, exc)
                await asyncio.sleep(backoff + random.random())
                backoff *= 2
        raise RuntimeError(f"{method} {path} failed after retries")

    async def course(self, course_id: str) -> dict[str, Any]:
        r = await self._request("GET", f"/get/coursenew_by_idv2?id={course_id}")
        data = r.get("data")
        if isinstance(data, list) and data:
            return data[0]
        return data or {}

    async def folder_contents(
        self, course_id: str, parent_id: str, start: int = 0
    ) -> list[dict[str, Any]]:
        r = await self._request(
            "GET",
            f"/get/folder_contentsv3?course_id={course_id}"
            f"&parent_id={parent_id}&start={start}",
        )
        d = r.get("data")
        return d if isinstance(d, list) else []

    async def folder_contents_all(
        self, course_id: str, parent_id: str
    ) -> list[dict[str, Any]]:
        """Paginate by the actual response size until it returns empty.

        Appx's `folder_contentsv3` does not honour a fixed page size -- the
        server may cap any single response at 20 even though the nominal
        page size is 30, and it signals 'done' only via an empty list.
        """
        out: list[dict[str, Any]] = []
        start = 0
        MAX_ITEMS = 20000
        while len(out) < MAX_ITEMS:
            chunk = await self.folder_contents(course_id, parent_id, start=start)
            if not chunk:
                break
            out.extend(chunk)
            start += len(chunk)
        return out

    async def video_details(
        self, course_id: str, video_id: str
    ) -> dict[str, Any]:
        r = await self._request(
            "GET",
            f"/get/fetchVideoDetailsById?course_id={course_id}"
            f"&video_id={video_id}&ytflag=0&folder_wise_course=1",
        )
        return r.get("data") or {}


# =============================================================================
# ENTRY MODEL + CRAWLER
# =============================================================================


@dataclass
class Entry:
    """One logical row in the .txt: a video with up to two PDF attachments."""
    index: int
    topic: str
    title: str
    date: str
    duration: str
    quality: str
    video_url: str
    pdf_url: str
    pdf_url2: str
    study_material_url: str = ""
    video_id: str = ""


def _fmt_duration_seconds(val: Any) -> str:
    """Turn a duration (seconds, MM:SS, or HH:MM:SS) into 'h m s'."""
    if not val:
        return ""
    s = str(val).strip()
    if not s:
        return ""
    if ":" in s:
        parts = [int(x) for x in s.split(":") if x.isdigit()]
        if len(parts) == 2:
            total = parts[0] * 60 + parts[1]
        elif len(parts) == 3:
            total = parts[0] * 3600 + parts[1] * 60 + parts[2]
        else:
            return s
    else:
        try:
            total = int(float(s))
        except ValueError:
            return s
    if total <= 0:
        return ""
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {sec}s"
    return f"{m}m {sec}s"


def _fmt_epoch(ts: Any) -> str:
    """'Wed, Apr 22, 2026 | 04:30 PM' from a unix-seconds value."""
    if not ts:
        return ""
    try:
        t = int(float(ts))
        if t <= 0:
            return ""
        return time.strftime("%a, %b %d, %Y | %I:%M %p", time.localtime(t))
    except (TypeError, ValueError):
        return ""


def _quality_rank(q: str) -> int:
    m = re.search(r"(\d+)", q or "")
    return int(m.group(1)) if m else 0


def _pick_best_video_url(details: dict[str, Any]) -> Optional[dict[str, str]]:
    links = details.get("encrypted_links") or []
    if not isinstance(links, list) or not links:
        return None
    sorted_links = sorted(links, key=lambda x: _quality_rank(x.get("quality", "")),
                          reverse=True)
    for link in sorted_links:
        url = decrypt_appx(link.get("path") or "")
        if not url.startswith("http"):
            url = decrypt_appx(link.get("backup_url") or "") or url
        if url.startswith("http"):
            return {"quality": str(link.get("quality") or ""), "url": url}
    return None


@dataclass
class IndexRow:
    """Cheap (folder-only) index row: enough to decide what to extract.

    Filled in by `crawl_course_index()` without calling video_details.
    """
    position: int         # 1-based row number across the whole course
    kind: str             # "video" or "pdf"
    topic: str
    title: str
    node: dict[str, Any]  # raw folder_contents entry (for later lookups)


async def crawl_course_index(
    api: YesOfficerApi,
    course_id: str,
    *,
    on_progress: Optional[Callable[[str], Awaitable[None]]] = None,
) -> tuple[dict[str, Any], list[IndexRow]]:
    """Walk folder structure only. No per-video API calls yet.

    This is cheap: for a 300-video course it costs ~15 API calls instead of
    ~300.  The returned index is enough to let the user choose a slice
    (all / last N / custom range) before we pay for `fetchVideoDetailsById`.
    """
    meta = await api.course(course_id)
    rows: list[IndexRow] = []
    stack: list[tuple[str, str]] = [("-1", "")]
    while stack:
        parent_id, topic = stack.pop()
        children = await api.folder_contents_all(course_id, parent_id)
        for node in children:
            mtype = (node.get("material_type") or "").upper()
            title = (node.get("Title") or "").strip() or f"id-{node.get('id')}"
            if mtype == "FOLDER":
                # Topic = only the immediate folder name (not the full chain)
                # so captions stay readable (e.g. "Puzzle Master" not
                # "Home / Puzzle Master").
                stack.append((str(node.get("id")), title))
                continue
            if mtype == "VIDEO":
                rows.append(IndexRow(
                    position=len(rows) + 1,
                    kind="video",
                    topic=topic or "Root",
                    title=title,
                    node=dict(node),
                ))
            elif mtype in {"FILE", "PDF", "DOC"}:
                rows.append(IndexRow(
                    position=len(rows) + 1,
                    kind="pdf",
                    topic=topic or "Root",
                    title=title,
                    node=dict(node),
                ))
            if on_progress and len(rows) % 25 == 1:
                await on_progress(
                    f"Indexing `{topic or '/'}` — {len(rows)} items so far…"
                )
    return meta, rows


async def resolve_rows_to_entries(
    api: YesOfficerApi,
    course_id: str,
    rows: list[IndexRow],
    *,
    on_progress: Optional[Callable[[str], Awaitable[None]]] = None,
) -> list[Entry]:
    """Expensive phase: fetch video_details for each selected row.

    The returned Entry rows keep their *global* position (so index numbering
    still matches the course), but only the selected slice is materialised.
    """
    entries: list[Entry] = []
    for row in rows:
        node = row.node
        title = row.title
        topic = row.topic
        if row.kind == "video":
            vid = str(node.get("id"))
            details = await api.video_details(course_id, vid)
            chosen = _pick_best_video_url(details)
            pdf1 = decrypt_appx(details.get("pdf_link") or "")
            pdf2 = decrypt_appx(details.get("pdf_link2") or "")
            sm = decrypt_appx(details.get("study_material_link") or "")
            date_src = (details.get("strtotime") or node.get("strtotime")
                        or details.get("date_and_time")
                        or details.get("event_date"))
            dur = (details.get("duration") or node.get("duration") or "")
            entries.append(Entry(
                index=row.position,
                topic=topic,
                title=title,
                date=_fmt_epoch(date_src),
                duration=_fmt_duration_seconds(dur),
                quality=(chosen or {}).get("quality", ""),
                video_url=(chosen or {}).get("url", ""),
                pdf_url=pdf1 if pdf1.startswith("http") else "",
                pdf_url2=pdf2 if pdf2.startswith("http") else "",
                study_material_url=sm if (sm.startswith("http")
                                          and sm not in (pdf1, pdf2)) else "",
                video_id=vid,
            ))
        else:
            link = decrypt_appx(node.get("pdf_link") or "")
            if link.startswith("http"):
                entries.append(Entry(
                    index=row.position,
                    topic=topic,
                    title=title,
                    date="", duration="", quality="",
                    video_url="",
                    pdf_url=link,
                    pdf_url2="",
                ))
        if on_progress and len(entries) % 10 == 1:
            await on_progress(
                f"Fetching details — {len(entries)}/{len(rows)} resolved…")
    return entries


async def crawl_course(
    api: YesOfficerApi,
    course_id: str,
    *,
    on_progress: Optional[Callable[[str], Awaitable[None]]] = None,
) -> tuple[dict[str, Any], list[Entry]]:
    """Convenience wrapper: index every item, then resolve every one."""
    meta, rows = await crawl_course_index(api, course_id, on_progress=on_progress)
    entries = await resolve_rows_to_entries(api, course_id, rows,
                                            on_progress=on_progress)
    return meta, entries


# =============================================================================
# TXT WRITER + PARSER  (oliveboard style)
# =============================================================================


def entries_to_text(meta: dict[str, Any], entries: list[Entry]) -> str:
    """Build the `.txt` body in oliveboard's exact layout."""
    bar = "=" * 70
    name = (meta.get("course_name") or meta.get("course_title") or
            f"Course {meta.get('id', '?')}")
    course_id = str(meta.get("id") or "")
    lines: list[str] = [
        bar,
        f"COURSE: {name}",
        f"ID: {course_id}",
        f"Total Entries: {len(entries)}",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime())}",
        bar,
        "",
    ]
    video_n = pdf_n = 0
    for e in entries:
        lines.append(f"[{str(e.index).zfill(3)}] [{e.topic}] {e.title}")
        if e.video_id:
            # Hidden refresh hint: uploader uses this to re-sign expired URLs.
            lines.append(f"    Video ID: {e.video_id}")
        if e.date:
            lines.append(f"    Date: {e.date}")
        if e.duration:
            lines.append(f"    Duration: {e.duration}")
        if e.quality:
            lines.append(f"    Quality: {e.quality}")
        if e.video_url:
            lines.append(f"    Video: {e.video_url}")
            video_n += 1
        elif e.pdf_url or e.pdf_url2:
            # All-PDF row: emit placeholder so parser can tell it's a PDF row.
            lines.append("    Video: -")
        if e.pdf_url:
            lines.append(f"    PDF: {e.pdf_url}")
            pdf_n += 1
        if e.pdf_url2:
            lines.append(f"    PDF 2: {e.pdf_url2}")
            pdf_n += 1
        if e.study_material_url:
            lines.append(f"    Study Material: {e.study_material_url}")
            pdf_n += 1
        lines.append("")
    lines += [
        bar, "SUMMARY", bar,
        f"Entries          : {len(entries)}",
        f"Videos extracted : {video_n}",
        f"PDFs extracted   : {pdf_n}",
    ]
    return "\n".join(lines) + "\n"


_ENTRY_HEADER = re.compile(r"^\[(\d+)\]\s*\[([^\]]+)\]\s*(.+?)\s*$", re.MULTILINE)


def _scan_url(block: str, *keys: str) -> str:
    for k in keys:
        m = re.search(
            rf"(?mi)^\s*{re.escape(k)}\s*:\s*(https?://\S+)\s*$", block)
        if m:
            return m.group(1)
    return ""


def parse_entries_text(text: str) -> tuple[str, str, list[Entry]]:
    """Parse the oliveboard-style .txt back into (course, course_id, entries)."""
    import html as _html
    course = ""
    mc = re.search(r"^COURSE:\s*(.+)$", text, re.MULTILINE)
    if mc:
        course = _html.unescape(mc.group(1).strip())
    course_id = ""
    mi = re.search(r"^ID:\s*(\d+)\s*$", text, re.MULTILINE)
    if mi:
        course_id = mi.group(1).strip()
    out: list[Entry] = []
    headers = list(_ENTRY_HEADER.finditer(text))
    for i, h in enumerate(headers):
        start = h.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = text[start:end]
        idx = int(h.group(1))
        topic = _html.unescape(h.group(2).strip())
        title = _html.unescape(h.group(3).strip())

        date = ""
        dm = re.search(r"(?mi)^\s*Date\s*:\s*([^\n]+)$", block)
        if dm:
            v = dm.group(1).strip()
            if v and not v.lower().startswith("duration"):
                date = v
        dur = ""
        du = re.search(r"(?mi)^\s*Duration\s*:\s*([^\n]+)$", block)
        if du:
            dur = du.group(1).strip()
        qual = ""
        qu = re.search(r"(?mi)^\s*Quality\s*:\s*([^\n]+)$", block)
        if qu:
            qual = qu.group(1).strip()
        vid = ""
        vm = re.search(r"(?mi)^\s*Video ID\s*:\s*(\d+)\s*$", block)
        if vm:
            vid = vm.group(1).strip()

        # Video URL: accept "Video:" or "Download URL:" or "Download:".
        vurl = _scan_url(block, "Video", "Download URL", "Download", "CDN download")

        pdf1 = _scan_url(block, "PDF", "Pdf")
        pdf2 = _scan_url(block, "PDF 2", "Pdf 2", "PDF2")
        sm = _scan_url(block, "Study Material", "Notes")

        out.append(Entry(
            index=idx, topic=topic, title=title, date=date,
            duration=dur, quality=qual,
            video_url=vurl, pdf_url=pdf1, pdf_url2=pdf2,
            study_material_url=sm,
            video_id=vid,
        ))
    return course, course_id, out


def total_items_in_entries(entries: list[Entry], start: int = 1) -> int:
    """Count of files (video + pdfs) across entries from start onward."""
    n = 0
    for e in entries:
        if e.index < start:
            continue
        if e.video_url and e.video_url != "-":
            n += 1
        if e.pdf_url:
            n += 1
        if e.pdf_url2:
            n += 1
        if e.study_material_url:
            n += 1
    return n


def build_item_queue(
    entries: list[Entry],
    start: int = 1,
    *,
    course_id: str = "",
) -> list[dict[str, Any]]:
    """Flatten entries into a per-file queue in delivery order.

    For each entry we emit VIDEO -> PDF -> PDF 2 -> Study Material.

    Two distinct indices are carried on every item:
      * `global_idx`  — 1-based position across the **entire** .txt,
        counting every link including those that belong to entries the
        user asked to skip.  This is what goes into the caption as
        "Index : NNN" so captions keep matching the .txt numbering even
        when the user started mid-batch (e.g. "start from entry 10").
      * `pos`         — 1-based queue position of items actually being
        uploaded (filters out skipped entries).  Used for the progress
        header "[pos/total]" and for ordering the delivery loop.
    """
    # Find the file-order position of the entry the user typed in
    # Step 4/4.  Matching is done against `e.index` (the `[NNN]` label
    # printed in the .txt), NOT against a per-link global counter — so
    # "start 13" jumps to the entry labelled [013] in the .txt and
    # downloads from there in file order, regardless of where that
    # entry physically appears in the list.
    start_cursor: Optional[int] = None
    if start > 1:
        for i, e in enumerate(entries):
            if e.index == start:
                start_cursor = i
                break
        if start_cursor is None:
            # Fall-back for users who type an index that isn't in the
            # .txt (e.g. the slice doesn't contain [013]): start from
            # the first entry whose label is >= N, matching the old
            # behaviour.
            for i, e in enumerate(entries):
                if e.index >= start:
                    start_cursor = i
                    break
    if start_cursor is None:
        # start > 1 was given but no entry matched and no entry has an
        # index >= start.  Queue nothing (user typed an index that
        # doesn't exist in this .txt and is above every present label).
        start_cursor = len(entries) if start > 1 else 0

    q: list[dict[str, Any]] = []
    global_idx = 0
    pos = 0
    for i, e in enumerate(entries):
        sub = 1
        for url, kind, label in [
            (e.video_url, "video", "video"),
            (e.pdf_url, "pdf", "pdf"),
            (e.pdf_url2, "pdf", "pdf2"),
            (e.study_material_url, "pdf", "notes"),
        ]:
            if not url or url == "-":
                continue
            global_idx += 1
            if i < start_cursor:
                # Count toward global_idx so captions stay stable, but
                # don't enqueue for upload.
                sub += 1
                continue
            pos += 1
            q.append({
                "pos": pos,
                "global_idx": global_idx,
                "entry_index": e.index,
                "entry_sub": sub,
                "kind": kind,
                "label": label,
                "url": url,
                "title": e.title,
                "topic": e.topic,
                "date": e.date,
                "duration": e.duration,
                "quality": e.quality,
                "video_id": e.video_id,
                "course_id": course_id,
            })
            sub += 1
    return q


def _url_expires_at(url: str) -> int:
    """Return the `Expires=` epoch for a signed Appx URL, or 0 if absent."""
    if not url or not url.startswith("http"):
        return 0
    m = re.search(r"[?&]Expires=(\d+)", url)
    return int(m.group(1)) if m else 0


# =============================================================================
# DOWNLOAD  (Referer header + on-the-fly EBML header fixup)
# =============================================================================


# Appx's static-trans-v2 and static-db-v2 CDNs only serve content when the
# request carries this exact Referer.  Missing it -> 404 "Google-Edge-Cache:
# not found. Error: 3" even for signed URLs.
PLAYER_REFERER = "https://player.akamai.net.in/"
PLAYER_ORIGIN = "https://player.akamai.net.in"

# Appx scrambles the first 40 bytes of every `encrypted.mkv` -- bytes past
# offset 0x28 are already a valid Matroska Segment.  Splicing a canonical
# 40-byte EBML header back on recovers a playable file.
_EBML_HEADER_40 = bytes.fromhex(
    "1A45DFA3"                       # EBML magic
    "9F"                             # size 31
    "4286" "81" "01"                 # EBMLVersion
    "42F7" "81" "01"                 # EBMLReadVersion
    "42F2" "81" "04"                 # MaxIDLength
    "42F3" "81" "08"                 # MaxSizeLength
    "4282" "88" "6D6174726F736B61"   # DocType = "matroska"
    "4287" "81" "04"                 # DocTypeVersion
    "4285" "81" "02"                 # DocTypeReadVersion
)
assert len(_EBML_HEADER_40) == 40


def _is_scrambled_mkv_header(first40: bytes) -> bool:
    return first40[:4] != b"\x1A\x45\xDF\xA3"


_BASE_DL_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": PLAYER_REFERER,
    "Origin": PLAYER_ORIGIN,
    "Accept": "*/*",
}


async def _probe_range(
    http: aiohttp.ClientSession, url: str,
) -> tuple[int, bool]:
    """Return (total_size, supports_range).  Uses a 1-byte Range GET so
    the Appx CDN always answers (it doesn't support HEAD for signed URLs).
    """
    try:
        headers = {**_BASE_DL_HEADERS, "Range": "bytes=0-0"}
        async with http.get(
            url, headers=headers, allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status == 206:
                cr = r.headers.get("Content-Range") or ""
                m = re.match(r"bytes\s+\d+-\d+/(\d+)", cr)
                if m:
                    return int(m.group(1)), True
            if r.status == 200:
                cl = r.headers.get("Content-Length")
                if cl and cl.isdigit():
                    return int(cl), False
    except Exception as exc:
        log.debug("probe failed: %s", exc)
    return 0, False


async def _download_chunk(
    http: aiohttp.ClientSession, url: str, fd: int,
    start: int, end: int, *,
    max_attempts: int = 6,
    on_bytes: Optional[Callable[[int], None]] = None,
) -> None:
    """Fetch bytes [start..end] inclusive and pwrite them into `fd`.

    Retries via a shrunken Range on transient network errors, resuming
    exactly where the previous attempt dropped off.
    """
    import os as _os
    got = 0
    attempt = 0
    while got < (end - start + 1):
        attempt += 1
        cur_start = start + got
        headers = {**_BASE_DL_HEADERS,
                   "Range": f"bytes={cur_start}-{end}"}
        try:
            async with http.get(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=60 * 60, sock_read=120),
            ) as r:
                if r.status not in (200, 206):
                    raise RuntimeError(f"HTTP {r.status}")
                off = cur_start
                async for data in r.content.iter_chunked(MULTI_CHUNK_IO):
                    _os.pwrite(fd, data, off)
                    off += len(data)
                    got += len(data)
                    if on_bytes:
                        on_bytes(len(data))
                if got >= (end - start + 1):
                    return
                raise aiohttp.ClientPayloadError(
                    f"short chunk: {got}/{end - start + 1}")
        except (aiohttp.ClientPayloadError,
                aiohttp.ServerDisconnectedError,
                aiohttp.ClientConnectionError,
                asyncio.TimeoutError) as exc:
            if attempt >= max_attempts:
                raise RuntimeError(
                    f"chunk {start}-{end} failed {attempt}x: {exc}") from exc
            await asyncio.sleep(min(30.0, 2.0 * attempt) +
                                random.uniform(0, 1.0))


async def _download_multi(
    http: aiohttp.ClientSession,
    url: str,
    dest: Path,
    total: int,
    *,
    kind: str = "",
    conns: int = 8,
    progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
) -> Path:
    """Parallel Range download.  Preallocates `dest.part`, splits into
    `conns` equal pieces, writes via os.pwrite (atomic per-call on POSIX)
    so concurrent writers never trample each other.  Patches the MKV
    EBML header as a final post-processing step when `kind=='video'`.
    """
    import os as _os
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
    # Pre-allocate full file so pwrite offsets are valid.
    fd = _os.open(str(tmp), _os.O_RDWR | _os.O_CREAT | _os.O_TRUNC, 0o644)
    try:
        _os.ftruncate(fd, total)
        # Equal-sized ranges.  Last chunk absorbs the remainder.
        per = max(1, total // conns)
        ranges: list[tuple[int, int]] = []
        for i in range(conns):
            s = i * per
            e = total - 1 if i == conns - 1 else s + per - 1
            if s > e:
                break
            ranges.append((s, e))

        downloaded = 0
        last_report = [0.0]
        # on_bytes is called from worker coroutines; kept sync, no lock
        # needed because asyncio is single-threaded within the loop.
        def on_bytes(n: int) -> None:
            nonlocal downloaded
            downloaded += n
            now = time.monotonic()
            if progress and (now - last_report[0]) > 3:
                last_report[0] = now
                # Fire-and-forget a reporter.  `progress` is an async fn.
                asyncio.create_task(progress(downloaded, total))

        tasks = [
            asyncio.create_task(
                _download_chunk(http, url, fd, s, e, on_bytes=on_bytes))
            for s, e in ranges
        ]
        try:
            await asyncio.gather(*tasks)
        except Exception:
            for t in tasks:
                t.cancel()
            raise

        # EBML header fix-up for scrambled MKVs.
        if kind == "video":
            head = _os.pread(fd, 40, 0)
            if len(head) == 40 and _is_scrambled_mkv_header(head):
                _os.pwrite(fd, _EBML_HEADER_40, 0)
    finally:
        try:
            _os.close(fd)
        except Exception:
            pass

    if progress:
        await progress(total, total)
    tmp.rename(dest)
    return dest


async def download_url(
    http: aiohttp.ClientSession,
    url: str,
    dest: Path,
    *,
    kind: str = "",
    progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
    max_attempts: int = 6,
) -> Path:
    """Download `url` to `dest` using N parallel Range connections when
    the file is large enough and the CDN supports range requests.

    Appx throttles each TCP connection to ~1.5 MB/s, so a 600 MB video
    over a single connection takes ~7 min.  Splitting into 8 parallel
    chunks regularly hits 8-12 MB/s on Kaggle (~60 s for the same file).

    Falls back to the classic single-stream path when the server doesn't
    expose Content-Length or doesn't honour Range (unlikely for Appx but
    we keep the safety net).
    """
    if MULTI_CONN > 1:
        total, ranges_ok = await _probe_range(http, url)
        if ranges_ok and total >= MULTI_MIN_SIZE:
            try:
                return await _download_multi(
                    http, url, dest, total,
                    kind=kind, conns=MULTI_CONN, progress=progress)
            except Exception as exc:
                log.warning(
                    "multi-conn download failed (%s); "
                    "falling back to single stream", exc)
                # Clean up any partial files before retrying single-stream.
                for p in (dest, dest.with_suffix(dest.suffix + ".part")):
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass
    return await _download_single(
        http, url, dest, kind=kind, progress=progress,
        max_attempts=max_attempts)


async def _download_single(
    http: aiohttp.ClientSession,
    url: str,
    dest: Path,
    *,
    kind: str = "",
    progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
    max_attempts: int = 6,
) -> Path:
    """Single-stream fallback with auto-resume via `Range:` on CDN drops."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
    base_headers = {
        "User-Agent": USER_AGENT,
        "Referer": PLAYER_REFERER,
        "Origin": PLAYER_ORIGIN,
        "Accept": "*/*",
    }

    downloaded = 0
    total = 0
    header_buf = bytearray()
    patched_header = False
    last_report = 0.0
    f = tmp.open("ab")

    try:
        for attempt in range(1, max_attempts + 1):
            headers = dict(base_headers)
            if downloaded > 0:
                headers["Range"] = f"bytes={downloaded}-"
            try:
                async with http.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60 * 60,
                                                  sock_read=120),
                ) as r:
                    if downloaded > 0 and r.status == 200:
                        # Server ignored our Range and restarted from 0.
                        # Truncate the temp file and accept a full restart.
                        f.close()
                        tmp.unlink(missing_ok=True)
                        f = tmp.open("ab")
                        downloaded = 0
                        header_buf = bytearray()
                        patched_header = False
                    elif r.status not in (200, 206):
                        raise RuntimeError(
                            f"HTTP {r.status} for {url[:120]}")

                    if downloaded == 0:
                        total = int(r.headers.get("Content-Length") or 0)
                    elif r.status == 206:
                        # Content-Range: bytes <from>-<to>/<total>
                        cr = r.headers.get("Content-Range") or ""
                        m = re.match(r"bytes\s+\d+-\d+/(\d+)", cr)
                        if m:
                            total = int(m.group(1))

                    async for chunk in r.content.iter_chunked(1 << 16):
                        if kind == "video" and not patched_header:
                            header_buf.extend(chunk)
                            if len(header_buf) < 40:
                                downloaded += len(chunk)
                                continue
                            if _is_scrambled_mkv_header(
                                    bytes(header_buf[:40])):
                                f.write(_EBML_HEADER_40)
                            else:
                                f.write(bytes(header_buf[:40]))
                            if len(header_buf) > 40:
                                f.write(bytes(header_buf[40:]))
                            patched_header = True
                            downloaded += len(chunk)
                            if progress and (
                                    time.monotonic() - last_report) > 3:
                                await progress(downloaded, total)
                                last_report = time.monotonic()
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress and (
                                time.monotonic() - last_report) > 3:
                            await progress(downloaded, total)
                            last_report = time.monotonic()

                    # Handle tiny files whose whole body landed in the
                    # header buffer (unlikely for 1 MB+ files but safe).
                    if (kind == "video" and not patched_header
                            and header_buf):
                        if _is_scrambled_mkv_header(
                                bytes(header_buf[:40])):
                            out = bytearray(
                                _EBML_HEADER_40[: len(header_buf)])
                            out.extend(header_buf[40:])
                            f.write(bytes(out))
                        else:
                            f.write(bytes(header_buf))
                        patched_header = True

                # Stream finished cleanly.
                if total and downloaded < total:
                    # Server ended the response early without raising --
                    # force a resume attempt.
                    raise aiohttp.ClientPayloadError(
                        f"short read: {downloaded}/{total}")
                break  # success

            except (aiohttp.ClientPayloadError,
                    aiohttp.ServerDisconnectedError,
                    aiohttp.ClientConnectionError,
                    asyncio.TimeoutError) as exc:
                if attempt >= max_attempts:
                    raise RuntimeError(
                        f"CDN dropped connection {attempt} times "
                        f"at {downloaded}/{total or '?'} bytes: {exc}"
                    ) from exc
                backoff = min(30.0, 2.0 * attempt) + random.uniform(0, 1.5)
                log.warning(
                    "download interrupted (%s) at %s/%s, retry %s/%s in %.1fs",
                    type(exc).__name__, downloaded, total or "?",
                    attempt, max_attempts, backoff)
                await asyncio.sleep(backoff)
                # Re-open the file for append; keep what we already have.
                try:
                    f.flush()
                except Exception:
                    pass
    finally:
        try:
            f.close()
        except Exception:
            pass

    if progress:
        await progress(downloaded, total or downloaded)
    tmp.rename(dest)
    return dest


# =============================================================================
# HELPERS: filename, caption, thumb, duration
# =============================================================================


_SAFE_NAME = re.compile(r"[^A-Za-z0-9 ._()\-]+")


def safe_filename(title: str, ext: str, pos: int = 0, max_len: int = 80) -> str:
    """Return a clean filename.  Falls back to Item_NNN when title is non-Latin."""
    import html as _html
    import unicodedata

    name = _html.unescape(title or "")
    try:
        ascii_ = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    except Exception:
        ascii_ = ""
    cleaned = _SAFE_NAME.sub(" ", ascii_).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    if len(cleaned) < 3:
        cleaned = f"Item_{str(pos).zfill(3)}" if pos else "Item"
    cleaned = cleaned[:max_len]
    suffix = FILENAME_SUFFIX or ""
    if suffix:
        cleaned = f"{cleaned}{suffix}"
    if not cleaned.lower().endswith(ext.lower()):
        cleaned = f"{cleaned}{ext}"
    return cleaned


def build_caption(
    pos: int,
    title: str,
    topic: str,
    date: str,
    duration: str = "",
    quality: str = "",
    batch_name: str = "",
    extracted_by: str = "",
) -> str:
    """Telegram caption (HTML parse mode) matching the oliveboard layout."""
    import html as _html

    def esc(s: str) -> str:
        return _html.escape(s or "", quote=False)

    # Replace ':' in date so Telegram does not turn it into a fake video
    # timestamp link.
    safe_date = (date or "").replace(":", ".")

    lines = [
        f"<b>Index :</b> {str(pos).zfill(3)}",
        "",
        f"<b>Title :</b> {esc(title)}",
        "",
        f"<b>Topic :</b> {esc(topic)}",
    ]
    if safe_date:
        lines += ["", f"<b>Date :</b> {esc(safe_date)}"]
    if batch_name.strip():
        lines += ["", f"<b>Batch :</b> {esc(batch_name.strip())}"]
    if extracted_by.strip():
        lines += ["", f"<b>Extracted By :</b> {esc(extracted_by.strip())}"]
    return "\n".join(lines)


def ffprobe_duration(path: Path) -> Optional[int]:
    if not shutil.which("ffprobe"):
        return None
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return int(float(r.stdout.strip()))
    except Exception:
        return None


def _mp4_is_valid(path: Path) -> bool:
    """Check that `path` has at least one decodable H.264 video stream
    with non-zero duration.  Guards against silently-broken remuxes
    where ffmpeg exits 0 but the MP4 has no playable video.
    """
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height:format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(path)],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if r.returncode != 0:
            return False
        lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        # Expect: codec_name, width, height, duration
        if len(lines) < 4:
            return False
        try:
            if float(lines[3]) <= 0.1:
                return False
        except ValueError:
            return False
        return True
    except Exception:
        return False


def remux_mkv_to_mp4(mkv_path: Path) -> Path:
    """Remux an .mkv into an .mp4 container with -c copy (no re-encode).

    Appx's raw stream is H.264 + AAC, which maps 1:1 into MP4, so this
    takes 1-3 seconds for a 600 MB file and is bit-identical to the
    original audio/video.  Falls back to returning the .mkv path
    unchanged if ffmpeg is missing, remux fails, or the remuxed MP4
    can't be verified by ffprobe (i.e. PC players wouldn't play it).

    Defensive flags vs. the plain `-c copy`:
      * `-map 0:v -map 0:a`        : only video+audio; skip subtitles
        (MKV SRT/ASS don't fit MP4 and kill the remux otherwise).
      * `-bsf:a aac_adtstoasc`     : safe no-op when source is already
        raw AAC; converts ADTS to ASC if the source had an ADTS header.
      * `-avoid_negative_ts make_zero` + `-fflags +genpts` : rebuild
        clean PTS so Windows Media Player / desktop VLC don't choke.
      * `-movflags +faststart`     : put moov atom at the start so the
        MP4 opens instantly in PC players (and streams on Telegram).
    """
    if not mkv_path.exists() or not shutil.which("ffmpeg"):
        return mkv_path
    mp4_path = mkv_path.with_suffix(".mp4")
    try:
        if mp4_path.exists():
            mp4_path.unlink()
        r = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-fflags", "+genpts",
             "-i", str(mkv_path),
             "-map", "0:v:0?", "-map", "0:a?",
             "-c:v", "copy", "-c:a", "copy",
             "-bsf:a", "aac_adtstoasc",
             "-avoid_negative_ts", "make_zero",
             "-movflags", "+faststart",
             "-map_metadata", "0",
             "-ignore_unknown",
             "-f", "mp4",
             str(mp4_path)],
            capture_output=True, timeout=600, check=False,
        )
        if (r.returncode == 0
                and mp4_path.exists()
                and mp4_path.stat().st_size > 0
                and _mp4_is_valid(mp4_path)):
            try:
                mkv_path.unlink()
            except OSError:
                pass
            return mp4_path
        err = (r.stderr or b"").decode("utf-8", "replace")[:400]
        log.warning("mkv→mp4 remux failed (rc=%s, err=%r); keeping .mkv",
                    r.returncode, err)
    except Exception as exc:
        log.warning("mkv→mp4 remux error: %s", exc)
    # Best-effort cleanup of any partial/invalid mp4 on failure.
    try:
        if mp4_path.exists():
            mp4_path.unlink()
    except OSError:
        pass
    return mkv_path


def extract_thumb(video_path: Path, out_path: Path, seek_secs: int = 5) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(seek_secs), "-i", str(video_path),
             "-vframes", "1", "-vf", "scale=320:-1", "-q:v", "2",
             str(out_path)],
            capture_output=True, timeout=20, check=False,
        )
        return r.returncode == 0 and out_path.exists()
    except Exception:
        return False


# Cache of Telegram-ready thumbs (one per unique input path) so we don't
# re-encode the same source image for every file in a batch.
_TG_THUMB_CACHE: dict[str, str] = {}

# Telegram hard limits for a document/video thumbnail.  We stay under
# both to avoid silent server-side resize.
_TG_THUMB_MAX_PX = 320
_TG_THUMB_MAX_BYTES = 200 * 1024


def prepare_tg_thumb(src: str) -> Optional[str]:
    """Return a path to a Telegram-optimal thumbnail derived from `src`.

    Pass-through when the input is already a JPEG within Telegram's
    limits (≤320x320 and ≤200 KB).  Otherwise we Lanczos-resize to fit
    inside 320x320 and JPEG-encode at quality 95 with 4:4:4 chroma
    subsampling -- visibly sharper than Pyrogram's default bilinear +
    q72 + 4:2:0 re-encode.
    """
    if not src:
        return None
    p = Path(src)
    if not p.exists():
        return None
    key = str(p.resolve())
    if key in _TG_THUMB_CACHE:
        cached = _TG_THUMB_CACHE[key]
        if cached and Path(cached).exists():
            return cached
    try:
        from PIL import Image
    except ImportError:
        log.warning("Pillow not installed; thumbnail quality will be "
                    "reduced by the Telegram client's default resize")
        return src

    try:
        with Image.open(p) as im:
            w, h = im.size
            fmt = (im.format or "").upper()
            needs_resize = (w > _TG_THUMB_MAX_PX or h > _TG_THUMB_MAX_PX)
            size_ok = p.stat().st_size <= _TG_THUMB_MAX_BYTES
            if not needs_resize and size_ok and fmt == "JPEG":
                _TG_THUMB_CACHE[key] = str(p)
                return str(p)
            # Compose onto white if the image has alpha, else drop alpha.
            rgb = im.convert("RGB")
            if needs_resize:
                # Pillow ≥9 exposes Resampling; older versions use LANCZOS.
                try:
                    resample = Image.Resampling.LANCZOS
                except AttributeError:  # pragma: no cover
                    resample = Image.LANCZOS  # type: ignore[attr-defined]
                rgb.thumbnail((_TG_THUMB_MAX_PX, _TG_THUMB_MAX_PX),
                              resample=resample)

        out = DOWNLOAD_DIR / "_tg_thumbs" / (p.stem + ".jpg")
        out.parent.mkdir(parents=True, exist_ok=True)
        # Try quality 95 → step down if it busts the 200 KB cap.
        for q in (95, 90, 85, 80, 75, 70):
            rgb.save(out, "JPEG", quality=q, subsampling=0,
                     optimize=True, progressive=False)
            if out.stat().st_size <= _TG_THUMB_MAX_BYTES:
                break
        _TG_THUMB_CACHE[key] = str(out)
        return str(out)
    except Exception as exc:
        log.warning("prepare_tg_thumb failed for %s: %s", src, exc)
        return src


def resolve_thumb(video_path: Path, user_thumb: str = "", default: str = "") -> Optional[str]:
    # Prefer the user's custom thumb, then auto-extracted frame, then
    # the DEFAULT_THUMB fallback.  Every branch now runs through
    # `prepare_tg_thumb` so Telegram receives the highest-quality
    # version it's willing to keep.
    if user_thumb and Path(user_thumb).exists():
        return prepare_tg_thumb(user_thumb)
    tmp = video_path.with_suffix(video_path.suffix + "_thumb.jpg")
    if extract_thumb(video_path, tmp):
        return prepare_tg_thumb(str(tmp))
    if default and Path(default).exists():
        return prepare_tg_thumb(default)
    return None


def wait_for_disk_space(needed: int = MIN_FREE_DISK_BYTES) -> None:
    while True:
        try:
            usage = shutil.disk_usage(DOWNLOAD_DIR)
        except OSError:
            return
        if usage.free >= needed:
            return
        log.warning("Free disk %s MB < threshold; waiting %ss",
                    usage.free // (1 << 20), DISK_WAIT_SECS)
        time.sleep(DISK_WAIT_SECS)


# =============================================================================
# DB  (users / cookies / txt queue / batch session)
# =============================================================================


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id    INTEGER PRIMARY KEY,
            jwt        TEXT DEFAULT '',
            appx_user  TEXT DEFAULT '',
            mode       TEXT DEFAULT '',
            pending    TEXT DEFAULT '',
            handle     TEXT DEFAULT '',
            default_thumb TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS txt_queue (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            file_path    TEXT NOT NULL,
            target_chat  INTEGER NOT NULL,
            batch_name   TEXT DEFAULT '',
            extracted_by TEXT DEFAULT '',
            thumb_path   TEXT DEFAULT '',
            start_entry  INTEGER DEFAULT 1,
            added_at     TEXT
        );
    """)
    conn.commit()
    return conn


def user_get(uid: int) -> dict[str, Any]:
    with _db() as c:
        r = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    return dict(r) if r else {}


def user_put(uid: int, **f: Any) -> None:
    cur = user_get(uid)
    cur.update(f)
    with _db() as c:
        c.execute(
            "INSERT INTO users(user_id,jwt,appx_user,mode,pending,handle,default_thumb)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET"
            " jwt=excluded.jwt, appx_user=excluded.appx_user,"
            " mode=excluded.mode, pending=excluded.pending,"
            " handle=excluded.handle, default_thumb=excluded.default_thumb",
            (uid, cur.get("jwt", ""), cur.get("appx_user", ""),
             cur.get("mode", ""), cur.get("pending", ""),
             cur.get("handle", ""), cur.get("default_thumb", "")),
        )
        c.commit()


def queue_push(uid: int, file_path: str, target_chat: int, *,
               batch_name: str, extracted_by: str,
               thumb_path: str, start_entry: int) -> int:
    with _db() as c:
        cur = c.execute(
            "INSERT INTO txt_queue(user_id,file_path,target_chat,batch_name,"
            "extracted_by,thumb_path,start_entry,added_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (uid, file_path, target_chat, batch_name,
             extracted_by, thumb_path, start_entry,
             time.strftime("%Y-%m-%dT%H:%M:%S")),
        )
        c.commit()
        return cur.lastrowid


def queue_pop(uid: int) -> Optional[dict[str, Any]]:
    with _db() as c:
        r = c.execute(
            "SELECT * FROM txt_queue WHERE user_id=? ORDER BY id LIMIT 1",
            (uid,),
        ).fetchone()
        if not r:
            return None
        c.execute("DELETE FROM txt_queue WHERE id=?", (r["id"],))
        c.commit()
        return dict(r)


def queue_count(uid: int) -> int:
    with _db() as c:
        r = c.execute(
            "SELECT COUNT(*) FROM txt_queue WHERE user_id=?", (uid,)
        ).fetchone()
    return int(r[0] if r else 0)


# =============================================================================
# PYROGRAM CLIENT + GLOBALS
# =============================================================================


app = Client(
    "yesofficer-bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=str(Path(__file__).parent),
)


_http: Optional[aiohttp.ClientSession] = None


async def http() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=CONCURRENT_HTTP, ssl=False),
            trust_env=True,
        )
    return _http


def _allowed(user_id: int) -> bool:
    return (not ALLOWED_USER_IDS) or user_id in ALLOWED_USER_IDS


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Extractor", callback_data="mode:extract"),
         InlineKeyboardButton("⬆️ Uploader", callback_data="mode:upload")],
        [InlineKeyboardButton("🧠 Quizzes", callback_data="mode:quiz"),
         InlineKeyboardButton("🔐 Cookies", callback_data="cookies")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help"),
         InlineKeyboardButton("📊 Status", callback_data="status")],
    ])


# Running-batch state per user.
active_batches: dict[int, dict[str, Any]] = {}

# In-flight extractor state per user: filled after the folder scan, consumed
# by the slice callback.  Keeps IndexRow lists alive between clicks.
_extract_state: dict[int, dict[str, Any]] = {}

# In-flight quiz-extract state per user: filled after the course quiz scan,
# consumed by the quiz-slice callback.  Keeps QuizRef lists alive between
# clicks.
_quiz_state: dict[int, dict[str, Any]] = {}


# =============================================================================
# COMMAND HANDLERS
# =============================================================================


HELP_TEXT = (
    "<b>YesOfficer Extractor + Uploader</b>\n\n"
    "<b>Extractor</b>: paste cookies, send a course URL, get a <code>.txt</code> "
    "listing every video + PDF.\n\n"
    "<b>Uploader</b>: send that <code>.txt</code>, answer 4 quick setup questions "
    "(Batch name / Extracted-by / Thumbnail / Start entry), and I'll "
    "download + upload video → PDF → PDF 2 for each entry.\n\n"
    "<b>Quizzes</b>: send a course URL; I'll walk the course, fetch every "
    "quiz's questions + options + correct answers + solutions, and send "
    "back a zip of styled HTML files.  <i>No cookies required.</i>\n\n"
    "<b>/queue</b> — reply to a <code>.txt</code> with <code>/queue</code> to line it up after the "
    "current batch.\n"
    "<b>/status</b> — current progress + queue.\n"
    "<b>/cancel</b> — abort the running batch.\n"
    "<b>/clean</b> — owner only, wipe <code>downloads/</code>.\n\n"
    "Signed CDN URLs are valid for ~2 h; upload in the same session you "
    "extracted or just re-extract."
)


@app.on_message(filters.command("start"))
async def cmd_start(_: Client, m: Message) -> None:
    if not _allowed(m.from_user.id):
        await m.reply_text("This bot is private.")
        return
    user_put(m.from_user.id, mode="", pending="")
    await m.reply_text(
        "👋 <b>YesOfficer Extractor + Uploader</b>\n\n"
        "Tap a mode below.  If I don't have your cookies yet I'll ask first.",
        reply_markup=main_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("cancel"))
async def cmd_cancel(_: Client, m: Message) -> None:
    uid = m.from_user.id
    user_put(uid, pending="")
    batch = active_batches.get(uid)
    if batch and batch.get("task"):
        batch["stop"] = True
        batch["task"].cancel()
        await m.reply_text("🛑 Cancelling current batch…")
    else:
        await m.reply_text("OK, nothing to cancel. /start to begin again.")


@app.on_message(filters.command("status"))
async def cmd_status(_: Client, m: Message) -> None:
    await _send_status(m)


async def _send_status(m: Message) -> None:
    uid = m.from_user.id
    batch = active_batches.get(uid)
    qcount = queue_count(uid)
    lines: list[str] = []
    if batch:
        tot = batch.get("total", 0)
        done = batch.get("done", 0)
        okc = batch.get("ok", 0)
        failed = batch.get("failed", 0)
        pct = (done * 100 // tot) if tot else 0
        lines += [
            f"<b>📥 Running:</b> <code>{batch.get('name','?')}</code>",
            f"Progress: <code>{done}/{tot}</code> ({pct}%)",
            f"Success : <code>{okc}</code>   Failed: <code>{failed}</code>",
        ]
    else:
        lines.append("<b>📥 Running:</b> nothing")
    lines.append(f"<b>📚 Queue:</b> {qcount} .txt waiting")
    try:
        free = shutil.disk_usage(DOWNLOAD_DIR).free
        lines.append(f"<b>💾 Free disk:</b> {free // (1<<30)} GB")
    except OSError:
        pass
    await m.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("clean"))
async def cmd_clean(_: Client, m: Message) -> None:
    uid = m.from_user.id
    if OWNER_ID and uid != OWNER_ID:
        await m.reply_text("⛔ /clean is owner-only.")
        return
    if active_batches.get(uid):
        await m.reply_text("⚠️ A batch is running — /cancel first.")
        return
    try:
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        await m.reply_text("🧹 Wiped downloads/.")
    except Exception as exc:
        await m.reply_text(f"❌ Clean failed: {exc}")


@app.on_message(filters.command("queue"))
async def cmd_queue(c: Client, m: Message) -> None:
    if not _allowed(m.from_user.id):
        return
    replied = m.reply_to_message
    if not replied or not replied.document:
        await m.reply_text(
            "Reply to a <code>.txt</code> file with <code>/queue</code>.",
            parse_mode=ParseMode.HTML)
        return
    if not (replied.document.file_name or "").lower().endswith(".txt"):
        await m.reply_text("That's not a .txt file.")
        return
    uid = m.from_user.id
    dest = DOWNLOAD_DIR / f"queue_{uid}_{int(time.time())}.txt"
    await replied.download(file_name=str(dest))
    user_put(uid, mode="upload", pending="await_batch_name",
             **{"_qpath": str(dest)})  # _qpath stored via state dict below
    _temp_state[uid] = {
        "file_path": str(dest),
        "target_chat": m.chat.id,
    }
    await m.reply_text(
        "📋 Queued.  Send a <b>Batch name</b> now "
        "(or type <code>-</code> to leave blank).",
        parse_mode=ParseMode.HTML)


# =============================================================================
# CALLBACKS + STATE MACHINE
# =============================================================================


# Per-user in-memory state for the 4-step upload setup (survives one batch).
_temp_state: dict[int, dict[str, Any]] = {}


@app.on_callback_query()
async def on_cb(c: Client, q: CallbackQuery) -> None:
    uid = q.from_user.id
    if not _allowed(uid):
        await q.answer("This bot is private.", show_alert=True)
        return
    data = q.data or ""
    await q.answer()

    if data == "help":
        await q.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True)
        return
    if data == "status":
        await _send_status(q.message)
        return
    if data == "cookies":
        user_put(uid, pending="await_cookies")
        await q.message.reply_text(
            "🔐 Send me your YesOfficer cookie string.\n"
            "(<code>Authorization=&lt;jwt&gt; ; User-ID=&lt;id&gt;</code> or the whole "
            "<code>Cookie:</code> line.)",
            parse_mode=ParseMode.HTML)
        return
    if data == "mode:extract":
        s = user_get(uid)
        if not s.get("jwt") or not s.get("appx_user"):
            user_put(uid, mode="extract", pending="await_cookies")
            await q.message.reply_text(
                "🔐 First, send me your cookie string "
                "(Authorization + User-ID).")
            return
        user_put(uid, mode="extract", pending="await_course_url")
        await q.message.reply_text(
            "📚 Send me the course URL or id (e.g. <code>268</code> or "
            "<code>https://www.yesofficer.com/new-courses/268/content</code>).",
            parse_mode=ParseMode.HTML)
        return
    if data == "mode:upload":
        user_put(uid, mode="upload", pending="await_txt")
        await q.message.reply_text(
            "📎 Send me the <code>.txt</code> file you got from <b>Extractor</b>.",
            parse_mode=ParseMode.HTML)
        return
    if data == "mode:quiz":
        # Quiz fetch works fully anonymous -- no cookies required.  If the
        # user already pasted cookies we'll use them to also overlay their
        # own answers on attempted quizzes, but it's entirely optional.
        user_put(uid, mode="quiz", pending="await_quiz_course_url")
        await q.message.reply_text(
            "🧠 Send me the course URL or id of a course with quizzes "
            "(e.g. <code>225</code> or "
            "<code>https://www.yesofficer.com/new-courses/225/content</code>).\n\n"
            "I'll walk the course tree, list every quiz, and generate "
            "an HTML file per quiz with questions + options + correct "
            "answers + full solutions.\n\n"
            "<i>No cookies needed for unattempted quizzes.  If you've "
            "already saved cookies via 🔐, I'll also overlay your own "
            "answers on quizzes you've attempted.</i>",
            parse_mode=ParseMode.HTML)
        return
    if data.startswith("slice:"):
        await _handle_slice_callback(c, q)
        return
    if data.startswith("qslice:"):
        await _handle_quiz_slice_callback(c, q)
        return


@app.on_message(filters.private & filters.text
                & ~filters.command(["start", "cancel", "status",
                                    "clean", "queue"]))
async def on_text(c: Client, m: Message) -> None:
    if not _allowed(m.from_user.id):
        return
    uid = m.from_user.id
    s = user_get(uid)
    pending = s.get("pending", "")
    text = (m.text or "").strip()

    if pending == "await_cookies":
        pairs = parse_cookie_blob(text)
        jwt = pairs.get("Authorization") or pairs.get("authorization") or ""
        appx_user = (pairs.get("User-ID") or pairs.get("user-id")
                     or pairs.get("user_id") or "")
        if not jwt and text.count(".") == 2 and text.startswith(("eyJ", "ey")):
            jwt = text
        if not jwt:
            await m.reply_text(
                "I couldn't find <code>Authorization=&lt;jwt&gt;</code>. Please resend.",
                parse_mode=ParseMode.HTML)
            return
        if not appx_user:
            try:
                payload_b64 = jwt.split(".")[1] + "=="
                payload = json.loads(base64.urlsafe_b64decode(payload_b64))
                appx_user = str(payload.get("id") or payload.get("user_id") or "")
            except Exception:
                appx_user = ""
        if not appx_user:
            await m.reply_text("I couldn't find your User-ID. Include it too.")
            return
        user_put(uid, jwt=jwt, appx_user=appx_user, pending="")
        await m.reply_text(
            f"✅ Saved cookies for user <code>{appx_user}</code>.",
            reply_markup=main_keyboard(),
            parse_mode=ParseMode.HTML)
        return

    if pending == "await_course_url":
        cid = _course_id_from_url(text)
        if not cid:
            await m.reply_text("That didn't look like a yesofficer course URL or id.")
            return
        user_put(uid, pending="")
        await handle_extract(c, m, cid)
        return

    if pending == "await_quiz_course_url":
        import quiz_fetcher as _qf
        kind, tid = _qf.parse_quiz_target(text)
        if not tid:
            await m.reply_text(
                "That didn't look like a yesofficer URL.\n"
                "Send a course URL (<code>/new-courses/&lt;id&gt;</code>), "
                "a test-series URL (<code>/test-series/&lt;id&gt;-&lt;slug&gt;</code>), "
                "or just a numeric id.",
                parse_mode=ParseMode.HTML)
            return
        user_put(uid, pending="")
        await handle_quiz_extract(c, m, tid, kind=kind)
        return

    if pending == "await_quiz_custom":
        st = _quiz_state.get(uid)
        if not st:
            user_put(uid, pending="")
            await m.reply_text("That quiz session expired. Start over.")
            return
        quizzes = st["quizzes"]
        start, end = _parse_custom_range(text, len(quizzes))
        if not start:
            await m.reply_text(
                "Use formats like <code>15</code> (= last 15), "
                "<code>10-25</code> (quizzes 10 through 25), or "
                "<code>50+</code> (quiz 50 to end).",
                parse_mode=ParseMode.HTML)
            return
        user_put(uid, pending="")
        picked = [q for q in quizzes if start <= q.position <= end]
        await _do_quiz_slice(c, uid, picked)
        return

    if pending == "await_slice_custom":
        st = _extract_state.get(uid)
        if not st:
            user_put(uid, pending="")
            await m.reply_text("That extract session expired. Start over.")
            return
        rows = st["rows"]
        start, end = _parse_custom_range(text, len(rows))
        if not start:
            await m.reply_text(
                "Use formats like <code>15</code> (= last 15), "
                "<code>10-25</code> (entries 10 through 25), or "
                "<code>200+</code> (entry 200 to end).",
                parse_mode=ParseMode.HTML)
            return
        user_put(uid, pending="")
        rows_slice = [r for r in rows if start <= r.position <= end]
        await _do_extract_slice(c, uid, rows_slice)
        return

    if pending in {"await_batch_name", "await_extracted_by",
                   "await_start_entry", "await_thumb_choice"}:
        await _handle_setup_step(c, m, pending, text)
        return

    # Fallback: if they pasted a JWT out of the blue, treat it as new cookies.
    if "Authorization=" in text or (text.count(".") == 2 and text.startswith("eyJ")):
        user_put(uid, pending="await_cookies")
        await on_text(c, m)
        return

    await m.reply_text("I'm not expecting any text right now. Tap /start.")


@app.on_message(filters.private & filters.document)
async def on_document(c: Client, m: Message) -> None:
    if not _allowed(m.from_user.id):
        return
    uid = m.from_user.id
    s = user_get(uid)
    pending = s.get("pending", "")
    doc = m.document
    if not doc:
        return

    if pending == "await_thumb_choice" and (doc.mime_type or "").startswith("image/"):
        path = DOWNLOAD_DIR / f"thumb_{uid}_{int(time.time())}.jpg"
        await m.download(file_name=str(path))
        _temp_state.setdefault(uid, {})["thumb_path"] = str(path)
        user_put(uid, pending="await_start_entry")
        await m.reply_text("🖼️ Thumb saved.  Now send the <b>start entry</b> "
                           "number (default <code>1</code>).",
                           parse_mode=ParseMode.HTML)
        return

    if pending != "await_txt":
        await m.reply_text("Tap /start and pick *Uploader* first.",
                            parse_mode=ParseMode.MARKDOWN)
        return

    if not (doc.file_name or "").lower().endswith(".txt"):
        await m.reply_text("Please send a `.txt` file from *Extractor*.",
                            parse_mode=ParseMode.MARKDOWN)
        return

    path = DOWNLOAD_DIR / f"upload_{uid}_{int(time.time())}.txt"
    await m.download(file_name=str(path))
    _temp_state[uid] = {"file_path": str(path), "target_chat": m.chat.id}
    user_put(uid, pending="await_batch_name")
    await m.reply_text(
        "📋 Got it.  <b>Step 1/4 — Batch name</b>\n"
        "Send a short name for this batch (shown in captions).  "
        "Type <code>-</code> to leave blank.",
        parse_mode=ParseMode.HTML)


@app.on_message(filters.private & filters.photo)
async def on_photo(c: Client, m: Message) -> None:
    uid = m.from_user.id
    if not _allowed(uid):
        return
    s = user_get(uid)
    if s.get("pending") != "await_thumb_choice":
        return
    path = DOWNLOAD_DIR / f"thumb_{uid}_{int(time.time())}.jpg"
    await m.download(file_name=str(path))
    _temp_state.setdefault(uid, {})["thumb_path"] = str(path)
    user_put(uid, pending="await_start_entry")
    await m.reply_text("🖼️ Thumb saved.  Send the <b>start entry</b> number "
                       "(default <code>1</code>).", parse_mode=ParseMode.HTML)


async def _handle_setup_step(c: Client, m: Message, step: str, text: str) -> None:
    uid = m.from_user.id
    st = _temp_state.setdefault(uid, {})

    if step == "await_batch_name":
        st["batch_name"] = "" if text.strip() == "-" else text.strip()
        user_put(uid, pending="await_extracted_by")
        await m.reply_text(
            "<b>Step 2/4 — Extracted By</b>\n"
            "Your handle / display name (or <code>-</code> to skip).",
            parse_mode=ParseMode.HTML)
        return
    if step == "await_extracted_by":
        st["extracted_by"] = "" if text.strip() == "-" else text.strip()
        user_put(uid, pending="await_thumb_choice")
        await m.reply_text(
            "<b>Step 3/4 — Thumbnail</b>\n"
            "Send an image to use as default thumbnail, or type "
            "<code>-</code> to skip (video frame will be auto-extracted).",
            parse_mode=ParseMode.HTML)
        return
    if step == "await_thumb_choice":
        st["thumb_path"] = ""
        user_put(uid, pending="await_start_entry")
        await m.reply_text(
            "<b>Step 4/4 — Start entry</b>\n"
            "Type the <code>[NNN]</code> label of the entry to start from "
            "(e.g. <code>13</code> jumps to <code>[013]</code>).  "
            "Default <code>1</code> = download everything.",
            parse_mode=ParseMode.HTML)
        return
    if step == "await_start_entry":
        try:
            n = int(text.strip()) if text.strip() not in {"", "-"} else 1
        except ValueError:
            n = 1
        st["start_entry"] = max(1, n)
        user_put(uid, pending="")
        await _kick_batch(c, m, uid)


async def _kick_batch(c: Client, m: Message, uid: int) -> None:
    st = _temp_state.pop(uid, None)
    if not st or not st.get("file_path"):
        await m.reply_text("Something went wrong — please /start again.")
        return
    qid = queue_push(
        uid, st["file_path"], st["target_chat"],
        batch_name=st.get("batch_name", ""),
        extracted_by=st.get("extracted_by", ""),
        thumb_path=st.get("thumb_path", ""),
        start_entry=int(st.get("start_entry", 1)),
    )
    if uid in active_batches:
        await m.reply_text(
            f"📥 Added to queue (position <code>{queue_count(uid)}</code>). "
            "Will start after the current batch.",
            parse_mode=ParseMode.HTML)
        return
    await m.reply_text("🚀 Starting batch…")
    asyncio.create_task(batch_runner(uid, c))


# =============================================================================
# EXTRACTOR FLOW
# =============================================================================


def _row_sort_key(row: "IndexRow") -> float:
    """Best-effort 'upload time' for a folder_contents row.

    Appx exposes several date-ish fields; we take the most recent one we can
    find and fall back to the row's global position so that rows without any
    date info still sort stably.
    """
    node = row.node
    best = 0.0
    for key in ("strtotime", "date_and_time", "event_date", "created_at",
                "createdAt", "created_on"):
        v = node.get(key)
        if not v:
            continue
        try:
            x = float(str(v))
            if x > best:
                best = x
        except (TypeError, ValueError):
            pass
    # If nothing else, fall back to position (course ordering).
    return best or float(row.position)


def _parse_custom_range(text: str, total: int) -> tuple[int, int]:
    """Return (start, end) 1-based inclusive; (0, 0) on parse failure."""
    s = (text or "").strip().replace(" ", "")
    if not s:
        return 0, 0
    # "200+"  =>  entry 200 to end
    m = re.fullmatch(r"(\d+)\+", s)
    if m:
        a = max(1, int(m.group(1)))
        return min(a, total), total
    # "10-25"  =>  entries 10 through 25
    m = re.fullmatch(r"(\d+)-(\d+)", s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a < 1 or b < a:
            return 0, 0
        return min(a, total), min(b, total)
    # bare number  =>  last N
    m = re.fullmatch(r"(\d+)", s)
    if m:
        n = int(m.group(1))
        if n < 1:
            return 0, 0
        n = min(n, total)
        return max(1, total - n + 1), total
    return 0, 0


async def _handle_slice_callback(c: Client, q: CallbackQuery) -> None:
    uid = q.from_user.id
    data = q.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) >= 2 else ""
    await q.answer()
    if action == "cancel":
        _extract_state.pop(uid, None)
        user_put(uid, pending="")
        try:
            await q.message.edit_text("❌ Extract cancelled.")
        except MessageNotModified:
            pass
        return
    st = _extract_state.get(uid)
    if not st:
        try:
            await q.message.edit_text("⚠️ That extract session expired.")
        except MessageNotModified:
            pass
        return
    rows = st["rows"]
    rows_slice: list[IndexRow] = []
    if action == "all":
        rows_slice = list(rows)
    elif action == "last" and len(parts) >= 3:
        try:
            n = int(parts[2])
        except ValueError:
            n = 0
        if n > 0:
            # Pick the N most-recently-added rows (by upload timestamp),
            # then re-sort ascending so the .txt reads old -> new.
            ranked = sorted(rows, key=_row_sort_key, reverse=True)
            picked = ranked[:n]
            rows_slice = sorted(picked, key=_row_sort_key)
    elif action == "custom":
        user_put(uid, pending="await_slice_custom")
        try:
            await q.message.edit_text(
                f"✏️ Send the range.\n"
                f"Total items: <b>{len(rows)}</b>.\n"
                "Examples:\n"
                "• <code>15</code>  = last 15 entries\n"
                "• <code>10-25</code>  = entries 10 through 25\n"
                "• <code>200+</code>  = entry 200 to the end",
                parse_mode=ParseMode.HTML,
            )
        except MessageNotModified:
            pass
        return
    await _do_extract_slice(c, uid, rows_slice)


def _make_extract_api(uid: int, status: Message) -> "YesOfficerApi":
    s = user_get(uid)
    session = ApiSession(jwt=s["jwt"], user_id=s["appx_user"])

    async def progress(line: str) -> None:
        try:
            await status.edit_text(f"⏳ {line}")
        except (MessageNotModified, Exception):
            pass

    async def on_long_pause(call_n: int, secs: float) -> None:
        try:
            await status.edit_text(
                f"😴 Safe-mode pause ({int(secs)} s) after {call_n} API "
                "calls — mimicking a normal student.")
        except (MessageNotModified, Exception):
            pass

    # Intentionally don't resuse the aiohttp session here, pull via http().
    h = _http  # assigned lazily via http(); non-None at this point
    api = YesOfficerApi(
        session, h,
        rate_limit_min=EXTRACT_MIN_DELAY,
        rate_limit_max=EXTRACT_MAX_DELAY,
        long_pause_every=EXTRACT_LONG_PAUSE_EVERY,
        long_pause_min=EXTRACT_LONG_PAUSE_MIN,
        long_pause_max=EXTRACT_LONG_PAUSE_MAX,
        on_long_pause=on_long_pause,
    )
    api._progress = progress  # type: ignore[attr-defined]
    return api


def _slice_keyboard(course_id: str, total: int) -> InlineKeyboardMarkup:
    def b(label: str, cb: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(label, callback_data=cb)
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([b(f"✅ All ({total})", f"slice:all:{course_id}")])
    quick: list[InlineKeyboardButton] = []
    for n in (10, 20, 50, 100):
        if n <= total:
            quick.append(b(f"Last {n}", f"slice:last:{n}:{course_id}"))
    # keep to 3 per row for readability
    for i in range(0, len(quick), 3):
        rows.append(quick[i:i + 3])
    rows.append([b("✏️ Custom range", f"slice:custom:{course_id}"),
                 b("❌ Cancel", "slice:cancel")])
    return InlineKeyboardMarkup(rows)


async def handle_extract(c: Client, m: Message, course_id: str) -> None:
    """Phase 1: crawl folder structure only, then ask user what to extract."""
    uid = m.from_user.id
    s = user_get(uid)
    if not s.get("jwt") or not s.get("appx_user"):
        await m.reply_text("No cookies saved yet.  Tap /start → Cookies.")
        return
    status = await m.reply_text(
        f"🔎 Indexing course <code>{course_id}</code> (cheap folder scan, "
        "no per-video fetches yet)…",
        parse_mode=ParseMode.HTML,
    )
    await http()  # ensure _http is set
    api = _make_extract_api(uid, status)
    try:
        meta, rows = await crawl_course_index(
            api, course_id,
            on_progress=api._progress,  # type: ignore[attr-defined]
        )
    except Exception as exc:
        log.exception("index failed")
        await status.edit_text(
            f"❌ Index failed: <code>{exc}</code>", parse_mode=ParseMode.HTML)
        return
    if not rows:
        await status.edit_text("⚠️ Course has no videos or PDFs.")
        return

    # Stash the index for the next step.
    _extract_state[uid] = {
        "course_id": course_id,
        "meta": meta,
        "rows": rows,
        "chat_id": m.chat.id,
        "status_id": status.id,
    }
    course_name = (meta.get("course_name") or meta.get("course_title")
                   or f"Course {course_id}")
    videos = sum(1 for r in rows if r.kind == "video")
    pdfs = sum(1 for r in rows if r.kind == "pdf")
    # Sort by upload time descending -- show genuinely most-recent rows,
    # even if the course ordering doesn't put them at the bottom.
    latest = sorted(rows, key=_row_sort_key, reverse=True)[:5]
    last_items = "\n".join(
        f"• <code>{r.position:03}</code> {'🎬' if r.kind=='video' else '📄'} "
        f"{r.title[:65]}"
        for r in latest
    )
    await status.edit_text(
        f"📚 <b>{course_name}</b>\n"
        f"Total items: <b>{len(rows)}</b>  ({videos} videos, {pdfs} PDFs)\n\n"
        f"<b>Latest entries:</b>\n{last_items}\n\n"
        "How much to extract?",
        parse_mode=ParseMode.HTML,
        reply_markup=_slice_keyboard(course_id, len(rows)),
    )


async def _do_extract_slice(c: Client, uid: int, rows_slice: list[IndexRow]) -> None:
    """Phase 2: resolve selected rows -> .txt file -> send to user."""
    st = _extract_state.get(uid)
    if not st:
        return
    course_id = st["course_id"]
    meta = st["meta"]
    chat_id = st["chat_id"]
    status_id = st["status_id"]
    status = await c.get_messages(chat_id, status_id)

    if not rows_slice:
        await status.edit_text("⚠️ Nothing selected.")
        return

    await status.edit_text(
        f"⚙️ Resolving <b>{len(rows_slice)}</b> item(s) in "
        f"<b>safe mode</b> ({EXTRACT_MIN_DELAY:.1f}–{EXTRACT_MAX_DELAY:.1f}s "
        f"per call + breather every {EXTRACT_LONG_PAUSE_EVERY} calls)…",
        parse_mode=ParseMode.HTML,
    )
    api = _make_extract_api(uid, status)
    try:
        entries = await resolve_rows_to_entries(
            api, course_id, rows_slice,
            on_progress=api._progress,  # type: ignore[attr-defined]
        )
    except Exception as exc:
        log.exception("resolve failed")
        await status.edit_text(
            f"❌ Extraction failed: <code>{exc}</code>",
            parse_mode=ParseMode.HTML)
        return
    if not entries:
        await status.edit_text("⚠️ Nothing resolved (all items returned empty).")
        return

    body = entries_to_text(meta, entries)
    ts = int(time.time())
    first, last = entries[0].index, entries[-1].index
    out = DOWNLOAD_DIR / f"yesofficer_{course_id}_{first:03}-{last:03}_{ts}.txt"
    out.write_text(body, encoding="utf-8")

    v = sum(1 for e in entries if e.video_url)
    p = sum((1 if e.pdf_url else 0) + (1 if e.pdf_url2 else 0)
            + (1 if e.study_material_url else 0) for e in entries)
    course_name = (meta.get("course_name") or meta.get("course_title")
                   or f"Course {course_id}")
    await status.edit_text(
        f"✅ Done.  Entries <code>{first}</code>–<code>{last}</code> — "
        f"{v} videos, {p} PDFs.",
        parse_mode=ParseMode.HTML,
    )
    await c.send_document(
        chat_id,
        document=str(out),
        caption=(
            f"📚 <b>{course_name}</b>\n"
            f"Entries <code>{first}</code>–<code>{last}</code> of "
            f"{len(st['rows'])} • {v} videos • {p} PDFs\n\n"
            "Send this back in <b>Uploader</b> mode to download everything."
        ),
        parse_mode=ParseMode.HTML,
    )
    _extract_state.pop(uid, None)


# =============================================================================
# QUIZ MODE  (fetch quizzes -> HTML zip, no cookies required)
# =============================================================================


def _quiz_slice_keyboard(total: int) -> InlineKeyboardMarkup:
    def b(label: str, cb: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(label, callback_data=cb)
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([b(f"✅ All ({total})", "qslice:all")])
    quick: list[InlineKeyboardButton] = []
    for n in (5, 10, 20, 50):
        if n < total:
            quick.append(b(f"Last {n}", f"qslice:last:{n}"))
    for i in range(0, len(quick), 3):
        rows.append(quick[i:i + 3])
    rows.append([b("✏️ Custom range", "qslice:custom"),
                 b("❌ Cancel", "qslice:cancel")])
    return InlineKeyboardMarkup(rows)


async def handle_quiz_extract(c: Client, m: Message, target_id: str,
                              *, kind: str = "course") -> None:
    """Phase 1 (quiz mode): enumerate every quiz in a course or test-series."""
    import quiz_fetcher

    uid = m.from_user.id
    s = user_get(uid)
    # jwt/user_id are OPTIONAL -- only used to overlay user's own answers.
    jwt = s.get("jwt") or ""
    appx_user = s.get("appx_user") or ""
    label = "test series" if kind == "test_series" else "course"
    status = await m.reply_text(
        f"🔎 Scanning {label} <code>{target_id}</code> for quizzes "
        "(no account needed)…",
        parse_mode=ParseMode.HTML,
    )

    http_sess = await http()

    async def _progress(line: str) -> None:
        try:
            await status.edit_text(line, parse_mode=ParseMode.HTML)
        except MessageNotModified:
            pass
        except Exception:
            pass

    try:
        if kind == "test_series":
            quizzes = await quiz_fetcher.list_test_series_quizzes(
                http_sess, jwt, appx_user, target_id,
            )
        else:
            quizzes = await quiz_fetcher.walk_course_for_quizzes(
                http_sess, jwt, appx_user, target_id,
                on_progress=_progress,
                min_delay=EXTRACT_MIN_DELAY,
                max_delay=EXTRACT_MAX_DELAY,
                long_pause_every=EXTRACT_LONG_PAUSE_EVERY,
                long_pause_min=EXTRACT_LONG_PAUSE_MIN,
                long_pause_max=EXTRACT_LONG_PAUSE_MAX,
            )
    except Exception as exc:
        log.exception("quiz scan failed")
        await status.edit_text(
            f"❌ Quiz scan failed: <code>{exc}</code>",
            parse_mode=ParseMode.HTML)
        return

    if not quizzes:
        await status.edit_text(
            f"⚠️ No quizzes found in that {label}.")
        return

    _quiz_state[uid] = {
        "course_id": target_id,
        "kind": kind,
        "quizzes": quizzes,
        "chat_id": m.chat.id,
        "status_id": status.id,
    }

    # Preview: latest 5 (DFS order end = latest in the content page, or
    # last mocks for a test series).
    preview = quizzes[-5:] if len(quizzes) > 5 else quizzes
    preview_lines = "\n".join(
        f"• <code>{q.position:03}</code> 📝 {q.title[:60]}"
        for q in preview
    )
    label_cap = "Test Series" if kind == "test_series" else "Course"
    await status.edit_text(
        f"🧠 <b>{label_cap} {target_id}</b>\n"
        f"Total quizzes: <b>{len(quizzes)}</b>\n\n"
        f"<b>Latest quizzes:</b>\n{preview_lines}\n\n"
        "How many to export as HTML?",
        parse_mode=ParseMode.HTML,
        reply_markup=_quiz_slice_keyboard(len(quizzes)),
    )


async def _handle_quiz_slice_callback(c: Client, q: CallbackQuery) -> None:
    uid = q.from_user.id
    data = q.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) >= 2 else ""
    await q.answer()
    if action == "cancel":
        _quiz_state.pop(uid, None)
        user_put(uid, pending="")
        try:
            await q.message.edit_text("❌ Quiz export cancelled.")
        except MessageNotModified:
            pass
        return
    st = _quiz_state.get(uid)
    if not st:
        try:
            await q.message.edit_text("⚠️ That quiz session expired.")
        except MessageNotModified:
            pass
        return
    quizzes = st["quizzes"]
    picked = []
    if action == "all":
        picked = list(quizzes)
    elif action == "last" and len(parts) >= 3:
        try:
            n = int(parts[2])
        except ValueError:
            n = 0
        if n > 0:
            picked = list(quizzes[-n:])
    elif action == "custom":
        user_put(uid, pending="await_quiz_custom")
        try:
            await q.message.edit_text(
                f"✏️ Send the range.\n"
                f"Total quizzes: <b>{len(quizzes)}</b>.\n"
                "Examples:\n"
                "• <code>15</code>  = last 15 quizzes\n"
                "• <code>10-25</code>  = quizzes 10 through 25\n"
                "• <code>50+</code>  = quiz 50 to the end",
                parse_mode=ParseMode.HTML,
            )
        except MessageNotModified:
            pass
        return
    await _do_quiz_slice(c, uid, picked)


async def _do_quiz_slice(c: Client, uid: int, picked: list[Any]) -> None:
    """Fetch picked quizzes -> render one Neo-theme HTML per quiz ->
    send each one as an individual .html document."""
    import quiz_fetcher
    import quiz_html

    st = _quiz_state.get(uid)
    if not st:
        return
    chat_id = st["chat_id"]
    status = await c.get_messages(chat_id, st["status_id"])

    if not picked:
        await status.edit_text("⚠️ Nothing selected.")
        return

    s = user_get(uid)
    jwt = s.get("jwt") or ""
    appx_user = s.get("appx_user") or ""
    http_sess = await http()

    total = len(picked)
    course_id = st["course_id"]
    ok = 0
    fail = 0
    attempted_count = 0
    last_edit = 0.0

    # Brand pulled from env (BOT_BRAND, BOT_CONTACT); defaults below.
    brand = (os.environ.get("BOT_BRAND") or "YesOfficer Quiz").strip()
    contact = (os.environ.get("BOT_CONTACT") or "").strip()

    for i, ref in enumerate(picked, 1):
        now = time.monotonic()
        if now - last_edit > 2.0 or i == 1 or i == total:
            try:
                await status.edit_text(
                    f"📝 Quiz <code>{i}/{total}</code> — "
                    f"<code>{ref.title[:55]}</code> "
                    f"(fetch + render + inline images…)",
                    parse_mode=ParseMode.HTML,
                )
            except MessageNotModified:
                pass
            except Exception:
                pass
            last_edit = now
        out: Optional[Path] = None
        try:
            data = await quiz_fetcher.fetch_quiz(
                http_sess, jwt, appx_user, ref,
                include_attempt=bool(jwt and appx_user),
            )
            if not data.questions:
                fail += 1
                continue
            html = await quiz_html.render_quiz_offline(
                data, http_sess, brand=brand, contact=contact,
            )
            out = DOWNLOAD_DIR / quiz_fetcher.quiz_filename(ref)
            out.write_text(html, encoding="utf-8")
            caption = (
                f"🧠 <b>{ref.title}</b>\n"
                f"Course <code>{course_id}</code> · "
                f"{quiz_html.quiz_metadata_caption(data)}"
            )
            await c.send_document(
                chat_id,
                document=str(out),
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
            ok += 1
            if data.attempted:
                attempted_count += 1
        except Exception as exc:
            log.warning("quiz %s render/send failed: %s",
                        ref.quiz_title_id, exc)
            fail += 1
        finally:
            if out is not None:
                try:
                    out.unlink()
                except OSError:
                    pass

    if ok == 0:
        await status.edit_text(
            "❌ Failed to fetch any quiz content. "
            "(Course might not expose questions, or all quizzes are empty.)")
    else:
        attempt_bits = (f" · {attempted_count} attempted"
                        if attempted_count else "")
        await status.edit_text(
            f"✅ Done. <b>{ok}</b> quiz(es) sent individually"
            + (f", <b>{fail}</b> skipped" if fail else "")
            + attempt_bits + ".",
            parse_mode=ParseMode.HTML,
        )

    _quiz_state.pop(uid, None)
    user_put(uid, pending="")


# =============================================================================
# BATCH RUNNER  (download + stage + deliver)
# =============================================================================


async def batch_runner(uid: int, c: Client) -> None:
    """Consume every queued .txt for this user, one batch at a time."""
    while True:
        item = queue_pop(uid)
        if not item:
            return
        try:
            await _run_batch(uid, c, item)
        except asyncio.CancelledError:
            log.info("batch cancelled for user %s", uid)
            # Leave remaining txts queued so /status still shows them;
            # user can resume by sending another /start.
            break
        except Exception as exc:
            log.exception("batch crashed")
            try:
                await c.send_message(item["target_chat"],
                                     f"❌ Batch failed: <code>{exc}</code>",
                                     parse_mode=ParseMode.HTML)
            except Exception:
                pass
        finally:
            active_batches.pop(uid, None)
            # Delete the txt after the batch (successful or failed).
            try:
                Path(item["file_path"]).unlink(missing_ok=True)
            except Exception:
                pass


async def _run_batch(uid: int, c: Client, row: dict[str, Any]) -> None:
    txt_path = Path(row["file_path"])
    target = int(row["target_chat"])
    batch_name = row["batch_name"] or txt_path.stem
    extracted_by = row["extracted_by"] or ""
    user_thumb = row["thumb_path"] or ""
    start_entry = int(row["start_entry"] or 1)

    if not txt_path.exists():
        await c.send_message(target, "❌ Queued .txt disappeared.")
        return

    text = txt_path.read_text(encoding="utf-8", errors="replace")
    course, course_id, entries = parse_entries_text(text)
    items = build_item_queue(entries, start=start_entry, course_id=course_id)
    total = len(items)
    if total == 0:
        await c.send_message(target, "⚠️ Nothing to upload (no links found).")
        return

    # Expiry-aware ordering: download URLs with the earliest Expires= first
    # so short-lived signatures go out before they die.  Items with no
    # Expires= (e.g. proxied URLs) come last.
    now = int(time.time())
    for it in items:
        it["expires_at"] = _url_expires_at(it["url"])
    items.sort(key=lambda it: it["expires_at"] or (1 << 31))

    # Pre-flight warning: list any URL already expired (or expiring in <60s).
    dead = [it for it in items
            if it["expires_at"] and it["expires_at"] - now < 60]
    if dead:
        sample = "\n".join(
            f"• <code>{it['entry_index']:03}</code> "
            f"{'🎬' if it['kind']=='video' else '📄'} "
            f"{it['title'][:55]} "
            f"(exp {abs(it['expires_at'] - now)//60}m ago)"
            for it in dead[:10])
        extra = f"\n…and {len(dead)-10} more" if len(dead) > 10 else ""
        await c.send_message(
            target,
            f"⚠️ <b>{len(dead)} URL(s) already expired.</b>  Skipping "
            f"them.  Re-extract those entries on your home machine and "
            f"resend a fresh .txt.\n\n{sample}{extra}",
            parse_mode=ParseMode.HTML,
        )
        # Drop the dead ones so the rest can still upload.
        items = [it for it in items
                 if not (it["expires_at"] and it["expires_at"] - now < 60)]
        total = len(items)
        if total == 0:
            return
        # Re-number the queue position only.  `global_idx` stays fixed so
        # captions keep reflecting the item's original position in the .txt.
        for i, it in enumerate(items, 1):
            it["pos"] = i

    header = await c.send_message(
        target,
        f"📥 <b>{batch_name}</b> — {total} files queued (starting from entry "
        f"<code>{start_entry}</code>).",
        parse_mode=ParseMode.HTML,
    )

    # Shared batch state.
    batch = {
        "name": batch_name,
        "total": total,
        "done": 0,
        "ok": 0,
        "failed": 0,
        "stop": False,
        "task": asyncio.current_task(),
    }
    active_batches[uid] = batch

    download_sem = asyncio.Semaphore(MAX_PARALLEL_DOWNLOADS)
    stage_sem = asyncio.Semaphore(MAX_PARALLEL_UPLOADS)

    # pos -> staged result dict; delivery loop waits for next_pos to appear.
    staged: dict[int, dict[str, Any]] = {}
    next_pos = 1
    last_edit = [0.0]

    async def edit_header(msg: str) -> None:
        if time.monotonic() - last_edit[0] < PROGRESS_INTERVAL:
            return
        last_edit[0] = time.monotonic()
        try:
            await header.edit_text(msg, parse_mode=ParseMode.HTML)
        except (MessageNotModified, Exception):
            pass

    async def process(item: dict[str, Any]) -> None:
        nonlocal next_pos
        pos = item["pos"]
        # global_idx is the item's absolute position in the full .txt
        # (counting every link including those in skipped entries).  We
        # show this in the caption so "Index : NNN" keeps matching the
        # original ordering even when the user started mid-batch.
        caption_idx = item.get("global_idx", pos)
        if batch["stop"]:
            staged[pos] = {"pos": pos, "ok": False, "error": "cancelled"}
            return
        ext = ".mkv" if item["kind"] == "video" else ".pdf"
        # Build the visible title for the uploaded file.  NEITHER videos
        # NOR PDFs get an index-number prefix (user request).  PDF 2 /
        # Notes get a short suffix so they don't clobber the main PDF.
        label = item.get("label") or item["kind"]
        base_title = item["title"] or ""
        if item["kind"] == "video":
            name_in = base_title
        elif label == "pdf2":
            name_in = f"{base_title} (PDF 2)"
        elif label == "notes":
            name_in = f"{base_title} (Notes)"
        else:
            name_in = base_title
        fname = safe_filename(name_in, ext, pos=caption_idx)
        local = DOWNLOAD_DIR / f"batch_{uid}" / fname
        caption = build_caption(
            pos=caption_idx, title=item["title"], topic=item["topic"],
            date=item["date"], duration=item["duration"],
            quality=item["quality"] if item["kind"] == "video" else "",
            batch_name=batch_name, extracted_by=extracted_by,
        )

        async with download_sem:
            wait_for_disk_space()
            if batch["stop"]:
                staged[pos] = {"pos": pos, "ok": False, "error": "cancelled"}
                return
            try:
                h = await http()
                await edit_header(
                    f"📥 <b>{batch_name}</b>\n"
                    f"<code>[{pos}/{total}]</code> ⬇️ "
                    f"{('🎬' if item['kind']=='video' else '📄')} "
                    f"{item['title'][:60]}\n"
                    f"✅ {batch['ok']}  ❌ {batch['failed']}"
                )
                await download_url(h, item["url"], local, kind=item["kind"])
            except Exception as exc:
                log.warning("download failed pos=%s title=%s: %s",
                            pos, item["title"], exc)
                staged[pos] = {"pos": pos, "ok": False, "error": str(exc)}
                # Jittered ban-safe pause on failure.
                await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
                return

        thumb_file: Optional[str] = None
        duration_s: Optional[int] = None
        if item["kind"] == "video":
            loop = asyncio.get_running_loop()
            # Remux MKV → MP4 (lossless, -c copy).  Appx's stream is
            # H.264+AAC which fits MP4 1:1, and desktop Telegram won't
            # play .mkv reliably.  Falls back to .mkv if ffmpeg can't
            # remux for any reason.  Can be disabled via REMUX_MP4=false.
            if REMUX_MP4:
                local = await loop.run_in_executor(
                    None, remux_mkv_to_mp4, local)
            duration_s = await loop.run_in_executor(None, ffprobe_duration, local)
            thumb_file = await loop.run_in_executor(
                None, resolve_thumb, local, user_thumb, DEFAULT_THUMB)
        else:
            # PDFs: reuse the user's custom thumb from Step 3/4 (and the
            # DEFAULT_THUMB env var as a fall-back).  We can't extract a
            # frame from a PDF, so there's nothing to do if neither is
            # set.  Same prepare_tg_thumb pipeline as video so PDFs
            # ship the sharpest possible 320x320 JPEG.
            if user_thumb and Path(user_thumb).exists():
                thumb_file = prepare_tg_thumb(user_thumb)
            elif DEFAULT_THUMB and Path(DEFAULT_THUMB).exists():
                thumb_file = prepare_tg_thumb(DEFAULT_THUMB)

        async with stage_sem:
            result: dict[str, Any] = {
                "pos": pos, "kind": item["kind"], "title": item["title"],
                "topic": item["topic"], "caption": caption,
                "filename": local.name, "ok": False,
                "duration": duration_s,
                "local_path": str(local),
                # `local_thumb` is treated as disposable (deleted after
                # delivery).  Only the auto-extracted video frame lands
                # here; user-provided thumbs go into `persistent_thumb`
                # so they survive the batch and can be reused for every
                # file.
                "local_thumb": thumb_file if (thumb_file and str(thumb_file).endswith(
                    "_thumb.jpg")) else None,
                "persistent_thumb": thumb_file if (thumb_file and not str(thumb_file).endswith(
                    "_thumb.jpg")) else None,
            }
            if LOG_CHANNEL:
                # Stage to log channel; grab file_id so we can delete local.
                try:
                    if item["kind"] == "video":
                        staged_msg = await c.send_video(
                            LOG_CHANNEL,
                            video=str(local),
                            caption=f"[STAGED {pos}/{total}] {item['title'][:60]}",
                            thumb=thumb_file,
                            duration=duration_s or 0,
                            file_name=local.name,
                            supports_streaming=True,
                        )
                        result["file_id"] = staged_msg.video.file_id
                        if staged_msg.video.thumbs:
                            result["thumb_file_id"] = staged_msg.video.thumbs[0].file_id
                    else:
                        staged_msg = await c.send_document(
                            LOG_CHANNEL,
                            document=str(local),
                            caption=f"[STAGED {pos}/{total}] {item['title'][:60]}",
                            thumb=thumb_file,
                            file_name=local.name,
                        )
                        result["file_id"] = staged_msg.document.file_id
                    result["ok"] = True
                    # Local file + extracted thumb no longer needed.
                    try:
                        local.unlink(missing_ok=True)
                    except Exception:
                        pass
                    result["local_path"] = ""
                    if result.get("local_thumb"):
                        try:
                            Path(result["local_thumb"]).unlink(missing_ok=True)
                        except Exception:
                            pass
                        result["local_thumb"] = None
                except FloodWait as fw:
                    log.warning("FloodWait staging pos=%s: %s s", pos, fw.value)
                    await asyncio.sleep(int(fw.value) + 1)
                    # Fall through -- we'll still deliver via local path below.
                    result["ok"] = True
                except Exception as exc:
                    log.exception("staging failed pos=%s", pos)
                    result["ok"] = True  # still deliverable from local_path
                    result["stage_error"] = str(exc)
            else:
                # No log channel -> deliver directly from disk.
                result["ok"] = True
            staged[pos] = result

    async def deliver() -> None:
        nonlocal next_pos
        while next_pos <= total:
            if batch["stop"] and next_pos not in staged:
                break
            if next_pos not in staged:
                await asyncio.sleep(0.3)
                continue
            r = staged.pop(next_pos)
            pos = r["pos"]
            if not r.get("ok"):
                batch["failed"] += 1
                batch["done"] += 1
                try:
                    await c.send_message(
                        target,
                        f"❌ <code>[{pos}/{total}]</code> failed: "
                        f"{r.get('error', 'unknown')[:120]}",
                        parse_mode=ParseMode.HTML)
                except Exception:
                    pass
                next_pos += 1
                continue
            try:
                file_id = r.get("file_id")
                local = r.get("local_path")
                caption = r["caption"]
                filename = r["filename"]
                duration = r.get("duration") or 0
                thumb_file_id = r.get("thumb_file_id")
                local_thumb = r.get("local_thumb")
                persistent_thumb = r.get("persistent_thumb")
                # Preference order when sending from a local path:
                # a Telegram file_id for the thumb (fast path), then the
                # auto-extracted frame, then the user's custom thumb.
                effective_thumb = thumb_file_id or local_thumb or persistent_thumb
                while True:
                    try:
                        if r["kind"] == "video":
                            await c.send_video(
                                target,
                                video=file_id or local,
                                caption=caption,
                                parse_mode=ParseMode.HTML,
                                thumb=effective_thumb if not file_id else None,
                                duration=duration,
                                file_name=filename,
                                supports_streaming=True,
                            )
                        else:
                            await c.send_document(
                                target,
                                document=file_id or local,
                                caption=caption,
                                parse_mode=ParseMode.HTML,
                                thumb=effective_thumb if not file_id else None,
                                file_name=filename,
                            )
                        break
                    except FloodWait as fw:
                        log.warning("FloodWait deliver pos=%s: %s", pos, fw.value)
                        await asyncio.sleep(int(fw.value) + 1)
                batch["ok"] += 1
            except Exception as exc:
                log.exception("delivery failed pos=%s", pos)
                batch["failed"] += 1
                try:
                    await c.send_message(
                        target,
                        f"❌ <code>[{pos}/{total}]</code> delivery: {exc}",
                        parse_mode=ParseMode.HTML)
                except Exception:
                    pass
            finally:
                batch["done"] += 1
                # Delete local artefacts if still on disk.
                if r.get("local_path"):
                    try:
                        Path(r["local_path"]).unlink(missing_ok=True)
                    except Exception:
                        pass
                if r.get("local_thumb"):
                    try:
                        Path(r["local_thumb"]).unlink(missing_ok=True)
                    except Exception:
                        pass
                next_pos += 1
                # Ban-safe pacing between deliveries.
                await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    # Launch download/staging for every item + the delivery loop.
    tasks = [asyncio.create_task(process(it)) for it in items]
    delivery_task = asyncio.create_task(deliver())
    try:
        await asyncio.gather(*tasks)
        await delivery_task
    except asyncio.CancelledError:
        batch["stop"] = True
        for t in tasks:
            t.cancel()
        delivery_task.cancel()
        raise

    summary = (
        f"✅ <b>{batch_name}</b> done.\n"
        f"Success <code>{batch['ok']}</code> / Failed "
        f"<code>{batch['failed']}</code> / Total <code>{total}</code>."
    )
    try:
        await c.send_message(target, summary, parse_mode=ParseMode.HTML)
    except Exception:
        pass

    # Clean up per-batch folder if empty.
    try:
        shutil.rmtree(DOWNLOAD_DIR / f"batch_{uid}", ignore_errors=True)
    except Exception:
        pass


# =============================================================================
# ENTRY POINT
# =============================================================================


async def on_shutdown() -> None:
    global _http
    if _http and not _http.closed:
        await _http.close()


def main() -> None:
    log.info("Starting bot (downloads in %s; LOG_CHANNEL=%s)",
             DOWNLOAD_DIR, LOG_CHANNEL or "disabled")
    try:
        app.run()
    finally:
        try:
            asyncio.get_event_loop().run_until_complete(on_shutdown())
        except Exception:
            pass


if __name__ == "__main__":
    main()
