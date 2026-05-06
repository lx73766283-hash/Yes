"""
YesOfficer Quiz HTML Generator (Neo theme)
==========================================

Wraps the IBPS TCS iON-style *neo* mock-test generator (quiz_html_neo.py)
and adapts YesOfficer / TestPass quiz JSON into the shape that generator
expects.  The resulting HTML is:

    * 100% self-contained (all CSS/JS inline, no CDN calls)
    * fully-offline capable — images can be inlined as base64 data URIs
      via `inline_images_into_html()` (async, optional)
    * shows mock-style metadata (duration / questions / marks /
      negative-marking) and a realistic test interface with timer,
      palette, language toggle and inline solutions

Public API
----------
    build_test_data(quiz_data)       -- QuizData -> neo input dict
    render_quiz(quiz_data, ...)       -- render to HTML string (images
                                         stay as remote URLs)
    render_quiz_offline(quiz, ...)    -- async; inlines every reachable
                                         image as base64 so the HTML
                                         works without any network
    quiz_metadata_caption(quiz_data)  -- short Telegram caption string

Nothing here talks to the network unless you call `render_quiz_offline`
(or the image inliner directly).
"""

from __future__ import annotations

import asyncio
import base64
import html as _html
import logging
import mimetypes
import re
from typing import Any, Iterable, Optional

import aiohttp

import quiz_html_neo
from quiz_fetcher import QuizData


log = logging.getLogger("quiz-html")


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------

# Appx CMS wraps every field with `<style>img{max-width:100%}</style>`
# tags that leak into the HTML stream and fight our own CSS.  Strip them.
_STYLE_TAG = re.compile(r"<style[^>]*>.*?</style>", re.I | re.S)
# Stray `<?xml ... ?>` declarations that break browsers in HTML mode.
_XML_DECL = re.compile(r"<\?xml[^>]*\?>", re.I)
# Pixel-width + pixel-height attributes that make CKEditor-embedded
# images spill out of the option cell.  We drop the hard sizes and let
# our CSS rule (`max-width:100%`) handle it.
_FIXED_SIZE = re.compile(r'\s(width|height)="\d+"', re.I)


def _clean_html(s: str) -> str:
    if not s:
        return ""
    s = _STYLE_TAG.sub("", s)
    s = _XML_DECL.sub("", s)
    s = _FIXED_SIZE.sub("", s)
    return s.strip()


# ---------------------------------------------------------------------------
# YesOfficer question JSON -> neo test_data
# ---------------------------------------------------------------------------


_CORRECT_TOKENS = re.compile(r"[,\s|;/]+")


def _parse_correct(answer: Any) -> int:
    """Return a 0-based correct-option index (first token wins).

    yesofficer's `answer` is typically a single-digit string (1-10).
    Multi-correct quizzes are rare in the source data — we just keep
    the first correct option for now (neo theme is single-choice).
    """
    if answer is None:
        return -1
    tokens = [t for t in _CORRECT_TOKENS.split(str(answer).strip()) if t]
    for t in tokens:
        if t.isdigit():
            v = int(t)
            if 1 <= v <= 10:
                return v - 1
    return -1


def _option_html(q: dict[str, Any], idx: int, *, lang: str = "en") -> str:
    """Return the combined (text + image) HTML for option `idx` (1-based).

    ``lang="hi"`` pulls the Hindi sibling field (`option_{idx}_hindi`) when
    present; image URLs are shared across languages.
    """
    if lang == "hi":
        text_raw = q.get(f"option_{idx}_hindi") or ""
    else:
        text_raw = q.get(f"option_{idx}") or ""
    text = _clean_html(text_raw)
    img = (q.get(f"option_image_{idx}") or "").strip()
    parts = []
    if text:
        parts.append(text)
    if img:
        parts.append(
            f'<div class="opt-img-wrap" style="margin-top:6px">'
            f'<img src="{_html.escape(img, quote=True)}" '
            f'alt="option {idx}" loading="lazy"></div>'
        )
    return "".join(parts)


_PASSAGE_MIN_LEN = 240  # chars; above this the block is treated as a passage


