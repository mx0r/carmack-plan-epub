#!/usr/bin/env python3
"""
Build a minimalist EPUB 3 from the John Carmack .plan archive.

Chapters are years, subchapters are dates. Prose reflows in a serif face;
changelogs, todo lists and code stay verbatim in monospace.

    python3 build_epub.py            # build epub/John-Carmack-Plan.epub
    python3 build_epub.py --diagnose # dump block-classification report
"""

from __future__ import annotations

import datetime as dt
import glob
import html
import os
import re
import shutil
import subprocess
import sys
import uuid
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "by_day")          # the .plan archive, one file per day
BUILD = os.path.join(HERE, "build")         # unpacked book (generated)
OUT = os.path.join(HERE, "dist", "carmack-plan.epub")

FONTS = [("FiraCode-Regular.ttf", "normal"), ("FiraCode-Bold.ttf", "bold")]
# Vendored so the build is hermetic; falls back to a macOS user font dir.
FONT_DIRS = [os.path.join(HERE, "fonts"), os.path.expanduser("~/Library/Fonts")]


def find_font(name: str) -> str:
    for d in FONT_DIRS:
        path = os.path.join(d, name)
        if os.path.exists(path):
            return path
    raise SystemExit(f"error: cannot find {name} in {FONT_DIRS}")

OPF_NS = "http://www.idpf.org/2007/opf"

