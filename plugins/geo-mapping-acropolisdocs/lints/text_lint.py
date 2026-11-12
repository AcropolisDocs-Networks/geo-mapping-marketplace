#!/usr/bin/env python3
"""text_lint: the words on the map (0.21.0; docs/specs/map-text-spec.md, Draft 2, owner decisions 2026-11-08).

The title names the subject; the subtitle adds the one thing a reader needs next; every other fact goes to the element that owns it (spec §3).
MAP-TEXT-001 to 012 never block a build or a deliverable (D3): each is a WARN or INFO with the text it found and one suggested fix, and every
WARN is listed in the chat build summary (`summary_lines`). Supplied text is drawn as written; only the title the plugin makes up when the
MapSpec gives none is tidied (`tidy_title`). MAP-CART-022 (title over 90 characters) is superseded by MAP-TEXT-001.
"""
from __future__ import annotations

import re

FILLER = ("a map showing", "a map of", "map showing", "map of", "overview of", "an overview of", "analysis of", "an analysis of", "sample", "draft")
METHOD_WORDS = ("buffer", "buffered", "isochrone", "isochrones", "kernel", "voronoi", "hexbin", "kriging", "idw", "interpolated", "interpolation",
                "heatmap", "geocoded", "spatial join")
SOURCE_WORDS = ("census bureau", "us census", "u.s. census", "usgs", "openstreetmap", "osm", "natural earth", "tiger/line", "overture", "fcc",
                "noaa", "esri", "mapbox", "carto", "valhalla", "openrouteservice")
NARRATION = (" over ", "shown ", " shown", "showing ", "colored by", "coloured by", "symbolized", "symbolised", "drawn ", "by census tract",
             "every building", " layer", "layered")
SERIES = (r"#\w+", r"\b30\s*day\s*map\s*challenge\b", r"\bday\s*0?\d{1,2}\b", r"\bmap produced\b", r"\breport\s+\d+\b")
MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
MONTHS_SHORT = "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
YEAR = r"(19|20)\d\d"                     # no counted braces: the edition build reads doubled braces as template fields
DATE_RX = re.compile(r"\b" + YEAR + r"\b|\bQ[1-4]\b|\b(" + MONTHS + "|" + MONTHS_SHORT + r")\.?\s+" + YEAR + r"\b|\b\d\d?/\d\d(\d\d)?\b", re.I)
MINOR = {"a", "an", "the", "and", "but", "or", "nor", "for", "so", "yet", "at", "by", "in", "of", "on", "to", "up", "as", "off", "per", "via"}
LOOSE = {"from", "with", "into", "over", "onto", "near", "upon"}   # four-letter prepositions: lower case when written (the spec's own "from Link"),
                                                                   # capitalized is accepted too (AP capitalizes words of four letters or more)
STOP = MINOR | LOOSE | {"is", "are", "was", "were", "be", "has", "have", "its", "it", "this", "that", "than", "within", "most", "more", "less", "all",
                "each", "per", "who", "which", "what", "where", "live", "lives"}
TITLE_WARN, TITLE_INFO, SUB_WARN, SUB_INFO = 60, 45, 80, 60
# names a sentence-case subtitle keeps capitalized (MAP-TEXT-010 counts the other capitals): US states, Canadian provinces, months, days
NAMES = {w for n in ("Alabama Alaska Arizona Arkansas California Colorado Connecticut Delaware Florida Georgia Hawaii Idaho Illinois Indiana "
                     "Iowa Kansas Kentucky Louisiana Maine Maryland Massachusetts Michigan Minnesota Mississippi Missouri Montana Nebraska Nevada "
                     "New Hampshire Jersey Mexico York North South Carolina Dakota Ohio Oklahoma Oregon Pennsylvania Rhode Island Tennessee Texas "
                     "Utah Vermont Virginia Washington West Wisconsin Wyoming District Columbia Puerto Rico Alberta British Manitoba Brunswick "
                     "Newfoundland Labrador Nova Scotia Ontario Prince Edward Quebec Saskatchewan Yukon Nunavut Northwest Territories "
                     "January February March April May June July August September October November December "
                     "Monday Tuesday Wednesday Thursday Friday Saturday Sunday",) for w in n.split()}


def _f(fid: str, sev: str, message: str, text: str, suggest: str, element: str) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": {"element": element, "text": text[:200], "suggest": suggest}}


