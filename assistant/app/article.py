"""Long-article index for chat: chunks, an outline, and keyword search.

Articles longer than LONG_ARTICLE_CHARS are not put into a chat's context whole.
The chat gets the outline plus the opening and looks the rest up with
search_article / read_article. Chunks are rebuilt from the entry text on demand
(cheap); the outline is stored in ai.article_outlines keyed by a hash of the text,
so it is built once per article version.

Outline sources, in order: the article's own headings (h1-h6 or bold-only
paragraphs), else one pass of a cheap model over the chunk-marked text.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass, field

from . import db, llm, prompts, scoring
from .content import truncate
from .freshrss import Entry

log = logging.getLogger(__name__)

LONG_ARTICLE_CHARS = 30_000
CHUNK_CHARS = 1_200
OPENING_CHARS = 6_000
READ_MAX_CHARS = 14_000
OUTLINE_MODEL = "claude-haiku-4-5"
OUTLINE_INPUT_CHARS = 600_000
MIN_HEADINGS = 4

_HEADING = re.compile(r"^## (.+)$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[a-z0-9]+(?:['’][a-z]+)?")
_STOP = frozenset("""a an and are as at be but by for from has have he her his i if in into is it its of on or our
    she so than that the their them then there these they this to was we were what when which who will with you your
    about also been can could did do does just more most not only other some such would""".split())


@dataclass
class Chunk:
    id: int
    section: str | None
    text: str


@dataclass
class Article:
    entry: Entry
    text: str
    chunks: list[Chunk]
    digest: str
    _index: "_BM25 | None" = field(default=None, repr=False)

    @property
    def is_long(self) -> bool:
        return len(self.text) > LONG_ARTICLE_CHARS

    @property
    def sections(self) -> list[tuple[str, int, int]]:
        """(heading, first chunk, last chunk) for each run of chunks under one heading."""
        out: list[tuple[str, int, int]] = []
        for c in self.chunks:
            if c.section is None:
                continue
            if out and out[-1][0] == c.section:
                out[-1] = (c.section, out[-1][1], c.id)
            else:
                out.append((c.section, c.id, c.id))
        return out

    def opening(self, max_chars: int = OPENING_CHARS) -> tuple[str, int]:
        """Leading chunks up to max_chars; returns (text, last chunk id included)."""
        parts, used, last = [], 0, -1
        for c in self.chunks:
            if used and used + len(c.text) > max_chars:
                break
            parts.append(c.text)
            used += len(c.text)
            last = c.id
        return "\n\n".join(parts), last

    def search(self, query: str, limit: int = 8) -> list[dict]:
        if self._index is None:
            self._index = _BM25([f"{c.section or ''}\n{c.text}" for c in self.chunks])
        phrase = query.strip().strip('"').lower()
        hits = []
        for i, score in self._index.rank(query):
            if '"' in query and phrase and phrase not in self.chunks[i].text.lower():
                continue
            hits.append({"chunk": i, "section": self.chunks[i].section, "score": round(score, 2),
                         "snippet": _snippet(self.chunks[i].text, query)})
            if len(hits) >= limit:
                break
        return hits

    def read(self, start: int, end: int) -> dict:
        start = max(0, start)
        end = min(len(self.chunks) - 1, end)
        parts, used, last = [], 0, start - 1
        section = None
        for c in self.chunks[start:end + 1]:
            if used and used + len(c.text) > READ_MAX_CHARS:
                break
            head = f"[#{c.id}]"
            if c.section and c.section != section:
                head += f" ({c.section})"
            section = c.section
            parts.append(f"{head}\n{c.text}")
            used += len(c.text)
            last = c.id
        out: dict = {"chunks": f"{start}-{last}", "total_chunks": len(self.chunks), "text": "\n\n".join(parts)}
        if last < end:
            out["note"] = f"Stopped at #{last} (size limit); read from #{last + 1} for more."
        return out


# ── Building ────────────────────────────────────────────────────────────────

def load(entry: Entry, rules: dict | None = None) -> Article:
    text = scoring.entry_text(entry, 10**9, rules=rules, headings=True)
    return Article(entry=entry, text=text, chunks=chunk_text(text),
                   digest=hashlib.sha1(text.encode()).hexdigest()[:16])


def chunk_text(text: str, target: int = CHUNK_CHARS) -> list[Chunk]:
    """Split into ~target-char chunks along paragraphs; '## ' lines start a new section."""
    chunks: list[Chunk] = []
    section: str | None = None
    buf: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buf, size
        if buf:
            chunks.append(Chunk(len(chunks), section, "\n".join(buf)))
        buf, size = [], 0

    for para in (p.strip() for p in text.split("\n")):
        if not para:
            continue
        m = _HEADING.match(para)
        if m:
            flush()
            section = m.group(1).strip()
            continue
        for piece in _split_long(para, target * 2):
            if size and size + len(piece) > target:
                flush()
            buf.append(piece)
            size += len(piece) + 1
    flush()
    return chunks


def _split_long(para: str, limit: int) -> list[str]:
    """Break one very long paragraph (transcripts often have no newlines) at sentence ends."""
    if len(para) <= limit:
        return [para]
    out, cur = [], ""
    for sent in _SENTENCE_END.split(para):
        if cur and len(cur) + len(sent) > limit // 2:
            out.append(cur)
            cur = ""
        cur = f"{cur} {sent}".strip()
        while len(cur) > limit:  # no sentence breaks at all
            cut = cur.rfind(" ", 0, limit // 2)
            cut = cut if cut > 0 else limit // 2
            out.append(cur[:cut])
            cur = cur[cut:].strip()
    if cur:
        out.append(cur)
    return out


# ── Outline ─────────────────────────────────────────────────────────────────

def outline(art: Article) -> str:
    """Stored outline for this text version, building it when missing."""
    sections = art.sections
    if len(sections) >= MIN_HEADINGS:
        lines = [f"#0-#{sections[0][1] - 1}: (opening, before the first heading)"] if sections[0][1] > 0 else []
        return "\n".join(lines + [f"#{a}-#{b}: {h}" for h, a, b in sections])
    row = db.fetch_one("SELECT outline FROM ai.article_outlines WHERE entry_id = %s AND digest = %s",
                       (int(art.entry.id), art.digest))
    if row:
        return row["outline"]
    text = _generate_outline(art)
    db.execute(
        """INSERT INTO ai.article_outlines (entry_id, digest, outline, model) VALUES (%s, %s, %s, %s)
           ON CONFLICT (entry_id) DO UPDATE SET digest = EXCLUDED.digest, outline = EXCLUDED.outline,
               model = EXCLUDED.model, created_at = now()""",
        (int(art.entry.id), art.digest, text, OUTLINE_MODEL))
    return text


_building: set[int] = set()
_building_lock = threading.Lock()


def outline_nowait(art: Article) -> str | None:
    """The outline if it is ready; otherwise start building it in the background and return None."""
    if outline_cached(art):
        return outline(art)
    entry_id = int(art.entry.id)
    with _building_lock:
        if entry_id in _building:
            return None
        _building.add(entry_id)

    def run() -> None:
        try:
            outline(art)
        except Exception as e:
            log.warning("outline for entry %s failed: %s", entry_id, e)
        finally:
            with _building_lock:
                _building.discard(entry_id)

    threading.Thread(target=run, name=f"outline-{entry_id}", daemon=True).start()
    return None


def prebuild(entry: Entry, rules: dict | None = None) -> bool:
    """Build and store the outline for a long entry ahead of any chat. Returns True if one was generated."""
    art = load(entry, rules)
    if not art.is_long or outline_cached(art):
        return False
    outline(art)
    return True


def outline_cached(art: Article) -> bool:
    if len(art.sections) >= MIN_HEADINGS:
        return True
    return db.fetch_one("SELECT 1 FROM ai.article_outlines WHERE entry_id = %s AND digest = %s",
                        (int(art.entry.id), art.digest)) is not None


def _generate_outline(art: Article) -> str:
    marked, used = [], 0
    for c in art.chunks:
        if used > OUTLINE_INPUT_CHARS:
            marked.append(f"[#{c.id}-#{art.chunks[-1].id} omitted: input limit]")
            break
        marked.append(f"[#{c.id}]\n{c.text}")
        used += len(c.text)
    user = prompts.ARTICLE_OUTLINE_USER.format(title=art.entry.title, source=art.entry.feed_name,
                                               last=art.chunks[-1].id, content="\n\n".join(marked))
    msg = llm.api(OUTLINE_MODEL).create(
        model=OUTLINE_MODEL, max_tokens=4000, system=prompts.ARTICLE_OUTLINE_SYSTEM,
        messages=[{"role": "user", "content": user}], **llm.thinking_params(OUTLINE_MODEL, "low"))
    llm.log_usage("article_outline", OUTLINE_MODEL, msg.usage, ref=str(art.entry.id))
    text = llm.text_of(msg).strip()
    if not text:
        raise RuntimeError("empty outline")
    return text


# ── Search ──────────────────────────────────────────────────────────────────

def _tokens(text: str) -> list[str]:
    out = []
    for w in _WORD.findall(text.lower().replace("’", "'")):
        w = w.split("'")[0]
        if w in _STOP or len(w) < 2:
            continue
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


class _BM25:
    def __init__(self, docs: list[str], k1: float = 1.4, b: float = 0.75) -> None:
        self.tfs = [Counter(_tokens(d)) for d in docs]
        self.lens = [sum(tf.values()) for tf in self.tfs]
        self.avg = (sum(self.lens) / len(self.lens)) if self.lens else 1.0
        df: Counter = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.k1, self.b = k1, b

    def rank(self, query: str) -> list[tuple[int, float]]:
        terms = set(_tokens(query))
        scores = []
        for i, tf in enumerate(self.tfs):
            s = 0.0
            for t in terms:
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.lens[i] / self.avg))
            if s > 0:
                scores.append((i, s))
        return sorted(scores, key=lambda x: -x[1])


def _snippet(text: str, query: str, width: int = 320) -> str:
    low = text.lower()
    pos = -1
    for t in sorted(set(_WORD.findall(query.lower())) - _STOP, key=len, reverse=True):
        pos = low.find(t)
        if pos >= 0:
            break
    if pos < 0 or len(text) <= width:
        return truncate(text, width)
    start = max(0, pos - width // 3)
    return ("…" if start else "") + truncate(text[start:], width)