def _question_html(q: dict[str, Any], *, lang: str = "en") -> str:
    """Assemble directions + heading + question body + any extra images.

    ``lang="hi"`` prefers the Hindi sibling fields (``*_hindi``) and falls
    back to the default English value when Hindi is missing for a field.
    Images are shared across languages.
    """
    def _pick(field: str) -> str:
        if lang == "hi":
            v = q.get(f"{field}_hindi") or q.get(field) or ""
        else:
            v = q.get(field) or ""
        return _clean_html(v)

    heading = _pick("question_heading")
    directive = _pick("directive")
    stem = _pick("question")
    imgs = []
    for i in range(1, 4):
        url = (q.get(f"image_link_{i}") or "").strip()
        if url:
            imgs.append(
                f'<div style="margin:8px 0"><img '
                f'src="{_html.escape(url, quote=True)}" '
                f'alt="figure {i}" loading="lazy"></div>'
            )
    blocks = []
    # Long directives/headings are passage-style ("Directions: ...") — give
    # them a scrollable panel so they don't push the options off-screen.
    def _wrap(label: str, content: str) -> str:
        if not content:
            return ""
        is_passage = len(_html.unescape(re.sub(r"<[^>]+>", "", content))) > _PASSAGE_MIN_LEN
        if is_passage:
            return (
                f'<div class="q-passage q-{label}" '
                f'style="background:#f0f5ff;border-left:4px solid #1a3a5c;'
                f'padding:12px 14px;margin:0 0 12px;border-radius:0 6px 6px 0;'
                f'max-height:240px;overflow-y:auto;line-height:1.7;'
                f'font-size:13px">{content}</div>'
            )
        if label == "heading":
            return (
                f'<div class="q-heading" style="font-weight:600;'
                f'margin-bottom:6px">{content}</div>'
            )
        if label == "directive":
            return (
                f'<div class="q-directive" style="font-style:italic;'
                f'color:#555;margin-bottom:6px">{content}</div>'
            )
        return f'<div class="q-{label}">{content}</div>'

    # Order: directions (long passage) → heading → stem.
    if directive:
        blocks.append(_wrap("directive", directive))
    if heading and heading != directive:
        blocks.append(_wrap("heading", heading))
    if stem:
        blocks.append(f'<div class="q-stem">{stem}</div>')
    if imgs:
        blocks.append("".join(imgs))
    return "".join(blocks) or "<div>—</div>"


def _solution_html(q: dict[str, Any]) -> str:
    heading = _clean_html(q.get("solution_heading") or "")
    body = _clean_html(q.get("solution_text") or "")
    imgs = []
    for i in range(1, 3):
        url = (q.get(f"solution_image_{i}") or "").strip()
        if url:
            imgs.append(
                f'<div style="margin:8px 0"><img '
                f'src="{_html.escape(url, quote=True)}" '
                f'alt="solution fig {i}" loading="lazy"></div>'
            )
    video = (q.get("solution_video") or "").strip()
    video_html = ""
    if video:
        video_html = (
            f'<div style="margin-top:8px"><a class="sol-video" '
            f'href="{_html.escape(video, quote=True)}" target="_blank" '
            f'rel="noopener">&#127909; Video solution</a></div>'
        )
    parts = []
    if heading:
        parts.append(
            f'<div class="sol-heading" style="font-weight:600;'
            f'margin-bottom:6px">{heading}</div>'
        )
    if body:
        parts.append(f'<div class="sol-body-text">{body}</div>')
    if imgs:
        parts.append("".join(imgs))
    if video_html:
        parts.append(video_html)
    return "".join(parts)


def _section_key(q: dict[str, Any]) -> str:
    """Stable grouping key for a question's section.

    We group by numeric `section_id` (what the Appx backend indexes on),
    NOT by the human-readable label — different sections can legitimately
    share a subject/topic string, and the frontend's sectional logic is
    keyed on section_id.
    """
    sid = str(q.get("section_id") or "").strip()
    if sid and sid != "0":
        return sid
    # Fallback: group by subject/topic when section_id is missing.
    for key in ("subject", "topic", "concept"):
        val = str(q.get(key) or "").strip()
        if val and val != "0":
            return f"name:{val}"
    return "full"


def _section_name(q: dict[str, Any], meta: dict[str, Any]) -> str:
    """Pick a sensible section label for a question."""
    for key in ("subject", "topic", "concept"):
        val = str(q.get(key) or "").strip()
        if val and val != "0":
            return val
    sid = str(q.get("section_id") or "").strip()
    if sid and sid != "0":
        return f"Section {sid}"
    return "Full Test"


def _parse_sectional_times(time_raw: Any) -> tuple[list[int], bool]:
    """Parse the `time` field the way yesofficer's frontend does.

    The meta `time` field is either a plain integer ("60" = 60-min single
    timer) or a delimited list ("20,20,20" / "20+20+20") where each value
    is per-section minutes.  `+` additionally means sections are locked
    (can't revisit once the section timer runs out / you hit Next).

    Returns (minutes_per_section_list, sections_restricted).  For a plain
    single-timer quiz the list has exactly one entry.
    """
    s = str(time_raw or "").strip()
    if not s:
        return [], False
    restricted = "+" in s
    parts = [p.strip() for p in re.split(r"[,+]", s) if p.strip()]
    mins: list[int] = []
    for p in parts:
        try:
            mins.append(int(float(p)))
        except ValueError:
            continue
    return mins, restricted