def _words(s: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9'’%.-]*", s or "")


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def _content_seq(s: str) -> list[tuple[str, bool, str]]:
    """The content words of a phrase in order, stemmed (walk, walks, walking are one word), each with whether it modifies the next word
    (followed directly by another content word with no stop word or comma between: "public" in "public hotspots")."""
    toks = re.findall(r"[A-Za-z0-9][A-Za-z0-9'’%.-]*|[,;·|:()]", s or "")
    seq: list[tuple[str, bool, str]] = []
    prev_content = False
    for tok in toks:
        lw = tok.lower().strip(".'’")
        content = not (lw in STOP or len(lw) < 3 or re.fullmatch(r"[\d.,%-]+", lw) or not lw[:1].isalnum())
        if content and prev_content and seq:
            seq[-1] = (seq[-1][0], True, seq[-1][2])
        if content:
            seq.append((_stem(lw), False, lw))
        prev_content = content
    return seq


def repeated_words(title: str, subtitle: str) -> list[str]:
    """Content words the subtitle repeats from the title (MAP-TEXT-006). A word both use only as a modifier ("Public Wi-Fi Clusters" and
    "public hotspots") is not counted: the repetition the rule is after is the subject or the measure said twice."""
    ts, ss = _content_seq(title), _content_seq(subtitle)
    sub_mod: dict[str, bool] = {}
    said: dict[str, str] = {}
    for w, m, orig in ss:
        sub_mod[w] = sub_mod.get(w, True) and m
        said.setdefault(w, orig)
    out = []
    for w, m, _orig in ts:
        if w in sub_mod and not (m and sub_mod[w]) and said[w] not in out:
            out.append(said[w])
    return out


def ap_title_case(s: str) -> str:
    """AP-style Title Case: every word capitalized but short articles, conjunctions and prepositions (unless first or last); words with digits,
    acronyms and words already mixed-case (iPhone, LinkNYC) are kept as written."""
    ws = s.split()
    out = []
    for i, w in enumerate(ws):
        core = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", w)
        after_break = i > 0 and (ws[i - 1] in ("-", "–", "—") or ws[i - 1].endswith(":"))     # a word after a dash or colon starts again
        if not core or any(c.isdigit() for c in core) or (core.isupper() and len(core) > 1) or (any(c.isupper() for c in core[1:])):
            out.append(w)
        elif core.lower() in MINOR | LOOSE and 0 < i < len(ws) - 1 and not after_break:
            out.append(w.replace(core, core.lower()))
        else:                                             # each part of a hyphenated word: Drive-Test, Phone-Only, Up-to-Date
            parts = core.split("-")
            parts = [pt[:1].upper() + pt[1:] if j == 0 or pt.lower() not in MINOR else pt.lower() for j, pt in enumerate(parts)]
            out.append(w.replace(core, "-".join(parts)))
    return " ".join(out)


def is_title_case(s: str) -> bool:
    want, got = ap_title_case(s).split(), s.split()
    return all(a == b or (a.lower() in LOOSE and a.lower() == b.lower()) for a, b in zip(want, got, strict=True))


def sentence_case_off(s: str) -> bool:
    """True when a subtitle looks Title Cased: three or more words after the first start with a capital and are not acronyms or names with digits."""
    ws = _words(s)[1:]
    caps = [w for w in ws if w[:1].isupper() and not w.isupper() and not any(c.isdigit() for c in w) and w.lower() not in MINOR
            and w.strip(".,") not in NAMES]
    return len(caps) >= 3 and len(caps) >= 0.6 * max(1, len([w for w in ws if w.lower() not in MINOR]))


def sentence_case(s: str) -> str:
    """A suggested sentence-case subtitle: the first word as written; a later word that is only capitalized (Public, Hotspots) is lowered;
    acronyms, mixed-case names (LinkNYC, Wi-Fi), words with digits and the NAMES are kept."""
    ws = s.split(" ")
    out = ws[:1]
    for w in ws[1:]:
        core = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", w)
        simple = core[:1].isupper() and core[1:].islower() and core.isalpha()
        out.append(w.replace(core, core.lower()) if simple and core not in NAMES else w)
    return " ".join(out)


def all_caps(s: str) -> bool:
    letters = [w for w in _words(s) if len(w) >= 4 and w.isalpha()]
    return len(letters) >= 2 and all(w.isupper() for w in letters)


def _first_idea(s: str) -> str:
    return re.split(r"\s[-–—]\s|:\s|\s\(", s, maxsplit=1)[0].strip()


def strip_filler(s: str) -> str:
    t = s.strip()
    changed = True
    while changed:
        changed = False
        for f in sorted(FILLER, key=len, reverse=True):
            if t.lower().startswith(f + " ") or t.lower() == f:
                t = t[len(f):].strip(" :,-")
                changed = True
    return t


def tidy_title(message: str) -> str:
    """The title the plugin makes when the MapSpec gives none (spec §10, the part a script can do): the message's first sentence, cut at a dash,
    colon or parenthesis, filler, dates and hashtags stripped, Title Case, no trailing period, at most 60 characters at a word boundary. The
    map-create skill writes a subject-led title itself (§7) and the script uses this only when it does not."""
    s = (message or "").split(". ")[0].strip().rstrip(".")
    s = strip_filler(_first_idea(s))
    s = re.sub(r"#\w+", "", s)
    s = DATE_RX.sub("", s)
    s = re.sub(r"\s+(in|of|for|from|during|as of|by)\s*(,|$)", r"\2", re.sub(r"\s{2,}", " ", s)).strip(" ,;:-–")
    s = ap_title_case(s[:1].upper() + s[1:] if s else s)
    if len(s) > TITLE_WARN:
        cut = s[:TITLE_WARN].rsplit(" ", 1)[0]
        s = cut.rstrip(",;:-– ")
    return s


def title_text(spec: dict) -> str:
    """The title drawn on the map: the MapSpec's text as written, or the tidied first sentence of the message (§10)."""
    el = (spec.get("elements") or {}).get("title") or {}
    return str(el.get("text") or tidy_title(str(spec.get("message") or spec.get("id") or "")) or spec.get("id") or "")


def two_ideas(title: str) -> str | None:
    if re.search(r"\s[-–—]\s", title):
        return "a dash"
    if ":" in title:
        return "a colon"
    if "(" in title:
        return "a parenthesis"
    parts = re.split(r"\s+and\s+", title, flags=re.I)
    if len(parts) == 2 and all(len(_words(p)) >= 3 for p in parts):
        return "'and' between two clauses"
    return None


def facts(s: str) -> set[str]:
    """Checkable facts in a piece of text: numbers with a unit or %, years, quarters, month-and-year."""
    out = set()
    for m in re.finditer(r"\b\d[\d,.]*\s?(%|percent|min|minutes?|mi|km|sq mi|dBm|k)\b|\b\d[\d,.]*%", s or "", re.I):
        out.add(re.sub(r"\s+", "", m.group(0).lower()).replace("percent", "%").replace("minutes", "min").replace("minute", "min"))
    for m in DATE_RX.finditer(s or ""):
        out.add(m.group(0).lower())
    return out


def lint(title: str, subtitle: str | None = None, *, tagline: str | None = None, legend_titles: list[str] | None = None,
         table_title: str | None = None, source: str | None = None) -> list[dict]:
    """MAP-TEXT findings for the map's text. Never BLOCK (D3)."""
    out: list[dict] = []
    t = (title or "").strip()
    s = (subtitle or "").strip()
    if t:
        if len(t) > TITLE_WARN:
            out.append(_f("MAP-TEXT-001", "WARN", f"the title is {len(t)} characters (limit {TITLE_WARN}, target {TITLE_INFO})", t,
                          f"shorten to the subject: \"{tidy_title(t)}\"", "title"))
        elif len(t) > TITLE_INFO:
            out.append(_f("MAP-TEXT-001", "INFO", f"the title is {len(t)} characters (target {TITLE_INFO} or fewer)", t,
                          "cut a word or two; 3 to 7 words reads at a glance", "title"))
        why = two_ideas(t)
        if why:
            out.append(_f("MAP-TEXT-002", "WARN", f"the title joins two ideas with {why}", t,
                          f"keep one: \"{ap_title_case(_first_idea(t))}\"; the rest goes to the subtitle", "title"))
        if strip_filler(t) != t:
            out.append(_f("MAP-TEXT-003", "WARN", "the title starts with filler (map of, overview of, sample, draft)", t,
                          f"\"{ap_title_case(strip_filler(t)[:1].upper() + strip_filler(t)[1:])}\"", "title"))
        low = t.lower()
        found = [m.group(0) for m in DATE_RX.finditer(t)] + [w for w in METHOD_WORDS if re.search(rf"\b{re.escape(w)}\b", low)] \
            + [w for w in SOURCE_WORDS if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", low)]
        if found:
            out.append(_f("MAP-TEXT-004", "WARN", f"the title holds {', '.join(repr(x) for x in found[:3])} (a date, source or method word)", t,
                          "dates go to the subtitle's scope or the footer, sources to the source line, method words to the source line's method phrase",
                          "title"))
        if all_caps(t) or t.endswith(".") or not is_title_case(t.rstrip(".")):
            out.append(_f("MAP-TEXT-010", "INFO", "the title is not in Title Case" + (" (it ends with a period)" if t.endswith(".") else ""), t,
                          f"\"{ap_title_case(t.rstrip('.').lower() if all_caps(t) else t.rstrip('.'))}\"", "title"))
    if s:
        if len(s) > SUB_WARN:
            out.append(_f("MAP-TEXT-005", "WARN", f"the subtitle is {len(s)} characters (limit {SUB_WARN}, target {SUB_INFO})", s,
                          "keep one of: the key number, the scope, or the measure", "subtitle"))
        elif len(s) > SUB_INFO:
            out.append(_f("MAP-TEXT-005", "INFO", f"the subtitle is {len(s)} characters (target {SUB_INFO} or fewer)", s, "trim it to one line", "subtitle"))
        shared = repeated_words(t, s)
        if shared:
            out.append(_f("MAP-TEXT-006", "WARN", f"the subtitle repeats {', '.join(repr(w) for w in shared[:3])} from the title", s,
                          f"drop {shared[0]!r} from the subtitle, or say the next thing instead", "subtitle"))
        nar = [n.strip() for n in NARRATION if n in f" {s.lower()} "]
        if nar:
            out.append(_f("MAP-TEXT-007", "WARN", f"the subtitle narrates the drawing ({', '.join(repr(n) for n in nar[:2])})", s,
                          "say what the colour or size measures, or leave the subtitle off; the legend shows the layers", "subtitle"))
        seps = s.count(" · ")
        if seps > 1 or "|" in s or ";" in s or " / " in s or re.search(r"\w/\w+/\w", s) or re.search(r":(\s|$)", s):
            out.append(_f("MAP-TEXT-008", "INFO", "the subtitle stacks separators (pipes, slashes, semicolons, colons or more than one ' · ')", s,
                          "one ' · ' joining scope pieces at most: \"King County · Q3 2026\"", "subtitle"))
        if sentence_case_off(s) or all_caps(s):
            out.append(_f("MAP-TEXT-010", "INFO", "the subtitle is not in sentence case", s,
                          f"\"{sentence_case(s.lower() if all_caps(s) else s)}\" (keep proper names capitalized)", "subtitle"))
    for el, txt in (("title", t), ("subtitle", s)):
        hit = [m.group(0) for rx in SERIES for m in re.finditer(rx, txt or "", re.I)]
        if hit:
            out.append(_f("MAP-TEXT-011", "WARN", f"the {el} carries {hit[0]!r} (a series name, hashtag or production date)", txt,
                          "series names go in the tagline (\"#30DayMapChallenge · Day 07 · 10 Minute Map\"), the production date in the footer", el))
    # MAP-TEXT-009: one fact, two elements
    owners = [("legend title", x) for x in (legend_titles or []) if x] + [("data table", table_title or ""), ("tagline", tagline or ""),
                                                                          ("source line", source or "")]
    for el, txt in (("title", t), ("subtitle", s)):
        for f_ in sorted(facts(txt)):
            for oname, otext in owners:
                theirs = facts(otext) if oname != "source line" else {x for x in facts(otext) if not DATE_RX.fullmatch(x)}  # a product's year is not the scope
                if f_ in theirs:
                    out.append(_f("MAP-TEXT-009", "WARN", f"{f_!r} appears in the {el} and the {oname}", txt,
                                  f"say it once: drop it from the {oname}", el))
                    break
        for oname, otext in owners[:-1]:
            if txt and otext and txt.strip().lower() == otext.strip().lower():
                out.append(_f("MAP-TEXT-009", "WARN", f"the {oname} repeats the {el} word for word", txt, f"the {oname} names the measure and its unit", el))
    # MAP-TEXT-012: the same unit or date written two ways
    alltext = " ".join([t, s, table_title or ""] + list(legend_titles or []))      # not the tagline: a series theme is a name ("10 Minute Map")
    pairs = [(r"\bmin\b", r"\bminutes?\b", "'min' and 'minute'"), (r"\bmi\b", r"\bmiles?\b", "'mi' and 'mile'"),
             (r"\b(" + MONTHS_SHORT + r")\.?\s+" + YEAR + r"\b", r"\b(" + MONTHS + r")\s+" + YEAR + r"\b", "a short and a long month")]
    for a, b, what in pairs:
        if re.search(a, alltext, re.I) and re.search(b, alltext, re.I):
            out.append(_f("MAP-TEXT-012", "INFO", f"the map's text writes {what}", alltext[:120], "pick one form for every element (house style: min, mi, November 2026)",
                          "map text"))
    if re.search(r"\b\d{1,2}/\d{2,4}\b", alltext):
        out.append(_f("MAP-TEXT-012", "INFO", "a date is written with slashes", alltext[:120], "write 'November 2026' or 'Q3 2026', never '11/26'", "map text"))
    return out


def summary_lines(findings: list[dict], client: bool = False) -> list[str]:
    """The chat callout (spec §11.1): every WARN (and every INFO on a client deliverable), one line each: code, what was found, one fix."""
    picked = [f for f in findings if f["id"].startswith("MAP-TEXT") and (f["severity"] == "WARN" or (client and f["severity"] == "INFO"))]
    if not picked:
        return []
    out = [f"Map text: {len(picked)} finding{'s' if len(picked) != 1 else ''} (not blocking)"]
    for f in picked:
        d = f.get("detail") or {}
        out.append(f"- {f['id']} {f['message']} -> suggest: {d.get('suggest')}")
    return out
