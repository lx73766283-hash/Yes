"""
YesOfficer Quiz Fetcher
=======================

Given a yesofficer course id + the user's JWT, walks the course content
tree, locates every TEST/QUIZ entry and fetches its full question bank
(question, options, correct answer, solution).

TEST entries on yesofficer are backed by the `thetestpassapi` service
(a shared Appx test-series backend).  The flow for each quiz is:

    folder_contentsv3       -- list course entries, find `material_type=TEST`
                               with a `quiz_title_id`.
    test_title_by_id        -- on thetestpassapi.akamai.net.in; returns
                               metadata including `test_questions_url`
                               (a direct JSON on appxcontent.securevideo.in).
    <test_questions_url>    -- the actual question bank (unattempted quiz
                               still includes correct answers + solutions).
    test_attempt_with_urls  -- on thetestpassapi; if the user attempted the
                               quiz, gives back `test_attempt.answer_url`
                               (a JSON with the user's picks, merged on top
                               of the question bank to produce an
                               "attempted" render).

Same flow for unattempted vs attempted -- attempted just overlays the
user's picks via answer_url.  Both paths yield Q/options/answer/solution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import aiohttp


log = logging.getLogger("quiz-fetcher")


# --- Endpoints ---------------------------------------------------------------

YESOFFICER_API = "https://yesofficerapi.cloudflare.net.in/"
TESTPASS_API = "https://thetestpassapi.akamai.net.in/"
FRONTEND = "https://www.yesofficer.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# --- Data classes ------------------------------------------------------------


@dataclass
class QuizRef:
    """Lightweight pointer to a quiz discovered in the course tree."""

    # Position in the course DFS walk; used for ordering.
    position: int
    # Material-type entry id (e.g. 24970) from folder_contentsv3.
    content_id: str
    # quiz_title_id -- the id we pass to test_title_by_id.
    quiz_title_id: str
    title: str
    folder_path: list[str] = field(default_factory=list)
    # Thumbnail url from folder_contentsv3 (optional).
    thumbnail: str = ""
    # Raw record in case the HTML generator needs more fields.
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class QuizData:
    """A fully-resolved quiz: metadata + questions + optional user answers."""

    ref: QuizRef
    meta: dict[str, Any]  # from test_title_by_id
    questions: list[dict[str, Any]]  # raw question records from test_questions_url
    user_answers: Optional[dict[str, Any]] = None  # keyed by question id -> answer
    attempted: bool = False


# --- Low-level fetchers ------------------------------------------------------


def build_headers(jwt: str, user_id: str) -> dict[str, str]:
    return {
        "Authorization": jwt,
        "User-ID": user_id,
        "Client-Service": "Appx",
        "Auth-Key": "appxapi",
        "Source": "website",
        "Origin": FRONTEND,
        "Referer": FRONTEND + "/",
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
    }


class _Pacer:
    """Small jittered rate-limiter, shared across the fetcher."""

    def __init__(self, min_delay: float, max_delay: float,
                 long_every: int = 0, long_min: float = 0.0,
                 long_max: float = 0.0) -> None:
        self.min = min_delay
        self.max = max_delay
        self._last = 0.0
        self._calls = 0
        self._long_every = long_every
        self._long_min = long_min
        self._long_max = long_max
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        long_pause = 0.0
        async with self._lock:
            self._calls += 1
            if (self._long_every and self._calls > 1
                    and self._calls % self._long_every == 0
                    and self._long_max > 0):
                long_pause = random.uniform(self._long_min, self._long_max)
            delay = random.uniform(self.min, self.max)
            now = time.monotonic()
            wait = (self._last + delay) - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
        if long_pause > 0:
            await asyncio.sleep(long_pause)
            async with self._lock:
                self._last = time.monotonic()


async def _get_json(http: aiohttp.ClientSession, url: str, *,
                    headers: Optional[dict[str, str]] = None,
                    params: Optional[dict[str, Any]] = None,
                    strip_auth: bool = False,
                    tries: int = 4) -> Any:
    """GET with retries; returns parsed JSON or None on 4xx (non-200)."""
    backoff = 1.5
    for attempt in range(tries):
        try:
            h = dict(headers or {})
            if strip_auth:
                for k in ("Authorization", "User-ID", "Client-Service",
                          "Auth-Key", "Source"):
                    h.pop(k, None)
            async with http.get(url, headers=h, params=params,
                                timeout=aiohttp.ClientTimeout(total=30)) as r:
                status = r.status
                ctype = r.headers.get("Content-Type", "")
                if status == 429 or 500 <= status < 600:
                    log.warning("GET %s -> %s, backoff", url, status)
                    await asyncio.sleep(backoff + random.random())
                    backoff *= 2
                    continue
                text = await r.text()
                if status >= 400:
                    # Some Appx endpoints return `status:400, message:...` with 200 HTTP;
                    # but HTTP 400 usually means actual error — still try to parse.
                    try:
                        return json.loads(text)
                    except Exception:
                        return None
                if not text:
                    return None
                if "json" in ctype.lower() or text.lstrip().startswith(("[", "{")):
                    return json.loads(text)
                return text
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("GET %s attempt %d failed: %s", url, attempt + 1, exc)
            await asyncio.sleep(backoff + random.random())
            backoff *= 2
    raise RuntimeError(f"GET {url} failed after {tries} attempts")


# --- Course tree walk (find TEST entries) ------------------------------------


async def _list_folder(http: aiohttp.ClientSession, headers: dict[str, str],
                       pacer: _Pacer, course_id: str,
                       parent_id: str) -> list[dict[str, Any]]:
    """Page through folder_contentsv3 for one folder until empty."""
    out: list[dict[str, Any]] = []
    start = 0
    while True:
        await pacer.wait()
        j = await _get_json(
            http,
            YESOFFICER_API + "get/folder_contentsv3",
            headers=headers,
            params={"course_id": course_id, "parent_id": str(parent_id),
                    "start": str(start)},
        )
        if not isinstance(j, dict):
            break
        data = j.get("data") or []
        if not data:
            break
        out.extend(data)
        start += len(data)
        # Hard cap against runaway pagination.
        if start > 5000:
            break
    return out


async def list_test_series_quizzes(
    http: aiohttp.ClientSession,
    jwt: str,
    user_id: str,
    test_series_id: str,
) -> list[QuizRef]:
    """List every mock in a yesofficer test-series (URL /test-series/<id>-<slug>).

    Uses `/get/test_titlev2` on thetestpassapi with the user's tenant
    (client_api_url=yesofficer).  Works without any login (userid=0 ok).
    """
    headers = build_headers(jwt or "", user_id or "")
    params = {
        "testseriesid": test_series_id,
        "subject_id": "-1",
        "userid": user_id or "0",
        "search": "",
        "client_api_url": YESOFFICER_API,
        "start": "-1",
    }
    j = await _get_json(
        http,
        TESTPASS_API + "get/test_titlev2",
        headers=headers,
        params=params,
    )
    out: list[QuizRef] = []
    if not isinstance(j, dict):
        return out
    titles = j.get("test_titles") or j.get("data") or []
    if not isinstance(titles, list):
        return out
    for i, t in enumerate(titles, 1):
        qti = str(t.get("id") or "").strip()
        if not qti or qti == "-1":
            continue
        out.append(QuizRef(
            position=i,
            content_id=qti,
            quiz_title_id=qti,
            title=(t.get("title") or f"Mock {i}").strip(),
            folder_path=[f"Test Series {test_series_id}"],
            thumbnail="",
            raw=t,
        ))
    return out


async def walk_course_for_quizzes(
    http: aiohttp.ClientSession,
    jwt: str,
    user_id: str,
    course_id: str,
    *,
    on_progress: Optional[Callable[[str], Awaitable[None]]] = None,
    min_delay: float = 1.0,
    max_delay: float = 2.0,
    long_pause_every: int = 40,
    long_pause_min: float = 15.0,
    long_pause_max: float = 35.0,
    max_depth: int = 8,
) -> list[QuizRef]:
    """Walk course tree, return every TEST entry (as QuizRef) in DFS order."""
    headers = build_headers(jwt, user_id)
    pacer = _Pacer(min_delay, max_delay, long_pause_every,
                   long_pause_min, long_pause_max)
    quizzes: list[QuizRef] = []
    seen_folders: set[str] = set()

    async def descend(parent: str, depth: int, folder_path: list[str]) -> None:
        if depth > max_depth or parent in seen_folders:
            return
        seen_folders.add(parent)
        if on_progress:
            await on_progress(
                f"🔍 Scanning {' › '.join(folder_path) or 'root'} "
                f"({len(quizzes)} quizzes found)…"
            )
        items = await _list_folder(http, headers, pacer, course_id, parent)
        for it in items:
            mt = (it.get("material_type") or "").upper()
            title = (it.get("Title") or it.get("title") or "").strip()
            if mt == "FOLDER":
                sub = it.get("id") or ""
                if sub:
                    await descend(str(sub), depth + 1, folder_path + [title])
                continue
            # Accept TEST (OMR-style) and the rare QUIZ material_type
            # via quiz_title_id.
            qti = (it.get("quiz_title_id") or "").strip() or ""
            if mt in ("TEST", "QUIZ") and qti and qti != "-1" and int(qti or "0") > 0:
                quizzes.append(QuizRef(
                    position=len(quizzes) + 1,
                    content_id=str(it.get("id") or ""),
                    quiz_title_id=qti,
                    title=title or f"Quiz {qti}",
                    folder_path=list(folder_path),
                    thumbnail=it.get("thumbnail") or "",
                    raw=it,
                ))
                continue
            # Many items (VIDEO, LINK, PDF, etc.) carry an embedded quiz
            # via test_title_id ("Attempt Test" button on the website).
            # These use the same test_title_by_id → test_questions_url
            # pipeline as quiz_title_id entries.
            tti = (it.get("test_title_id") or "").strip() or ""
            if tti and tti != "-1" and int(tti or "0") > 0:
                quizzes.append(QuizRef(
                    position=len(quizzes) + 1,
                    content_id=str(it.get("id") or ""),
                    quiz_title_id=tti,
                    title=title or f"Quiz {tti}",
                    folder_path=list(folder_path),
                    thumbnail=it.get("thumbnail") or "",
                    raw=it,
                ))

    # Start from the virtual root (parent_id=-1). descend() lists root-level
    # items and recurses into every FOLDER it finds, while also picking up
    # any TEST/QUIZ entries or items with a test_title_id at every level.
    await descend("-1", 0, [])
    return quizzes


# --- Fetch one quiz's question bank -----------------------------------------


async def fetch_quiz(
    http: aiohttp.ClientSession,
    jwt: str,
    user_id: str,
    ref: QuizRef,
    *,
    include_attempt: bool = True,
) -> QuizData:
    """Fetch the metadata, question bank, and optional user-attempt for a quiz.

    Primary source is yesofficer's own ``test_title_by_id`` endpoint
    (yesofficerapi.cloudflare.net.in) -- that's what the website actually
    uses and it returns the *tenant-specific* title / time / question bank
    (e.g. "Test 1: Coding Decoding", 7-10 min, 15 questions).

    The shared testpass backend (thetestpassapi.akamai.net.in) is kept as a
    fallback only -- it stores the same id under a different (often stale,
    e.g. "Mock Test 2") record, so we only consult it when the yesofficer
    one yields no data.
    """
    headers = build_headers(jwt, user_id)
    yo_params = {
        "id": ref.quiz_title_id,
        "userid": user_id or "0",
    }
    meta: dict[str, Any] = {}
    try:
        j = await _get_json(
            http,
            YESOFFICER_API + "get/test_title_by_id",
            headers=headers,
            params=yo_params,
        )
        if isinstance(j, dict):
            d = j.get("data")
            if isinstance(d, dict) and d:
                meta = d
            elif isinstance(d, list) and d and isinstance(d[0], dict):
                meta = d[0]
    except Exception as exc:
        log.warning("yesofficer test_title_by_id failed for qti=%s: %s",
                    ref.quiz_title_id, exc)

    # Fallback to the shared testpass backend only when yesofficer has
    # nothing to say about this id (rare for course-tree quizzes; common
    # for some test-series-only ids).
    if not meta:
        try:
            j = await _get_json(
                http,
                TESTPASS_API + "get/test_title_by_id",
                headers=headers,
                params={
                    "id": ref.quiz_title_id,
                    "userid": user_id,
                    "client_api_url": YESOFFICER_API,
                },
            )
            if isinstance(j, dict):
                d = j.get("data")
                if isinstance(d, dict) and d:
                    meta = d
        except Exception as exc:
            log.warning("testpass fallback failed for qti=%s: %s",
                        ref.quiz_title_id, exc)
    if not meta:
        return QuizData(ref=ref, meta={}, questions=[], attempted=False)

    q_url = meta.get("test_questions_url") or ""
    q_url_2 = meta.get("test_questions_url_2") or ""
    questions: list[dict[str, Any]] = []
    if q_url:
        try:
            raw = await _get_json(http, q_url, strip_auth=True)
            if isinstance(raw, list):
                questions = raw
            elif isinstance(raw, dict) and isinstance(raw.get("data"), list):
                questions = raw["data"]
        except Exception as exc:
            log.warning("questions fetch failed for qti=%s: %s",
                        ref.quiz_title_id, exc)

    # Optional second-language (typically Hindi) question bank.  When
    # present and distinct from q_url, fetch and merge by question id so
    # each question record gains `*_hindi` sibling fields that the HTML
    # generator can render behind a language toggle.
    if q_url_2 and q_url_2 != q_url and questions:
        try:
            raw2 = await _get_json(http, q_url_2, strip_auth=True)
            q2_list: list[dict[str, Any]] = []
            if isinstance(raw2, list):
                q2_list = raw2
            elif isinstance(raw2, dict) and isinstance(raw2.get("data"), list):
                q2_list = raw2["data"]
            if q2_list:
                by_id = {str(q.get("id")): q for q in q2_list if q.get("id")}
                # Fall back to positional merge when ids don't line up.
                positional = len(q2_list) == len(questions)
                hi_fields = (
                    "question", "question_heading", "directive",
                    "option_1", "option_2", "option_3", "option_4",
                    "option_5", "option_6", "option_7", "option_8",
                    "option_9", "option_10",
                    "solution_text", "solution_heading",
                )
                for i, q in enumerate(questions):
                    alt = by_id.get(str(q.get("id"))) or (
                        q2_list[i] if positional else None
                    )
                    if not alt:
                        continue
                    for fld in hi_fields:
                        val = alt.get(fld)
                        if val and val != q.get(fld):
                            q[f"{fld}_hindi"] = val
        except Exception as exc:
            log.warning("hindi questions fetch failed for qti=%s: %s",
                        ref.quiz_title_id, exc)

    user_answers: Optional[dict[str, Any]] = None
    attempted = False
    if include_attempt:
        try:
            attempt = await _get_json(
                http,
                TESTPASS_API + "test_omr/test_attempt_with_urls",
                headers=headers,
                params={"test_id": ref.quiz_title_id, "user_id": user_id,
                        "client_api_url": YESOFFICER_API},
            )
            if isinstance(attempt, dict):
                adata = attempt.get("data")
                if isinstance(adata, dict):
                    ta = adata.get("test_attempt") or {}
                    ans_url = ta.get("answer_url") or ""
                    if ans_url:
                        attempted = True
                        ans_raw = await _get_json(http, ans_url, strip_auth=True)
                        if isinstance(ans_raw, dict):
                            user_answers = ans_raw
                        elif isinstance(ans_raw, list):
                            # Some answer_urls return an array -- index by question id.
                            user_answers = {}
                            for row in ans_raw:
                                qid = str(row.get("question_id")
                                          or row.get("qid") or row.get("id") or "")
                                if qid:
                                    user_answers[qid] = row
        except Exception as exc:
            log.info("no attempt data for qti=%s (%s)", ref.quiz_title_id, exc)

    return QuizData(
        ref=ref,
        meta=meta,
        questions=questions,
        user_answers=user_answers,
        attempted=attempted,
    )


# --- Helpers: slugify / filenames --------------------------------------------


# --- URL parsing -------------------------------------------------------------


def parse_quiz_target(text: str) -> tuple[str, str]:
    """Parse a user-supplied URL/id and return (kind, id).

    kind is 'course' (for /new-courses/<id>/...) or 'test_series' (for
    /test-series/<id>-<slug>).  Bare numeric ids default to 'course'
    for backwards compat.
    """
    s = (text or "").strip()
    if not s:
        return ("", "")
    # /test-series/<id>-<slug> or /test-series/<id>
    m = re.search(r"/test-series/(\d+)", s)
    if m:
        return ("test_series", m.group(1))
    m = re.search(r"/new-courses?/(\d+)", s)
    if m:
        return ("course", m.group(1))
    if s.isdigit():
        return ("course", s)
    m = re.search(r"(\d+)", s)
    if m:
        return ("course", m.group(1))
    return ("", "")


_SLUG_BAD = re.compile(r"[^A-Za-z0-9._-]+")


def quiz_filename(ref: QuizRef, ext: str = "html",
                  index: Optional[int] = None) -> str:
    """Safe on-disk filename for a quiz HTML file.

    Uses the course-tree quiz title only — no numeric id suffix, to match
    what the user sees in the yesofficer course list.  Index (if given)
    prefixes the name to keep on-disk ordering stable when dumping a
    whole course.
    """
    title = (ref.title or f"quiz_{ref.quiz_title_id}").strip()
    slug = _SLUG_BAD.sub("_", title).strip("._-") or f"quiz_{ref.quiz_title_id}"
    slug = slug[:120]
    prefix = f"{index:03d}_" if index is not None else ""
    return f"{prefix}{slug}.{ext}"