def _float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build_test_data(quiz: QuizData) -> dict[str, Any]:
    """Translate a QuizData into the dict shape the neo generator expects."""
    meta = quiz.meta or {}
    q_src = quiz.questions or []

    # ── per-question positive/negative marks ──────────────────────────
    pos_vals = [
        _float(q.get("positive_marks"), 0.0)
        for q in q_src
        if q.get("positive_marks") not in (None, "")
    ]
    neg_vals = [
        _float(q.get("negative_marks"), 0.0)
        for q in q_src
        if q.get("negative_marks") not in (None, "")
    ]
    mark_correct = pos_vals[0] if pos_vals else 1.0
    # `negative_marks` is stored as a positive number that represents
    # how much gets deducted (e.g. "0.25").  Present as a negative.
    mark_wrong = -abs(neg_vals[0]) if neg_vals else 0.0
    # Marks per section: if uniform, we reuse mark_correct; otherwise
    # pick per-section majority later (simple average is fine here).

    # ── questions list ────────────────────────────────────────────────
    # We carry BOTH the numeric section_id (for grouping / matching
    # against the per-section time list) and the display name.
    neo_questions: list[dict[str, Any]] = []
    sec_keys_order: list[str] = []
    sec_names: dict[str, str] = {}
    sec_counts: dict[str, int] = {}
    sec_scores: dict[str, float] = {}
    for i, q in enumerate(q_src, 1):
        key = _section_key(q)
        name = _section_name(q, meta)
        if key not in sec_names:
            sec_names[key] = name
            sec_keys_order.append(key)
        sec_counts[key] = sec_counts.get(key, 0) + 1
        sec_scores[key] = sec_scores.get(key, 0.0) + _float(
            q.get("positive_marks"), mark_correct
        )
        # Did any text field (question/heading/directive/option/solution)
        # have a distinct Hindi sibling merged in by the fetcher?
        q_has_hindi = any(
            (q.get(f"{f}_hindi") or "").strip() and
            (q.get(f"{f}_hindi") or "").strip() != (q.get(f) or "").strip()
            for f in ("question", "question_heading", "directive",
                      "solution_text", "solution_heading",
                      "option_1", "option_2", "option_3", "option_4",
                      "option_5", "option_6", "option_7", "option_8",
                      "option_9", "option_10")
        )
        opts: list[dict[str, Any]] = []
        for j in range(1, 11):
            html_en = _option_html(q, j, lang="en")
            html_hi = _option_html(q, j, lang="hi") if q_has_hindi else ""
            if not html_en and not html_hi:
                continue
            opts.append({
                "label": chr(64 + len(opts) + 1),  # A, B, C, ...
                "html_en": html_en,
                "html_hi": html_hi if html_hi != html_en else "",
            })
        if not opts:
            # Skip questions with zero options (corrupt records).
            continue
        correct_idx = _parse_correct(q.get("answer"))
        # Guard against answer index that maps to a collapsed option.
        if correct_idx >= len(opts):
            correct_idx = -1
        q_en = _question_html(q, lang="en")
        q_hi = _question_html(q, lang="hi") if q_has_hindi else ""
        neo_questions.append({
            "number": i,
            "section": sec_names[key],
            "section_key": key,
            "question_html": q_en,
            "question_hi": q_hi if q_hi and q_hi != q_en else "",
            "options": opts,
            "correct": correct_idx,
            "solution_html": _solution_html(q),
            "solution_hi": "",
        })

    # ── sections + sectional-timer detection ──────────────────────────
    # yesofficer's frontend algorithm (reverse-engineered from the JS):
    #   * meta["time"] is "60" (single timer) OR "20,20,20" / "20+20+20"
    #     (per-section minutes; "+" = sections locked, can't revisit).
    #   * If the parsed list has >1 entries AND matches the number of
    #     distinct section_ids in the question bank → sectional mode
    #     with one independent timer per section.
    #   * Otherwise → single shared timer across the whole paper.
    # Prefer duration from the course-tree entry (raw.duration /
    # raw.max_time_allowed) — these are what yesofficer's website
    # actually enforces per-quiz — and fall back to meta.time.
    raw = quiz.ref.raw or {}
    duration_src = (str(raw.get("duration") or "").strip()
                    or str(raw.get("max_time_allowed") or "").strip()
                    or str(meta.get("time") or "").strip())
    section_mins, sections_restricted = _parse_sectional_times(duration_src)
    total_time_min = sum(section_mins) if section_mins else 0
    is_sectional = (
        len(section_mins) > 1
        and len(section_mins) == len(sec_keys_order)
    )
    # The `show_sectionselector` flag is a separate feature (user picks
    # which sections to attempt) and also implies sectional mode even
    # when time doesn't split.
    if not is_sectional and str(meta.get("show_sectionselector") or "") == "1":
        is_sectional = len(sec_keys_order) > 1

    total_marks_meta = _float(meta.get("marks"), 0.0)
    sections: list[dict[str, Any]] = []
    for idx, key in enumerate(sec_keys_order):
        qc = sec_counts.get(key, 0)
        name = sec_names.get(key, f"Section {idx + 1}")
        if is_sectional and section_mins and idx < len(section_mins):
            sec_time = section_mins[idx]
        elif not is_sectional and len(sec_keys_order) == 1 and total_time_min:
            sec_time = total_time_min
        else:
            sec_time = 0
        # Prefer the quiz-level `marks` when everything is in one section.
        if len(sec_keys_order) == 1 and total_marks_meta > 0:
            score = total_marks_meta
        else:
            score = round(sec_scores.get(key, qc * mark_correct), 4)
        sections.append({
            "name": name,
            "time": sec_time,
            "max_score": score or qc * mark_correct,
        })

    # Prefer the quiz title shown in the yesofficer course list
    # (ref.title) — the testpass `meta.title` is often a stale/generic
    # name from when the test was first authored (e.g. "Mock Test 2")
    # and does not match what the user sees on the website.
    title = (quiz.ref.title or meta.get("title")
             or f"Quiz {quiz.ref.quiz_title_id}").strip()

    return {
        "title": title,
        "total_time": total_time_min or 60,
        "mark_correct": mark_correct,
        "mark_wrong": mark_wrong,
        # sectional=True enables the neo template's per-section timer,
        # "Next Section" button, and section-lock-on-submit UI.
        "sectional": is_sectional,
        "sections_restricted": sections_restricted,
        "sections": sections,
        "questions": neo_questions,
    }


