"""A novel's working folder, and splitting it into chunks.

    novels/<slug>/
      book.json               metadata, chapter list, chunk plan
      glossary.md             names, terms, pronouns — the authority
      style.md                this novel's translation rules
      source/<original file>
      source/chapters/0001.json   parsed blocks, per chapter
      source/images/          images carried over into the output
      ko/c0001.txt            what the translator reads
      en/c0001.txt            what the translator writes
      en_draft/               snapshot taken before the copyedit
      notes/c0001.md          new terms and doubts from each translator
      reports/                check, audit and copyedit reports
      state.json              copyedit progress
      out/<Title>.epub

Everything is a plain file, so a run interrupted by a usage limit resumes by
looking at what exists, and anything can be fixed by hand in an editor.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from . import segfile
from .ingest import Block, Book, load

# One chunk is one subagent write. Sized so that:
#   * a typical web-novel chapter (5–6k Korean characters) is exactly one
#     chunk, so chunk boundaries coincide with chapter boundaries;
#   * the English (~2.5× the characters, ~5–6k tokens) is comfortably inside a
#     single response, with room left for thinking at medium effort;
#   * a model is still attending to line 1 when it writes the last line.
# Longer chapters are split into balanced parts, at a scene break if one is
# near the cut.
MAX_CHUNK_CHARS = 7500
PART_FILL = 0.85

GLOSSARY_TEMPLATE = """# Glossary

The authority on names, terms, pronouns and forms of address. Translators get
the rows whose Korean appears in their chunk. Edit freely; takes effect on the
next chunk.

- **한국어** may list aliases separated by ` / ` (e.g. `유현 / 현이`).
- **type**: person, place, org, title, skill, item, term, address, other.
- **pronoun**: he / she / they / it — persons only. For a deliberate reveal
  write e.g. `he→she @ch120` and explain in notes.

| 한국어 | English | type | pronoun | notes |
|---|---|---|---|---|
"""

STYLE_TEMPLATE = """# Style sheet — {title}

Novel-specific translation rules. Read by every translator and editor. The
generic rules live in the agent definitions; this file only records the
decisions that belong to THIS book. Keep it short and concrete.

## Book
- Genre / setting:
- Narration: (1st/3rd person, tense)
- Tone:

## Conventions
- Spelling: American English
- Chapter headings: `Chapter N` or `Chapter N: Title` (drop 제/화 numbering words)
- Honorifics: (decide from genre and context — e.g. romance fantasy: drop -nim/oppa/hyung and
  carry the relationship in tone; keep one only where the text makes it deliberate, and say which)
- System windows / messages: keep the source brackets exactly, e.g. [Quest Complete]; Title Case inside
- Sound effects:
- Speech: double quotes; thoughts and remembered speech: single quotes
- Ellipses: write `...` (the build normalises all ellipses; set "ellipsis" in book.json to "…" for the single character)
- Units: bare heights/lengths get their unit (190 → "190 centimeters")

## Forms of address
(e.g. 전하 → Your Highness, 공작님 → Your Grace)

## Characters' voices
(one line per major character: how they speak)