# Derived from the project URL: a valid UUID, and stable across rebuilds so
# readers treat a new build as the same book rather than a different one.
BOOK_ID = "urn:uuid:" + str(uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/mx0r/carmack-plan-epub"))
TITLE = ".plan"
SUBTITLE = "The collected .plan files of John Carmack, 1996–2010"
AUTHOR = "John Carmack"

# ---------------------------------------------------------------------------
# text model
# ---------------------------------------------------------------------------

PROSE_WIDTH = 88          # block max line width at/above which a block reflows
TABSTOP = 4
CHUNK = 500               # entries per file; high enough that a year is never split

RULE_RE = re.compile(r"^[-=_~]{3,}$")
BULLET_RE = re.compile(r"^\s*([*+\-o>•]|\d+[.)])(\s|$)")
URL_RE = re.compile(r"(https?://[^\s<>\"')\]]+[^\s<>\"')\].,;:!?])")

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


class Entry:
    def __init__(self, date: dt.date, lines: list[str], verbatim: set[int]):
        self.date = date
        self.lines = lines
        self.verbatim = verbatim     # line indices that came from [code] fences
        self.dek = ""

    @property
    def anchor(self) -> str:
        return "d" + self.date.strftime("%Y%m%d")

    @property
    def long_date(self) -> str:
        return f"{self.date:%A}, {self.date.day} {MONTHS[self.date.month - 1]} {self.date.year}"

    @property
    def short_date(self) -> str:
        return f"{self.date.day} {MONTHS[self.date.month - 1][:3]}"

    @property
    def words(self) -> int:
        return sum(len(l.split()) for l in self.lines)


CODE_OPEN = "[code]"
CODE_CLOSE = "[/code]"


def decode(data: bytes) -> str:
    """The archive is mostly ASCII, but a few files are CP1252 (smart quotes)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252")


def strip_code_fences(lines: list[str]) -> tuple[list[str], set[int]]:
    """Remove the archive's [code]/[/code] markup, remembering what it wrapped."""
    out: list[str] = []
    fenced: set[int] = set()
    in_code = False
    for line in lines:
        while True:
            low = line.lower()
            at_open = low.find(CODE_OPEN)
            at_close = low.find(CODE_CLOSE)
            if at_open < 0 and at_close < 0:
                break
            # whichever tag comes first wins this pass
            if at_close < 0 or (0 <= at_open < at_close):
                before, line = line[:at_open], line[at_open + len(CODE_OPEN):]
                if before.strip():
                    out.append(before.rstrip())
                in_code = True
            else:
                before, line = line[:at_close], line[at_close + len(CODE_CLOSE):]
                if before.strip():
                    fenced.add(len(out))
                    out.append(before.rstrip())
                in_code = False
        if line.strip() or out:
            if in_code:
                fenced.add(len(out))
            out.append(line.rstrip())
    return out, fenced


def load_entries() -> list[Entry]:
    entries = []
    for path in sorted(glob.glob(os.path.join(SRC, "johnc_plan_*.txt"))):
        stamp = re.search(r"(\d{8})\.txt$", path)
        if not stamp:
            continue
        date = dt.datetime.strptime(stamp.group(1), "%Y%m%d").date()
        raw = decode(open(path, "rb").read())
        raw = raw.replace("\r\n", "\n").replace("\r", "\n").expandtabs(TABSTOP)
        lines, fenced = strip_code_fences([l.rstrip() for l in raw.split("\n")])
        # drop leading/trailing blanks, keeping the fenced indices aligned
        start, end = 0, len(lines)
        while start < end and not lines[start].strip():
            start += 1
        while end > start and not lines[end - 1].strip():
            end -= 1
        lines = lines[start:end]
        fenced = {i - start for i in fenced if start <= i < end}
        if lines:
            entries.append(Entry(date, lines, fenced))
    entries.sort(key=lambda e: e.date)
    return entries


DIVIDER_RE = re.compile(r"^[-=_~*+.#]{2,}$")
CAPS_RE = re.compile(r"^[A-Z0-9][A-Z0-9 '/&.,()-]*$")
AT_HEAD_RE = re.compile(r"@([A-Za-z][^@]{0,60})@")


def is_divider(lines: list[str]) -> bool:
    return len(lines) == 1 and bool(DIVIDER_RE.fullmatch(lines[0].strip()))


def is_caps_heading(lines: list[str]) -> bool:
    """A lone ALL-CAPS line, e.g. 'PACKET FILTERING', reads as a heading."""
    if len(lines) != 1:
        return False
    s = lines[0].strip()
    if not (2 <= len(s) <= 50) or s.endswith((".", ":", "!", "?")):
        return False
    if sum(c.isalpha() for c in s) < 4 or len(s.split()) > 6:
        return False
    return bool(CAPS_RE.fullmatch(s))


def looks_like_sentence(line: str) -> bool:
    """A lone short line that is really a sentence, not a todo item."""
    if line[:1].isspace() or BULLET_RE.match(line):
        return False
    stripped = line.strip()
    if len(stripped.split()) < 3 or stripped.endswith(".."):
        return False
    return stripped.endswith((".", "!", "?", ":", '."', '?"', '!"'))


def looks_like_wrapped_prose(lines: list[str]) -> bool:
    """Several short lines that are really one hard-wrapped paragraph."""
    if len(lines) < 2:
        return False
    if any(l[:1].isspace() or BULLET_RE.match(l) for l in lines):
        return False
    widths = [len(l) for l in lines]
    if sum(widths) / len(widths) < 55:
        return False
    # every line but the last should run on without terminal punctuation
    if not lines[-1].strip().endswith((".", "!", "?", '"')):
        return False
    runs_on = sum(1 for l in lines[:-1] if not l.strip().endswith((".", "!", "?", ":")))
    return runs_on >= len(lines) - 1


def classify(buf: list[str]) -> tuple[str, object]:
    if is_divider(buf):
        return ("rule", "")
    if is_caps_heading(buf):
        return ("head", buf[0].strip())
    if max(len(l) for l in buf) >= PROSE_WIDTH:
        return ("prose", buf)
    if len(buf) == 1 and looks_like_sentence(buf[0]):
        return ("prose", buf)
    if looks_like_wrapped_prose(buf):
        return ("prose", [" ".join(l.strip() for l in buf)])
    return ("pre", buf)


def blockify(lines: list[str], fenced: set[int] | None = None) -> list[tuple[str, object]]:
    """Split an entry into ('head'|'rule'|'prose'|'pre', payload) blocks."""
    fenced = fenced or set()
    heads: set[int] = set()
    rules: set[int] = set()
    for i in range(len(lines) - 1):
        if i in fenced or i + 1 in fenced:
            continue
        cur, nxt = lines[i].strip(), lines[i + 1].strip()
        if not cur or not nxt:
            continue
        if RULE_RE.fullmatch(nxt) and not RULE_RE.fullmatch(cur) and len(cur) <= 60:
            heads.add(i)
            rules.add(i + 1)

    out: list[tuple[str, object]] = []
    buf: list[str] = []
    buf_fenced = False

    def flush():
        nonlocal buf, buf_fenced
        if buf:
            while buf and not buf[-1].strip():
                buf.pop()
        if buf:
            out.append(("pre", buf) if buf_fenced else classify(buf))
        buf, buf_fenced = [], False

    for i, line in enumerate(lines):
        in_fence = i in fenced
        if in_fence != buf_fenced:
            flush()
            buf_fenced = in_fence
        if in_fence:
            # keep blank lines and spacing exactly as written inside a listing
            if line.strip() or buf:
                buf.append(line)
            continue
        if i in rules:
            continue
        marker = AT_HEAD_RE.fullmatch(line.strip())
        if marker:
            flush()
            out.append(("head", marker.group(1).strip()))
        elif i in heads:
            flush()
            out.append(("head", lines[i].strip()))
        elif not line.strip():
            flush()
        else:
            buf.append(line)
    flush()
    return out


# ---------------------------------------------------------------------------
# xhtml rendering
# ---------------------------------------------------------------------------

def esc(text: str) -> str:
    return html.escape(text, quote=False)


def linkify(escaped: str) -> str:
    return URL_RE.sub(lambda m: f'<a href="{html.escape(m.group(1), quote=True)}">{m.group(1)}</a>',
                      escaped)


def render_entry(entry: Entry) -> str:
    parts = [f'<section class="entry" epub:type="subchapter" id="{entry.anchor}">',
             f'<h2 class="date" epub:type="title">{esc(entry.long_date)}</h2>']
    if entry.dek:
        parts.append(f'<p class="dek">{esc(entry.dek)}</p>')
    for kind, payload in blockify(entry.lines, entry.verbatim):
        if kind == "rule":
            parts.append('<hr class="hair entry-rule"/>')
        elif kind == "head":
            parts.append(f'<h3 class="sub">{esc(payload)}</h3>')
        elif kind == "prose":
            for line in payload:
                parts.append(f"<p>{linkify(esc(line.strip()))}</p>")
        else:
            spans = "".join(f"<span>{linkify(esc(l)) or '&#160;'}</span>" for l in payload)
            parts.append(f'<pre class="verbatim">{spans}</pre>')
    parts.append("</section>")
    return "\n".join(parts)


PAGE = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en">
<head>
<meta charset="utf-8"/>
<title>{title}</title>
<link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body class="{cls}"{etype}>
{body}
</body>
</html>
"""


def page(title: str, body: str, cls: str = "", etype: str = "") -> str:
    """etype is an EPUB structural-semantics division: front/body/backmatter."""
    attr = f' epub:type="{etype}"' if etype else ""
    return PAGE.format(title=esc(title), body=body, cls=cls, etype=attr)


def render_year_part(year: int, part: int, nparts: int, entries: list[Entry],
                     intro: tuple[str, str] | None, total: int) -> str:
    body = [f'<section class="year" epub:type="chapter" id="y{year}">']
    if part == 0:
        tagline, blurb = intro or ("", "")
        body.append('<section class="year-open">')
        body.append(f'<p class="year-num">{year}</p>')
        body.append('<hr class="hair"/>')
        if tagline:
            body.append(f'<p class="year-tag">{esc(tagline)}</p>')
        if blurb:
            body.append(f'<p class="year-blurb">{esc(blurb)}</p>')
        body.append(f'<p class="year-count">{total} '
                    f'{"entry" if total == 1 else "entries"}</p>')
        body.append("</section>")
    body.extend(render_entry(e) for e in entries)
    body.append("</section>")
    label = str(year) if nparts == 1 else f"{year} ({part + 1}/{nparts})"
    return page(label, "\n".join(body), cls="chapter", etype="bodymatter")


# ---------------------------------------------------------------------------
# stylesheet
# ---------------------------------------------------------------------------

CSS = """@charset "utf-8";

@font-face {
  font-family: "PlanMono";
  font-weight: normal; font-style: normal;
  src: url("fonts/FiraCode-Regular.ttf");
}
@font-face {
  font-family: "PlanMono";
  font-weight: bold; font-style: normal;
  src: url("fonts/FiraCode-Bold.ttf");
}

:root {
  --ink:   #1c1b19;
  --muted: #6f6a61;
  --faint: #979186;
  --rule:  #cfc9bd;
}

html { font-size: 100%; }

body {
  font-family: "Iowan Old Style", "Charter", "Palatino", "Palatino Linotype",
               "Book Antiqua", Georgia, serif;
  color: #1c1b19;
  color: var(--ink);
  line-height: 1.52;
  margin: 0 3.2%;
  padding: 0;
  text-align: justify;
  -webkit-hyphens: auto; hyphens: auto;
  widows: 2; orphans: 2;
}

p { margin: 0 0 0.85em 0; text-indent: 0; }
a { color: inherit; text-decoration: none; border-bottom: 1px solid #cfc9bd; }

/* ---- monospace, ligatures off (Carmack writes -> and != a lot) ---- */
.mono, pre.verbatim, h2.date, .year-num, .year-count, .kicker {
  font-family: "PlanMono", "SF Mono", "Menlo", "Consolas", monospace;
  -webkit-font-feature-settings: "liga" 0, "calt" 0;
  font-feature-settings: "liga" 0, "calt" 0;
  font-variant-ligatures: none;
}

/* ---- entries ---- */
section.entry { margin: 0 0 2.6em 0; }
section.entry + section.entry { margin-top: 2.9em; }

h2.date {
  font-size: 0.72em;
  font-weight: normal;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: #6f6a61;
  color: var(--muted);
  text-align: left;
  margin: 0 0 0.5em 0;
  padding: 0 0 0.5em 0;
  border-bottom: 1px solid #cfc9bd;
  border-bottom: 1px solid var(--rule);
  page-break-after: avoid; break-after: avoid;
  page-break-inside: avoid;
}

p.dek {
  font-style: italic;
  font-size: 0.97em;
  color: #6f6a61;
  color: var(--muted);
  text-align: left;
  margin: 0 0 1.05em 0;
  -webkit-hyphens: none; hyphens: none;
  page-break-after: avoid; break-after: avoid;
}

h3.sub {
  font-family: "PlanMono", "SF Mono", "Menlo", monospace;
  -webkit-font-feature-settings: "liga" 0, "calt" 0;
  font-feature-settings: "liga" 0, "calt" 0;
  font-size: 0.8em;
  font-weight: bold;
  letter-spacing: 0.04em;
  text-align: left;
  margin: 1.5em 0 0.6em 0;
  page-break-after: avoid; break-after: avoid;
}

/* ---- verbatim blocks: one <span> per source line so that wrapped
        continuations hang instead of clipping off the page ---- */
pre.verbatim {
  white-space: normal;
  font-size: 0.76em;
  line-height: 1.5;
  margin: 0 0 1.05em 0;
  text-align: left;
  -webkit-hyphens: none; hyphens: none;
}
pre.verbatim span {
  display: block;
  white-space: pre-wrap;
  word-wrap: break-word;
  overflow-wrap: break-word;
  text-indent: -1.4em;
  padding-left: 1.4em;
}

/* ---- year openers ---- */
body.chapter section.year-open {
  page-break-before: always; break-before: page;
  margin: 12% 0 3.4em 0;
  text-align: left;
}
.year-num {
  font-size: 3.4em;
  line-height: 1;
  letter-spacing: 0.06em;
  margin: 0 0 0.28em 0;
}
hr.hair {
  border: 0;
  border-top: 1px solid #cfc9bd;
  border-top: 1px solid var(--rule);
  margin: 0 0 1.3em 0;
  height: 0;
}
.year-tag {
  font-style: italic;
  font-size: 1.12em;
  text-align: left;
  margin: 0 0 0.9em 0;
  -webkit-hyphens: none; hyphens: none;
}
.year-blurb { text-align: left; margin: 0 0 1.4em 0; }
.year-count {
  font-size: 0.68em;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: #979186;
  color: var(--faint);
  margin: 0;
}
body.chapter section.entry:first-of-type { page-break-before: avoid; }

/* ---- front and back matter ---- */
body.plate { text-align: left; }
section.plate {
  page-break-before: always; break-before: page;
  margin-top: 22%;
}
.plate h1 {
  font-family: "PlanMono", "SF Mono", "Menlo", monospace;
  -webkit-font-feature-settings: "liga" 0, "calt" 0;
  font-feature-settings: "liga" 0, "calt" 0;
  font-size: 3.1em;
  font-weight: normal;
  letter-spacing: 0.02em;
  margin: 0 0 0.5em 0;
}
.plate h2 {
  font-size: 1.0em;
  font-weight: normal;
  font-style: italic;
  color: #6f6a61;
  color: var(--muted);
  margin: 0 0 2.4em 0;
}
.kicker {
  font-size: 0.68em;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: #979186;
  color: var(--faint);
  margin: 0 0 1.6em 0;
}
h1.page-title {
  font-family: "PlanMono", "SF Mono", "Menlo", monospace;
  font-size: 1.15em;
  font-weight: normal;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  margin: 0 0 1.8em 0;
  padding-bottom: 0.7em;
  border-bottom: 1px solid #cfc9bd;
  border-bottom: 1px solid var(--rule);
}
.note { font-size: 0.9em; color: #6f6a61; color: var(--muted); }

/* ---- career list: inline-block rather than grid, for older readers ---- */
ol.life {
  list-style: none;
  margin: 1.5em 0 0.4em 0;
  padding: 0;
  text-align: left;
  -webkit-hyphens: none; hyphens: none;
}
ol.life li {
  margin: 0;
  padding: 0.34em 0 0.34em 0.85em;
  border-left: 1px solid #cfc9bd;
  border-left: 1px solid var(--rule);
  line-height: 1.4;
}
ol.life li.in-book { border-left-color: #1c1b19; border-left-color: var(--ink); }
ol.life .yr {
  font-family: "PlanMono", "SF Mono", "Menlo", monospace;
  -webkit-font-feature-settings: "liga" 0, "calt" 0;
  font-feature-settings: "liga" 0, "calt" 0;
  font-size: 0.72em;
  letter-spacing: 0.08em;
  display: inline-block;
  width: 4.2em;
  color: #6f6a61;
  color: var(--muted);
}
ol.life li.in-book .yr { font-weight: bold; color: #1c1b19; color: var(--ink); }
ol.life .sub {
  display: block;
  margin-left: 4.2em;
  font-size: 0.86em;
  color: #6f6a61;
  color: var(--muted);
}

/* ---- cover ---- */
body.cover { margin: 0; padding: 0; text-align: center; }
body.cover img { max-width: 100%; max-height: 100%; }

/* ---- contents ---- */
nav#toc ol { list-style: none; margin: 0; padding: 0; }
nav#toc ol ol { margin: 0.3em 0 1.4em 0; }
nav#toc li { margin: 0.15em 0; }
nav#toc a { border: 0; }

@media (prefers-color-scheme: dark) {
  :root { --ink:#e7e3dc; --muted:#a09a8f; --faint:#8b857a; --rule:#4b463f; }
  ol.life li { border-left-color: #4b463f; }
  ol.life li.in-book { border-left-color: #e7e3dc; }
  ol.life .yr, ol.life .sub { color: #a09a8f; }
  ol.life li.in-book .yr { color: #e7e3dc; }
  body { color: #e7e3dc; color: var(--ink); }
  a { border-bottom-color: #4b463f; }
  h2.date { color: #a09a8f; border-bottom-color: #4b463f; }
  p.dek, .plate h2, .note { color: #a09a8f; }
  hr.hair, h1.page-title { border-color: #4b463f; }
  .year-count, .kicker { color: #8b857a; }
}
"""


# ---------------------------------------------------------------------------
# cover
# ---------------------------------------------------------------------------

COVER_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="2100" viewBox="0 0 1400 2100">
  <rect width="1400" height="2100" fill="#141310"/>
  <rect x="86" y="86" width="1228" height="1928" fill="none" stroke="#3a352c" stroke-width="2"/>
  <g font-family="Fira Code, Menlo, monospace" fill="#f2ede1">
    <text x="170" y="566" font-size="40" letter-spacing="14" fill="#8e8574">JOHN CARMACK</text>
    <text x="170" y="982" font-size="250" letter-spacing="4">.plan</text>
    <rect x="952" y="862" width="104" height="128" fill="#c9a227"/>
    <line x1="170" y1="1122" x2="1230" y2="1122" stroke="#3a352c" stroke-width="2"/>
    <text x="170" y="1214" font-size="42" letter-spacing="8" fill="#8e8574">1996 &#8212; 2010</text>
    <text x="170" y="1904" font-size="30" letter-spacing="6" fill="#6d6557">394 ENTRIES</text>
    <text x="170" y="1956" font-size="30" letter-spacing="6" fill="#6d6557">A PROGRAMMER&#8217;S NOTEBOOK</text>
  </g>
</svg>
"""


def build_cover(count: int) -> bool:
    svg_path = os.path.join(BUILD, "cover.svg")
    png_path = os.path.join(BUILD, "cover.png")
    svg = COVER_SVG.replace("394 ENTRIES FROM THE", f"{count} ENTRIES FROM THE")
    with open(svg_path, "w", encoding="utf-8") as fh:
        fh.write(svg)
    try:
        # rsvg picks the cover font via fontconfig; warn loudly if the real
        # face is missing rather than shipping a silent fallback.
        try:
            got = subprocess.run(["fc-match", "-f", "%{family}", "Fira Code"],
                                 capture_output=True, text=True, check=True).stdout
            if "fira code" not in got.lower():
                print(f"  ! cover font fallback: 'Fira Code' resolved to {got!r}",
                      file=sys.stderr)
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass                                  # no fontconfig; nothing to check
        subprocess.run(["rsvg-convert", "-w", "1400", "-h", "2100",
                        "-o", png_path, svg_path], check=True,
                       capture_output=True)
    except Exception as exc:                                   # pragma: no cover
        print(f"  ! cover render failed: {exc}", file=sys.stderr)
        return False
    finally:
        os.remove(svg_path)
    return os.path.exists(png_path)


# ---------------------------------------------------------------------------
# package
# ---------------------------------------------------------------------------

def read_tsv(name: str, cols: int) -> dict[str, list[str]]:
    path = os.path.join(HERE, name)
    data: dict[str, list[str]] = {}
    if not os.path.exists(path):
        return data
    for raw in open(path, encoding="utf-8"):
        raw = raw.rstrip("\n")
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        bits = raw.split("\t")
        if len(bits) < cols:
            bits += [""] * (cols - len(bits))
        data[bits[0].strip()] = [b.strip() for b in bits[1:cols]]
    return data


def build() -> None:
    entries = load_entries()
    deks = read_tsv("summaries.tsv", 2)
    intros = read_tsv("year_intros.tsv", 3)

    missing = 0
    for e in entries:
        row = deks.get(e.date.strftime("%Y%m%d"))
        if row and row[0]:
            e.dek = row[0]
        else:
            missing += 1

    by_year: dict[int, list[Entry]] = {}
    for e in entries:
        by_year.setdefault(e.date.year, []).append(e)
    years = sorted(by_year)

    if os.path.isdir(BUILD):
        shutil.rmtree(BUILD)
    os.makedirs(os.path.join(BUILD, "fonts"), exist_ok=True)

    files: list[tuple[str, str, str]] = []   # (name, media-type, properties)
    spine: list[str] = []
    nonlinear: set[str] = set()              # in the spine, but not in reading order

    has_cover = build_cover(len(entries))
    if has_cover:
        files.append(("cover.png", "image/png", ""))
        write(BUILD, "cover.xhtml",
              page("Cover", '<section epub:type="cover"><img src="cover.png" alt="'
                            + esc(f"{TITLE} — {AUTHOR}") + '"/></section>',
                   cls="cover", etype="frontmatter"))
        files.append(("cover.xhtml", "application/xhtml+xml", ""))
        spine.append("cover.xhtml")

    # title plate
    write(BUILD, "title.xhtml", page("Title", f"""<section class="plate" epub:type="titlepage">
<p class="kicker">{esc(AUTHOR)}</p>
<h1>{esc(TITLE)}</h1>
<h2>{esc(SUBTITLE)}</h2>
<p class="note">{len(entries)} entries &#183; {years[0]}&#8211;{years[-1]}</p>
</section>""", cls="plate", etype="frontmatter"))
    files.append(("title.xhtml", "application/xhtml+xml", ""))
    spine.append("title.xhtml")

    # about plate
    write(BUILD, "about.xhtml", page("About", ABOUT, cls="plate", etype="frontmatter"))
    files.append(("about.xhtml", "application/xhtml+xml", ""))
    spine.append("about.xhtml")

    # who wrote this
    write(BUILD, "who.xhtml", page("Who wrote this", WHO, cls="plate",
                                   etype="frontmatter"))
    files.append(("who.xhtml", "application/xhtml+xml", ""))
    spine.append("who.xhtml")

    # year chapters
    toc: list[tuple[int, list[tuple[Entry, str]]]] = []
    for year in years:
        items = by_year[year]
        parts = [items[i:i + CHUNK] for i in range(0, len(items), CHUNK)] or [[]]
        links: list[tuple[Entry, str]] = []
        for pi, chunk in enumerate(parts):
            name = f"y{year}.xhtml" if len(parts) == 1 else f"y{year}-{pi + 1}.xhtml"
            intro_row = intros.get(str(year))
            intro = (intro_row[0], intro_row[1]) if intro_row else None
            write(BUILD, name, render_year_part(year, pi, len(parts), chunk,
                                                intro, len(items)))
            files.append((name, "application/xhtml+xml", ""))
            spine.append(name)
            links.extend((e, f"{name}#{e.anchor}") for e in chunk)
        toc.append((year, links))

    # colophon
    write(BUILD, "colophon.xhtml", page("Colophon", COLOPHON, cls="plate", etype="backmatter"))
    files.append(("colophon.xhtml", "application/xhtml+xml", ""))
    spine.append("colophon.xhtml")

    # navigation
    back: list[tuple[str, str]] = []
    licence = font_license_page()
    if licence:
        write(BUILD, "font-license.xhtml",
              page("Font licence", licence, cls="plate", etype="backmatter"))
        files.append(("font-license.xhtml", "application/xhtml+xml", ""))
        spine.append("font-license.xhtml")
        back.append(("Font licence", "font-license.xhtml"))

    write(BUILD, "nav.xhtml", build_nav(toc, back))
    files.append(("nav.xhtml", "application/xhtml+xml", "nav"))
    spine.append("nav.xhtml")          # referenced by the toc landmark
    nonlinear.add("nav.xhtml")
    write(BUILD, "toc.ncx", build_ncx(toc, back))
    files.append(("toc.ncx", "application/x-dtbncx+xml", ""))

    write(BUILD, "style.css", CSS)
    files.append(("style.css", "text/css", ""))

    for fname, _ in FONTS:
        shutil.copyfile(find_font(fname), os.path.join(BUILD, "fonts", fname))
        files.append((f"fonts/{fname}", "font/ttf", ""))

    write(BUILD, "content.opf",
          build_opf(files, spine, nonlinear, has_cover, len(entries)))

    zip_epub()
    print(f"  entries      {len(entries)}  ({years[0]}–{years[-1]})")
    print(f"  deks         {len(entries) - missing}/{len(entries)}"
          + ("" if not missing else f"   ({missing} missing)"))
    print(f"  year intros  {len(intros)}/{len(years)}")
    print(f"  spine items  {len(spine)}")
    print(f"  wrote        {OUT}  ({os.path.getsize(OUT) / 1024:.0f} KB)")


def write(base: str, name: str, text: str) -> None:
    path = os.path.join(base, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


ABOUT = """<section class="plate" epub:type="preface">
<h1 class="page-title">About this book</h1>
<p>Before blogs, before social media, Unix carried a small courtesy: any user
could leave a plain text file called <span class="mono">.plan</span> in their
home directory, and anyone on the internet could read it by running
<span class="mono">finger</span> against their address. It was meant for
&#8220;back at 3pm&#8221; notes.</p>
<p>From 1996 onward, John Carmack used his as a working notebook. What began as
terse Quake changelogs &#8212; a line per bug fixed, a line per thing still
broken &#8212; grew into the most detailed public record we have of how a
first&#8209;rate engine programmer actually thinks: rendering research,
hardware autopsies, arguments about drivers and platforms, and the occasional
flat admission that an approach was wrong and had been thrown away.</p>
<p>This edition collects every surviving entry, in order, chaptered by year.</p>
<h1 class="page-title" style="margin-top:2.4em">A note on the text</h1>
<p>The entries are reproduced verbatim, typos and all. Two typographic
decisions were made for reading on a screen:</p>
<p>Paragraphs that Carmack wrote as single long lines are reflowed in a serif
face, so they wrap to your page and your chosen text size.</p>
<p>Changelogs, todo lists, tables and code are kept exactly as written, in
monospace, because their line breaks and alignment carry meaning. Where such a
line is too long for the page it wraps with a hanging indent rather than
running off the edge.</p>
<p>The italic line beneath each date is editorial &#8212; a one&#8209;line note on
what that entry covers, added for this edition. Everything else is his.</p>
</section>"""

WHO = """<section class="plate" epub:type="preface">
<h1 class="page-title">Who wrote this</h1>
<p>John Carmack is the programmer who made 3D games run on ordinary computers.
At id Software, which he co&#8209;founded in 1991, he wrote the engines behind
Wolfenstein&#160;3D, DOOM and Quake &#8212; work that took real&#8209;time 3D
graphics off expensive workstations and onto the PC in someone&#8217;s
bedroom.</p>
<p>He also gave most of it away. The source code to nearly every engine he wrote
was released publicly once the games had had their commercial run. The same
instinct runs through these notes: he sets down what he tried, what it cost, and
what turned out to be wrong.</p>
<p>The entries here run from 1996 to 2010, from Quake through to the mobile ports
at the end of his time at id. What came afterwards is not in them.</p>
<ol class="life">
<li><span class="yr">1990</span>Commander Keen<span class="sub">Smooth scrolling on PC hardware that was not supposed to manage it</span></li>
<li><span class="yr">1992</span>Wolfenstein 3D</li>
<li><span class="yr">1993</span>DOOM</li>
<li class="in-book"><span class="yr">1996</span>Quake<span class="sub">True 3D, and the client/server networking that made internet play work</span></li>
<li class="in-book"><span class="yr">1997</span>Quake II</li>
<li class="in-book"><span class="yr">1999</span>Quake III Arena</li>
<li class="in-book"><span class="yr">2000</span>Founded Armadillo Aerospace<span class="sub">A rocket company he engineered for alongside the day job</span></li>
<li class="in-book"><span class="yr">2004</span>DOOM 3</li>
<li class="in-book"><span class="yr">2010</span>The last entry in this book<span class="sub">After fourteen years the notebook stops, on &#8220;it works, and it was probably the right decision&#8221;</span></li>
<li><span class="yr">2011</span>Rage</li>
<li><span class="yr">2013</span>Became chief technology officer at Oculus<span class="sub">Through the first wave of consumer virtual reality</span></li>
<li><span class="yr">2022</span>Founded Keen Technologies, to work on artificial general intelligence</li>
</ol>
<p class="note">The years set in bold are the years this book covers.</p>
</section>"""

COLOPHON = """<section class="plate" epub:type="colophon">
<h1 class="page-title">Colophon</h1>
<p>Set in Iowan Old Style, with Fira Code for all monospaced text. Fira Code is
used under the SIL Open Font License 1.1, reproduced in full at the end of this
book.</p>
<p>Text from the day&#8209;by&#8209;day <span class="mono">.plan</span> archive at
<span class="mono">github.com/ESWAT/john-carmack-plan-archive</span>, which
mirrors the original collection at
<span class="mono">floodyberry.com/carmack/plan.html</span>.</p>
<p class="note">The .plan files are the work of John Carmack. This edition adds
only typesetting and the editorial summary lines.</p>
</section>"""


def font_license_page() -> str | None:
    """The OFL requires its text to accompany any copy that bundles the fonts."""
    path = os.path.join(HERE, "fonts", "OFL.txt")
    if not os.path.exists(path):
        print("  ! fonts/OFL.txt missing; omitting the licence page", file=sys.stderr)
        return None
    text = open(path, encoding="utf-8").read().rstrip()
    lines = "".join(f"<span>{esc(l) or '&#160;'}</span>" for l in text.split("\n"))
    return (f'<section class="plate">'
            f'<h1 class="page-title">Font licence</h1>'
            f'<p class="note">Fira Code is embedded in this book and used under '
            f'the terms reproduced below.</p>'
            f'<pre class="verbatim">{lines}</pre></section>')


def build_nav(toc, back=()) -> str:
    rows = ['<nav epub:type="toc" id="toc"><h1 class="page-title">Contents</h1><ol>']
    rows.append('<li><a href="about.xhtml">About this book</a></li>')
    rows.append('<li><a href="who.xhtml">Who wrote this</a></li>')
    for year, links in toc:
        first = links[0][1].split("#")[0] if links else "colophon.xhtml"
        rows.append(f'<li><a href="{first}">{year}</a><ol>')
        for entry, href in links:
            rows.append(f'<li><a href="{href}">{esc(entry.short_date)}</a></li>')
        rows.append("</ol></li>")
    rows.append('<li><a href="colophon.xhtml">Colophon</a></li>')
    for label, href in back:
        rows.append(f'<li><a href="{href}">{esc(label)}</a></li>')
    rows.append("</ol></nav>")
    first_year = toc[0][1][0][1].split("#")[0] if toc and toc[0][1] else "colophon.xhtml"
    rows.append('<nav epub:type="landmarks" hidden="hidden"><ol>'
                '<li><a epub:type="cover" href="cover.xhtml">Cover</a></li>'
                '<li><a epub:type="titlepage" href="title.xhtml">Title page</a></li>'
                '<li><a epub:type="toc" href="nav.xhtml">Contents</a></li>'
                f'<li><a epub:type="bodymatter" href="{first_year}">Start reading</a></li>'
                "</ol></nav>")
    return page("Contents", "\n".join(rows), cls="plate", etype="frontmatter")


def build_ncx(toc, back=()) -> str:
    out = ['<?xml version="1.0" encoding="utf-8"?>',
           '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">',
           f'<head><meta name="dtb:uid" content="{BOOK_ID}"/>',
           '<meta name="dtb:depth" content="2"/>',
           '<meta name="dtb:totalPageCount" content="0"/>',
           '<meta name="dtb:maxPageNumber" content="0"/></head>',
           f"<docTitle><text>{esc(TITLE)}</text></docTitle>", "<navMap>"]
    order = 1

    def nav_point(label: str, src: str, children: str = "") -> str:
        nonlocal order
        pid, order = f"np{order}", order + 1
        return (f'<navPoint id="{pid}" playOrder="{order - 1}">'
                f"<navLabel><text>{esc(label)}</text></navLabel>"
                f'<content src="{src}"/>{children}</navPoint>')

    out.append(nav_point("About this book", "about.xhtml"))
    out.append(nav_point("Who wrote this", "who.xhtml"))
    for year, links in toc:
        first = links[0][1].split("#")[0] if links else "colophon.xhtml"
        # reserve the parent's play order before its children
        pid, parent_order = f"npY{year}", order
        order += 1
        kids = "".join(nav_point(e.short_date, href) for e, href in links)
        out.append(f'<navPoint id="{pid}" playOrder="{parent_order}">'
                   f"<navLabel><text>{year}</text></navLabel>"
                   f'<content src="{first}"/>{kids}</navPoint>')
    out.append(nav_point("Colophon", "colophon.xhtml"))
    for label, href in back:
        out.append(nav_point(label, href))
    out.append("</navMap></ncx>")
    return "".join(out)


def build_opf(files, spine, nonlinear, has_cover: bool, count: int) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = []
    for i, (name, mtype, props) in enumerate(files):
        ident = "cover-image" if name == "cover.png" else \
                "ncx" if name == "toc.ncx" else f"i{i}"
        extra = f' properties="{props}"' if props else ""
        if name == "cover.png":
            extra += ' properties="cover-image"' if not props else ""
        items.append(f'<item id="{ident}" href="{name}" '
                     f'media-type="{mtype}"{extra}/>')
    ids = {name: ("cover-image" if name == "cover.png" else
                  "ncx" if name == "toc.ncx" else f"i{i}")
           for i, (name, _, _) in enumerate(files)}
    refs = "".join(
        f'<itemref idref="{ids[n]}"' + (' linear="no"' if n in nonlinear else '') + "/>"
        for n in spine)
    cover_meta = '<meta name="cover" content="cover-image"/>' if has_cover else ""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="{OPF_NS}" version="3.0" unique-identifier="bookid" xml:lang="en">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{BOOK_ID}</dc:identifier>
    <dc:title>{esc(TITLE)}</dc:title>
    <dc:creator id="creator">{esc(AUTHOR)}</dc:creator>
    <meta refines="#creator" property="role" scheme="marc:relators">aut</meta>
    <meta refines="#creator" property="file-as">Carmack, John</meta>
    <dc:language>en</dc:language>
    <dc:description>{esc(SUBTITLE)} &#8212; {count} entries.</dc:description>
    <dc:subject>Computers</dc:subject>
    <dc:subject>Game development</dc:subject>
    <meta property="dcterms:modified">{stamp}</meta>
    {cover_meta}
  </metadata>
  <manifest>
    {chr(10).join("    " + i for i in items).strip()}
  </manifest>
  <spine toc="ncx">{refs}</spine>
</package>
"""


def zip_epub() -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    if os.path.exists(OUT):
        os.remove(OUT)
    with zipfile.ZipFile(OUT, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                    compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml",
                    '<?xml version="1.0" encoding="utf-8"?>\n'
                    '<container version="1.0" '
                    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                    "<rootfiles><rootfile full-path=\"OEBPS/content.opf\" "
                    'media-type="application/oebps-package+xml"/></rootfiles>'
                    "</container>", zipfile.ZIP_DEFLATED)
        for root, _, names in os.walk(BUILD):
            for name in sorted(names):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, BUILD).replace(os.sep, "/")
                zf.write(full, f"OEBPS/{rel}", zipfile.ZIP_DEFLATED)


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------

def diagnose() -> None:
    entries = load_entries()
    tally = {"head": 0, "prose": 0, "pre": 0, "rule": 0}
    samples = {"head": [], "prose": [], "pre": [], "rule": []}
    joined = []
    for e in entries:
        for kind, payload in blockify(e.lines, e.verbatim):
            tally[kind] += 1
            text = payload if isinstance(payload, str) else "\n".join(payload)
            if len(samples[kind]) < 6 and e.date.year >= 1998:
                samples[kind].append((e.date, text[:230]))
            if kind == "prose" and isinstance(payload, list) and len(payload) == 1 \
                    and len(payload[0]) < PROSE_WIDTH:
                joined.append((e.date, payload[0][:180]))
    print("block tally:", tally)
    for kind in ("head", "prose", "pre"):
        print(f"\n===== {kind.upper()} =====")
        for date, text in samples[kind]:
            print(f"--- {date} ---\n{text}\n")
    print(f"\n===== REFLOWED SHORT BLOCKS ({len(joined)}) =====")
    for date, text in joined[:14]:
        print(f"{date}  {text}")


if __name__ == "__main__":
    if "--diagnose" in sys.argv:
        diagnose()
    else:
        build()