# ---------------------------------------------------------------------------
# Branding & metadata injection
# ---------------------------------------------------------------------------


def _apply_branding(html: str, brand: str, contact: str) -> str:
    """Replace the Daredevil_Mock branding baked into the neo template
    with the caller's brand / telegram contact.  When `contact` is empty
    the contact link is hidden entirely.
    """
    brand_esc = _html.escape(brand)
    contact_plain = contact.lstrip("@") if contact else ""
    contact_at = f"@{contact_plain}" if contact_plain else ""

    # Longest tokens first so we don't partially-match.  The neo template
    # uses these exact strings in several spots (cards, footers, instr
    # page, contact link).
    html = html.replace("@Daredevil_Mock_bot",
                         _html.escape(contact_at) if contact_at else "")
    html = html.replace("https://t.me/Daredevil_Mock_bot",
                         f"https://t.me/{contact_plain}" if contact_plain
                         else "#")
    html = html.replace("Daredevil_Mock_bot",
                         _html.escape(contact_plain) if contact_plain else brand_esc)
    html = html.replace("Daredevil_Mock", brand_esc)
    html = html.replace("Fear No Exam", "Mock Test Series")
    return html


def _inject_metadata_banner(html: str, quiz: QuizData,
                             test_data: dict[str, Any]) -> str:
    """No-op.

    Previously a floating blue "Duration / Questions / Marks" banner was
    prepended into the body; it overlapped the pre-test instructions
    screen and was redundant with the built-in instructions page.  Left
    as a function so the call-site in render_quiz() stays stable.
    """
    return html


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_quiz(quiz: QuizData, *, brand: str = "YesOfficer Quiz",
                contact: str = "") -> str:
    """Render a QuizData to a self-contained HTML string.

    Images stay as remote <img src="http…"> URLs — call
    `render_quiz_offline()` for a fully-offline output.
    """
    test_data = build_test_data(quiz)
    if not test_data.get("questions"):
        return _empty_quiz_html(quiz, test_data, brand)
    html = quiz_html_neo.generate_mock_test_html(test_data)
    html = _apply_branding(html, brand, contact)
    html = _inject_metadata_banner(html, quiz, test_data)
    return html