## Lessons from review
(Recurring defects found by calibration and copyedit. Translators apply every
item. Each one line, with the fix: e.g. "X's 하게체 to merchants is lofty, no
contractions, all conversation long.")

## Decisions log
(one-liners: what was decided and why, so later chunks stay consistent)
"""


def root() -> Path:
    return Path(os.environ.get("KT_ROOT", "novels"))


def slugify(name: str) -> str:
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"[^\w\s-]", "", name).strip().lower()
    name = re.sub(r"[\s_-]+", "-", name)
    return name[:60] or "novel"


@dataclass
class Chunk:
    id: str
    chapter: int
    part: int
    parts: int
    first: int
    last: int
    chars: int

    def label(self) -> str:
        part = f" · part {self.part}/{self.parts}" if self.parts > 1 else ""
        return f"{self.id} · chapter {self.chapter}{part} · segments {self.first}–{self.last} · {self.chars:,} chars"


class Project:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._meta: dict | None = None

    # ── lookup ──
    @classmethod
    def find(cls, name: str | None) -> "Project":
        if name:
            direct = Path(name)
            if (direct / "book.json").is_file():
                return cls(direct)
            candidate = root() / name
            if (candidate / "book.json").is_file():
                return cls(candidate)
            raise SystemExit(f"No project '{name}' (looked in {candidate}). Run: python3 -m kt init <file>")
        books = sorted(p.parent for p in root().glob("*/book.json"))
        if len(books) == 1:
            return cls(books[0])
        if not books:
            raise SystemExit("No projects yet. Run: python3 -m kt init <file>")
        raise SystemExit("Several projects exist; name one: " + ", ".join(b.name for b in books))

    @property
    def slug(self) -> str:
        return self.path.name

    @property
    def meta(self) -> dict:
        if self._meta is None:
            self._meta = json.loads((self.path / "book.json").read_text("utf-8"))
        return self._meta

    def save_meta(self) -> None:
        _write_json(self.path / "book.json", self.meta)

    # ── files ──
    def ko(self, chunk_id: str) -> Path:
        return self.path / "ko" / f"{chunk_id}.txt"

    def en(self, chunk_id: str) -> Path:
        return self.path / "en" / f"{chunk_id}.txt"

    def notes(self, chunk_id: str) -> Path:
        return self.path / "notes" / f"{chunk_id}.md"

    @property
    def glossary_file(self) -> Path:
        return self.path / "glossary.md"

    @property
    def style_file(self) -> Path:
        return self.path / "style.md"

    @property
    def reports(self) -> Path:
        path = self.path / "reports"
        path.mkdir(exist_ok=True)
        return path

    def chapter_blocks(self, n: int) -> list[dict]:
        return json.loads((self.path / "source" / "chapters" / f"{n:04d}.json").read_text("utf-8"))

    # ── chunks ──
    @property
    def chunks(self) -> list[Chunk]:
        return [Chunk(**c) for c in self.meta["chunks"]]

    def chunk(self, chunk_id: str) -> Chunk:
        chunk_id = normalise_id(chunk_id)
        for c in self.chunks:
            if c.id == chunk_id:
                return c
        raise SystemExit(f"No chunk {chunk_id} in {self.slug} (have c0001–{self.chunks[-1].id}).")

    def resolve(self, specs: list[str]) -> list[Chunk]:
        """Chunk ids from 'c0003', '3', 'c0003-c0010', 'ch5' or 'ch5-ch9'."""
        chunks = self.chunks
        picked: list[Chunk] = []
        for spec in specs:
            for piece in spec.split(","):
                piece = piece.strip().lower()
                if not piece:
                    continue
                if piece.startswith("ch"):
                    a, _, b = piece[2:].partition("-")
                    lo, hi = int(a), int(b.lstrip("ch") or a)
                    picked += [c for c in chunks if lo <= c.chapter <= hi]
                else:
                    a, _, b = piece.partition("-")
                    lo, hi = normalise_id(a), normalise_id(b or a)
                    picked += [c for c in chunks if lo <= c.id <= hi]
        seen, out = set(), []
        for c in picked:
            if c.id not in seen:
                seen.add(c.id)
                out.append(c)
        if not out:
            raise SystemExit(f"No chunks match {' '.join(specs)!r}.")
        return out

    def is_translated(self, chunk_id: str) -> bool:
        path = self.en(chunk_id)
        return path.is_file() and path.stat().st_size > 0

    def source_segments(self, chunk: Chunk) -> dict[int, str]:
        return segfile.read(self.ko(chunk.id)).segments

    # ── state ──
    @property
    def state(self) -> dict:
        path = self.path / "state.json"
        if path.is_file():
            return json.loads(path.read_text("utf-8"))
        return {"edited": []}

    def save_state(self, state: dict) -> None:
        _write_json(self.path / "state.json", state)


def normalise_id(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if not digits:
        raise SystemExit(f"Not a chunk id: {value!r}")
    return f"c{int(digits):04d}"


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ── chunk planning ───────────────────────────────────────────────────────────

def number_blocks(blocks: list[Block]) -> list[dict]:
    """Blocks as stored: translatable ones get a chapter-local number."""
    out, n = [], 0
    for block in blocks:
        data = block.to_json()
        if block.kind in ("h", "p"):
            n += 1
            data["n"] = n
        out.append(data)
    return out


def plan_chapter(chapter_no: int, blocks: list[dict], max_chars: int) -> list[tuple[int, int, int]]:
    """(first n, last n, chars) for each part of one chapter."""
    text = [(b["n"], len(b.get("t", ""))) for b in blocks if "n" in b]
    if not text:
        return []
    total = sum(size for _, size in text)
    if total <= max_chars:
        return [(text[0][0], text[-1][0], total)]

    parts = math.ceil(total / (max_chars * PART_FILL))
    target = total / parts
    # Numbers directly after a scene break are preferred cut points.
    after_break = set()
    pending = False
    for b in blocks:
        if b["k"] == "sep":
            pending = True
        elif "n" in b:
            if pending:
                after_break.add(b["n"])
            pending = False

    cuts: list[int] = []  # index into `text` where a new part starts
    running = 0
    goal = target
    window = target * 0.15
    for index, (n, size) in enumerate(text):
        if index and len(cuts) < parts - 1 and running >= goal - window:
            # Take a scene break inside the window, else the first boundary
            # past the goal.
            if n in after_break or running >= goal:
                cuts.append(index)
                goal += target
        running += size

    bounds = [0] + cuts + [len(text)]
    out = []
    for a, b in zip(bounds, bounds[1:]):
        piece = text[a:b]
        out.append((piece[0][0], piece[-1][0], sum(s for _, s in piece)))
    return out


def render_ko(chunk: Chunk, blocks: list[dict]) -> str:
    """The translator's source file. Scene breaks and images are shown as
    `~~~` hints so the model knows a scene cut happened; they are never
    numbered and never translated."""
    lines = [f"# {chunk.label()}"]
    previous = 0
    for index, b in enumerate(blocks):
        n = b.get("n")
        if n is not None:
            previous = n
            if chunk.first <= n <= chunk.last:
                lines.append(f"[[{n}]] {b['t']}")
            continue
        following = next((x["n"] for x in blocks[index + 1 :] if "n" in x), None)
        inside = chunk.first <= following <= chunk.last if following else previous == chunk.last
        if inside:
            lines.append("~~~ scene break ~~~" if b["k"] == "sep" else "~~~ image ~~~")
    return "\n".join(lines) + "\n"


def init(source: Path, *, slug: str = "", max_chars: int = MAX_CHUNK_CHARS, force: bool = False) -> Project:
    source = Path(source)
    if not source.is_file():
        raise SystemExit(f"File not found: {source}")
    book: Book = load(source)
    slug = slug or slugify(source.stem)
    path = root() / slug
    if (path / "book.json").exists() and not force:
        raise SystemExit(
            f"{path} already exists. Use it (python3 -m kt status {slug}), pick another --slug, "
            f"or --force to re-plan (translations in en/ are kept only if you re-plan with the same settings)."
        )
    for sub in ("source/chapters", "source/images", "ko", "en", "notes", "reports", "out"):
        (path / sub).mkdir(parents=True, exist_ok=True)
    copied = path / "source" / source.name
    if source.resolve() != copied.resolve():
        shutil.copy2(source, copied)
    for name, data in book.images.items():
        (path / "source" / "images" / name).write_bytes(data)
    if book.documents:
        # Where each paragraph sits in the source EPUB, so the English can be
        # written back into the original pages with their formatting intact.
        _write_json(path / "source" / "regions.json",
                    {"documents": book.documents, "regions": book.regions})

    chapters_meta, chunks_meta = [], []
    for number, chapter in enumerate(book.chapters, 1):
        blocks = number_blocks(chapter.blocks)
        _write_json(path / "source" / "chapters" / f"{number:04d}.json", blocks)
        segments = sum(1 for b in blocks if "n" in b)
        chars = sum(len(b.get("t", "")) for b in blocks if "n" in b)
        chapters_meta.append({"n": number, "title_ko": chapter.title, "segments": segments, "chars": chars})
        parts = plan_chapter(number, blocks, max_chars)
        for part, (first, last, size) in enumerate(parts, 1):
            chunk = Chunk(f"c{len(chunks_meta) + 1:04d}", number, part, len(parts), first, last, size)
            chunks_meta.append(chunk.__dict__)
            (path / "ko" / f"{chunk.id}.txt").write_text(render_ko(chunk, blocks), encoding="utf-8")

    meta = {
        "version": 1,
        "slug": slug,
        "source": f"source/{source.name}",
        "title_ko": book.title,
        "author_ko": book.author,
        "title_en": "",
        "author_en": "",
        "cover": book.cover,
        "max_chunk_chars": max_chars,
        "chapters": chapters_meta,
        "chunks": chunks_meta,
    }
    _write_json(path / "book.json", meta)
    if not (path / "glossary.md").exists():
        (path / "glossary.md").write_text(GLOSSARY_TEMPLATE, encoding="utf-8")
    if not (path / "style.md").exists():
        (path / "style.md").write_text(STYLE_TEMPLATE.format(title=book.title), encoding="utf-8")
    return Project(path)
