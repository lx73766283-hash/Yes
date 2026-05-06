"""
Oliveboard Mock Test Generator — 100% IBPS TCS iON Interface
Matches ibps_sample.html + solutions_full_sample.html exactly.
No candidate name or roll number anywhere.
"""
from collections import defaultdict, OrderedDict
import html as _html
import re as _re
import json as _json

_GARBAGE_PATTERN = _re.compile(
    r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffd\ufffe\uffff\ue000-\uf8ff]+'
)
# Pattern to remove max-height and overflow CSS from passage elements (for full content display)
_MAXHEIGHT_OVERFLOW_PATTERN = _re.compile(
    r'max-height\s*:\s*\d+px\s*;?|overflow-y\s*:\s*auto\s*;?|overflow\s*:\s*auto\s*;?',
    _re.IGNORECASE
)
# Pattern to strip Word-export inline background/color styles that override passage container styling
# Removes: background:white, background:#fff, background:#ffffff, color:black, color:#000, color:#000000
_WORD_STYLE_PATTERN = _re.compile(
    r'\bbackground\s*:\s*(?:white|#fff(?:fff)?)\s*;?\s*|'
    r'\bcolor\s*:\s*(?:black|#000(?:000)?)\s*;?\s*',
    _re.IGNORECASE
)

def _sanitize(text):
    text = _GARBAGE_PATTERN.sub('', text) if text else text
    # Remove max-height and overflow from passage blocks to show full content
    text = _MAXHEIGHT_OVERFLOW_PATTERN.sub('', text) if text else text
    # Strip Word-export background:white / color:black that bleed into passage containers
    text = _WORD_STYLE_PATTERN.sub('', text) if text else text
    return text



def generate_mock_test_html(test_data):
    title        = test_data.get("title", "Mock Test")
    total_time   = test_data.get("total_time", 60)
    questions    = test_data.get("questions", [])
    mark_correct = test_data.get("mark_correct", 1.0)
    mark_wrong   = test_data.get("mark_wrong", -0.25)
    # sectional=True  → per-section timers, sections lock after submit (Banking/IBPS)
    # sectional=False → single shared timer, free navigation between sections (SSC)
    sectional    = test_data.get("sectional", False)  # default False — only True when explicitly set by bot.py

    # ── section ordering ─────────────────────────────────────────
    section_order = list(OrderedDict.fromkeys(q.get("section","General") for q in questions))
    for i, q in enumerate(questions):
        q["number"] = i + 1
        if not q.get("section"): q["section"] = "General"

    sec_groups = defaultdict(list)
    for q in questions: sec_groups[q["section"]].append(q)

    input_times  = {s["name"]: s.get("time", 0)     for s in test_data.get("sections", [])}
    input_scores = {s["name"]: s.get("max_score", 0) for s in test_data.get("sections", [])}
    sections = []
    ptr = 1
    for sn in section_order:
        qc = len(sec_groups[sn])
        # For sectional exams: use per-section time from part.php (e.g. 35, 45 min)
        # For non-sectional: use total_time so the SECTIONS JS array has it for display
        st = input_times.get(sn) or (total_time if sectional else 0)
        sm = input_scores.get(sn) or qc   # fallback: 1 mark per question
        sections.append({"name":sn,"qcount":qc,"time":st,"start":ptr,"end":ptr+qc-1,"max_score":sm})
        ptr += qc

    total_q = len(questions)

    # ── JS data arrays ────────────────────────────────────────────
    secs_js = _json.dumps([{
        "name":      s["name"],
        "start":     s["start"],
        "end":       s["end"],
        "secs":      s["time"] * 60,
        "max_score": s["max_score"],
    } for s in sections])

    # Per-section Hindi availability: {si: True/False}
    sec_has_hindi = {}
    for si, sname in enumerate(section_order):
        qs = sec_groups[sname]
        has_hi = any(
            # question_hi must exist AND differ from English question
            (q.get("question_hi","").strip() and
             q.get("question_hi","").strip() != q.get("question_html","").strip()) or
            # Passage-only Hindi (e.g. PM English Language section has Hindi
            # translation of reading passage but English-only questions/options)
            # does NOT count — the toggle would have nothing to swap at question
            # level. Only question_hi / option_hi differences trigger Hindi.
            # at least one option html_hi must exist AND differ from html_en
            any(
                (o.get("html_hi","") if isinstance(o,dict) else "") and
                (o.get("html_hi","") if isinstance(o,dict) else "").strip() !=
                (o.get("html_en","") if isinstance(o,dict) else "").strip()
                for o in q.get("options",[])
            )
            for q in qs
        )
        sec_has_hindi[si] = has_hi
    sec_hindi_js = _json.dumps(sec_has_hindi)

    # Per-section extra-language availability (Testbook multi-lang support)
    # Collects {lang_code: label} for each section where that lang has content
    sec_extra_langs = {}
    for si, sname in enumerate(section_order):
        qs = sec_groups[sname]
        langs_in_sec = {}
        for q in qs:
            for lang_code, lang_data in (q.get("extra_langs") or {}).items():
                if lang_code not in langs_in_sec:
                    if (lang_data.get("question","").strip() or
                            any(v.strip() for v in lang_data.get("options",[]) if isinstance(v,str))):
                        langs_in_sec[lang_code] = lang_data.get("label", lang_code)
        sec_extra_langs[si] = langs_in_sec
    sec_extra_langs_js = _json.dumps(sec_extra_langs)

    correct_map = {}
    for q in questions:
        ci = q.get("correct")
        if ci is not None: correct_map[str(q["number"])] = ci + 1
    correct_map_js = _json.dumps(correct_map)

    # ── section tabs ──────────────────────────────────────────────
    sec_tabs = ""
    for i, sec in enumerate(sections):
        cls = "sec-tab active" if i == 0 else "sec-tab"
        mins = sec["time"]
        timer_txt = "{:02d}:{:02d}".format(mins, 0)
        # Non-sectional (SSC): hide per-section timer badge — single total timer shown in header
        timer_span = ('<span class="sec-timer" id="sectimer-{}">{}</span>'.format(i, timer_txt)
                      if sectional else
                      '<span class="sec-timer" id="sectimer-{}" style="display:none"></span>'.format(i))
        sec_tabs += (
            '<div class="{}" id="sectab-{}" onclick="switchSec({})">'.format(cls,i,i) +
            _html.escape(sec["name"]) +
            timer_span +
            '</div>'
        )

    # ── per-section marks-per-question lookup ──────────────────────
    sec_mpq = {
        s["name"]: round(s["max_score"] / s["qcount"], 4) if s["qcount"] > 0 else mark_correct
        for s in sections
    }

    # ── hidden question blocks ────────────────────────────────────
    qblocks = ""
    for q in questions:
        qn   = q["number"]
        qh   = _sanitize(q.get("question_html") or "<p>{}</p>".format(_html.escape(str(q.get("question_text","")))))
        qhi  = _sanitize(q.get("question_hi","") or "")
        sn   = q.get("section","")
        opts = q.get("options",[])
        ci   = q.get("correct")
        si   = section_order.index(sn)
        ca   = (ci+1) if ci is not None else -1

        # options
        opts_h = ""
        for oi, opt in enumerate(opts):
            if isinstance(opt,dict):
                lbl        = (opt.get("label") or chr(65+oi)+")").rstrip(")").strip()
                html_en    = _sanitize(opt.get("html_en") or opt.get("text","") or "")
                html_hi    = _sanitize(opt.get("html_hi","") or "")
                opt_extra  = opt.get("extra_langs") or {}
            else:
                lbl        = chr(65+oi)
                html_en    = _sanitize(str(opt))
                html_hi    = ""
                opt_extra  = {}

            if opt_extra:
                inner = '<div class="opt-text-en" data-lang="en">{}</div>'.format(html_en)
                for lang_code, lang_val in opt_extra.items():
                    lang_html = _sanitize(lang_val or "")
                    if lang_html and lang_html.strip() != html_en.strip():
                        inner += '<div class="opt-text-extra" data-lang="{}">{}</div>'.format(lang_code, lang_html)
            elif html_hi:
                inner = '<div class="opt-text-en">{}</div><div class="opt-text-hi">{}</div>'.format(html_en, html_hi)
            else:
                inner = '<div class="opt-text-en opt-text-hi">{}</div>'.format(html_en)
            opts_h += (
                '<div class="opt-row" id="opt-{}-{}" onclick="selOpt({},{})">'.format(qn,oi+1,qn,oi+1) +
                '<div class="radio-outer"><div class="radio-inner"></div></div>' +
                '<span class="opt-lbl">{}.</span>'.format(_html.escape(lbl)) +
                '<div style="flex:1">{}</div>'.format(inner) +
                '</div>'
            )

        # passage handling — extract any passage-block elements out of the
        # question body (English or extra-language) and emit them as sibling
        # q-passage divs so the langbar can toggle them per-language.
        # Without this, an extra-language question body retains an embedded
        # passage-block, which renders alongside the English q-passage →
        # "double passage" bug when switching to Hindi.
        from bs4 import BeautifulSoup as _BS
        def _split_passage(raw_html, default_lang):
            """Return (passage_html, rest_html). passage-block divs are
            stripped from rest_html and converted to q-passage divs. If a
            passage-block has no lang-* class, default_lang is applied."""
            if "passage-block" not in (raw_html or ""):
                return "", raw_html or ""
            _s = _BS(raw_html, "html.parser")
            _passes = _s.find_all(class_="passage-block")
            parts = []
            for p in _passes:
                classes = [c for c in p.get("class", []) if c != "passage-block"]
                has_lang = any(c.startswith("lang-") for c in classes)
                if not has_lang and default_lang:
                    classes.append("lang-" + default_lang)
                p["class"] = (["q-passage"] + classes)
                parts.append(str(p))
                p.extract()
            _subq = _s.find(class_="sub-question")
            rest = str(_subq) if _subq else str(_s)
            return "".join(parts), rest

        en_pass, en_rest = _split_passage(qh, "en")

        # Extra language question text (Testbook multi-lang)
        q_extra_langs = q.get("extra_langs") or {}
        if q_extra_langs:
            qbody = en_pass + '<div class="q-text-en">{}</div>'.format(en_rest)
            for lang_code, lang_data in q_extra_langs.items():
                lang_q = _sanitize(lang_data.get("question","") or "")
                if not lang_q or lang_q.strip() == qh.strip():
                    continue
                lang_pass, lang_rest = _split_passage(lang_q, lang_code)
                qbody = lang_pass + qbody
                qbody += '<div class="q-text-extra" data-lang="{}">{}</div>'.format(lang_code, lang_rest)
        elif qhi:
            # OB/SK/Guidely: existing Hindi field
            qbody = en_pass + '<div class="q-text-en">{}</div>'.format(en_rest)
            hi_pass, hi_rest = _split_passage(qhi, "hi")
            qbody = hi_pass + qbody
            qbody += '<div class="q-text-hi">{}</div>'.format(hi_rest)
        else:
            # No Hindi translation — emit BOTH classes on the same div so the
            # `body.hindi-only .q-text-en{display:none}` rule is overridden by
            # the `.q-text-en.q-text-hi{display:block!important}` fallback rule.
            # This prevents language-neutral sub-questions (number series,
            # equations, diagrams) from vanishing when the reader toggles to
            # Hindi-only mode on a bilingual passage whose sub-Q has no Hindi
            # twin.
            qbody = en_pass + '<div class="q-text-en q-text-hi">{}</div>'.format(en_rest)

        sol_html    = _sanitize(q.get("solution_html", "") or "")
        _sol_hi_raw = _sanitize(q.get("solution_hi",   "") or "")
        # Discard Hindi solution if identical to English (Guidely duplicates for EN sections)
        sol_hi = _sol_hi_raw if _sol_hi_raw.strip() != sol_html.strip() else ""

        # Build solution block with extra langs if present
        sol_block = ""
        if sol_html:
            if q_extra_langs:
                sol_body = '<div class="sol-lang-en" data-lang="en">{}</div>'.format(sol_html)
                for lang_code, lang_data in q_extra_langs.items():
                    lang_sol = _sanitize(lang_data.get("solution","") or "")
                    if lang_sol and lang_sol.strip() != sol_html.strip():
                        sol_body += '<div class="sol-lang-extra" data-lang="{}">{}</div>'.format(lang_code, lang_sol)
            elif sol_hi:
                sol_body = (
                    '<div class="sol-lang-en">{}</div>'.format(sol_html) +
                    '<div class="sol-lang-hi">{}</div>'.format(sol_hi)
                )
            else:
                sol_body = sol_html
            sol_block = (
                '<div class="sol-box" id="solbox-{}">'
                '<div class="sol-hdr">&#128161; Solution</div>'
                '<div class="sol-body">{}</div>'
                '</div>'
            ).format(qn, sol_body)

        mpq = sec_mpq.get(sn, mark_correct)
        qblocks += (
            '<div class="qblock" id="qblock-{}" data-si="{}" data-ca="{}" data-sec="{}" data-mpq="{}" style="display:none">'.format(
                qn, si, ca, _html.escape(sn), mpq) +
            '<div class="q-bilingual">{}</div>'.format(qbody) +
            '<div style="font-size:11px;font-weight:600;color:#888;margin:4px 0 6px;padding:0 20px">Choose the correct answer:</div>' +
            '<div class="opts" id="opts-{}">{}</div>'.format(qn, opts_h) +
            sol_block +
            '</div>'
        )

    # ── palette grids ──────────────────────────────────────────────
    # Sectional: one grid per section, shown/hidden as sections change
    # Non-sectional: all sections always visible in one combined palette
    pal_grids = ""
    for si, sec in enumerate(sections):
        # All grids start visible for non-sectional; only first for sectional
        vis = "block" if (not sectional or si == 0) else "none"
        pal_grids += '<div id="palgrid-{}" style="display:{}">'.format(si, vis)
        pal_grids += '<div class="pal-sec-label">{}</div>'.format(_html.escape(sec["name"]))
        pal_grids += '<div class="pal-grid">'
        for q in sec_groups[sec["name"]]:
            pal_grids += '<div class="pq not-visited" id="pq-{}" onclick="goQ({})">'.format(q["number"],q["number"])
            pal_grids += '{}</div>'.format(q["number"])
        pal_grids += '</div></div>'

    max_score = sum(s["max_score"] for s in sections)
    te = _html.escape(title)

    return _build_html(
        te, sec_tabs, qblocks, pal_grids,
        total_q, secs_js, correct_map_js,
        mark_correct, mark_wrong, total_time*60,
        max_score, len(sections), section_order, sec_hindi_js,
        sectional, sec_extra_langs_js
    )


def _build_html(TITLE, SEC_TABS, QBLOCKS, PAL_GRIDS,
                TOTAL_Q, SECS_JS, CORRECT_MAP_JS,
                MARK_RIGHT, MARK_WRONG, TOTAL_SECS,
                MAX_SCORE, NUM_SECS, section_order, SEC_HINDI_JS,
                SECTIONAL=True, SEC_EXTRA_LANGS_JS='{}'):

    sec_order_js = _json.dumps(section_order)

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>""" + TITLE + """</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Arial,sans-serif;font-size:14px;background:#e8e8e8;height:100vh;height:100dvh;min-height:600px;display:flex;flex-direction:column;overflow:hidden}