def _empty_quiz_html(quiz: QuizData, test_data: dict[str, Any],
                     brand: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{_html.escape(test_data.get('title',''))}</title></head>"
        "<body style='font-family:sans-serif;max-width:700px;margin:40px auto;"
        "padding:20px;color:#1f2937'>"
        f"<h1>{_html.escape(test_data.get('title',''))}</h1>"
        f"<p>{_html.escape(brand)}</p>"
        "<p>No questions could be fetched for this quiz. The source may "
        "have an empty or restricted question bank.</p>"
        "</body></html>"
    )


async def render_quiz_offline(
    quiz: QuizData,
    http: aiohttp.ClientSession,
    *,
    brand: str = "YesOfficer Quiz",
    contact: str = "",
    max_parallel: int = 8,
    size_cap: int = 1_500_000,
    inline: bool = True,
) -> str:
    """Same as `render_quiz` but downloads every remote `<img>` referenced
    in the HTML and rewrites its `src` to a `data:` URI so the file
    opens with no network access.

    Images that fail to download (404 / 5xx / too big) are left as the
    original http URL so the browser can still try.
    """
    html = render_quiz(quiz, brand=brand, contact=contact)
    if not inline:
        return html
    return await inline_images_into_html(html, http,
                                          max_parallel=max_parallel,
                                          size_cap=size_cap)


# ---------------------------------------------------------------------------
# Image inliner
# ---------------------------------------------------------------------------


_IMG_SRC = re.compile(
    r'<img\b[^>]*?\bsrc="(https?://[^"]+)"',
    re.I,
)


async def _fetch_as_data_uri(
    http: aiohttp.ClientSession,
    url: str,
    size_cap: int,
) -> Optional[str]:
    try:
        async with http.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return None
            ctype = r.headers.get("Content-Type", "").split(";")[0].strip()
            raw = await r.read()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
    except Exception as exc:  # noqa: BLE001
        log.debug("image fetch error %s: %s", url, exc)
        return None
    if not raw or len(raw) > size_cap:
        return None
    if not ctype:
        ctype = (mimetypes.guess_type(url)[0] or "image/jpeg")
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{ctype};base64,{b64}"


async def inline_images_into_html(
    html: str,
    http: aiohttp.ClientSession,
    *,
    max_parallel: int = 8,
    size_cap: int = 1_500_000,
) -> str:
    """Download every http(s) `<img src>` in `html` and rewrite to data URI."""
    urls: list[str] = []
    seen: set[str] = set()
    for m in _IMG_SRC.finditer(html):
        u = m.group(1)
        if u not in seen:
            seen.add(u)
            urls.append(u)
    if not urls:
        return html
    sem = asyncio.Semaphore(max_parallel)

    async def one(u: str) -> tuple[str, Optional[str]]:
        async with sem:
            du = await _fetch_as_data_uri(http, u, size_cap)
            return u, du

    results = await asyncio.gather(*(one(u) for u in urls),
                                    return_exceptions=False)
    mapping = {u: du for u, du in results if du}
    if not mapping:
        return html

    # Replace every occurrence.  Use a single-pass replacement to avoid
    # quadratic behaviour on large HTML docs.
    def sub(m: re.Match) -> str:
        u = m.group(1)
        du = mapping.get(u)
        if not du:
            return m.group(0)
        return m.group(0).replace(f'"{u}"', f'"{du}"')

    out = _IMG_SRC.sub(sub, html)
    # Also rewrite any bare http URLs in `url(...)` CSS refs (rare but
    # appears in a few CKEditor <style> leftovers).
    for u, du in mapping.items():
        out = out.replace(f"url({u})", f"url({du})")
        out = out.replace(f'url("{u}")', f'url("{du}")')
        out = out.replace(f"url('{u}')", f"url('{du}')")
    return out


# ---------------------------------------------------------------------------
# Telegram caption helper
# ---------------------------------------------------------------------------


def quiz_metadata_caption(quiz: QuizData) -> str:
    """Return a short plain-text string suitable for a Telegram caption."""
    meta = quiz.meta or {}
    td = build_test_data(quiz)
    q = len(td.get("questions") or [])
    t = td.get("total_time") or 0
    marks = _float(meta.get("marks"), q * (td.get("mark_correct") or 1.0))
    mc = td.get("mark_correct") or 1.0
    mw = td.get("mark_wrong") or 0.0
    bits = [
        f"⏱ {t} min",
        f"❓ {q} questions",
        f"🎯 {marks:g} marks",
        f"+{mc:g} / {mw:g}" if mw else f"+{mc:g}",
    ]
    if quiz.attempted:
        bits.append("🟢 your attempt overlaid")
    return " · ".join(bits)