/* ══ HEADER ══════════════════════════════════════════════════════ */
#hdr{
  background:linear-gradient(135deg,#1a3a5c 0%,#0d2b47 100%);
  color:#fff;padding:0 16px;height:54px;
  display:flex;align-items:center;justify-content:space-between;
  flex-shrink:0;border-bottom:3px solid #f7a800;
}
.hdr-left{display:flex;align-items:center;gap:12px}
.ibps-logo{
  background:#f7a800;color:#1a3a5c;font-weight:900;font-size:16px;
  padding:4px 10px;border-radius:4px;letter-spacing:1px
}
.yo-logo{
  display:flex;align-items:center;gap:6px;
  background:#f7a800;color:#1a3a5c;font-weight:900;font-size:14px;
  padding:4px 10px;border-radius:4px;letter-spacing:.5px;white-space:nowrap;
}
.exam-title{font-size:13px;font-weight:600;color:#d0dff0}
.hdr-right{display:flex;align-items:center;gap:14px}
.timer-box{
  background:#fff;border:2px solid #f7a800;border-radius:4px;
  padding:4px 12px;text-align:center;min-width:90px;flex-shrink:0
}
.timer-lbl{font-size:9px;color:#666;font-weight:600;letter-spacing:.5px;display:block}
.timer-val{font-size:16px;font-weight:700;color:#c0392b;font-family:'Courier New',monospace}
.timer-val.low{color:#c0392b;animation:tblink 1s infinite}
@keyframes tblink{50%{opacity:.5}}

/* ══ SECTION BAR ═════════════════════════════════════════════════ */
#secbar{
  background:#fff;border-bottom:1px solid #ccc;
  display:flex;align-items:stretch;flex-shrink:0;height:40px;
  padding:0 8px;overflow-x:auto;scrollbar-width:none;
}
#secbar::-webkit-scrollbar{display:none}
.sec-tab{
  padding:0 18px;cursor:pointer;font-size:12.5px;font-weight:600;
  color:#555;border-bottom:3px solid transparent;
  white-space:nowrap;display:flex;align-items:center;gap:6px;
  transition:all .15s;user-select:none;
}
.sec-tab:hover{color:#1a3a5c;background:#f0f5ff}
.sec-tab.active{color:#1a3a5c;border-bottom:3px solid #1a3a5c;background:#e8f0fb}
.sec-tab.locked{color:#aaa;cursor:not-allowed;background:#fafafa}
.sec-tab.done{color:#2e7d32;background:#f0fff5;cursor:default;border-bottom:3px solid #2e7d32}
.sec-timer{
  background:#e8f0fb;color:#1a3a5c;font-size:10px;
  padding:1px 6px;border-radius:10px;font-weight:700;
}
.sec-tab.active .sec-timer{background:#1a3a5c;color:#fff}
.sec-tab.done .sec-timer{background:#2e7d32;color:#fff}

/* ══ MAIN LAYOUT ════════════════════════════════════════════════ */
#main{display:flex;flex:1;overflow:hidden}

/* ══ QUESTION AREA ══════════════════════════════════════════════ */
#qarea{flex:1;overflow-y:auto;background:#fff;min-width:0;position:relative;display:flex;flex-direction:column}
.q-header{
  background:#f5f7fa;border-bottom:1px solid #ddd;
  padding:8px 20px;display:flex;justify-content:space-between;
  align-items:center;position:sticky;top:0;z-index:5;flex-shrink:0;
}
.q-num{font-weight:700;color:#1a3a5c;font-size:13px}
.q-type{font-size:11px;color:#888;background:#e8f0fb;padding:2px 8px;border-radius:10px}
.q-marks{font-size:11px;color:#666}
.q-lang-bar{
  display:flex;align-items:center;gap:8px;
  padding:6px 20px;background:#fffbf0;border-bottom:1px solid #f0e8c8;
  font-size:11px;color:#888;flex-shrink:0;
}
.lang-toggle-btn{
  padding:2px 10px;border-radius:3px;border:1px solid #ccc;
  background:#fff;font-size:11px;cursor:pointer;font-weight:600;
}
.lang-toggle-btn.active{background:#1a3a5c;color:#fff;border-color:#1a3a5c}
#qcontent{flex:1;padding:16px 20px 8px}
.q-bilingual{margin-bottom:14px}
.q-text-en{font-size:14px;line-height:1.7;color:#1a1a1a;margin-bottom:6px}
.q-text-hi{
  font-size:14px;line-height:1.7;color:#1a1a1a;
  font-family:'Noto Sans Devanagari',Arial,sans-serif;
  border-top:1px dashed #ddd;padding-top:6px;
}
body.english-only .q-text-hi{display:none}
body.hindi-only .q-text-en{display:none}
/* Question text with NO Hindi twin — dual-class fallback keeps it visible in
   either language mode (language-neutral content). */
body.english-only .q-text-en.q-text-hi{display:block !important}
body.hindi-only .q-text-en.q-text-hi{display:block !important}
/* Multi-language extra content (Testbook) */
.q-text-extra{font-size:14px;line-height:1.7;color:#1a1a1a;border-top:1px dashed #ddd;padding-top:6px;margin-top:4px}
body.english-only .q-text-extra{display:none}
body.english-only .opt-text-extra{display:none}
/* OB eqt/hqt spans — always visible (parent div controls language) */
.eqt,.hqt{display:inline}
/* Passage lang toggle */
.q-passage.lang-hi{display:none}
body.english-only .q-passage.lang-hi{display:none}
body.hindi-only .q-passage.lang-en{display:none}
body.hindi-only .q-passage.lang-hi{display:block}
.q-passage{
  background:#f0f5ff;border-left:4px solid #1a3a5c;
  border-radius:0 6px 6px 0;padding:12px 14px;
  margin-bottom:12px;font-size:13px;line-height:1.7;color:#222;
}
.q-passage table{border-collapse:collapse;width:100%;margin:8px 0;font-size:12px}
.q-passage td,.q-passage th{border:1px solid #b3c5d9;padding:5px 8px}
.q-passage th{background:#d0e0f0;color:#1a3a5c;font-weight:700}
.q-passage img,.q-text-en img,.q-text-hi img{max-width:100%;height:auto}

/* Options */
.opts{display:flex;flex-direction:column;gap:0;margin:0 0 8px}
.opt-row{
  display:flex;align-items:flex-start;gap:10px;
  padding:9px 14px;cursor:pointer;
  border-bottom:1px solid #f0f0f0;
  transition:background .1s;user-select:none;
}
.opt-row:last-child{border-bottom:none}
.opt-row:hover{background:#f5f8ff}
.opt-row.selected{background:#e8f0fe}
.radio-outer{
  width:17px;height:17px;border-radius:50%;
  border:2px solid #888;flex-shrink:0;margin-top:3px;
  display:flex;align-items:center;justify-content:center;
}
.opt-row:hover .radio-outer,.opt-row.selected .radio-outer{border-color:#1a3a5c}
.radio-inner{width:9px;height:9px;border-radius:50%;background:#1a3a5c;display:none}
.opt-row.selected .radio-inner{display:block}
.opt-lbl{font-weight:700;color:#1a3a5c;font-size:13px;min-width:20px;flex-shrink:0}
.opt-text-en{font-size:13.5px;color:#222;line-height:1.6}
.opt-text-hi{font-size:13px;color:#444;line-height:1.5;font-family:'Noto Sans Devanagari',Arial,sans-serif;margin-top:2px}
body.english-only .opt-text-hi{display:none}
body.hindi-only .opt-text-en{display:none}
/* English-only option has both classes — must always show regardless of mode */
body.english-only .opt-text-en.opt-text-hi{display:block !important}
body.hindi-only .opt-text-en.opt-text-hi{display:block !important}
.opt-row.submitted-correct{background:#e8f8e8 !important}
.opt-row.submitted-correct .radio-outer{border-color:#2e7d32}
.opt-row.submitted-correct .radio-inner{display:block;background:#2e7d32}
.opt-row.submitted-correct .opt-lbl{color:#2e7d32}
.opt-row.submitted-wrong{background:#fdecea !important}
.opt-row.submitted-wrong .radio-outer{border-color:#c62828}
.opt-row.submitted-wrong .radio-inner{display:block;background:#c62828}
.opt-row.submitted-wrong .opt-lbl{color:#c62828}

/* ══ FOOTER BUTTONS ═════════════════════════════════════════════ */
#qfooter{
  background:#f0f0f0;border-top:1px solid #ccc;
  padding:8px 16px;display:flex;align-items:center;justify-content:space-between;
  flex-shrink:0;position:sticky;bottom:0;z-index:10;
}
.footer-left{display:flex;gap:8px;flex-wrap:wrap}
.footer-right{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}
.btn{padding:8px 16px;border-radius:4px;font-size:12.5px;font-weight:600;cursor:pointer;transition:all .15s;border:1px solid transparent}
.btn-back{background:#fff;color:#555;border-color:#bbb}
.btn-back:hover{background:#e8e8e8}
.btn-mark{background:#7b61d6;color:#fff;border-color:#6a4fc2}
.btn-mark:hover{background:#6a4fc2}
.btn-clear{background:#fff;color:#1a3a5c;border:1px solid #1a3a5c}
.btn-clear:hover{background:#e8f0fb}
.btn-save{background:#2563eb;color:#fff;border-color:#1d4ed8}
.btn-save:hover{background:#1d4ed8}
.btn-submit-sec{background:#e85d00;color:#fff;border-color:#c44d00}
.btn-submit-sec:hover{background:#c44d00}
.btn-submit-exam-footer{background:#c0392b;color:#fff;border-color:#a93226;font-weight:700}
.btn-submit-exam-footer:hover{background:#a93226}

/* ══ PALETTE ════════════════════════════════════════════════════ */
#palette-wrap{
  width:220px;flex-shrink:0;background:#f5f7fa;
  border-left:1px solid #ddd;display:flex;flex-direction:column;overflow:hidden;
  transition:width .2s;position:relative;
}
#pal-toggle{
  position:absolute;left:-14px;top:50%;transform:translateY(-50%);
  background:#1a3a5c;color:#fff;width:14px;height:40px;
  display:flex;align-items:center;justify-content:center;
  cursor:pointer;border-radius:4px 0 0 4px;font-size:10px;z-index:20;user-select:none;
}
.palette-hdr{
  background:#1a3a5c;color:#fff;padding:8px 12px;
  font-size:12px;font-weight:700;letter-spacing:.3px;flex-shrink:0;
}
.palette-body{overflow-y:auto;flex:1;padding:8px 10px}

/* Section summary */
.pal-sec-summary{
  background:#fff;border:1px solid #ddd;border-radius:4px;
  margin-bottom:8px;overflow:hidden;
}
.pal-sec-hdr{
  background:#e8f0fb;padding:5px 10px;
  font-size:11px;font-weight:700;color:#1a3a5c;
  display:flex;justify-content:space-between;
}
.pal-sec-stats{
  padding:6px 10px;display:grid;grid-template-columns:1fr 1fr;gap:4px;
}
.pal-stat{font-size:10.5px;color:#555}
.pal-stat span{font-weight:700;color:#1a3a5c}

/* Palette section label */
.pal-sec-label{
  font-size:10px;font-weight:700;color:#888;
  text-transform:uppercase;letter-spacing:.5px;
  padding:6px 4px 3px;margin-bottom:4px;
  border-bottom:1px solid #e0e0e0;
}

/* Question grid */
.pal-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:4px;margin-bottom:12px}
.pq{
  width:100%;aspect-ratio:1;display:flex;align-items:center;justify-content:center;
  font-size:11px;font-weight:700;cursor:pointer;transition:transform .1s;
  user-select:none;position:relative;
}
.pq:hover{transform:scale(1.15)}
.pq.not-visited{background:#d0d0d0;color:#555;border-radius:3px;border:1px solid #bbb}
.pq.not-answered{background:#e74c3c;color:#fff;border-radius:50%;border:none}
.pq.answered{background:#27ae60;color:#fff;border-radius:50%;border:none}
.pq.review{background:#7b61d6;color:#fff;border-radius:50%;border:none}
.pq.answered-review{background:#7b61d6;color:#fff;border-radius:50%;border:none}
.pq.answered-review::after{
  content:'';position:absolute;bottom:1px;right:1px;
  width:7px;height:7px;background:#27ae60;border-radius:50%;border:1px solid #fff;
}
.pq.current{outline:3px solid #f7a800;outline-offset:1px}

/* Legend */
.pal-legend{border-top:1px solid #ddd;padding-top:8px;margin-top:4px}
.legend-item{display:flex;align-items:center;gap:8px;font-size:10.5px;color:#444;padding:3px 0}
.legend-icon{width:18px;height:18px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:700;color:#fff}
.li-grey{background:#d0d0d0;color:#555;border-radius:3px;border:1px solid #bbb}
.li-red{background:#e74c3c;border-radius:50%}
.li-green{background:#27ae60;border-radius:50%}
.li-purple{background:#7b61d6;border-radius:50%}
.li-purgreen{background:#7b61d6;border-radius:50%;position:relative}
.li-purgreen::after{content:'';position:absolute;bottom:-1px;right:-1px;width:7px;height:7px;background:#27ae60;border-radius:50%;border:1px solid #f5f7fa}

/* ══ BOTTOM BAR ══════════════════════════════════════════════════ */
#submitBar{
  background:#f0f0f0;border-top:2px solid #1a3a5c;
  padding:7px 16px;display:flex;justify-content:space-between;
  align-items:center;flex-shrink:0;position:sticky;bottom:0;z-index:10;
}
.bar-stats{display:flex;gap:16px;font-size:12px;color:#555}
.btn-submit-exam{background:#c0392b;color:#fff;border:none;padding:8px 22px;border-radius:4px;font-size:13px;font-weight:700;cursor:pointer}
.btn-submit-exam:hover{background:#a93226}

/* ══ MODAL ══════════════════════════════════════════════════════ */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.5);display:none;align-items:center;justify-content:center;z-index:100}
.overlay.show{display:flex}
.modal-box{background:#fff;border-radius:8px;width:480px;max-width:95vw;overflow:hidden;box-shadow:0 8px 32px rgba(0,0,0,.25)}
.modal-hdr{background:#1a3a5c;color:#fff;padding:12px 18px;font-size:14px;font-weight:700}
.modal-body{padding:18px}
.modal-table{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:14px}
.modal-table th{background:#e8f0fb;color:#1a3a5c;padding:7px 10px;text-align:left;border:1px solid #ddd}
.modal-table td{padding:7px 10px;border:1px solid #ddd}
.modal-stats{display:flex;gap:16px;background:#f9f9f9;border:1px solid #ddd;border-radius:4px;padding:10px 14px;margin-bottom:14px;font-size:12px}
.modal-stat-item{text-align:center;flex:1}
.modal-stat-val{font-size:18px;font-weight:700;color:#1a3a5c}
.modal-stat-lbl{font-size:10px;color:#888;margin-top:2px}
.modal-btns{display:flex;justify-content:flex-end;gap:8px}
.mbtn-cancel{padding:8px 20px;border-radius:4px;border:1px solid #bbb;background:#fff;font-size:13px;cursor:pointer;font-weight:600}
.mbtn-submit{padding:8px 20px;border-radius:4px;border:none;background:#1a7a2e;color:#fff;font-size:13px;cursor:pointer;font-weight:600}

/* ══ SCORE SCREEN ════════════════════════════════════════════════ */
#scoreScreen{display:none;position:fixed;inset:0;background:#e8e8e8;z-index:200;overflow-y:auto;padding:24px 16px}
.score-card{max-width:700px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,.1)}
.score-hdr{background:linear-gradient(135deg,#1a3a5c,#0d2b47);color:#fff;padding:20px 24px;text-align:center}
.score-hdr h2{font-size:18px;margin-bottom:4px}
.score-hdr p{font-size:12px;color:#99b8d0}
.score-big-box{padding:20px;text-align:center;border-bottom:1px solid #eee}
.score-num{font-size:48px;font-weight:700;color:#1a3a5c}
.score-num-lbl{font-size:13px;color:#888;margin-top:4px}
.score-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:#eee;border-bottom:1px solid #eee}
.score-cell{background:#fff;padding:14px;text-align:center}
.score-cell-val{font-size:22px;font-weight:700}
.score-cell-lbl{font-size:11px;color:#888;margin-top:3px}
.sc-right{color:#2e7d32}.sc-wrong{color:#c62828}.sc-skip{color:#888}.sc-acc{color:#1a3a5c}
.score-sec-table{width:100%;border-collapse:collapse;font-size:13px}
.score-sec-table th{background:#f5f7fa;padding:8px 12px;text-align:left;border:1px solid #ddd;font-size:12px}
.score-sec-table td{padding:8px 12px;border:1px solid #ddd}
.review-btn{display:block;margin:16px auto 0;padding:10px 28px;background:#1a3a5c;color:#fff;border:none;border-radius:4px;font-size:13px;font-weight:600;cursor:pointer}
.review-btn:hover{background:#0d2b47}

/* ══ SOLUTIONS SCREEN ════════════════════════════════════════════ */
#solScreen{display:none;position:fixed;inset:0;background:#e8e8e8;z-index:300;flex-direction:column}
#solScreen.show{display:flex}

/* Screen 1 — list */
#sol-s1{display:flex;flex-direction:column;flex:1;overflow:hidden}
#sol-s1-hdr{
  background:#1a3a5c;color:#fff;padding:10px 14px;
  display:flex;align-items:center;gap:10px;flex-shrink:0;
}
#sol-s1-hdr h2{font-size:13px;font-weight:700;flex:1}
.hdr-score-btn{background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.35);color:#fff;padding:5px 12px;border-radius:4px;font-size:11px;font-weight:700;cursor:pointer}
.filter-bar{background:#fff;padding:10px 12px;display:flex;gap:6px;flex-wrap:wrap;border-bottom:2px solid #e0e0e0;flex-shrink:0}
.fbtn{padding:5px 13px;border-radius:14px;border:1px solid #ccc;background:#fff;color:#555;font-size:11px;font-weight:600;cursor:pointer;white-space:nowrap;transition:all .12s}
.fbtn:hover{border-color:#1a3a5c;color:#1a3a5c}
.fbtn.active{background:#1a3a5c;color:#fff;border-color:#1a3a5c}
.fbtn.fc{color:#2e7d32;border-color:#81c784}
.fbtn.fc.active{background:#2e7d32;border-color:#2e7d32;color:#fff}
.fbtn.fw{color:#c62828;border-color:#e57373}
.fbtn.fw.active{background:#c62828;border-color:#c62828;color:#fff}
.fbtn.fsk{color:#777;border-color:#bbb}
.fbtn.fsk.active{background:#777;border-color:#777;color:#fff}
.stats-strip{background:#f5f7fa;padding:7px 14px;display:flex;gap:16px;border-bottom:1px solid #e0e0e0;flex-shrink:0;font-size:11px;color:#555;flex-wrap:wrap}
.stat-item{display:flex;align-items:center;gap:5px}
.stat-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.qlist{flex:1;overflow-y:auto;padding:10px 12px}
.qcard{background:#fff;border-radius:6px;border:1px solid #dde3ee;margin-bottom:8px;cursor:pointer;overflow:hidden;transition:border-color .12s,box-shadow .12s}
.qcard:hover{border-color:#1a3a5c;box-shadow:0 2px 8px rgba(26,58,92,.08)}
.qcard-hdr{padding:8px 12px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #f2f4f8}
.qcard-num{font-weight:700;color:#1a3a5c;font-size:12px}
.qcard-sec{font-size:10px;color:#aaa;margin-left:6px}
.status-badge{font-size:11px;padding:2px 10px;border-radius:10px;font-weight:700}
.sb-c{background:#e8f8e8;color:#2e7d32}.sb-w{background:#fdecea;color:#c62828}.sb-s{background:#f5f5f5;color:#888}
.qcard-preview{padding:9px 12px 4px;font-size:12.5px;color:#333;line-height:1.55}
.qcard-footer{padding:4px 12px 8px;font-size:10px;color:#bbb;text-align:right;font-style:italic}

/* Screen 2 — detail */
#sol-s2{display:none;flex:1;overflow:hidden;flex-direction:row}
#sol-s2.show{display:flex}
#s2-qarea{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
#s2-topbar{background:#1a3a5c;color:#fff;padding:8px 14px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.s2-back{background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.35);color:#fff;padding:5px 11px;border-radius:4px;font-size:11px;font-weight:700;cursor:pointer}
.s2-qnum-lbl{font-size:13px;font-weight:700;color:#fff}
.s2-sec-badge{font-size:10px;color:#d0e8f8;background:rgba(255,255,255,.15);padding:2px 8px;border-radius:10px}
.s2-status{font-size:11px;padding:3px 10px;border-radius:10px;font-weight:700}
.s2-st-c{background:#e8f8e8;color:#2e7d32}.s2-st-w{background:#fdecea;color:#c62828}.s2-st-s{background:#f5f5f5;color:#666}
#s2-subhdr{background:#f5f7fa;padding:6px 14px;border-bottom:1px solid #dde3ee;flex-shrink:0;display:flex;align-items:center;justify-content:space-between;font-size:11px;color:#888}
.marks-info{background:#fff;border:1px solid #e0e0e0;padding:2px 10px;border-radius:10px;font-size:10.5px;color:#555;font-weight:600}
#s2-body{flex:1;overflow-y:auto;padding:16px 18px}
.s2-q-passage{background:#f0f5ff;border-left:4px solid #1a3a5c;border-radius:0 6px 6px 0;padding:12px 14px;margin-bottom:12px;font-size:13px;line-height:1.7;color:#222}
.s2-q-passage table{border-collapse:collapse;width:100%;margin:8px 0;font-size:12px}
.s2-q-passage td,.s2-q-passage th{border:1px solid #b3c5d9;padding:5px 8px}
.s2-q-passage th{background:#d0e0f0;color:#1a3a5c;font-weight:700}
/* passage-block inside s2-qtext divs */
.s2-qtext-en .passage-block,.s2-qtext-hi .passage-block{display:block;background:#f0f5ff;border-left:4px solid #1a3a5c;border-radius:0 6px 6px 0;padding:12px 14px;margin-bottom:10px;font-size:13px;line-height:1.7;color:#222}
.s2-qtext-en .passage-block table,.s2-qtext-hi .passage-block table{border-collapse:collapse;width:100%;margin:8px 0;font-size:12px}
.s2-qtext-en .passage-block td,.s2-qtext-en .passage-block th,
.s2-qtext-hi .passage-block td,.s2-qtext-hi .passage-block th{border:1px solid #b3c5d9;padding:5px 8px}
.s2-qtext-en .passage-block th,.s2-qtext-hi .passage-block th{background:#d0e0f0;color:#1a3a5c;font-weight:700}
.s2-pass-hdr{display:block;font-size:10px;font-weight:700;color:#1a3a5c;letter-spacing:.5px;text-transform:uppercase;margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid #c5d8f0}
.q-text{font-size:13.5px;line-height:1.72;color:#1a1a1a;margin-bottom:14px}
.opts-heading{font-size:10px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.4px;margin-bottom:8px}
.s2-opts{display:flex;flex-direction:column;gap:7px;margin-bottom:18px}
.s2-opt{display:flex;align-items:flex-start;gap:10px;padding:10px 13px;border-radius:6px;border:1.5px solid #dde3ee;background:#fafbff;font-size:13px;line-height:1.5}
.s2-opt.opt-correct{background:#e8f8e8;border-color:#2e7d32;border-width:2px}
.s2-opt.opt-wrong{background:#fdecea;border-color:#c62828;border-width:2px}
.radio-wrap{width:17px;height:17px;border-radius:50%;border:2px solid #bbb;flex-shrink:0;margin-top:1px;display:flex;align-items:center;justify-content:center}
.s2-opt.opt-correct .radio-wrap{border-color:#2e7d32}
.s2-opt.opt-wrong .radio-wrap{border-color:#c62828}
.radio-fill{width:9px;height:9px;border-radius:50%;display:none}
.s2-opt.opt-correct .radio-fill{display:block;background:#2e7d32}
.s2-opt.opt-wrong .radio-fill{display:block;background:#c62828}
.opt-label{font-weight:700;font-size:12px;min-width:20px;color:#1a3a5c;flex-shrink:0}
.s2-opt.opt-correct .opt-label{color:#2e7d32}
.s2-opt.opt-wrong .opt-label{color:#c62828}
.opt-tag{display:inline-block;font-size:10px;font-weight:700;margin-left:8px;padding:1px 8px;border-radius:8px;vertical-align:middle}
.tag-correct{background:#c8e6c9;color:#1b5e20}
.tag-yours{background:#ffcdd2;color:#b71c1c}
.skip-note{background:#f5f5f5;border-left:3px solid #ccc;border-radius:0 4px 4px 0;padding:9px 13px;font-size:12px;color:#888;margin-bottom:14px}
.sol-box{background:#fffde7;border-left:4px solid #f9a825;border-radius:0 6px 6px 0;padding:13px 15px}
.sol-hdr{font-weight:700;color:#1a3a5c;font-size:13px;margin-bottom:9px;display:flex;align-items:center;gap:6px}
.sol-body{font-size:13px;color:#333;line-height:1.75}
.sol-body b{color:#1a3a5c}
.sol-body table{border-collapse:collapse;width:100%;margin:8px 0;font-size:12px}
.sol-body td,.sol-body th{border:1px solid #e0c860;padding:5px 9px}
.sol-body th{background:#fff9c4;color:#5d4037;font-weight:700}
.sol-body img,.s2-q-passage img,.q-text img{max-width:100%;height:auto}
/* Solutions screen bilingual option content */
.s2-opt-en{font-size:13px;line-height:1.6;color:#222}
.s2-opt-hi{font-size:13px;line-height:1.5;color:#222;font-family:'Noto Sans Devanagari',Arial,sans-serif;margin-top:2px;border-top:1px dashed #e0e0e0;padding-top:2px}
/* Solutions screen lang mode — controlled by #s2-langbar toggle */
#s2-body.s2-en .s2-opt-hi{display:none}
#s2-body.s2-hi .s2-opt-en{display:none}
/* Solution screen passage language control (passage-block → q-passage in DOM) */
#s2-body.s2-en .q-passage.lang-hi{display:none}
#s2-body.s2-hi .q-passage.lang-en{display:none !important}
#s2-body.s2-hi .q-passage.lang-hi{display:block !important}
#s2-body.s2-en .s2-opt-en.s2-opt-hi{display:block !important}
#s2-body.s2-hi .s2-opt-en.s2-opt-hi{display:block !important}
/* Solution body bilingual — controlled ONLY by #s2-body class, never body class */
.sol-lang-en,.sol-lang-hi{font-size:13px;line-height:1.6}
.sol-lang-hi{font-family:'Noto Sans Devanagari',Arial,sans-serif;margin-top:6px;padding-top:6px;border-top:1px dashed #d0d8e8;display:none}
#s2-body.s2-hi .sol-lang-hi{display:block}
#s2-body.s2-hi .sol-lang-en{display:none}
#s2-body.s2-en .sol-lang-hi{display:none}
#s2-body.s2-en .sol-lang-en{display:block}
/* Solutions q-text lang */
.s2-qtext-en{font-size:13.5px;line-height:1.72;color:#1a1a1a;margin-bottom:14px}
.s2-qtext-hi{font-size:13.5px;line-height:1.72;color:#1a1a1a;margin-bottom:14px;font-family:'Noto Sans Devanagari',Arial,sans-serif}
#s2-body.s2-en .s2-qtext-hi{display:none}
#s2-body.s2-hi .s2-qtext-en{display:none}
#s2-body.s2-en .s2-qtext-en.s2-qtext-hi{display:block !important}
#s2-body.s2-hi .s2-qtext-en.s2-qtext-hi{display:block !important}
/* Extras are controlled entirely by setSolLang JS — no default display:none */
.s2-qtext-extra{font-size:14px;line-height:1.7;color:#222;margin-top:8px;padding-top:6px;border-top:1px dashed #ddd}
.s2-opt-extra{font-size:13px;color:#444;line-height:1.5;margin-top:2px}
#s2-nav{background:#f0f0f0;border-top:1px solid #d0d0d0;padding:8px 14px;display:flex;justify-content:space-between;align-items:center;flex-shrink:0}
.nav-btn{padding:8px 18px;border-radius:4px;font-size:12px;font-weight:700;cursor:pointer;border:1px solid #bbb;background:#fff;color:#1a3a5c;transition:background .1s}
.nav-btn:hover:not(:disabled){background:#e8f0fb}
.nav-btn:disabled{opacity:.35;cursor:default}
#navInfo{font-size:11px;color:#888;font-weight:600}

/* Solutions palette */
#s2-palette{width:188px;flex-shrink:0;background:#f5f7fa;border-left:1px solid #dde3ee;display:flex;flex-direction:column;overflow:hidden}
.pal-hdr{background:#1a3a5c;color:#fff;padding:8px 12px;font-size:11px;font-weight:700;letter-spacing:.3px;flex-shrink:0}
.pal-body{overflow-y:auto;flex:1;padding:8px 8px 4px}
.pal-sec-lbl{font-size:10px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.4px;padding:5px 2px 3px;border-bottom:1px solid #e0e0e0;margin-bottom:4px}
.pal-grid2{display:grid;grid-template-columns:repeat(5,1fr);gap:3px;margin-bottom:10px}
.pq2{width:100%;aspect-ratio:1;display:flex;align-items:center;justify-content:center;font-size:10.5px;font-weight:700;cursor:pointer;border:none;transition:transform .1s;position:relative;border-radius:50%}
.pq2:hover{transform:scale(1.18)}
.pq2.pq-c{background:#27ae60;color:#fff}
.pq2.pq-w{background:#e74c3c;color:#fff}
.pq2.pq-s{background:#b0b0b0;color:#fff}
.pq2.pq-cur{outline:3px solid #f7a800;outline-offset:2px}
.pq2.dimmed{opacity:.28;cursor:default;pointer-events:none}
.pal-legend2{border-top:1px solid #ddd;padding:8px 4px 6px;display:flex;flex-direction:column;gap:5px}
.leg-row{display:flex;align-items:center;gap:7px;font-size:10.5px;color:#555}
.leg-dot{width:13px;height:13px;flex-shrink:0;border-radius:50%}
.leg-dot.c{background:#27ae60}.leg-dot.w{background:#e74c3c}.leg-dot.s{background:#b0b0b0}
/* Hide embedded solution during test; show after submit */
#qcontent .sol-box{display:none}
body.submitted #qcontent .sol-box{display:block}

/* ══ PRE-TEST SCREENS ═══════════════════════════════════════════ */
#noticeScreen,#instrScreen{
  display:flex;position:fixed;inset:0;z-index:900;
  background:#e8ecf1;align-items:center;justify-content:center;
  padding:16px;overflow-y:auto;
}
#instrScreen{display:none}
.pre-card{
  background:#fff;border-radius:10px;width:100%;max-width:580px;
  box-shadow:0 4px 24px rgba(0,0,0,.14);overflow:hidden;
}
.pre-hdr{
  background:linear-gradient(135deg,#1a3a5c 0%,#0d2b47 100%);
  border-bottom:3px solid #f7a800;
  padding:20px 24px;display:flex;align-items:center;gap:12px;
}
.pre-brand{font-size:22px;font-weight:800;color:#fff;letter-spacing:.5px}
.pre-tagline{font-size:12px;color:#a8c4e0;margin-top:3px;font-weight:500}
.pre-brand-sm{font-size:11px;font-weight:700;color:#a8c4e0;letter-spacing:.5px;margin-left:auto}
.pre-body{padding:22px 24px;overflow-y:auto;max-height:calc(100vh - 200px)}
.pre-section{margin-bottom:18px}
.pre-section:last-child{margin-bottom:0}
.pre-section-title{
  font-size:11px;font-weight:700;color:#1a3a5c;
  text-transform:uppercase;letter-spacing:.6px;
  margin-bottom:7px;padding-bottom:4px;
  border-bottom:1px solid #e8edf3;
}
.pre-section-body{font-size:13.5px;color:#333;line-height:1.7}
.pre-alert{
  background:#fff5f5;border:1px solid #f5c6c6;border-radius:6px;
  padding:11px 14px;font-size:13px;color:#333;line-height:1.6;
}
.pre-alert b{color:#c0392b}
.pre-contact{
  background:#f0f5ff;border:1px solid #c5d8f0;border-radius:6px;
  padding:11px 14px;font-size:13px;color:#333;line-height:1.7;
}
.pre-contact a{color:#1a3a5c;font-weight:700;text-decoration:none}
.pre-footer{
  padding:16px 24px;background:#f5f7fa;border-top:1px solid #e8edf3;
  display:flex;justify-content:flex-end;align-items:center;gap:10px;
}
.pre-btn{
  background:linear-gradient(135deg,#1a7a2e,#155a22);color:#fff;
  border:none;border-radius:6px;padding:10px 28px;
  font-size:14px;font-weight:700;cursor:pointer;
  transition:opacity .15s;letter-spacing:.3px;
}
.pre-btn:hover{opacity:.88}
/* Instructions screen */
.instr-stats{display:flex;gap:8px;margin-bottom:18px;flex-wrap:wrap}
.instr-stat{
  flex:1;min-width:76px;background:#f0f5ff;border:1px solid #c5d8f0;
  border-radius:7px;padding:10px 12px;text-align:center;
}
.instr-stat-val{font-size:20px;font-weight:800;color:#1a3a5c}
.instr-stat-lbl{font-size:10px;color:#666;font-weight:600;
  text-transform:uppercase;letter-spacing:.4px;margin-top:2px}
.instr-list{list-style:none;padding:0;margin:0}
.instr-list li{
  padding:7px 0 7px 22px;position:relative;
  font-size:13px;color:#333;line-height:1.6;
  border-bottom:1px solid #f0f0f0;
}
.instr-list li:last-child{border-bottom:none}
.instr-list li::before{
  content:'';position:absolute;left:6px;top:15px;
  width:6px;height:6px;border-radius:50%;background:#f7a800;
}
.instr-list li b{color:#1a3a5c}
/* Section breakdown table inside instructions */
.sec-instr-table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:10px}
.sec-instr-table th{background:#1a3a5c;color:#fff;padding:6px 10px;text-align:left}
.sec-instr-table td{padding:6px 10px;border-bottom:1px solid #eee;color:#333}
.sec-instr-table tr:last-child td{border-bottom:none}
/* ══ MOBILE ONLY (≤600px) — desktop completely untouched ════ */
@media (max-width: 600px) {

  /* ── Header: compact ── */
  #hdr { height: 44px; padding: 0 10px; }
  .ibps-logo { font-size: 13px; padding: 3px 7px; }
  .yo-logo { font-size: 11px; padding: 3px 7px; gap: 4px; }
  .exam-title { font-size: 11px; }
  .timer-box { padding: 2px 8px; min-width: 72px; }
  .timer-val { font-size: 13px; }
  .timer-lbl { font-size: 8px; }

  /* ── Section bar: compact ── */
  #secbar { height: 32px; padding: 0 4px; }
  .sec-tab { padding: 0 10px; font-size: 11px; }
  .sec-timer { font-size: 9px; padding: 1px 4px; }

  /* ── Question header: compact ── */
  .q-header { padding: 5px 12px; }
  .q-num { font-size: 12px; }
  .q-type { font-size: 10px; padding: 1px 6px; }
  .q-marks { font-size: 10px; }
  .q-lang-bar { padding: 4px 12px; font-size: 10px; }
  #qcontent { padding: 10px 12px 4px; }

  /* ── Options: tighter ── */
  .opt-row { padding: 7px 10px; gap: 8px; }
  .opt-lbl { font-size: 12px; }
  .opt-text-en { font-size: 12.5px; }

  /* ── Footer buttons: SINGLE ROW all 5 — text always fits ── */
  #qfooter {
    padding: 3px 5px;
    flex-wrap: nowrap;
    gap: 3px;
    justify-content: stretch;
    align-items: stretch;
  }
  .footer-left { gap: 3px; flex-shrink: 0; display: flex; align-items: stretch; }
  .footer-right { gap: 3px; flex: 1; justify-content: flex-end; display: flex; align-items: stretch; }
  .btn {
    padding: 4px 4px;
    font-size: 9px;
    border-radius: 3px;
    white-space: normal;
    line-height: 1.2;
    text-align: center;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 32px;
  }
  .btn-back              { flex: 0 0 42px; font-size: 9px; }
  .btn-mark              { flex: 1; min-width: 0; font-size: 8.5px; }
  .btn-clear             { flex: 0 0 44px; font-size: 8.5px; }
  .btn-save              { flex: 1; min-width: 0; font-size: 9px; }
  .btn-submit-sec        { flex: 0 0 52px; font-size: 8.5px; }
  .btn-submit-exam-footer{ flex: 0 0 52px; font-size: 8.5px; }

  /* ── Bottom stats+submit bar: ULTRA-SLIM — same height as button row ── */
  #submitBar {
    background: #1a3a5c;
    border-top: none;
    padding: 3px 6px;
    min-height: 0;
    align-items: center;
  }
  .bar-stats {
    gap: 6px;
    font-size: 8.5px;
    color: #a8c4e0;
    align-items: center;
    flex-wrap: nowrap;
  }
  .bar-stats b { color: #fff; font-size: 9px; }
  .btn-submit-exam {
    padding: 4px 7px;
    font-size: 9px;
    border-radius: 3px;
    white-space: nowrap;
    flex-shrink: 0;
  }

  /* ── Palette: bottom sheet on mobile ── */
  #palette-wrap {
    position: fixed;
    bottom: 0; left: 0; right: 0;
    width: 100% !important;
    height: 0;
    overflow: hidden;
    border-left: none;
    border-top: 2px solid #1a3a5c;
    border-radius: 14px 14px 0 0;
    transition: height 0.25s ease;
    z-index: 50;
  }
  #palette-wrap.mob-open { height: 55vh; overflow: hidden; }
  #pal-toggle { display: none; }
  .palette-body { max-height: calc(55vh - 36px); overflow-y: auto; }

  /* Floating palette button */
  #mobPalBtn {
    display: flex;
    position: fixed;
    bottom: 72px; right: 10px;
    width: 36px; height: 36px;
    background: #1a3a5c;
    color: #fff;
    border-radius: 50%;
    align-items: center; justify-content: center;
    font-size: 16px;
    box-shadow: 0 3px 10px rgba(0,0,0,.3);
    cursor: pointer;
    z-index: 60;
    border: none;
  }
  /* Palette close bar */
  #mobPalClose {
    display: flex;
    align-items: center; justify-content: center;
    background: #1a3a5c;
    color: #fff;
    padding: 6px;
    font-size: 11px;
    font-weight: 700;
    cursor: pointer;
    flex-shrink: 0;
  }
  /* Pal grid: 6 cols */
  .pal-grid { grid-template-columns: repeat(6,1fr); gap: 3px; }
  .pq { font-size: 10px; }

  /* Swipe hint */
  #swipeHint {
    display: none;
    position: fixed;
    bottom: 80px; left: 50%;
    transform: translateX(-50%);
    background: rgba(26,58,92,.88);
    color: #fff;
    padding: 5px 14px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 600;
    z-index: 70;
    pointer-events: none;
    white-space: nowrap;
  }
}

/* ── Desktop: hide ALL mobile-only elements, restore palette ── */
@media (min-width: 601px) {
  #mobPalBtn    { display: none !important; }
  #mobPalClose  { display: none !important; }
  #swipeHint    { display: none !important; }
  #solPalBtn    { display: none !important; }
  #solPalClose  { display: none !important; }
  /* Restore desktop s2-palette (side panel) */
  #s2-palette   { position: static !important; height: auto !important;
                  width: 188px !important; display: flex !important;
                  border-radius: 0 !important; border-left: 1px solid #dde3ee !important;
                  border-top: none !important; }
  #s2-palette .pal-body { max-height: none !important; }
  /* Swipe JS attaches to #qarea — desktop mouse drags won't trigger 50px touch threshold */

  /* ── Desktop font size overrides ─────────────────────────────────────── */
  /* Base */
  body                       { font-size: 16px; }

  /* Question text (English + Hindi) */
  .q-text-en                 { font-size: 18px; line-height: 1.8; }
  .q-text-hi                 { font-size: 18px; line-height: 1.8; }

  /* Passage / reading comprehension block */
  .q-passage                 { font-size: 16px; line-height: 1.8; }
  .q-passage table           { font-size: 14px; }

  /* Options */
  .opt-text-en               { font-size: 17px; line-height: 1.7; }
  .opt-text-hi               { font-size: 16px; line-height: 1.65; }
  .opt-lbl                   { font-size: 15px; }

  /* Section tabs */
  .sec-tab                   { font-size: 14px; }

  /* Question header bar */
  .q-num                     { font-size: 15px; }
  .q-type                    { font-size: 12.5px; }
  .q-marks                   { font-size: 12.5px; }

  /* Timer */
  .timer-val                 { font-size: 18px; }
  .timer-lbl                 { font-size: 10px; }

  /* Exam title in header */
  .exam-title                { font-size: 14.5px; }

  /* Footer nav buttons */
  .btn                       { font-size: 14px; }

  /* Solution screen — list (S1) */
  .qcard-num                 { font-size: 13px; }
  .qcard-preview             { font-size: 13.5px; }
  #sol-s1-hdr h2             { font-size: 14px; }
  .hdr-score-btn             { font-size: 12px; }
  .fbtn                      { font-size: 12px; }
  .stats-strip               { font-size: 12px; }

  /* Solution screen — detail (S2) */
  .s2-qtext-en, .s2-qtext-hi { font-size: 17px; line-height: 1.8; }
  .s2-q-passage              { font-size: 16px; }
  .s2-opt                    { font-size: 14px; }
  .opt-label                 { font-size: 13px; }
  .sol-hdr                   { font-size: 14px; }
  .sol-body                  { font-size: 16px; line-height: 1.8; }
  .s2-qnum-lbl               { font-size: 14px; }
  #navInfo                   { font-size: 13px; }

  /* Palette sidebar */
  .pal-hdr                   { font-size: 13px; }
  .pal-stat                  { font-size: 12px; }
  /* ─────────────────────────────────────────────────────────────────────── */
}

/* ══ MOBILE SOLUTION SCREEN (≤600px) ═══════════════════════ */
@media (max-width: 600px) {

  /* ── Score screen: fits phone, table scrollable ── */
  #scoreScreen { padding: 10px 8px; }
  .score-card { border-radius: 6px; }
  .score-hdr { padding: 14px 14px; }
  .score-hdr h2 { font-size: 15px; }
  .score-hdr p { font-size: 11px; }
  .score-big-box { padding: 12px 10px; }
  .score-num { font-size: 36px; }
  .score-num-lbl { font-size: 11px; }
  .score-grid { grid-template-columns: repeat(4,1fr); }
  .score-cell { padding: 10px 4px; }
  .score-cell-val { font-size: 18px; }
  .score-cell-lbl { font-size: 9px; }
  /* Section table: horizontal scroll so nothing gets cut */
  .score-card > div:last-child { padding: 10px 8px; overflow-x: auto; }
  .score-sec-table { font-size: 11px; min-width: 340px; }
  .score-sec-table th { padding: 6px 8px; font-size: 10px; }
  .score-sec-table td { padding: 6px 8px; }
  .review-btn { padding: 9px 20px; font-size: 12px; width: 100%; }

  /* ── Sol list screen (S1) ── */
  #sol-s1-hdr { padding: 7px 10px; }
  #sol-s1-hdr h2 { font-size: 11px; }
  .hdr-score-btn { padding: 4px 8px; font-size: 10px; }
  .filter-bar { padding: 6px 8px; gap: 4px; }
  .fbtn { padding: 4px 9px; font-size: 10px; }
  .stats-strip { padding: 5px 10px; gap: 10px; font-size: 10px; }
  .qcard { margin-bottom: 6px; }
  .qcard-hdr { padding: 6px 10px; }
  .qcard-num { font-size: 11px; }
  .qcard-preview { padding: 7px 10px 3px; font-size: 11.5px; }

  /* ── Sol detail screen (S2): palette → bottom sheet ── */
  #sol-s2 { flex-direction: column; }
  #s2-qarea { width: 100%; }
  /* s2-palette becomes bottom sheet */
  #s2-palette {
    position: fixed;
    bottom: 0; left: 0; right: 0;
    width: 100% !important;
    height: 0;
    overflow: hidden;
    border-left: none;
    border-top: 2px solid #1a3a5c;
    border-radius: 14px 14px 0 0;
    transition: height 0.25s ease;
    z-index: 50;
    flex-direction: column;
    display: flex;
  }
  #s2-palette.sol-pal-open {
    height: 55vh;
    overflow: hidden;
  }
  #s2-palette .pal-body {
    max-height: calc(55vh - 36px);
    overflow-y: auto;
  }
  .pal-grid2 { grid-template-columns: repeat(6,1fr); gap: 3px; }
  /* Floating palette button for solution screen
     Pure CSS: hidden by default, shown ONLY when #sol-s2 has class 'show'
     No JS needed — 100% reliable */
  #sol-s2 #solPalBtn {
    display: none;
    position: fixed;
    bottom: 52px; right: 10px;
    width: 36px; height: 36px;
    background: #1a3a5c;
    color: #fff;
    border-radius: 50%;
    align-items: center; justify-content: center;
    font-size: 16px;
    box-shadow: 0 3px 10px rgba(0,0,0,.3);
    cursor: pointer;
    z-index: 60;
    border: none;
  }
  #sol-s2.show #solPalBtn {
    display: flex !important;
  }
  /* Sol palette close bar */
  #solPalClose {
    display: flex;
    align-items: center; justify-content: center;
    background: #1a3a5c;
    color: #fff;
    padding: 6px;
    font-size: 11px;
    font-weight: 700;
    cursor: pointer;
    flex-shrink: 0;
  }

  /* ── S2 topbar: compact ── */
  #s2-topbar { padding: 6px 10px; }
  .s2-back { padding: 4px 8px; font-size: 10px; }
  .s2-qnum-lbl { font-size: 12px; }
  .s2-sec-badge { font-size: 9px; }
  .s2-status { font-size: 10px; padding: 2px 7px; }

  /* ── S2 subhdr ── */
  #s2-subhdr { padding: 4px 10px; font-size: 10px; }
  .marks-info { font-size: 9.5px; padding: 1px 7px; }

  /* ── S2 lang bar ── */
  #s2-langbar { padding: 4px 10px; font-size: 10px; }

  /* ── S2 body: tighter padding ── */
  #s2-body { padding: 10px 12px; }
  .s2-qtext-en, .s2-qtext-hi { font-size: 13px; line-height: 1.65; margin-bottom: 10px; }
  .s2-q-passage { padding: 9px 11px; font-size: 12px; margin-bottom: 9px; }
  .opts-heading { font-size: 9px; margin-bottom: 6px; }
  .s2-opts { gap: 5px; margin-bottom: 12px; }
  .s2-opt { padding: 8px 10px; font-size: 12px; gap: 8px; border-radius: 5px; }
  .opt-label { font-size: 11px; }
  .opt-tag { font-size: 9px; padding: 1px 6px; }
  .sol-box { padding: 10px 11px; }
  .sol-hdr { font-size: 12px; margin-bottom: 7px; }
  .sol-body { font-size: 12px; line-height: 1.65; }

  /* ── S2 nav: slim, full width, easy tap targets ── */
  #s2-nav {
    padding: 6px 10px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .nav-btn {
    padding: 7px 18px;
    font-size: 12px;
    font-weight: 700;
    border-radius: 4px;
    min-width: 80px;
  }
  #navInfo { font-size: 11px; }

  /* ── Hide test-screen palette button when on sol screen ── */
  #solScreen #mobPalBtn { display: none !important; }

  /* ── Sol screen swipe ── */
  /* s2-body gets touch listeners via JS — same 50px threshold */
}

/* ════════════════════════════════════════════════════════════════
   NEOEXAM PRO THEME — Color-only overrides
   All sizes, padding, fonts, layout = 100% original unchanged
   Only colors are replaced below
   ════════════════════════════════════════════════════════════════ */
/* (Google Fonts removed — fully-offline build relies on system fonts) */

:root {
  --neo-blue:     #2563eb;
  --neo-blue2:    #1d4ed8;
  --neo-blue3:    #1e40af;
  --neo-blueL:    #dbeafe;
  --neo-blueL2:   #eff6ff;
  --neo-charcoal: #1c2333;
  --neo-charcoal2:#0f1624;
  --neo-muted:    #64748b;
  --neo-muted2:   #94a3b8;
  --neo-border:   #e2e8f0;
  --neo-table-border: #94a3b8;
  --neo-border2:  #cbd5e1;
  --neo-bg:       #f8fafc;
  --neo-green:    #16a34a;
  --neo-red:      #dc2626;
  --neo-text:     #0f172a;
  --neo-slate:    #334155;
}

/* Font — body only, no size change */
* { font-family: 'Inter', 'Segoe UI', Arial, sans-serif !important; }

/* ── Body background ── */
body { background: var(--neo-bg) !important; }

/* ── Header — color only, height/padding untouched ── */
#hdr {
  background: var(--neo-charcoal) !important;
  border-bottom: 3px solid var(--neo-blue) !important;
  box-shadow: 0 2px 8px rgba(0,0,0,.2) !important;
}
.ibps-logo {
  background: var(--neo-blue) !important;
  color: #fff !important;
  border-radius: 4px !important;
}
.yo-logo {
  background: var(--neo-blue) !important;
  color: #fff !important;
  border-radius: 4px !important;
}
.exam-title { color: rgba(255,255,255,.65) !important; }

/* Timer — color only, size/padding untouched */
.timer-box {
  background: rgba(255,255,255,.07) !important;
  border: 2px solid rgba(255,255,255,.18) !important;
}
.timer-lbl { color: rgba(255,255,255,.45) !important; }
.timer-val { color: #fff !important; font-family: 'Courier New', monospace !important; }
.timer-val.low { color: #f87171 !important; }

/* ── Section bar — color only ── */
#secbar {
  background: #fff !important;
  border-bottom: 1px solid var(--neo-border) !important;
}
.sec-tab { color: var(--neo-muted) !important; }
.sec-tab:hover { color: var(--neo-text) !important; background: var(--neo-blueL2) !important; }
.sec-tab.active {
  color: var(--neo-blue) !important;
  border-bottom-color: var(--neo-blue) !important;
  background: var(--neo-blueL2) !important;
}
.sec-tab.locked { color: var(--neo-muted2) !important; background: var(--neo-bg) !important; }
.sec-tab.done { color: var(--neo-green) !important; background: #f0fdf4 !important; border-bottom-color: var(--neo-green) !important; }
.sec-timer { background: var(--neo-blueL) !important; color: var(--neo-blue2) !important; }
.sec-tab.active .sec-timer { background: var(--neo-blue) !important; color: #fff !important; }
.sec-tab.done .sec-timer { background: var(--neo-green) !important; color: #fff !important; }

/* ── Question area ── */
#qarea { background: #fff !important; }
.q-header { background: var(--neo-bg) !important; border-bottom-color: var(--neo-border) !important; }
.q-num { color: var(--neo-text) !important; }
.q-type { background: var(--neo-blueL) !important; color: var(--neo-blue2) !important; }
.q-marks { color: var(--neo-muted) !important; }
.q-lang-bar { background: var(--neo-bg) !important; border-bottom-color: var(--neo-border) !important; color: var(--neo-muted2) !important; }
.lang-toggle-btn { background: #fff !important; border-color: var(--neo-border2) !important; color: var(--neo-slate) !important; }
.lang-toggle-btn.active { background: var(--neo-charcoal) !important; color: #fff !important; border-color: var(--neo-charcoal) !important; }

/* Passage */
.q-passage { background: var(--neo-blueL2) !important; border-left-color: var(--neo-blue) !important; color: var(--neo-text) !important; }
.q-passage th { background: var(--neo-charcoal) !important; color: #fff !important; }
.q-passage td,.q-passage th { border-color: var(--neo-table-border) !important; }
.q-passage td { color: var(--neo-text) !important; }

/* Question text */
.q-text-en, .q-text-hi, .eqt, .hqt { color: var(--neo-text) !important; }

/* ── Options — color only, all padding/gap untouched ── */
.opt-row { border-bottom-color: var(--neo-border) !important; }
.opt-row:hover { background: var(--neo-blueL2) !important; }
.opt-row.selected { background: var(--neo-blueL) !important; }
.opt-row:hover .radio-outer, .opt-row.selected .radio-outer { border-color: var(--neo-blue) !important; }
.radio-inner { background: var(--neo-blue) !important; }
.opt-lbl { color: var(--neo-slate) !important; }
.opt-row.selected .opt-lbl { color: var(--neo-blue) !important; }
.opt-text-en, .opt-text-hi { color: var(--neo-text) !important; }
.opt-row.submitted-correct { background: #f0fdf4 !important; }
.opt-row.submitted-correct .radio-outer { border-color: var(--neo-green) !important; }
.opt-row.submitted-correct .radio-inner { background: var(--neo-green) !important; }
.opt-row.submitted-correct .opt-lbl { color: var(--neo-green) !important; }
.opt-row.submitted-wrong { background: #fef2f2 !important; }
.opt-row.submitted-wrong .radio-outer { border-color: var(--neo-red) !important; }
.opt-row.submitted-wrong .radio-inner { background: var(--neo-red) !important; }
.opt-row.submitted-wrong .opt-lbl { color: var(--neo-red) !important; }

/* ── Footer buttons — color only, ALL sizes/padding = original ── */
#qfooter { background: var(--neo-bg) !important; border-top-color: var(--neo-border) !important; }
.btn-back { background: #fff !important; color: #555 !important; border-color: var(--neo-border2) !important; }
.btn-back:hover { background: var(--neo-bg) !important; }
.btn-mark { background: #7b61d6 !important; color: #fff !important; border-color: #6a4fc2 !important; }
.btn-mark:hover { background: #6a4fc2 !important; }
.btn-clear { background: #fff !important; color: var(--neo-blue) !important; border: 1px solid var(--neo-blue) !important; }
.btn-clear:hover { background: var(--neo-blueL) !important; }
.btn-save { background: var(--neo-blue) !important; color: #fff !important; border-color: var(--neo-blue2) !important; }
.btn-save:hover { background: var(--neo-blue2) !important; }
.btn-submit-sec { background: var(--neo-charcoal) !important; color: #fff !important; border-color: var(--neo-charcoal2) !important; }
.btn-submit-sec:hover { background: var(--neo-charcoal2) !important; }
.btn-submit-exam-footer { background: var(--neo-red) !important; color: #fff !important; border-color: #b91c1c !important; }
.btn-submit-exam-footer:hover { background: #b91c1c !important; }

/* ── Submit bar — color only ── */
#submitBar { background: var(--neo-bg) !important; border-top: 1px solid var(--neo-border) !important; }
.bar-stats { color: var(--neo-muted) !important; }
.bar-stats b { color: var(--neo-text) !important; }
.btn-submit-exam { background: var(--neo-red) !important; color: #fff !important; }
.btn-submit-exam:hover { background: #b91c1c !important; }

/* Mobile submit bar override — keep original dark bg behaviour */
@media (max-width: 600px) {
  #submitBar { background: var(--neo-charcoal) !important; border-top: none !important; }
  .bar-stats { color: rgba(255,255,255,.7) !important; }
  .bar-stats b { color: #fff !important; }
  #mobPalBtn, #solPalBtn { background: var(--neo-blue) !important; }
  #mobPalClose, #solPalClose { background: var(--neo-charcoal) !important; color: #fff !important; }
  #palette-wrap { border-top-color: var(--neo-blue) !important; }
  #s2-palette { border-top-color: var(--neo-blue) !important; }
  #swipeHint { background: rgba(15,22,36,.88) !important; }
}

/* ── Palette sidebar ── */
#palette-wrap { background: var(--neo-bg) !important; border-left-color: var(--neo-border) !important; }
.palette-hdr { background: var(--neo-charcoal) !important; }
#pal-toggle { background: var(--neo-charcoal) !important; }
.pal-sec-summary { background: #fff !important; border-color: var(--neo-border) !important; }
.pal-sec-hdr { background: var(--neo-bg) !important; color: var(--neo-slate) !important; }
.pal-stat { color: var(--neo-muted) !important; }
.pal-stat span { color: var(--neo-text) !important; }
.pal-sec-label, .pal-sec-lbl { color: var(--neo-muted2) !important; }
.pq.not-visited { background: var(--neo-border) !important; color: var(--neo-slate) !important; border-color: var(--neo-border2) !important; }
.pq.not-answered { background: var(--neo-red) !important; }
.pq.answered { background: var(--neo-green) !important; }
.pq.review { background: #7c3aed !important; }
.pq.answered-review { background: #7c3aed !important; }
.pq.current { outline-color: var(--neo-blue) !important; }
.pal-legend { border-top-color: var(--neo-border) !important; }
.legend-item { color: var(--neo-muted) !important; }
.li-grey { background: var(--neo-border) !important; color: var(--neo-slate) !important; border-color: var(--neo-border2) !important; }
.li-red { background: var(--neo-red) !important; }
.li-green { background: var(--neo-green) !important; }
.li-purple { background: #7c3aed !important; }

/* ── Overlay / Modal ── */
.overlay { background: rgba(0,0,0,.45) !important; }
.modal-box { background: #fff !important; border-radius: 8px !important; }
.modal-hdr { background: var(--neo-charcoal) !important; color: #fff !important; }
.modal-table th { background: var(--neo-bg) !important; color: var(--neo-slate) !important; border-color: var(--neo-border) !important; }
.modal-table td { border-color: var(--neo-border) !important; color: var(--neo-text) !important; }
.modal-stats { background: var(--neo-bg) !important; border-color: var(--neo-border) !important; }
.modal-stat-val { color: var(--neo-blue) !important; }
.modal-stat-lbl { color: var(--neo-muted2) !important; }
.mbtn-cancel { background: #fff !important; color: var(--neo-muted) !important; border-color: var(--neo-border2) !important; }
.mbtn-submit { background: var(--neo-green) !important; color: #fff !important; }

/* ── Score screen ── */
#scoreScreen { background: var(--neo-bg) !important; }
.score-card { background: #fff !important; }
.score-hdr { background: var(--neo-charcoal) !important; border-bottom: none !important; }
.score-hdr h2 { color: #fff !important; }
.score-hdr p { color: rgba(255,255,255,.5) !important; }
.score-big-box { border-bottom-color: var(--neo-border) !important; }
.score-num { color: var(--neo-text) !important; }
.score-num-lbl { color: var(--neo-muted2) !important; }
.score-grid { background: var(--neo-border) !important; }
.score-cell { background: #fff !important; }
.score-cell-lbl { color: var(--neo-muted2) !important; }
.sc-right { color: var(--neo-green) !important; }
.sc-wrong { color: var(--neo-red) !important; }
.sc-skip { color: var(--neo-muted2) !important; }
.sc-acc { color: var(--neo-blue) !important; }
.score-sec-table th { background: var(--neo-bg) !important; border-color: var(--neo-border) !important; color: var(--neo-slate) !important; }
.score-sec-table td { border-color: var(--neo-border) !important; color: var(--neo-text) !important; }
.review-btn { background: var(--neo-blue) !important; color: #fff !important; }
.review-btn:hover { background: var(--neo-blue2) !important; }

/* ── Solutions screen ── */
#sol-s1-hdr { background: var(--neo-charcoal) !important; }
#sol-s1-hdr h2 { color: #fff !important; }
.hdr-score-btn { background: rgba(255,255,255,.12) !important; border-color: rgba(255,255,255,.25) !important; color: #fff !important; }
.filter-bar { background: #fff !important; border-bottom-color: var(--neo-border) !important; }
.fbtn { background: #fff !important; color: var(--neo-muted) !important; border-color: var(--neo-border2) !important; }
.fbtn:hover { border-color: var(--neo-blue) !important; color: var(--neo-blue) !important; }
.fbtn.active { background: var(--neo-blue) !important; color: #fff !important; border-color: var(--neo-blue) !important; }
.fbtn.fc { color: #2e7d32 !important; border-color: #81c784 !important; }
.fbtn.fc.active { background: #2e7d32 !important; border-color: #2e7d32 !important; color: #fff !important; }
.fbtn.fw { color: #c62828 !important; border-color: #e57373 !important; }
.fbtn.fw.active { background: #c62828 !important; border-color: #c62828 !important; color: #fff !important; }
.fbtn.fsk { color: #777 !important; border-color: #bbb !important; }
.fbtn.fsk.active { background: #777 !important; border-color: #777 !important; color: #fff !important; }
.stats-strip { background: var(--neo-bg) !important; border-bottom-color: var(--neo-border) !important; color: var(--neo-muted) !important; }
.qlist { background: var(--neo-bg) !important; }
.qcard { background: #fff !important; border-color: var(--neo-border) !important; }
.qcard:hover { border-color: var(--neo-blue) !important; box-shadow: 0 2px 8px rgba(37,99,235,.08) !important; }
.qcard-hdr { border-bottom-color: var(--neo-border) !important; }
.qcard-num { color: var(--neo-text) !important; }
.qcard-sec { color: var(--neo-muted2) !important; }
.qcard-preview { color: var(--neo-slate) !important; }
.qcard-footer { color: var(--neo-muted2) !important; }
.sb-c { background: #f0fdf4 !important; color: var(--neo-green) !important; }
.sb-w { background: #fef2f2 !important; color: var(--neo-red) !important; }
.sb-s { background: var(--neo-bg) !important; color: var(--neo-muted) !important; }

/* Solution detail (S2) */
#s2-topbar { background: var(--neo-charcoal) !important; }
.s2-back { background: rgba(255,255,255,.12) !important; border-color: rgba(255,255,255,.25) !important; color: #fff !important; }
.s2-qnum-lbl { color: #fff !important; }
.s2-sec-badge { color: rgba(255,255,255,.6) !important; background: rgba(255,255,255,.1) !important; }
.s2-st-c { background: #f0fdf4 !important; color: var(--neo-green) !important; }
.s2-st-w { background: #fef2f2 !important; color: var(--neo-red) !important; }
.s2-st-s { background: var(--neo-bg) !important; color: var(--neo-muted) !important; }
#s2-subhdr { background: var(--neo-bg) !important; border-bottom-color: var(--neo-border) !important; color: var(--neo-muted) !important; }
.marks-info { background: #fff !important; border-color: var(--neo-border) !important; color: var(--neo-muted) !important; }
#s2-body { background: #fff !important; }
.s2-qtext-en, .s2-qtext-hi { color: var(--neo-text) !important; }
.s2-q-passage { background: var(--neo-blueL2) !important; border-left-color: var(--neo-blue) !important; color: var(--neo-text) !important; }
.s2-q-passage th { background: var(--neo-charcoal) !important; color: #fff !important; }
.s2-q-passage td,.s2-q-passage th { border-color: var(--neo-table-border) !important; }
.s2-q-passage td { color: var(--neo-text) !important; }
.s2-qtext-en .passage-block, .s2-qtext-hi .passage-block { background: var(--neo-blueL2) !important; border-left-color: var(--neo-blue) !important; color: var(--neo-text) !important; }
.s2-qtext-en .passage-block th, .s2-qtext-hi .passage-block th { background: var(--neo-charcoal) !important; color: #fff !important; }
.s2-qtext-en .passage-block td,.s2-qtext-en .passage-block th,.s2-qtext-hi .passage-block td,.s2-qtext-hi .passage-block th { border-color: var(--neo-table-border) !important; }
.s2-pass-hdr { color: var(--neo-blue) !important; border-bottom-color: var(--neo-blueL) !important; }
.s2-opt { background: var(--neo-bg) !important; border-color: var(--neo-border) !important; }
.s2-opt.opt-correct { background: #f0fdf4 !important; border-color: var(--neo-green) !important; }
.s2-opt.opt-wrong { background: #fef2f2 !important; border-color: var(--neo-red) !important; }
.s2-opt-en, .s2-opt-hi { color: var(--neo-text) !important; }
.opt-label { color: var(--neo-slate) !important; }
.s2-opt.opt-correct .opt-label { color: var(--neo-green) !important; }
.s2-opt.opt-wrong .opt-label { color: var(--neo-red) !important; }
.tag-correct { background: #dcfce7 !important; color: #15803d !important; }
.tag-yours { background: #fee2e2 !important; color: #b91c1c !important; }
.skip-note { background: var(--neo-bg) !important; border-left-color: var(--neo-border2) !important; color: var(--neo-muted) !important; }
.sol-box { background: var(--neo-blueL2) !important; border-left-color: var(--neo-blue) !important; }
.sol-hdr { color: var(--neo-blue) !important; }
.sol-body { color: var(--neo-text) !important; }
.sol-body b { color: var(--neo-charcoal) !important; }
.sol-body td,.sol-body th { border-color: var(--neo-table-border) !important; }
.sol-body td { color: var(--neo-text) !important; }
.sol-body th { background: var(--neo-charcoal) !important; color: #fff !important; }
#s2-nav { background: var(--neo-bg) !important; border-top-color: var(--neo-border) !important; }
.nav-btn { background: #fff !important; color: var(--neo-blue) !important; border-color: var(--neo-border2) !important; }
.nav-btn:hover:not(:disabled) { background: var(--neo-blueL) !important; }
#navInfo { color: var(--neo-muted2) !important; }
#s2-palette { background: var(--neo-bg) !important; border-left-color: var(--neo-border) !important; }
.pal-hdr { background: var(--neo-charcoal) !important; color: #fff !important; }
.pal-body { background: var(--neo-bg) !important; }
.pal-sec-lbl { color: var(--neo-muted2) !important; }
.pal-legend2 { border-top-color: var(--neo-border) !important; }
.leg-row { color: var(--neo-muted) !important; }
.leg-dot.c { background: var(--neo-green) !important; }
.leg-dot.w { background: var(--neo-red) !important; }
.leg-dot.s { background: var(--neo-border2) !important; }
.pq2.pq-c { background: var(--neo-green) !important; }
.pq2.pq-w { background: var(--neo-red) !important; }
.pq2.pq-s { background: var(--neo-border2) !important; }
.pq2.pq-cur { outline-color: var(--neo-blue) !important; }

/* ── Pre-test screens ── */
#noticeScreen, #instrScreen { background: var(--neo-bg) !important; }
.pre-card { background: #fff !important; box-shadow: 0 8px 40px rgba(0,0,0,.08) !important; }
.pre-hdr { background: var(--neo-charcoal) !important; border-bottom: none !important; }
.pre-brand { color: #fff !important; }
.pre-tagline { color: rgba(255,255,255,.5) !important; }
.pre-brand-sm { color: rgba(255,255,255,.5) !important; }
.pre-body { background: #fff !important; }
.pre-section-title { color: var(--neo-muted2) !important; border-bottom-color: var(--neo-border) !important; }
.pre-section-body { color: var(--neo-slate) !important; }
.pre-alert { background: #fef2f2 !important; border-color: #fecaca !important; color: var(--neo-text) !important; }
.pre-alert b { color: var(--neo-red) !important; }
.pre-contact { background: var(--neo-blueL2) !important; border-color: var(--neo-blueL) !important; color: var(--neo-text) !important; }
.pre-contact a { color: var(--neo-blue) !important; }
.pre-footer { background: var(--neo-bg) !important; border-top-color: var(--neo-border) !important; }
.pre-btn { background: var(--neo-blue) !important; color: #fff !important; border: none !important; }
.pre-btn:hover { background: var(--neo-blue2) !important; opacity: 1 !important; }
.instr-stat { background: var(--neo-bg) !important; border-color: var(--neo-border) !important; }
.instr-stat-val { color: var(--neo-text) !important; }
.instr-stat-lbl { color: var(--neo-muted2) !important; }
.instr-list li { color: var(--neo-slate) !important; border-bottom-color: var(--neo-border) !important; }
.instr-list li::before { background: var(--neo-blue) !important; }
.instr-list li b { color: var(--neo-text) !important; }
.sec-instr-table th { background: var(--neo-charcoal) !important; color: #fff !important; }
.sec-instr-table td { border-bottom-color: var(--neo-border) !important; color: var(--neo-slate) !important; }

/* JS inline color references — override dynamically generated inline styles */
.score-sec-table td[style*="color:#1a3a5c"] { color: var(--neo-text) !important; }

/* ── Scrollbar ── */
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--neo-border2); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--neo-muted); }
</style>
</head>
<body class="english-only">

<!-- ══ SCREEN 1: NOTICE ══════════════════════════════════════════ -->
<div id="noticeScreen">
  <div class="pre-card">
    <div class="pre-hdr">
      <div>
        <div class="pre-brand" style="display:flex;align-items:center"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 28 32" style="width:26px;height:30px;display:inline-block;vertical-align:middle;margin-right:8px;flex-shrink:0"><path d="M9 9 L6.5 1 L11.5 7.5 Z" fill="#fff"/><path d="M19 9 L21.5 1 L16.5 7.5 Z" fill="#fff"/><ellipse cx="14" cy="19" rx="11" ry="12" fill="#fff"/><path d="M3 16 Q14 11 25 16 Q19 20 14 20 Q9 20 3 16Z" fill="rgba(0,0,0,0.15)"/><path d="M7 16.5 Q10 14 12.5 16.5" stroke="#1c2333" stroke-width="2" fill="none" stroke-linecap="round"/><path d="M15.5 16.5 Q18 14 21 16.5" stroke="#1c2333" stroke-width="2" fill="none" stroke-linecap="round"/></svg>Daredevil_Mock</div>
        <div class="pre-tagline">Free Mock Tests &mdash; Fear No Exam</div>
      </div>
    </div>
    <div class="pre-body">
      <div class="pre-section">
        <div class="pre-section-title">This File is Free</div>
        <div class="pre-section-body">This mock test is provided completely free of charge. No payment is required at any stage.</div>
      </div>
      <div class="pre-section">
        <div class="pre-section-title">About</div>
        <div class="pre-section-body">Daredevil_Mock is a free study resource dedicated to delivering high-quality practice mock tests to banking exam aspirants.</div>
      </div>
      <div class="pre-section">
        <div class="pre-section-title">Important</div>
        <div class="pre-alert">
          This file is <b>completely FREE</b>. Do not pay anyone for this file. If someone is charging you money for this test &mdash; they are a <b>scammer</b>. Report them immediately.
        </div>
      </div>
      <div class="pre-section">
        <div class="pre-section-title">Contact</div>
        <div class="pre-contact">
          If any platform or rights holder has concerns regarding our work or intentions, we encourage them to contact us directly. We are committed to addressing all concerns promptly and in good faith.<br><br>
          For questions or concerns, reach us on Telegram: <a href="https://t.me/Daredevil_Mock_bot" target="_blank">@Daredevil_Mock_bot</a>
        </div>
      </div>
    </div>
    <div class="pre-footer">
      <button class="pre-btn" onclick="showInstructions()">I Understand &nbsp;&#8594;</button>
    </div>
  </div>
</div>
<!-- ══ SCREEN 2: INSTRUCTIONS ════════════════════════════════════ -->
<div id="instrScreen">
  <div class="pre-card">
    <div class="pre-hdr">
      <div>
        <div class="pre-brand">Instructions</div>
        <div class="pre-tagline" id="pre-exam-title">Mock Test</div>
      </div>
      <div class="pre-brand-sm" style="display:flex;align-items:center;gap:4px"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 28 32" style="width:14px;height:16px;display:inline-block;vertical-align:middle;margin-right:4px;flex-shrink:0;opacity:.7"><path d="M9 8 L7 1 L11 7 Z" fill="#fff"/><path d="M19 8 L21 1 L17 7 Z" fill="#fff"/><ellipse cx="14" cy="18" rx="11" ry="13" fill="#fff"/><path d="M3 15 Q14 10 25 15 Q20 19 14 19 Q8 19 3 15Z" fill="rgba(0,0,0,0.18)"/><path d="M7 15.5 Q10 13 12.5 15.5" stroke="#1c2333" stroke-width="1.8" fill="none" stroke-linecap="round"/><path d="M15.5 15.5 Q18 13 21 15.5" stroke="#1c2333" stroke-width="1.8" fill="none" stroke-linecap="round"/></svg>Daredevil_Mock</div>
    </div>
    <div class="pre-body">
      <!-- Stats row — filled by JS from TOTAL / SECTIONS / MARK_RIGHT etc. -->
      <div class="instr-stats">
        <div class="instr-stat">
          <div class="instr-stat-val" id="is-total">—</div>
          <div class="instr-stat-lbl">Questions</div>
        </div>
        <div class="instr-stat">
          <div class="instr-stat-val" id="is-time">—</div>
          <div class="instr-stat-lbl">Total Time</div>
        </div>
        <div class="instr-stat">
          <div class="instr-stat-val" id="is-right" style="color:#2e7d32">—</div>
          <div class="instr-stat-lbl">Correct</div>
        </div>
        <div class="instr-stat">
          <div class="instr-stat-val" id="is-wrong" style="color:#c0392b">—</div>
          <div class="instr-stat-lbl">Wrong</div>
        </div>
        <div class="instr-stat">
          <div class="instr-stat-val" id="is-secs">—</div>
          <div class="instr-stat-lbl">Sections</div>
        </div>
        <div class="instr-stat">
          <div class="instr-stat-val" id="is-marks" style="color:#1a3a5c">—</div>
          <div class="instr-stat-lbl">Total Marks</div>
        </div>
      </div>
      <!-- Section breakdown table — filled by JS -->
      <div class="pre-section">
        <div class="pre-section-title">Section Details</div>
        <table class="sec-instr-table">
          <thead><tr><th>#</th><th>Section</th><th>Questions</th><th>Time</th><th>Marks</th></tr></thead>
          <tbody id="is-sec-body"></tbody>
        </table>
      </div>
      <!-- General instructions -->
      <div class="pre-section">
        <div class="pre-section-title">Instructions</div>
        <ul class="instr-list">
          <li>Timer starts when you click <b>Start Test</b> and cannot be paused.</li>
          <li id="instr-sec-order" style="display:none">Sections must be completed in order &mdash; each section locks after submission.</li>
          <li id="instr-free-nav" style="display:none">You can navigate freely between all sections at any time.</li>
          <li>Use <b>Save &amp; Next</b> to save your answer and move to the next question.</li>
          <li>Use <b>Mark for Review &amp; Next</b> to flag a question and continue.</li>
          <li>Use <b>Clear Response</b> to remove your current selection.</li>
          <li id="instr-submit-sec" style="display:none">Use <b>Submit Section</b> to complete the current section and unlock the next.</li>
          <li>Use <b>Submit Exam</b> to finish the test.</li>
          <li>After submission, use <b>View Solutions</b> to review all questions with answers and explanations.</li>
          <li><b>+""" + str(MARK_RIGHT) + """</b> for every correct answer. <b style="color:#c0392b">""" + str(MARK_WRONG) + """</b> for every wrong answer. <b>0</b> for unattempted.</li>
        </ul>
      </div>
    </div>
    <div class="pre-footer">
      <button class="pre-btn" style="background:linear-gradient(135deg,#c0392b,#a93226)" onclick="showNotice()">&#8592; Back</button>
      <button class="pre-btn" onclick="startTest()">Start Test &nbsp;&#9654;</button>
    </div>
  </div>
</div>

<!-- ══ HEADER ═══════════════════════════════════════════════════ -->
<div id="hdr">
  <div class="hdr-left">
    <div class="yo-logo"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 28 32" style="width:18px;height:20px;display:inline-block;vertical-align:middle;flex-shrink:0"><path d="M9 9 L6.5 1 L11.5 7.5 Z" fill="currentColor"/><path d="M19 9 L21.5 1 L16.5 7.5 Z" fill="currentColor"/><ellipse cx="14" cy="19" rx="11" ry="12" fill="currentColor"/><path d="M3 16 Q14 11 25 16 Q19 20 14 20 Q9 20 3 16Z" fill="rgba(0,0,0,0.18)"/><path d="M7 16.5 Q10 14 12.5 16.5" stroke="#1a3a5c" stroke-width="2" fill="none" stroke-linecap="round"/><path d="M15.5 16.5 Q18 14 21 16.5" stroke="#1a3a5c" stroke-width="2" fill="none" stroke-linecap="round"/></svg>YesOfficer</div>
    <div class="exam-title">""" + TITLE + """</div>
  </div>
  <div class="hdr-right">
    <div class="timer-box">
      <span class="timer-lbl">TIME LEFT</span>
      <span class="timer-val" id="timerVal">00:00:00</span>
    </div>
  </div>
</div>

<!-- ══ SECTION TABS ══════════════════════════════════════════════ -->
<div id="secbar">""" + SEC_TABS + """</div>

<!-- ══ MAIN ══════════════════════════════════════════════════════ -->
<div id="main">

  <!-- Question area -->
  <div id="qarea">
    <div class="q-header">
      <span class="q-num" id="qNumDisplay">Question No. 1</span>
      <div style="display:flex;align-items:center;gap:8px">
        <span class="q-type">Multiple Choice Question</span>
        <span class="q-marks" id="qMarksDisplay">Marks: +""" + str(MARK_RIGHT) + """ | \u2212""" + str(abs(MARK_WRONG)) + """</span>
      </div>
    </div>
    <div class="q-lang-bar" id="qlangbar" style="display:none">
      <span style="font-size:11px;color:#666;margin-right:4px">View in:</span>
      <button class="lang-toggle-btn" id="btnBilingual" onclick="setLang('both')">Bilingual</button>
      <button class="lang-toggle-btn active" id="btnEn" onclick="setLang('en')">English only</button>
      <button class="lang-toggle-btn" id="btnHi" onclick="setLang('hi')">&#2361;&#2367;&#2306;&#2342;&#2368; only</button>
    </div>
    <div id="qcontent">""" + QBLOCKS + """</div>
    <div id="qfooter">
      <div class="footer-left">
        <button class="btn btn-back" onclick="goQ(currentQ-1)">&#9664; Back</button>
      </div>
      <div class="footer-right">
        <button class="btn btn-mark" onclick="markForReview()">&#128204; Mark for Review &amp; Next</button>
        <button class="btn btn-clear" onclick="clearResponse()">&#10005; Clear Response</button>
        <button class="btn btn-save" id="btnSaveNext" onclick="saveAndNext()">Save &amp; Next &#9654;</button>
        <button class="btn btn-submit-sec" id="btnSubmitSec" onclick="submitSection()" style="display:none">Submit Section &#9654;</button>
        <button class="btn btn-submit-exam-footer" id="btnSubmitExam" onclick="openSubmit()" style="display:none">Submit Exam &#10003;</button>
      </div>
    </div>
  </div>

  <!-- Palette -->
  <div id="palette-wrap">
    <div id="mobPalClose" onclick="toggleMobPalette()">&#9660; Close Palette</div>
    <div id="pal-toggle" onclick="togglePalette()">&#9668;</div>
    <div class="palette-hdr">Question Palette</div>
    <div class="palette-body">
      <div class="pal-sec-summary">
        <div class="pal-sec-hdr"><span>Section Summary</span></div>
        <div class="pal-sec-stats">
          <div class="pal-stat">Answered: <span id="sumAns">0</span></div>
          <div class="pal-stat">Not Ans: <span id="sumNot">0</span></div>
          <div class="pal-stat">Marked: <span id="sumMrk">0</span></div>
          <div class="pal-stat">Not Visited: <span id="sumNV">0</span></div>
        </div>
      </div>
      <div id="palGrid">""" + PAL_GRIDS + """</div>
      <div class="pal-legend">
        <div class="legend-item"><div class="legend-icon li-grey">1</div>Not Visited</div>
        <div class="legend-item"><div class="legend-icon li-red"></div>Not Answered</div>
        <div class="legend-item"><div class="legend-icon li-green"></div>Answered</div>
        <div class="legend-item"><div class="legend-icon li-purple"></div>Marked for Review</div>
        <div class="legend-item"><div class="legend-icon li-purgreen"></div>Ans &amp; Marked for Review</div>
      </div>
    </div>
  </div>
</div>

<!-- ══ BOTTOM BAR ════════════════════════════════════════════════ -->
<div id="submitBar">
  <div class="bar-stats">
    <span>&#10003; Answered: <b id="barAns" style="color:#27ae60">0</b></span>
    <span>&#128204; Marked: <b id="barMrk" style="color:#7b61d6">0</b></span>
    <span>&#9633; Not answered: <b id="barNot">""" + str(TOTAL_Q) + """</b></span>
  </div>
  <button class="btn-submit-exam" onclick="openSubmit()">Submit Examination</button>
</div>

<!-- ══ MOBILE PALETTE BUTTON + SWIPE HINT ════════════════════ -->
<button id="mobPalBtn" onclick="toggleMobPalette()" title="Question Palette">&#128203;</button>
<div id="swipeHint">&#8592; swipe to navigate &#8594;</div>

<!-- ══ SUBMIT MODAL ══════════════════════════════════════════════ -->
<div class="overlay" id="submitModal">
  <div class="modal-box">
    <div class="modal-hdr">&#128203; Confirm Submission</div>
    <div class="modal-body">
      <p style="font-size:12px;color:#555;margin-bottom:12px">You are about to submit the examination. Please check the summary below.</p>
      <table class="modal-table" id="modalSecTable">
        <tr><th>#</th><th>Section</th><th>Questions</th><th>Time</th></tr>
      </table>
      <div class="modal-stats">
        <div class="modal-stat-item"><div class="modal-stat-val" id="mAns">0</div><div class="modal-stat-lbl">Answered</div></div>
        <div class="modal-stat-item"><div class="modal-stat-val" id="mNot">""" + str(TOTAL_Q) + """</div><div class="modal-stat-lbl">Not Answered</div></div>
        <div class="modal-stat-item"><div class="modal-stat-val" id="mMrk">0</div><div class="modal-stat-lbl">Marked for Review</div></div>
        <div class="modal-stat-item"><div class="modal-stat-val" id="mNV">0</div><div class="modal-stat-lbl">Not Visited</div></div>
      </div>
      <p style="font-size:11px;color:#c62828;margin-bottom:14px;font-weight:600">&#9888; Once submitted, you cannot change your answers.</p>
      <div class="modal-btns">
        <button class="mbtn-cancel" onclick="closeModal()">Cancel</button>
        <button class="mbtn-submit" onclick="submitExam()">Submit Examination</button>
      </div>
    </div>
  </div>
</div>

<!-- ══ SCORE SCREEN ══════════════════════════════════════════════ -->
<div id="scoreScreen">
  <div class="score-card">
    <div class="score-hdr">
      <h2>""" + TITLE + """</h2>
      <p>Examination Completed &mdash; Score Report</p>
    </div>
    <div class="score-big-box">
      <div class="score-num" id="finalScore">0</div>
      <div class="score-num-lbl">Total Score (out of """ + str(int(MAX_SCORE) if MAX_SCORE==int(MAX_SCORE) else MAX_SCORE) + """ marks) &nbsp;|&nbsp; +""" + str(MARK_RIGHT) + """ Correct &nbsp;|&nbsp; &minus;""" + str(abs(MARK_WRONG)) + """ Wrong</div>
    </div>
    <div class="score-grid">
      <div class="score-cell"><div class="score-cell-val sc-right" id="scRight">0</div><div class="score-cell-lbl">Correct</div></div>
      <div class="score-cell"><div class="score-cell-val sc-wrong" id="scWrong">0</div><div class="score-cell-lbl">Incorrect</div></div>
      <div class="score-cell"><div class="score-cell-val sc-skip" id="scSkip">0</div><div class="score-cell-lbl">Unattempted</div></div>
      <div class="score-cell"><div class="score-cell-val sc-acc" id="scAcc">0%</div><div class="score-cell-lbl">Accuracy</div></div>
    </div>
    <div style="padding:16px">
      <div style="font-size:13px;font-weight:700;color:#1a3a5c;margin-bottom:10px">Section-wise Performance</div>
      <table class="score-sec-table">
        <tr><th>Section</th><th>Total</th><th>Correct</th><th>Incorrect</th><th>Unattempted</th><th>Score</th></tr>
        <tbody id="secScoreBody"></tbody>
      </table>
      <p style="font-size:11px;color:#888;margin-top:12px;text-align:center">Marking Scheme: +""" + str(MARK_RIGHT) + """ for correct &nbsp;|&nbsp; &minus;""" + str(abs(MARK_WRONG)) + """ for incorrect &nbsp;|&nbsp; 0 for unattempted</p>
      <button class="review-btn" onclick="openSolutions()">&#128203; View Solutions &amp; Analysis</button>
    </div>
  </div>
</div>

<!-- ══ SOLUTIONS SCREEN ══════════════════════════════════════════ -->
<div id="solScreen">
  <!-- Screen 1: filter + list -->
  <div id="sol-s1">
    <div id="sol-s1-hdr">
      <h2>Solutions &amp; Analysis &mdash; """ + TITLE + """</h2>
      <button class="hdr-score-btn" onclick="closeSolutions()">&#8592; Score</button>
    </div>
    <div class="filter-bar" id="filterBar"></div>
    <div class="stats-strip" id="statsStrip"></div>
    <div class="qlist" id="solQlist"></div>
  </div>
  <!-- Screen 2: detail -->
  <div id="sol-s2">
    <button id="solPalBtn" onclick="toggleSolPalette()" title="Question Palette">&#128203;</button>
    <div id="s2-qarea">
      <div id="s2-topbar">
        <div style="display:flex;align-items:center;gap:8px">
          <button class="s2-back" onclick="showSolList()">&#8592; All Questions</button>
          <span class="s2-qnum-lbl" id="s2QNum">Q1</span>
          <span class="s2-sec-badge" id="s2QSec"></span>
        </div>
        <span class="s2-status" id="s2QStatus"></span>
      </div>
      <div id="s2-subhdr">
        <span>Multiple Choice Question</span>
        <span class="marks-info">+""" + str(MARK_RIGHT) + """ Correct &nbsp;|&nbsp; &minus;""" + str(abs(MARK_WRONG)) + """ Wrong &nbsp;|&nbsp; 0 Skipped</span>
      </div>
      <div id="s2-langbar" class="q-lang-bar" style="display:none">
        <span style="font-size:11px;color:#666;margin-right:4px">View in:</span>
        <button class="lang-toggle-btn active" id="s2BtnEn" onclick="setSolLang('en')">English only</button>
        <button class="lang-toggle-btn" id="s2BtnHi" onclick="setSolLang('hi')">&#2361;&#2367;&#2306;&#2342;&#2368; only</button>
      </div>
      <div id="s2-body"></div>
      <div id="s2-nav">
        <button class="nav-btn" id="prevBtn" onclick="navSol(-1)">&#9664; Prev</button>
        <span id="navInfo"></span>
        <button class="nav-btn" id="nextBtn" onclick="navSol(1)">Next &#9654;</button>
      </div>
    </div>
    <div id="s2-palette">
      <div id="solPalClose" onclick="toggleSolPalette()">&#9660; Close Palette</div>
      <div class="pal-hdr">Question Palette</div>
      <div class="pal-body" id="palBody"></div>
    </div>
  </div>
</div>

<script>
// ════════════════════════════════════════════════════════════════
//  DATA
// ════════════════════════════════════════════════════════════════
var TOTAL = """ + str(TOTAL_Q) + """;
var SECTIONS = """ + SECS_JS + """;
var CORRECT_MAP = """ + CORRECT_MAP_JS + """;
var MARK_RIGHT = """ + str(MARK_RIGHT) + """;
var MARK_WRONG = """ + str(MARK_WRONG) + """;
var TOTAL_SECS_INIT = """ + str(TOTAL_SECS) + """;
var SEC_ORDER = """ + sec_order_js + """;
var SECTIONAL = """ + ("true" if SECTIONAL else "false") + """;  // true=per-section locked timers, false=free nav single timer
var SEC_HINDI = """ + SEC_HINDI_JS + """;  // {si: true/false} — does section have Hindi?
var SEC_EXTRA_LANGS = """ + SEC_EXTRA_LANGS_JS + """;  // {si: {lang_code: label}} — extra languages per section

// ════════════════════════════════════════════════════════════════
//  STATE
// ════════════════════════════════════════════════════════════════
var answers = {};        // qn (1-based) -> option idx (1-based)
var visited = {};        // qn -> true
var marked  = {};        // qn -> true
var currentQ   = 1;
var currentSec = 0;
var activeSec  = 0;      // currently unlocked section
var doneSecs   = {};     // si -> true
var submitted  = false;
var langMode   = "en";
var paletteCollapsed = false;
var totalTimeLeft = TOTAL_SECS_INIT;
var timerInt = null;

// ════════════════════════════════════════════════════════════════
//  INIT: build submit modal table + init section
// ════════════════════════════════════════════════════════════════
(function initModalTable() {
  var tbl = document.getElementById("modalSecTable");
  for (var i = 0; i < SECTIONS.length; i++) {
    var s = SECTIONS[i];
    var row = tbl.insertRow();
    row.insertCell().textContent = i + 1;
    row.insertCell().textContent = s.name;
    row.insertCell().textContent = s.end - s.start + 1;
    row.insertCell().textContent = Math.round(s.secs / 60) + " min";
  }
  // Show correct instruction bullets based on exam mode
  var elSecOrder = document.getElementById("instr-sec-order");
  var elFreeNav  = document.getElementById("instr-free-nav");
  var elSubSec   = document.getElementById("instr-submit-sec");
  if (SECTIONAL) {
    if (elSecOrder) elSecOrder.style.display = "list-item";
    if (elSubSec)   elSubSec.style.display   = "list-item";
  } else {
    if (elFreeNav)  elFreeNav.style.display  = "list-item";
  }
})();

// ════════════════════════════════════════════════════════════════
//  QUESTION STATE HELPER
// ════════════════════════════════════════════════════════════════
function getQState(qn) {
  var ans = answers[qn];
  var vis = visited[qn];
  var mrk = marked[qn];
  if (!vis)                      return "not-visited";
  if (ans !== undefined && mrk)  return "answered-review";
  if (ans !== undefined)         return "answered";
  if (mrk)                       return "review";
  return "not-answered";
}

// ════════════════════════════════════════════════════════════════
//  SHOW QUESTION
// ════════════════════════════════════════════════════════════════
function goQ(qn) {
  if (qn < 1 || qn > TOTAL) return;

  // Hide current
  var oldEl = document.getElementById("qblock-" + currentQ);
  if (oldEl) oldEl.style.display = "none";

  currentQ = qn;
  visited[qn] = true;

  // Which section is this question in?
  var si = -1;
  for (var i = 0; i < SECTIONS.length; i++) {
    if (qn >= SECTIONS[i].start && qn <= SECTIONS[i].end) { si = i; break; }
  }

  // Section nav blocked if locked (sectional exams only)
  if (SECTIONAL && si !== activeSec && !doneSecs[si]) {
    alert("🔒 Submit current section first to unlock: " + SECTIONS[si].name);
    currentQ = SECTIONS[activeSec].start; // reset
    var back = document.getElementById("qblock-" + currentQ);
    if (back) back.style.display = "block";
    visited[currentQ] = true;
    return;
  }
  // Non-sectional: follow question freely across sections
  if (!SECTIONAL && si >= 0) activeSec = si;

  var el = document.getElementById("qblock-" + qn);
  if (el) el.style.display = "block";
  document.getElementById("qarea").scrollTop = 0;

  // Restore option selection
  renderOptSel(qn);

  // Update question number display
  document.getElementById("qNumDisplay").textContent = "Question No. " + qn;
  // Update per-question marks display (varies by section for non-uniform marking)
  var _qEl = document.getElementById("qblock-" + qn);
  var _mpq = _qEl ? (parseFloat(_qEl.getAttribute("data-mpq")) || MARK_RIGHT) : MARK_RIGHT;
  var _mpqDisp = _mpq % 1 === 0 ? String(_mpq) : _mpq.toFixed(2);
  var _mEl = document.getElementById("qMarksDisplay");
  if (_mEl) _mEl.textContent = "Marks: +" + _mpqDisp + " | \u2212" + Math.abs(MARK_WRONG);

  // Highlight active section tab
  for (var t = 0; t < SECTIONS.length; t++) {
    var tab = document.getElementById("sectab-" + t);
    if (tab) tab.classList.toggle("active", t === si && t === activeSec);
  }
  currentSec = si;

  // Show right palette grid
  updatePaletteGrid();
  updateSummaryBar();
}

// ════════════════════════════════════════════════════════════════
//  OPTION SELECTION
// ════════════════════════════════════════════════════════════════
function selOpt(qn, idx) {
  if (submitted) return;
  if (SECTIONAL && doneSecs[activeSec] && getSectionIdx(qn) === activeSec) return;
  // Toggle: clicking already-selected option unselects it
  if (answers[qn] === idx) {
    delete answers[qn];
  } else {
    answers[qn] = idx;
  }
  renderOptSel(qn);
  updatePaletteGrid();
  updateSummaryBar();
}

function renderOptSel(qn) {
  var sel = answers[qn];
  for (var o = 1; o <= 8; o++) {
    var el = document.getElementById("opt-" + qn + "-" + o);
    if (!el) break;
    el.classList.remove("selected");
    if (o === sel) el.classList.add("selected");
  }
}

function getSectionIdx(qn) {
  for (var i = 0; i < SECTIONS.length; i++) {
    if (qn >= SECTIONS[i].start && qn <= SECTIONS[i].end) return i;
  }
  return -1;
}

// ════════════════════════════════════════════════════════════════
//  NAVIGATION BUTTONS
// ════════════════════════════════════════════════════════════════
function saveAndNext() { goQ(currentQ + 1); }

function markForReview() {
  if (marked[currentQ]) delete marked[currentQ];
  else marked[currentQ] = true;
  updatePaletteGrid();
  goQ(currentQ + 1);
}

function clearResponse() {
  delete answers[currentQ];
  delete marked[currentQ];
  renderOptSel(currentQ);
  updatePaletteGrid();
  updateSummaryBar();
}

// ════════════════════════════════════════════════════════════════
//  SECTION SWITCHING (locked until submit)
// ════════════════════════════════════════════════════════════════
function switchSec(si) {
  if (si === activeSec) {
    goQ(SECTIONS[si].start);
    return;
  }
  if (!SECTIONAL) {
    // Non-sectional: free nav — just jump to section start
    activeSec = si;
    goQ(SECTIONS[si].start);
    updatePaletteGrid();
    return;
  }
  if (doneSecs[si]) {
    alert("Section " + SECTIONS[si].name + " is already submitted.");
    return;
  }
  alert("🔒 Submit current section first to unlock: " + SECTIONS[si].name);
}

// ════════════════════════════════════════════════════════════════
//  SECTION SUBMIT
// ════════════════════════════════════════════════════════════════
function submitSection() {
  var sec = SECTIONS[activeSec];
  var att = 0;
  for (var q = sec.start; q <= sec.end; q++) if (answers[q]) att++;
  var unat = (sec.end - sec.start + 1) - att;
  if (!confirm("Submit Section: " + sec.name + "\\n\\nAnswered: " + att + "\\nNot Answered: " + unat + "\\n\\nProceed?")) return;
  clearInterval(timerInt); timerInt = null;
  lockSection(activeSec);
  var next = activeSec + 1;
  if (next >= SECTIONS.length) {
    openSubmit();
  } else {
    startSection(next, false);
  }
}

function lockSection(si) {
  doneSecs[si] = true;
  var tab = document.getElementById("sectab-" + si);
  if (tab) { tab.classList.remove("active","locked"); tab.classList.add("done"); }
  var grid = document.getElementById("palgrid-" + si);
  if (grid) {
    var pqs = grid.querySelectorAll(".pq");
    for (var i = 0; i < pqs.length; i++) pqs[i].style.opacity = "0.5";
  }
}

function startSection(si, autoAdv) {
  activeSec = si;
  var tab = document.getElementById("sectab-" + si);
  if (tab) { tab.classList.remove("locked","done"); tab.classList.add("active"); }

  // Show correct palette grid
  // Non-sectional (SSC): show ALL section grids together always
  // Sectional (Banking): show only the active section grid
  for (var i = 0; i < SECTIONS.length; i++) {
    var g = document.getElementById("palgrid-" + i);
    if (g) g.style.display = (!SECTIONAL || i === si) ? "block" : "none";
  }

  // Update footer buttons
  var isLast = (si === SECTIONS.length - 1);
  var bss  = document.getElementById("btnSaveNext");
  var bsub = document.getElementById("btnSubmitSec");
  var bexm = document.getElementById("btnSubmitExam");
  // Non-sectional: always hide Submit Section, always show Submit Exam
  // Sectional: Submit Section on non-last, Submit Exam on last
  if (bss)  bss.style.display  = "inline-block";
  if (bsub) bsub.style.display = (SECTIONAL && !isLast) ? "inline-block" : "none";
  if (bexm) bexm.style.display = (!SECTIONAL || isLast) ? "inline-block" : "none";

  goQ(SECTIONS[si].start);
  // Sectional: per-section timer; Non-sectional: single shared total timer (start only once)
  if (SECTIONAL) {
    startTimer(SECTIONS[si].secs);
  } else if (si === 0) {
    startTimer(TOTAL_SECS_INIT);  // start once at exam begin
  }
  updateLangBar(si);
  if (autoAdv) alert("Time up! Starting: " + SECTIONS[si].name);
}

// ════════════════════════════════════════════════════════════════
//  PALETTE
// ════════════════════════════════════════════════════════════════
function updatePaletteGrid() {
  // Sectional: update only active section's pq buttons
  // Non-sectional: update ALL sections' pq buttons (all visible together)
  var startSec = SECTIONAL ? activeSec : 0;
  var endSec   = SECTIONAL ? activeSec : SECTIONS.length - 1;
  for (var si = startSec; si <= endSec; si++) {
    var sec = SECTIONS[si];
    for (var q = sec.start; q <= sec.end; q++) {
      var el = document.getElementById("pq-" + q);
      if (!el) continue;
      var st = getQState(q);
      el.className = "pq " + st + (q === currentQ ? " current" : "");
    }
  }
  updateSectionSummary();
}

function updateSectionSummary() {
  var sec = SECTIONS[currentSec < SECTIONS.length ? currentSec : activeSec];
  var ans=0, notAns=0, mrk=0, nv=0;
  for (var q = sec.start; q <= sec.end; q++) {
    var st = getQState(q);
    if (st==="answered"||st==="answered-review") ans++;
    else if (st==="review") mrk++;
    else if (st==="not-answered") notAns++;
    else nv++;
  }
  document.getElementById("sumAns").textContent = ans;
  document.getElementById("sumNot").textContent = notAns;
  document.getElementById("sumMrk").textContent = mrk;
  document.getElementById("sumNV").textContent  = nv;
}

function updateSummaryBar() {
  var ans=0, mrk=0;
  for (var q = 1; q <= TOTAL; q++) {
    var st = getQState(q);
    if (st==="answered") ans++;
    else if (st==="answered-review") { ans++; mrk++; }
    else if (st==="review") mrk++;
  }
  document.getElementById("barAns").textContent = ans;
  document.getElementById("barMrk").textContent = mrk;
  document.getElementById("barNot").textContent = TOTAL - ans;
  document.getElementById("mAns").textContent = ans;
  document.getElementById("mNot").textContent = TOTAL - ans;
  document.getElementById("mMrk").textContent = mrk;
  var nv = 0;
  for (var q = 1; q <= TOTAL; q++) if (!visited[q]) nv++;
  document.getElementById("mNV").textContent = nv;
}

// ════════════════════════════════════════════════════════════════
//  LANGUAGE TOGGLE
// ════════════════════════════════════════════════════════════════
function updateLangBar(si) {
  var bar = document.getElementById("qlangbar");
  if (!bar) return;

  var extraLangs = (SEC_EXTRA_LANGS[String(si)] || SEC_EXTRA_LANGS[si]) || {};
  var hasExtra   = Object.keys(extraLangs).length > 0;
  var hasHindi   = SEC_HINDI[String(si)] || SEC_HINDI[si];

  if (hasExtra) {
    // ── Testbook multi-language mode: build dynamic buttons ──
    bar.style.display = "";
    bar.innerHTML = '<span style="font-size:11px;color:#666;margin-right:4px">View in:</span>';

    var btnBi = document.createElement("button");
    btnBi.className = "lang-toggle-btn" + (langMode === "both" ? " active" : "");
    btnBi.textContent = "Bilingual";
    btnBi.onclick = function() { setLang("both"); };
    bar.appendChild(btnBi);

    var btnEn = document.createElement("button");
    btnEn.className = "lang-toggle-btn" + (langMode === "en" ? " active" : "");
    btnEn.textContent = "English";
    btnEn.onclick = function() { setLang("en"); };
    bar.appendChild(btnEn);

    Object.keys(extraLangs).forEach(function(code) {
      var btn = document.createElement("button");
      btn.className = "lang-toggle-btn" + (langMode === code ? " active" : "");
      btn.textContent = extraLangs[code];
      btn.setAttribute("data-lang", code);
      btn.onclick = (function(c) { return function() { setLang(c); }; })(code);
      bar.appendChild(btn);
    });

    setLangMulti(langMode, extraLangs);

  } else if (hasHindi) {
    // ── OB/SK/Guidely bilingual mode: original 2-button bar ──
    bar.style.display = "";
    bar.innerHTML =
      '<span style="font-size:11px;color:#666;margin-right:4px">View in:</span>' +
      '<button class="lang-toggle-btn" id="btnBilingual" onclick="setLang(&quot;both&quot;)">Bilingual</button>' +
      '<button class="lang-toggle-btn active" id="btnEn" onclick="setLang(&quot;en&quot;)">English only</button>' +
      '<button class="lang-toggle-btn" id="btnHi" onclick="setLang(&quot;hi&quot;)">\u0939\u093f\u0902\u0926\u0940 only</button>';
    setLang(langMode);

  } else {
    // ── English-only section: hide bar ──
    bar.style.display = "none";
    document.body.className = "english-only";
    langMode = "en";
  }
}

function setLang(mode) {
  langMode = mode;
  var extraLangs = (SEC_EXTRA_LANGS[String(currentSec)] || SEC_EXTRA_LANGS[currentSec]) || {};
  if (Object.keys(extraLangs).length > 0) {
    setLangMulti(mode, extraLangs);
  } else {
    // Original OB/SK/Guidely 2-lang logic — untouched
    document.body.className = mode==="en" ? "english-only" : (mode==="hi" ? "hindi-only" : "");
    var b = document.getElementById("btnBilingual");
    var e = document.getElementById("btnEn");
    var h = document.getElementById("btnHi");
    if (b) b.classList.toggle("active", mode==="both");
    if (e) e.classList.toggle("active", mode==="en");
    if (h) h.classList.toggle("active", mode==="hi");
  }
  setSolLang(mode);
}

function setLangMulti(mode, extraLangs) {
  var showEn = (mode === "en" || mode === "both");

  // Clear body class so CSS lang-visibility rules don't conflict with JS inline styles
  document.body.className = "";

  // Question text — supports TB's .q-text-extra[data-lang] AND PM's legacy
  // .q-text-hi. Fall back to English when selected non-English lang has no
  // content for this question so the question never blanks out.
  document.querySelectorAll(".q-bilingual").forEach(function(block) {
    var enEl = block.querySelector(".q-text-en");
    var extras = block.querySelectorAll(".q-text-extra");
    var legacyHiEl = block.querySelector(".q-text-hi");
    var match = null;
    if (!showEn) {
      for (var i = 0; i < extras.length; i++) {
        if (extras[i].getAttribute("data-lang") === mode &&
            extras[i].textContent.trim() !== "") {
          match = extras[i]; break;
        }
      }
      if (!match && mode === "hi" && legacyHiEl &&
          legacyHiEl.textContent.trim() !== "") {
        match = legacyHiEl;
      }
    }
    var fallbackToEn = !showEn && !match;
    if (enEl) enEl.style.display = (showEn || fallbackToEn) ? "" : "none";
    extras.forEach(function(el) {
      var code = el.getAttribute("data-lang");
      el.style.display = (mode === "both" || mode === code) ? "" : "none";
    });
    if (legacyHiEl) {
      legacyHiEl.style.display =
        (mode === "both" || mode === "hi") ? "" : "none";
    }
  });

  // Passages inside q-bilingual — prefer lang-specific passage, fall back to
  // English (lang-en / untagged) passage if no passage exists for selected lang.
  document.querySelectorAll(".q-bilingual").forEach(function(block) {
    var passes = block.querySelectorAll(".q-passage");
    if (passes.length === 0) return;
    var hasMatch = false;
    if (!showEn) {
      for (var i = 0; i < passes.length; i++) {
        if (passes[i].classList.contains("lang-" + mode) &&
            passes[i].textContent.trim() !== "") {
          hasMatch = true; break;
        }
      }
    }
    passes.forEach(function(p) {
      var isEn = p.classList.contains("lang-en") ||
                 !Array.from(p.classList).some(function(c){ return c.indexOf("lang-") === 0; });
      var isMode = p.classList.contains("lang-" + mode);
      var show = (mode === "both") ? true
               : (showEn ? isEn
                        : (hasMatch ? isMode : isEn));
      p.style.display = show ? "" : "none";
    });
  });

  // Options — per opt-row: supports TB's .opt-text-extra[data-lang] AND PM's
  // legacy .opt-text-hi. Only hide English when a translation exists for the
  // selected language.
  document.querySelectorAll(".opt-row").forEach(function(row) {
    var enEl       = row.querySelector(".opt-text-en");
    var legacyHiEl = row.querySelector(".opt-text-hi");
    var extraEl    = (mode !== "en" && mode !== "both")
                     ? row.querySelector(".opt-text-extra[data-lang='" + mode + "']")
                     : null;
    var hasTranslation = !!(extraEl && extraEl.textContent.trim() !== "");
    if (!hasTranslation && !showEn && mode === "hi" &&
        legacyHiEl && legacyHiEl.textContent.trim() !== "") {
      hasTranslation = true;
    }
    if (enEl) {
      enEl.style.display = (showEn || !hasTranslation) ? "" : "none";
    }
    row.querySelectorAll(".opt-text-extra").forEach(function(el) {
      var code = el.getAttribute("data-lang");
      el.style.display = (mode === "both" || mode === code) ? "" : "none";
    });
    if (legacyHiEl) {
      legacyHiEl.style.display =
        (mode === "both" || mode === "hi") ? "" : "none";
    }
  });

  // Solutions inline
  document.querySelectorAll(".sol-lang-en[data-lang]").forEach(function(el) {
    el.style.display = showEn ? "" : "none";
  });
  document.querySelectorAll(".sol-lang-extra").forEach(function(el) {
    var code = el.getAttribute("data-lang");
    el.style.display = (mode === "both" || mode === code) ? "" : "none";
  });

  // Update button active states
  var bar = document.getElementById("qlangbar");
  if (!bar) return;
  bar.querySelectorAll(".lang-toggle-btn").forEach(function(btn) {
    var code = btn.getAttribute("data-lang");
    if (!code) {
      btn.classList.toggle("active",
        (btn.textContent === "Bilingual" && mode === "both") ||
        (btn.textContent === "English"   && mode === "en"));
    } else {
      btn.classList.toggle("active", mode === code);
    }
  });
}

// ════════════════════════════════════════════════════════════════
//  TIMER
// ════════════════════════════════════════════════════════════════
function fmtTime(s) {
  var h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sc = s%60;
  return (h<10?"0":"") + h + ":" + (m<10?"0":"") + m + ":" + (sc<10?"0":"") + sc;
}

function startTimer(secs) {
  clearInterval(timerInt); timerInt = null;
  totalTimeLeft = secs;
  document.getElementById("timerVal").textContent = fmtTime(totalTimeLeft);
  document.getElementById("timerVal").classList.remove("low");
  timerInt = setInterval(function() {
    if (submitted) { clearInterval(timerInt); return; }
    totalTimeLeft--;
    if (totalTimeLeft <= 0) {
      clearInterval(timerInt);
      document.getElementById("timerVal").textContent = fmtTime(0);
      autoAdvance();
      return;
    }
    document.getElementById("timerVal").textContent = fmtTime(totalTimeLeft);
    document.getElementById("timerVal").classList.toggle("low", totalTimeLeft <= 300);
  }, 1000);
}

function autoAdvance() {
  if (!SECTIONAL) {
    // Non-sectional: single timer expired — submit whole exam
    submitExam();
    return;
  }
  lockSection(activeSec);
  var next = activeSec + 1;
  if (next >= SECTIONS.length) submitExam();
  else startSection(next, true);
}

// ════════════════════════════════════════════════════════════════
//  SUBMIT
// ════════════════════════════════════════════════════════════════
function openSubmit() {
  updateSummaryBar();
  document.getElementById("submitModal").classList.add("show");
}
function closeModal() { document.getElementById("submitModal").classList.remove("show"); }

function submitExam() {
  clearInterval(timerInt);
  closeModal();
  submitted = true;
  document.body.classList.add("submitted");
  for (var qn = 1; qn <= TOTAL; qn++) {
    var ca = CORRECT_MAP[String(qn)];
    var ua = answers[qn];
    if (ca !== undefined) {
      var cel = document.getElementById("opt-" + qn + "-" + ca);
      if (cel) { cel.classList.remove("selected"); cel.classList.add("submitted-correct"); }
    }
    if (ua !== undefined && ca !== undefined && ua !== ca) {
      var wel = document.getElementById("opt-" + qn + "-" + ua);
      if (wel) { wel.classList.remove("selected"); wel.classList.add("submitted-wrong"); }
    }
  }
  showScore();
}

// ════════════════════════════════════════════════════════════════
//  SCORE
// ════════════════════════════════════════════════════════════════
function showScore() {
  var totalRight=0, totalWrong=0, totalSkip=0;
  var tbody = "";
  for (var si = 0; si < SECTIONS.length; si++) {
    var sec = SECTIONS[si];
    var r=0, w=0, s=0;
    for (var q = sec.start; q <= sec.end; q++) {
      var qa = answers[q];
      var ca = CORRECT_MAP[String(q)];
      if (qa === undefined) { s++; }
      else if (ca === undefined || qa === ca) { r++; }
      else { w++; }
    }
    var _secMpq = (sec.max_score && sec.max_score > 0) ? sec.max_score / (sec.end - sec.start + 1) : MARK_RIGHT;
    var sc = r*_secMpq + w*MARK_WRONG;
    var fs = sc%1===0 ? String(sc) : sc.toFixed(2);
    totalRight+=r; totalWrong+=w; totalSkip+=s;
    tbody += "<tr><td>" + sec.name + "</td><td style='text-align:center'>" + (sec.end-sec.start+1) + "</td><td style='text-align:center;color:#2e7d32;font-weight:700'>" + r + "</td><td style='text-align:center;color:#c62828;font-weight:700'>" + w + "</td><td style='text-align:center;color:#888'>" + s + "</td><td style='text-align:center;font-weight:700'>" + fs + "</td></tr>";
  }
  // totalScore = sum of per-section scores (handles non-uniform marking)
  var totalScore = 0;
  for (var _si2 = 0; _si2 < SECTIONS.length; _si2++) {
    var _s2 = SECTIONS[_si2], _r2=0, _w2=0;
    var _mpq2 = (_s2.max_score && _s2.max_score > 0) ? _s2.max_score / (_s2.end - _s2.start + 1) : MARK_RIGHT;
    for (var _q2 = _s2.start; _q2 <= _s2.end; _q2++) {
      var _a2 = answers[_q2], _c2 = CORRECT_MAP[String(_q2)];
      if (_a2 !== undefined) { if (_c2 === undefined || _a2 === _c2) _r2++; else _w2++; }
    }
    totalScore += _r2*_mpq2 + _w2*MARK_WRONG;
  }
  var fts = totalScore%1===0 ? String(totalScore) : totalScore.toFixed(2);
  var att = totalRight + totalWrong;
  var acc = att > 0 ? Math.round(totalRight/att*100) : 0;
  document.getElementById("finalScore").textContent = fts;
  document.getElementById("scRight").textContent  = totalRight;
  document.getElementById("scWrong").textContent  = totalWrong;
  document.getElementById("scSkip").textContent   = totalSkip;
  document.getElementById("scAcc").textContent    = acc + "%";
  document.getElementById("secScoreBody").innerHTML = tbody;
  document.getElementById("scoreScreen").style.display = "block";
}

// ════════════════════════════════════════════════════════════════
//  SOLUTIONS
// ════════════════════════════════════════════════════════════════
var solFilter = "all";
var solFiltered = [];
var solIdx = 0;
var solLangMode = "en";  // persists across solution questions

function openSolutions() {
  document.getElementById("scoreScreen").style.display = "none";
  document.getElementById("solScreen").classList.add("show");
  buildFilterBar();
  setFilter("all", null);
}
function closeSolutions() {
  var sp = document.getElementById('s2-palette');
  if (sp) sp.classList.remove('sol-pal-open');
  document.getElementById("solScreen").classList.remove("show");
  document.getElementById("scoreScreen").style.display = "block";
}

function buildFilterBar() {
  // Count by section and status
  var counts = {all:0, correct:0, wrong:0, skip:0};
  var secCounts = {};
  for (var i = 0; i < SECTIONS.length; i++) secCounts[i] = 0;
  for (var qn = 1; qn <= TOTAL; qn++) {
    counts.all++;
    var ua = answers[qn], ca = CORRECT_MAP[String(qn)];
    if (ua === undefined) counts.skip++;
    else if (ca === undefined || ua === ca) counts.correct++;
    else counts.wrong++;
    for (var si = 0; si < SECTIONS.length; si++) {
      if (qn >= SECTIONS[si].start && qn <= SECTIONS[si].end) { secCounts[si]++; break; }
    }
  }

  var fb = document.getElementById("filterBar");
  fb.innerHTML = "";
  function mkFBtn(label, cls, filterVal) {
    var b = document.createElement("button");
    b.className = cls;
    b.textContent = label;
    b.onclick = (function(fv){ return function(){ setFilter(fv, b); }; })(filterVal);
    fb.appendChild(b);
    return b;
  }
  mkFBtn("All (" + counts.all + ")", "fbtn active", "all");
  for (var si = 0; si < SECTIONS.length; si++) {
    mkFBtn(SECTIONS[si].name + " (" + secCounts[si] + ")", "fbtn", si);
  }
  mkFBtn("\u2713 Correct (" + counts.correct + ")", "fbtn fc", "correct");
  mkFBtn("\u2717 Wrong (" + counts.wrong + ")", "fbtn fw", "wrong");
  mkFBtn("\u2014 Skipped (" + counts.skip + ")", "fbtn fsk", "skip");

  // Stats strip
  var strip = "<div class='stat-item'><div class='stat-dot' style='background:#27ae60'></div><b>" + counts.correct + "</b>&nbsp;Correct</div>";
  strip += "<div class='stat-item'><div class='stat-dot' style='background:#e74c3c'></div><b>" + counts.wrong + "</b>&nbsp;Wrong</div>";
  strip += "<div class='stat-item'><div class='stat-dot' style='background:#b0b0b0'></div><b>" + counts.skip + "</b>&nbsp;Skipped</div>";

  var totalScore2 = 0;
  for (var qn = 1; qn <= TOTAL; qn++) {
    var ua2 = answers[qn], ca2 = CORRECT_MAP[String(qn)];
    if (ua2 !== undefined) {
      if (ca2 === undefined || ua2 === ca2) totalScore2 += MARK_RIGHT;
      else totalScore2 += MARK_WRONG;
    }
  }
  var fts2 = totalScore2%1===0 ? String(totalScore2) : totalScore2.toFixed(2);
  strip += "<div class='stat-item' style='margin-left:auto;color:#1a3a5c;font-weight:700'>Score: " + fts2 + " / " + TOTAL + "</div>";
  document.getElementById("statsStrip").innerHTML = strip;
}

function setFilter(f, btn) {
  solFilter = f;
  if (btn) {
    document.querySelectorAll(".fbtn").forEach(function(b){ b.classList.remove("active"); });
    btn.classList.add("active");
  }
  renderSolList();
}

function getFiltered() {
  var result = [];
  for (var qn = 1; qn <= TOTAL; qn++) {
    var ua = answers[qn], ca = CORRECT_MAP[String(qn)];
    var st;
    if (ua === undefined) st = "skip";
    else if (ca === undefined || ua === ca) st = "correct";
    else st = "wrong";
    var si2 = getSectionIdx(qn);
    var qd = {n: qn, sec: SECTIONS[si2] ? SECTIONS[si2].name : "", _st: st};
    if (solFilter === "all") { result.push(qd); continue; }
    if (typeof solFilter === "number" && si2 === solFilter) { result.push(qd); continue; }
    if (solFilter === "correct" && st === "correct") { result.push(qd); continue; }
    if (solFilter === "wrong"   && st === "wrong")   { result.push(qd); continue; }
    if (solFilter === "skip"    && st === "skip")    { result.push(qd); continue; }
  }
  return result;
}

function renderSolList() {
  solFiltered = getFiltered();
  var el = document.getElementById("solQlist");
  if (!solFiltered.length) {
    el.innerHTML = "<div style='text-align:center;padding:60px 20px;color:#bbb'><p style='font-size:14px;color:#999'>No questions in this filter.</p></div>";
    return;
  }
  var html = "";
  for (var i = 0; i < solFiltered.length; i++) {
    var qd = solFiltered[i];
    var sc2 = qd._st==="correct" ? "sb-c" : qd._st==="wrong" ? "sb-w" : "sb-s";
    var st2 = qd._st==="correct" ? "Correct \u2713" : qd._st==="wrong" ? "Wrong \u2717" : "Skipped \u2014";
    // Get preview text from DOM instead of QDATA
    var qblock = document.getElementById("qblock-" + qd.n);
    var qEnEl = qblock ? qblock.querySelector(".q-text-en") : null;
    var preview = qEnEl ? qEnEl.textContent : "";
    var short = preview.length > 100 ? preview.slice(0,100) + "\u2026" : preview;
    html += "<div class='qcard' onclick='openSolQ(" + i + ")'>";
    html += "<div class='qcard-hdr'><div><span class='qcard-num'>Q" + qd.n + "</span><span class='qcard-sec'>" + qd.sec + "</span></div><span class='status-badge " + sc2 + "'>" + st2 + "</span></div>";
    html += "<div class='qcard-preview'>" + short + "</div>";
    html += "<div class='qcard-footer'>Tap to view options &amp; solution \u2192</div>";
    html += "</div>";
  }
  el.innerHTML = html;
}

function openSolQ(idx) {
  solIdx = idx;
  renderSolDetail();
  document.getElementById("sol-s1").style.display = "none";
  document.getElementById("sol-s2").classList.add("show");
}

function showSolList() {
  var sp = document.getElementById('s2-palette');
  if (sp) sp.classList.remove('sol-pal-open');
  document.getElementById("sol-s2").classList.remove("show");
  document.getElementById("sol-s1").style.display = "flex";
}

function navSol(dir) {
  var next = solIdx + dir;
  if (next < 0 || next >= solFiltered.length) return;
  solIdx = next;
  renderSolDetail();
  document.getElementById("s2-body").scrollTop = 0;
}

function setSolLang(mode) {
  solLangMode = mode;
  var body = document.getElementById("s2-body");
  if (!body) return;

  // Clear legacy s2-en / s2-hi classes — JS alone controls display now so
  // a stale class from a previous render can't fight inline display values.
  body.classList.remove("s2-en", "s2-hi");
  var isEnMode = (mode === "en" || mode === "both");

  // ── Passages (.q-passage.lang-XX): swap with the selected language.
  //    Supports arbitrary lang codes (lang-en / lang-hi / lang-hn / ...).
  //    Falls back to lang-en when the selected non-English language has
  //    no matching passage in this question.
  var allPasses = body.querySelectorAll(".q-passage");
  var passHasMatch = false;
  if (!isEnMode && mode !== "both") {
    for (var pi = 0; pi < allPasses.length; pi++) {
      if (allPasses[pi].classList.contains("lang-" + mode) &&
          allPasses[pi].textContent.trim() !== "") {
        passHasMatch = true; break;
      }
    }
  }
  allPasses.forEach(function(pEl) {
    var cls = pEl.classList;
    var hasAnyLang = false;
    cls.forEach(function(c) { if (c.indexOf("lang-") === 0) hasAnyLang = true; });
    var isEn = cls.contains("lang-en") || !hasAnyLang;
    var isMode = cls.contains("lang-" + mode);
    var show = (mode === "both") ? true
             : (isEnMode ? isEn
                        : (passHasMatch ? isMode : isEn));
    pEl.style.display = show ? "block" : "none";
  });

  // ── Per-question fallback (Testbook multi-lang): if the selected language
  // has no extra content for this question, fall back to English so the
  // question/options/solution never appear blank.
  var qWrappers = body.querySelectorAll(".s2-q-wrap");
  // If there is no explicit wrapper, treat the whole #s2-body as one group.
  var groups = qWrappers.length ? qWrappers : [body];
  groups.forEach(function(g) {
    var enQ   = g.querySelector(".s2-qtext-en");
    var enOpts= g.querySelectorAll(".s2-opt-en");
    var qExtras = g.querySelectorAll(".s2-qtext-extra");
    var oExtras = g.querySelectorAll(".s2-opt-extra");

    // Decide per-group fallback: selected mode has non-empty question OR
    // any non-empty option in that language.
    var matchQ = null;
    if (!isEnMode) {
      for (var i = 0; i < qExtras.length; i++) {
        if (qExtras[i].getAttribute("data-lang") === mode &&
            qExtras[i].textContent.trim() !== "") {
          matchQ = qExtras[i]; break;
        }
      }
    }
    var hasExtraOpt = false;
    if (!isEnMode) {
      for (var j = 0; j < oExtras.length; j++) {
        if (oExtras[j].getAttribute("data-lang") === mode &&
            oExtras[j].textContent.trim() !== "") {
          hasExtraOpt = true; break;
        }
      }
    }
    // Legacy OB/SK/Guidely Hindi (.s2-qtext-hi / .s2-opt-hi with no data-lang)
    // must also block the fallback — otherwise selecting Hindi shows English
    // AND Hindi simultaneously (the "shows twice" bug).
    var legacyHiQEl = g.querySelector(".s2-qtext-hi:not(.s2-qtext-en)");
    var hasLegacyHiQ = !!(legacyHiQEl && legacyHiQEl.textContent.trim() !== "");
    var hasLegacyHiOpt = false;
    var legacyHiOptEls = g.querySelectorAll(".s2-opt-hi:not(.s2-opt-en)");
    for (var lhi = 0; lhi < legacyHiOptEls.length; lhi++) {
      if (legacyHiOptEls[lhi].textContent.trim() !== "") {
        hasLegacyHiOpt = true; break;
      }
    }
    // Separate fallback for question vs options: a question with no
    // Hindi text must fall back to English even if its options do have
    // Hindi (otherwise the user sees blank question body in hi mode).
    var qFallbackToEn = !isEnMode && !matchQ
                        && !(mode === "hi" && hasLegacyHiQ);

    // Question text
    if (enQ) enQ.style.display = (isEnMode || qFallbackToEn
                                 || enQ.classList.contains("s2-qtext-hi")) ? "" : "none";
    g.querySelectorAll(".s2-qtext-hi:not(.s2-qtext-en)").forEach(function(el) {
      el.style.display = (mode === "hi" || mode === "both") ? "" : "none";
    });
    qExtras.forEach(function(el) {
      var c = el.getAttribute("data-lang");
      el.style.display = (mode === "both" || mode === c) ? "" : "none";
    });

    // Options — per-row fallback so rows that lack Hindi still show English
    enOpts.forEach(function(el) {
      var row = el.closest(".s2-opt") || el.parentElement;
      var rowLegacyHi = row ? row.querySelector(".s2-opt-hi:not(.s2-opt-en)") : null;
      var rowHasLegacyHi = !!(rowLegacyHi && rowLegacyHi.textContent.trim() !== "");
      var rowExtraMatch = false;
      if (row && !isEnMode) {
        var rExtras = row.querySelectorAll(".s2-opt-extra");
        for (var re = 0; re < rExtras.length; re++) {
          if (rExtras[re].getAttribute("data-lang") === mode &&
              rExtras[re].textContent.trim() !== "") { rowExtraMatch = true; break; }
        }
      }
      var rowFallback = !isEnMode && !rowExtraMatch
                        && !(mode === "hi" && rowHasLegacyHi);
      el.style.display = (isEnMode || rowFallback
                         || el.classList.contains("s2-opt-hi")) ? "" : "none";
    });
    g.querySelectorAll(".s2-opt-hi:not(.s2-opt-en)").forEach(function(el) {
      el.style.display = (mode === "hi" || mode === "both") ? "" : "none";
    });
    oExtras.forEach(function(el) {
      var c = el.getAttribute("data-lang");
      el.style.display = (mode === "both" || mode === c) ? "" : "none";
    });
  });

  // ── Inline solutions: per sol-box fallback to English when needed.
  body.querySelectorAll(".sol-box .sol-body").forEach(function(sb) {
    var enEl = sb.querySelector(".sol-lang-en[data-lang]");
    var extras = sb.querySelectorAll(".sol-lang-extra");
    var legacyEn = sb.querySelector(".sol-lang-en:not([data-lang])");
    var legacyHi = sb.querySelector(".sol-lang-hi");
    var match = null;
    if (!isEnMode) {
      for (var i = 0; i < extras.length; i++) {
        if (extras[i].getAttribute("data-lang") === mode &&
            extras[i].textContent.trim() !== "") {
          match = extras[i]; break;
        }
      }
    }
    var legacyHiHasContent = !!(legacyHi && legacyHi.textContent.trim() !== "");
    var fallbackToEn = !isEnMode && !match && !(mode === "hi" && legacyHiHasContent);
    if (enEl) enEl.style.display = (isEnMode || fallbackToEn) ? "" : "none";
    extras.forEach(function(el) {
      var c = el.getAttribute("data-lang");
      el.style.display = (mode === "both" || mode === c) ? "" : "none";
    });
    // Legacy OB/SK bilingual solution — .sol-lang-hi has CSS default display:none,
    // must use "block" to show.
    if (legacyEn) legacyEn.style.display = (mode === "en" || mode === "both" || (fallbackToEn && !legacyHiHasContent)) ? "" : "none";
    if (legacyHi) legacyHi.style.display = (mode === "hi" || mode === "both") ? "block" : "none";
  });

  // Update lang bar buttons
  var bar = document.getElementById("s2-langbar");
  if (!bar) return;
  bar.querySelectorAll(".lang-toggle-btn").forEach(function(btn) {
    var code = btn.getAttribute("data-lang");
    if (!code) {
      btn.classList.toggle("active",
        (btn.textContent === "Bilingual" && mode === "both") ||
        (btn.textContent === "English"   && mode === "en") ||
        (btn.textContent === "English only" && mode === "en"));
    } else {
      btn.classList.toggle("active", mode === code);
    }
  });
}

function renderSolDetail() {
  var qd = solFiltered[solIdx];
  var ua3 = answers[qd.n];
  var ca3 = CORRECT_MAP[String(qd.n)];
  var st3 = qd._st || "skip";

  document.getElementById("s2QNum").textContent = "Q" + qd.n;
  document.getElementById("s2QSec").textContent = qd.sec;
  var stEl = document.getElementById("s2QStatus");
  if (st3==="correct") { stEl.className="s2-status s2-st-c"; stEl.textContent="Correct \u2713"; }
  else if (st3==="wrong") { stEl.className="s2-status s2-st-w"; stEl.textContent="Wrong \u2717"; }
  else { stEl.className="s2-status s2-st-s"; stEl.textContent="Skipped \u2014"; }

  // ── Read all content from the question DOM block ──────────────
  var qblock = document.getElementById("qblock-" + qd.n);
  var ca_1based = qblock ? parseInt(qblock.getAttribute("data-ca")) : -1;

  var qBiEl      = qblock ? qblock.querySelector(".q-bilingual") : null;
  var qPassageEls = qBiEl ? qBiEl.querySelectorAll(".q-passage") : [];
  var qEnEl      = qBiEl ? qBiEl.querySelector(".q-text-en") : null;
  var qHiEl      = qBiEl ? qBiEl.querySelector(".q-text-hi") : null;

  // Collect extra lang question divs (Testbook)
  var qExtraEls = qBiEl ? qBiEl.querySelectorAll(".q-text-extra[data-lang]") : [];
  var extraLangMap = {};  // {code: {label, qHtml}}
  for (var ei = 0; ei < qExtraEls.length; ei++) {
    var code = qExtraEls[ei].getAttribute("data-lang");
    extraLangMap[code] = { qHtml: qExtraEls[ei].innerHTML };
  }

  // Collect labels from SEC_EXTRA_LANGS for the current section
  var si = qblock ? parseInt(qblock.getAttribute("data-si")) : 0;
  var secExtra = (SEC_EXTRA_LANGS[String(si)] || SEC_EXTRA_LANGS[si]) || {};
  Object.keys(secExtra).forEach(function(code) {
    if (!extraLangMap[code]) extraLangMap[code] = { qHtml: "" };
    extraLangMap[code].label = secExtra[code];
  });

  var passageHtml = "";
  for (var pi = 0; pi < qPassageEls.length; pi++) {
    passageHtml += qPassageEls[pi].outerHTML;
  }
  var qEnHtml  = qEnEl  ? qEnEl.innerHTML  : "";
  var qHiHtml  = qHiEl  ? qHiEl.innerHTML  : "";
  var hasHindi = !!(qHiHtml && qHiHtml.trim());
  var hasExtra = Object.keys(extraLangMap).length > 0;
  // Section-level Hindi availability: keeps the lang bar consistent across
  // all questions in a bilingual section, even when a specific question's
  // Hindi text equals English (filtered out by the fetcher) — options/solution
  // may still have Hindi, and the user expects the toggle to stay visible.
  var secHasHindi = !!(SEC_HINDI[String(si)] || SEC_HINDI[si]);

  var isPassage   = qPassageEls.length > 0;
  var passHeader  = isPassage ? "<b class='s2-pass-hdr'>DIRECTIONS / PASSAGE</b>" : "";

  // Build question text block. Passages are rendered at the top as
  // siblings of s2-qtext-* so setSolLang can toggle each lang variant
  // independently. Embedding passageHtml INSIDE each s2-qtext-* would
  // cause double-passage (both lang-en and lang-hi visible) when the
  // active text block is shown.
  var qText = passageHtml + passHeader
            + "<div class='s2-qtext-en'>" + qEnHtml + "</div>";
  if (hasExtra) {
    Object.keys(extraLangMap).forEach(function(code) {
      var html = extraLangMap[code].qHtml || "";
      if (html.trim()) {
        qText += "<div class='s2-qtext-extra' data-lang='" + code + "'>" + html + "</div>";
      }
    });
  } else if (hasHindi) {
    qText += "<div class='s2-qtext-hi'>" + qHiHtml + "</div>";
  }

  // Build s2-langbar dynamically
  var langBar = document.getElementById("s2-langbar");
  if (langBar) {
    if (hasExtra) {
      langBar.style.display = "";
      langBar.innerHTML = '<span style="font-size:11px;color:#666;margin-right:4px">View in:</span>';
      // Solution tab: no Bilingual — only single-language buttons.
      // Normalize "both" → "en" so the English button appears highlighted.
      if (solLangMode === "both") { solLangMode = "en"; }
      // English
      var enBtnS = document.createElement("button");
      enBtnS.className = "lang-toggle-btn" + (solLangMode === "en" ? " active" : "");
      enBtnS.textContent = "English";
      enBtnS.onclick = function() { setSolLang("en"); };
      langBar.appendChild(enBtnS);
      // One per extra lang
      Object.keys(extraLangMap).forEach(function(code) {
        var label = extraLangMap[code].label || code;
        var btn = document.createElement("button");
        btn.className = "lang-toggle-btn" + (solLangMode === code ? " active" : "");
        btn.textContent = label;
        btn.setAttribute("data-lang", code);
        btn.onclick = (function(c) { return function() { setSolLang(c); }; })(code);
        langBar.appendChild(btn);
      });
    } else if (secHasHindi) {
      langBar.style.display = "";
      langBar.innerHTML =
        '<span style="font-size:11px;color:#666;margin-right:4px">View in:</span>' +
        '<button class="lang-toggle-btn' + (solLangMode === "en" ? " active" : "") + '" data-lang="en" onclick="setSolLang(&quot;en&quot;)">English only</button>' +
        '<button class="lang-toggle-btn' + (solLangMode === "hi" ? " active" : "") + '" data-lang="hi" onclick="setSolLang(&quot;hi&quot;)">\u0939\u093f\u0902\u0926\u0940 only</button>';
    } else {
      langBar.style.display = "none";
    }
  }

  // Skipped note
  var skipNote = ua3 === undefined ? "<div class='skip-note'>\u2014 You did not attempt this question.</div>" : "";

  // Options — read from DOM, support opt-text-extra
  var optEls = qblock ? qblock.querySelectorAll(".opt-row") : [];
  var optsHtml = "";
  for (var i = 0; i < optEls.length; i++) {
    var optEl   = optEls[i];
    var lblEl   = optEl.querySelector(".opt-lbl");
    var enEl    = optEl.querySelector(".opt-text-en");
    var hiEl    = optEl.querySelector(".opt-text-hi:not(.opt-text-en)");
    var extraOptEls = optEl.querySelectorAll(".opt-text-extra[data-lang]");

    var lbl2     = lblEl ? lblEl.textContent.replace(".","").trim() : String.fromCharCode(65 + i);
    var enHtml2  = enEl  ? enEl.innerHTML : "";
    var hiHtml2  = hiEl  ? hiEl.innerHTML : "";
    var hasOptHi = !!(hiHtml2 && hiHtml2.trim());

    var optIdx1based = i + 1;
    var isCorrect    = (optIdx1based === ca_1based);
    var isUserWrong  = (ua3 !== undefined && ua3 === optIdx1based && !isCorrect);

    var cls3 = "s2-opt";
    var tag3 = "";
    if (isCorrect)    { cls3 += " opt-correct"; tag3 = "<span class='opt-tag tag-correct'>Correct Answer</span>"; }
    else if (isUserWrong) { cls3 += " opt-wrong"; tag3 = "<span class='opt-tag tag-yours'>Your Answer</span>"; }

    var optContent;
    if (extraOptEls.length > 0) {
      // Testbook multi-lang options
      optContent = "<div class='s2-opt-en'>" + enHtml2 + tag3 + "</div>";
      for (var ej = 0; ej < extraOptEls.length; ej++) {
        var eCode  = extraOptEls[ej].getAttribute("data-lang");
        var eHtml  = extraOptEls[ej].innerHTML;
        if (eHtml.trim()) {
          optContent += "<div class='s2-opt-extra' data-lang='" + eCode + "'>" + eHtml + "</div>";
        }
      }
    } else if (hasOptHi) {
      optContent = "<div class='s2-opt-en'>" + enHtml2 + tag3 + "</div>" +
                   "<div class='s2-opt-hi'>" + hiHtml2 + "</div>";
    } else {
      optContent = "<div class='s2-opt-en s2-opt-hi'>" + enHtml2 + tag3 + "</div>";
    }
    optsHtml += "<div class='" + cls3 + "'>" +
                "<div class='radio-wrap'><div class='radio-fill'></div></div>" +
                "<span class='opt-label'>" + _html_esc(lbl2) + ".</span>" +
                "<div class='opt-content'>" + optContent + "</div></div>";
  }

  // Solution — read from embedded sol-box in qblock (already contains sol-lang-en/extra divs)
  var solBodyEl = qblock ? qblock.querySelector(".sol-box .sol-body") : null;
  var solHtml = solBodyEl
    ? "<div class='sol-box'><div class='sol-hdr'>&#128161; Solution</div><div class='sol-body'>" + solBodyEl.innerHTML + "</div></div>"
    : "";

  var s2body = document.getElementById("s2-body");
  s2body.innerHTML =
    qText +
    "<div class='opts-heading'>Options</div>" +
    skipNote +
    "<div class='s2-opts'>" + optsHtml + "</div>" +
    solHtml;

  // Apply persistent lang mode
  setSolLang(solLangMode);

  document.getElementById("navInfo").textContent = (solIdx+1) + " of " + solFiltered.length;
  document.getElementById("prevBtn").disabled = (solIdx === 0);
  document.getElementById("nextBtn").disabled = (solIdx === solFiltered.length - 1);

  renderSolPalette(qd.n);
}

function _html_esc(s) {
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

function renderSolPalette(activeN) {
  var filteredNums = {};
  for (var i = 0; i < solFiltered.length; i++) filteredNums[solFiltered[i].n] = i;

  // Auto grid columns + cell size based on total questions
  // Larger tests need more columns + smaller cells to fit in bottom sheet
  // Desktop: always 5 cols (matches old behaviour); Mobile: auto-scale by total
  var isMobile = window.innerWidth <= 600;
  var cols = isMobile ? (TOTAL > 150 ? 10 : TOTAL > 100 ? 9 : TOTAL > 50 ? 8 : 7) : 5;
  var cellSize = TOTAL > 150 ? "26px" : TOTAL > 100 ? "28px" : TOTAL > 50 ? "30px" : "32px";
  var fontSize = TOTAL > 100 ? "8px" : "9px";

  // Group by section
  var html2 = "";
  for (var si = 0; si < SECTIONS.length; si++) {
    var sec = SECTIONS[si];
    html2 += "<div class='pal-sec-lbl'>" + sec.name + "</div>";
    html2 += "<div class='pal-grid2' style='grid-template-columns:repeat(" + cols + ",1fr);gap:2px;margin-bottom:8px'>";
    for (var q = sec.start; q <= sec.end; q++) {
      var ua4 = answers[q], ca4 = CORRECT_MAP[String(q)];
      var sc4;
      if (ua4 === undefined) sc4 = "pq-s";
      else if (ca4 === undefined || ua4 === ca4) sc4 = "pq-c";
      else sc4 = "pq-w";
      var cur2 = q === activeN ? " pq-cur" : "";
      var inFilt = filteredNums[q] !== undefined;
      var dim2 = inFilt ? "" : " dimmed";
      var clickAttr = inFilt ? "onclick='openSolQ(" + filteredNums[q] + ")'" : "";
      html2 += "<div class='pq2 " + sc4 + cur2 + dim2 + "' " + clickAttr +
               " style='width:100%;height:" + cellSize + ";font-size:" + fontSize + ";border-radius:50%'" +
               " title='Q" + q + "'>" + q + "</div>";
    }
    html2 += "</div>";
  }
  html2 += "<div class='pal-legend2'><div class='leg-row'><div class='leg-dot c'></div>Correct</div><div class='leg-row'><div class='leg-dot w'></div>Wrong</div><div class='leg-row'><div class='leg-dot s'></div>Skipped</div></div>";
  document.getElementById("palBody").innerHTML = html2;
}

// ════════════════════════════════════════════════════════════════
//  PALETTE COLLAPSE
// ════════════════════════════════════════════════════════════════
function togglePalette() {
  paletteCollapsed = !paletteCollapsed;
  var pw = document.getElementById("palette-wrap");
  var tog = document.getElementById("pal-toggle");
  if (paletteCollapsed) {
    pw.style.width = "14px"; pw.style.overflow = "hidden";
    tog.textContent = "\u25B6";
  } else {
    pw.style.width = "220px"; pw.style.overflow = "";
    tog.textContent = "\u25C4";
  }
}

// ════════════════════════════════════════════════════════════════
//  PRE-TEST SCREENS
// ════════════════════════════════════════════════════════════════
function showNotice() {
  document.getElementById("instrScreen").style.display = "none";
  document.getElementById("noticeScreen").style.display = "flex";
}

function showInstructions() {
  document.getElementById("noticeScreen").style.display = "none";
  // Populate stats from JS data vars
  document.getElementById("is-total").textContent = TOTAL;
  var totalMins = Math.round(TOTAL_SECS_INIT / 60);
  document.getElementById("is-time").textContent = totalMins + " min";
  document.getElementById("is-right").textContent = "+" + MARK_RIGHT;
  document.getElementById("is-wrong").textContent = MARK_WRONG;
  document.getElementById("is-secs").textContent = SECTIONS.length;
  var _totalMarks = SECTIONS.reduce(function(a, s) { return a + (s.max_score || 0); }, 0);
  document.getElementById("is-marks").textContent = _totalMarks;
  // Populate exam title
  var titleEl = document.getElementById("pre-exam-title");
  if (titleEl) titleEl.textContent = document.title;
  // Build section table
  var tbody = document.getElementById("is-sec-body");
  tbody.innerHTML = "";
  for (var i = 0; i < SECTIONS.length; i++) {
    var s = SECTIONS[i];
    var qcount = s.end - s.start + 1;
    var mins = Math.round(s.secs / 60);
    var tr = document.createElement("tr");
    tr.innerHTML = "<td style='text-align:center;font-weight:700;color:#1c2333'>" + (i+1) + "</td>" +
                   "<td>" + s.name + "</td>" +
                   "<td style='text-align:center'>" + qcount + "</td>" +
                   "<td style='text-align:center'>" + (mins > 0 ? mins + " min" : "—") + "</td>" +
                   "<td style='text-align:center;font-weight:700;color:#1c2333'>" + (s.max_score || qcount) + "</td>";
    tbody.appendChild(tr);
  }
  document.getElementById("instrScreen").style.display = "flex";
}

function startTest() {
  document.getElementById("instrScreen").style.display = "none";
  startSection(0, false);
}


// ════════════════════════════════════════════════════════════════
//  MOBILE: SWIPE NAVIGATION — test screen + solution screen
//  Swipe RIGHT → previous | Swipe LEFT → next
//  Min 50px horizontal | Ignores vertical-dominant (scrolling)
//  touchstart/touchend = touch devices only, never fires on desktop
// ════════════════════════════════════════════════════════════════
function _attachSwipe(el, onLeft, onRight) {
  if (!el) return;
  var _tx = 0, _ty = 0, _sw = false;
  el.addEventListener('touchstart', function(e) {
    var t = e.changedTouches[0];
    _tx = t.clientX; _ty = t.clientY; _sw = true;
  }, { passive: true });
  el.addEventListener('touchend', function(e) {
    if (!_sw) return; _sw = false;
    var t = e.changedTouches[0];
    var dx = t.clientX - _tx;
    var dy = t.clientY - _ty;
    if (Math.abs(dy) > Math.abs(dx)) return;
    if (Math.abs(dx) < 50) return;
    if (dx < 0) onLeft(); else onRight();
  }, { passive: true });
}

// Test screen — qarea
// Swipe respects section boundaries:
// - Cannot swipe INTO a future section (not yet started)
// - Cannot swipe BACK into a done/submitted section
// Silently clamps to current section edges — no alert, just stops.
_attachSwipe(
  document.getElementById('qarea'),
  function() {
    // Swipe LEFT = next question
    var target = currentQ + 1;
    if (target > TOTAL) return;
    var targetSec = getSectionIdx(target);
    // Block if target is in a future section that hasn't started yet
    if (SECTIONAL && targetSec > activeSec) return;
    // Block if target is in a done (submitted) section
    if (SECTIONAL && doneSecs[targetSec] && targetSec !== activeSec) return;
    goQ(target);
  },
  function() {
    // Swipe RIGHT = previous question
    var target = currentQ - 1;
    if (target < 1) return;
    var targetSec = getSectionIdx(target);
    // Block if target is in a done (submitted) section
    if (SECTIONAL && doneSecs[targetSec]) return;
    // Block if target is in a future locked section
    if (SECTIONAL && targetSec > activeSec) return;
    goQ(target);
  }
);

// Solution detail screen — s2-body (dynamically populated, attach once)
_attachSwipe(
  document.getElementById('s2-body'),
  function() { navSol(1); },
  function() { navSol(-1); }
);

// ════════════════════════════════════════════════════════════════
//  MOBILE: PALETTE BOTTOM SHEET TOGGLE — test screen
// ════════════════════════════════════════════════════════════════
function toggleMobPalette() {
  var pw = document.getElementById('palette-wrap');
  pw.classList.toggle('mob-open');
}
// Close test palette when tapping outside
document.getElementById('qarea').addEventListener('click', function() {
  var pw = document.getElementById('palette-wrap');
  if (pw.classList.contains('mob-open')) pw.classList.remove('mob-open');
});

// ════════════════════════════════════════════════════════════════
//  MOBILE: SOLUTION PALETTE BOTTOM SHEET
// ════════════════════════════════════════════════════════════════
function toggleSolPalette() {
  var sp = document.getElementById('s2-palette');
  if (sp) sp.classList.toggle('sol-pal-open');
}
// solPalBtn visibility handled by pure CSS (#sol-s2.show #solPalBtn)

// ════════════════════════════════════════════════════════════════
//  MOBILE: SHOW SWIPE HINT ONCE (first visit only)
// ════════════════════════════════════════════════════════════════
(function() {
  var hint = document.getElementById('swipeHint');
  if (!hint) return;
  var shown = false;
  // Show hint 1.5s after test starts, hide after 2.5s — only once
  var _origStart = window.startTest;
  window.startTest = function() {
    _origStart();
    if (!shown && window.innerWidth <= 600) {
      shown = true;
      setTimeout(function() {
        hint.style.display = 'block';
        setTimeout(function() { hint.style.display = 'none'; }, 2500);
      }, 1500);
    }
  };
})();
// ════════════════════════════════════════════════════════════════
//  INIT — show notice screen first (test starts only on "Start Test")
// ════════════════════════════════════════════════════════════════
// (startSection called inside startTest(), not on page load)
</script>
</body>
</html>"""
