"""Reading a novel into chapters of blocks.

Every input format ends up as the same shape:

    Book(title, author, chapters=[Chapter(title, blocks=[Block, …]), …])

where a Block is a heading (`h`), a paragraph (`p`), a scene break (`sep`) or
an image (`img`). Only `h` and `p` are translated. Inline italics, bold and
line breaks travel as {i}…{/i}, {b}…{/b} and {br}; everything else about the
source's markup is dropped, because the output EPUB is rebuilt cleanly rather
than patched — Korean EPUB stylesheets set Korean fonts and vertical rhythm
that look wrong around English.

Supported: .epub .txt .md .markdown .html .htm .xhtml .docx
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from .text import collapse, has_hangul, repair_markup, tidy_markup

SUPPORTED = (".epub", ".txt", ".md", ".markdown", ".html", ".htm", ".xhtml", ".docx")


@dataclass
class Block:
    kind: str  # h | p | sep | img
    text: str = ""
    src: str = ""  # img only: file name under images/
    loc: object = None  # EPUB only: (document index, region index) in the source

    def to_json(self) -> dict:
        data = {"k": self.kind}
        if self.loc is not None:
            data["loc"] = list(self.loc)
        if self.text:
            data["t"] = self.text
        if self.src:
            data["src"] = self.src
        return data


@dataclass
class Chapter:
    title: str = ""
    blocks: list[Block] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(b.text) for b in self.blocks if b.kind in ("h", "p"))


@dataclass
class Book:
    title: str = ""
    author: str = ""
    language: str = "ko"
    chapters: list[Chapter] = field(default_factory=list)
    images: dict[str, bytes] = field(default_factory=dict)
    cover: str = ""  # key into images
    # EPUB only: spine documents and, per document, the regions blocks came from.
    documents: list[str] = field(default_factory=list)
    regions: dict[str, list[tuple[int, int]]] = field(default_factory=dict)


# ── shared heuristics ────────────────────────────────────────────────────────

# A chapter heading: short, announces a number or a named section.
HEADING = re.compile(
    r"""^\s*(?:
        \#{1,3}\s+\S.*                                   # markdown heading
      | 제\s*\d+\s*[장화편부권회].*                        # 제12장 / 제12화
      | \d{1,4}\s*[장화편회]\b.*                          # 12화
      | (?:프롤로그|에필로그|외전|후기|작가의\s*말|서장|종장|번외).*
      | (?:chapter|episode|part|volume|prologue|epilogue|side\s*story)\b.*
      | \d{1,4}\s*[.:]\s+\S.{0,40}                       # "12. 제목"
      | [<《【\[]\s*\d{1,4}\s*(?:화|장)?\s*[>》】\]].*     # <12화>, 【12】
      | \#?\d{1,4}\s*$                                   # bare number
    )\s*$""",
    re.VERBOSE | re.IGNORECASE,
)
MAX_HEADING_CHARS = 40
# "1장." / "외전." / "Volume 2:" — a bare label that merely ends in punctuation.
SHORT_LABEL = re.compile(
    r"^(?:제?\s*\d{1,4}\s*[장화편회부권]|프롤로그|에필로그|외전|후기|서장|종장|번외|volume\s*\d+)\s*[.:]$",
    re.IGNORECASE,
)
PROSE_END = (".", "。", "!", "?", "！", "？", "다.", "요.")

# A paragraph made only of these characters is a scene divider.
SEPARATOR = re.compile(r"^[\s*＊✽✱◆◇◈○●◎□■☆★♧♣♤♠♡♥※+=~〜ㅡ\-─━_·•.。…,⁂#]+$")


def looks_like_heading(text: str) -> bool:
    text = text.strip()
    if not text or len(text) > MAX_HEADING_CHARS and not text.startswith("#"):
        return False
    if text.endswith(PROSE_END) and not text.startswith("#") and not SHORT_LABEL.match(text):
        return False
    return bool(HEADING.match(text))


def is_separator(text: str) -> bool:
    text = text.strip()
    if not text or len(text) > 30:
        return False
    # "……" and "?!" are beats of silence and get translated; only decorative
    # runs count as a break.
    if re.fullmatch(r"[\s.。…,!?！？~〜\-—]+", text):
        return False
    return bool(SEPARATOR.match(text))


def classify(text: str) -> Block:
    if is_separator(text):
        return Block("sep", "* * *")
    return Block("p", text)


LONG_BREAK_PARAGRAPH = 400


def split_breaks(text: str) -> list[str]:
    """Split a block at {br} when it is really several paragraphs.

    Some EPUBs put an entire chapter in one <p> separated by <br/>. A double
    break always separates paragraphs; a single one only does in a long block.
    """
    if "{br}" not in text:
        return [text]
    text = re.sub(r"\s*\{br\}\s*", "{br}", text)
    if "{br}{br}" in text:
        parts = re.split(r"(?:\{br\}){2,}", text)
    elif len(text) > LONG_BREAK_PARAGRAPH:
        parts = text.split("{br}")
    else:
        return [text]
    out = []
    carry: list[str] = []
    for part in parts:
        # Re-open any tag left open across the split, close it at the end.
        part = "".join("{%s}" % t for t in carry) + part
        carry = _open_tags(part)
        part += "".join("{/%s}" % t for t in reversed(carry))
        part = tidy_markup(part)
        if collapse(part):
            out.append(part)
    return out


def _open_tags(text: str) -> list[str]:
    stack: list[str] = []
    for m in re.finditer(r"\{(/?)(i|b)\}", text):
        if m.group(1):
            if stack and stack[-1] == m.group(2):
                stack.pop()
        else:
            stack.append(m.group(2))
    return stack


# ── HTML / XHTML ─────────────────────────────────────────────────────────────

BLOCK_TAGS = {
    "p", "div", "li", "dd", "dt", "td", "th", "blockquote", "pre", "figcaption",
    "section", "article", "header", "footer", "aside", "body", "tr", "table",
    "ul", "ol", "dl", "figure", "main", "nav",
}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
SKIP_TAGS = {"script", "style", "head", "title", "rt", "rp", "svg:title"}
ITALIC = {"i", "em", "cite", "dfn", "var"}
BOLD = {"b", "strong"}


class _Extractor(HTMLParser):
    """Flatten an (X)HTML document into blocks, remembering where each came from.

    Every block records the *region* of the source text it was read from: the
    raw markup between two block-level tag boundaries (after `<p class="x">`,
    up to `</p>`). Writing a translation back means replacing exactly that
    region, so the element around it — its class, its style, the table cell
    or boxed div a system window lives in — is untouched.
    """

    def __init__(self, source: str = "", resolve_image=None) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.line_starts = [0]
        for m in re.finditer("\n", source):
            self.line_starts.append(m.end())
        self.blocks: list[Block] = []
        self.regions: list[tuple[int, int]] = []
        self.region_start = 0
        self.region_blocks = 0
        self.buffer: list[str] = []
        self.kind = "p"
        self.skip = 0
        self.inline: list[str] = []  # open inline markers
        self.resolve_image = resolve_image or (lambda src: "")

    def _offset(self) -> int:
        line, col = self.getpos()
        return self.line_starts[line - 1] + col if line - 1 < len(self.line_starts) else len(self.source)

    def _after_tag(self) -> int:
        start = self._offset()
        text = self.get_starttag_text()
        if text and self.source.startswith(text, start):
            return start + len(text)
        end = self.source.find(">", start)
        return end + 1 if end >= 0 else len(self.source)

    def _emit(self, block: Block) -> None:
        block.loc = len(self.regions)
        self.blocks.append(block)
        self.region_blocks += 1

    def flush(self) -> None:
        """End the current paragraph (not necessarily the region)."""
        for tag in reversed(self.inline):
            self.buffer.append("{/%s}" % tag)
        text = collapse("".join(self.buffer))
        text = tidy_markup(repair_markup(text))
        self.buffer = ["{%s}" % t for t in self.inline]
        kind, self.kind = self.kind, "p"
        if not collapse(re.sub(r"\{/?[ib]\}|\{br\}", "", text)):
            return
        for piece in split_breaks(text):
            self._emit(Block("h", piece) if kind == "h" else classify(piece))

    def boundary(self, end: int, next_start: int) -> None:
        """A block-level tag: close the paragraph and the region."""
        self.flush()
        if self.region_blocks:
            self.regions.append((self.region_start, end))
        self.region_blocks = 0
        self.region_start = next_start

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in BLOCK_TAGS or tag in HEADING_TAGS:
            self.boundary(self._offset(), self._after_tag())
            if tag in HEADING_TAGS:
                self.kind = "h"
        elif tag in ITALIC or tag in BOLD:
            marker = "i" if tag in ITALIC else "b"
            self.inline.append(marker)
            self.buffer.append("{%s}" % marker)
        elif tag == "br":
            self.buffer.append("{br}")
        elif tag == "hr":
            self.flush()
            self._emit(Block("sep", "* * *", src="hr"))
        elif tag in ("img", "image"):
            attributes = dict(attrs)
            src = attributes.get("src") or attributes.get("xlink:href") or attributes.get("href") or ""
            name = self.resolve_image(src) if src else ""
            if name:
                self.flush()
                self._emit(Block("img", src=name))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() in ITALIC | BOLD:
            self.handle_endtag(tag)
        elif tag.lower() in BLOCK_TAGS or tag.lower() in HEADING_TAGS:
            self.kind = "p"

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in BLOCK_TAGS or tag in HEADING_TAGS:
            start = self._offset()
            end = self.source.find(">", start)
            self.boundary(start, end + 1 if end >= 0 else len(self.source))
        elif tag in ITALIC or tag in BOLD:
            marker = "i" if tag in ITALIC else "b"
            if marker in self.inline:
                # Close everything opened after it too (mis-nested source).
                while self.inline:
                    top = self.inline.pop()
                    self.buffer.append("{/%s}" % top)
                    if top == marker:
                        break

    def handle_data(self, data):
        if not self.skip:
            # Braces are our markup; neutralise any in the source text.
            self.buffer.append(data.replace("{", "(").replace("}", ")"))

    def close(self):
        super().close()
        self.inline.clear()
        self.boundary(len(self.source), len(self.source))


def extract(markup: str, resolve_image=None) -> tuple[list[Block], list[tuple[int, int]]]:
    """Blocks and the source regions they came from (block.loc indexes regions)."""
    parser = _Extractor(markup, resolve_image)
    parser.feed(markup)
    parser.close()
    return parser.blocks, parser.regions


def html_blocks(markup: str, resolve_image=None) -> list[Block]:
    return extract(markup, resolve_image)[0]


def promote_heading(blocks: list[Block], label: str = "") -> None:
    """Mark the chapter's title line as a heading if the markup did not."""
    for block in blocks[:3]:
        if block.kind == "h":
            return
        if block.kind != "p":
            continue
        text = collapse(re.sub(r"\{/?[ib]\}|\{br\}", "", block.text))
        if looks_like_heading(text) or (label and collapse(label) == text):
            block.kind = "h"
        return


# ── EPUB ─────────────────────────────────────────────────────────────────────

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "xhtml": "http://www.w3.org/1999/xhtml",
}
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/svg+xml"}


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp949"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if encoding == "utf-16" and not raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            continue
        return text
    return raw.decode("utf-8", errors="replace")


def _xml(raw: bytes) -> ET.Element:
    return ET.fromstring(raw)


def read_epub(path: Path) -> Book:
    book = Book()
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        container = _xml(z.read("META-INF/container.xml"))
        rootfile = container.find(".//c:rootfile", NS)
        opf_path = rootfile.get("full-path")
        opf_dir = posixpath.dirname(opf_path)
        opf = _xml(z.read(opf_path))

        def text_of(tag: str) -> str:
            node = opf.find(f".//dc:{tag}", NS)
            return collapse(node.text or "") if node is not None and node.text else ""

        book.title = text_of("title") or path.stem
        book.author = text_of("creator")
        book.language = text_of("language") or "ko"

        manifest: dict[str, dict] = {}
        for item in opf.findall(".//opf:manifest/opf:item", NS):
            href = posixpath.normpath(posixpath.join(opf_dir, unquote(item.get("href", ""))))
            manifest[item.get("id")] = {
                "href": href,
                "type": item.get("media-type", ""),
                "props": item.get("properties", ""),
            }

        # Cover: EPUB 3 property, then the EPUB 2 <meta name="cover">.
        cover_id = next((i for i, m in manifest.items() if "cover-image" in m["props"]), None)
        if not cover_id:
            meta = opf.find(".//opf:metadata/opf:meta[@name='cover']", NS)
            if meta is not None:
                cover_id = meta.get("content")

        used: dict[str, str] = {}

        def add_image(zip_path: str) -> str:
            if zip_path in used:
                return used[zip_path]
            if zip_path not in names:
                return ""
            base = posixpath.basename(zip_path)
            stem, dot, ext = base.rpartition(".")
            name = re.sub(r"[^A-Za-z0-9._-]", "_", base) or "image"
            counter = 1
            while name in book.images:
                counter += 1
                name = f"{re.sub(r'[^A-Za-z0-9_-]', '_', stem)}_{counter}.{ext}"
            book.images[name] = z.read(zip_path)
            used[zip_path] = name
            return name

        if cover_id and cover_id in manifest and manifest[cover_id]["type"] in IMAGE_TYPES:
            book.cover = add_image(manifest[cover_id]["href"])

        labels = _toc_labels(z, opf, manifest, names)

        spine = [
            ref.get("idref")
            for ref in opf.findall(".//opf:spine/opf:itemref", NS)
            if ref.get("linear", "yes") != "no"
        ]
        for idref in spine:
            entry = manifest.get(idref)
            if not entry or entry["href"] not in names or "nav" in entry["props"].split():
                continue
            href = entry["href"]
            doc_dir = posixpath.dirname(href)

            def resolve(src: str, doc_dir=doc_dir) -> str:
                target = posixpath.normpath(posixpath.join(doc_dir, unquote(src.split("#")[0])))
                if target == manifest.get(cover_id or "", {}).get("href"):
                    return ""  # the cover is placed once, not inline
                return add_image(target)

            blocks, regions = extract(_decode(z.read(href)), resolve)
            label = labels.get(href, "")
            if regions:
                doc_index = len(book.documents)
                book.documents.append(href)
                book.regions[href] = regions
                for block in blocks:
                    block.loc = (doc_index, block.loc)
            if not blocks:
                continue
            text_blocks = [b for b in blocks if b.kind in ("h", "p")]
            if not text_blocks and all(b.kind == "img" for b in blocks) and not book.chapters:
                # A cover or title-page image before any text.
                if not book.cover:
                    book.cover = blocks[0].src
                    continue
            promote_heading(blocks, label)
            has_heading = any(b.kind == "h" for b in blocks[:3])
            if book.chapters and not label and not has_heading:
                # A continuation file: some EPUBs split long chapters.
                book.chapters[-1].blocks.extend(blocks)
                continue
            title = label or next((b.text for b in blocks if b.kind == "h"), "")
            book.chapters.extend(_split_by_headings(Chapter(title, blocks)))
    book.chapters = [c for c in book.chapters if c.blocks]
    return book


def _toc_labels(z, opf, manifest, names) -> dict[str, str]:
    """Document href → the first TOC label pointing at it."""
    labels: dict[str, str] = {}
    nav = next((m for m in manifest.values() if "nav" in m["props"].split()), None)
    if nav and nav["href"] in names:
        base = posixpath.dirname(nav["href"])
        html = _decode(z.read(nav["href"]))
        for href, label in re.findall(r"<a\b[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", html, re.S | re.I):
            target = posixpath.normpath(posixpath.join(base, unquote(href.split("#")[0])))
            labels.setdefault(target, collapse(unescape(re.sub(r"<[^>]+>", "", label))))
    ncx = next((m for m in manifest.values() if m["type"] == "application/x-dtbncx+xml"), None)
    if not labels and ncx and ncx["href"] in names:
        base = posixpath.dirname(ncx["href"])
        root = _xml(z.read(ncx["href"]))
        for point in root.iter(f"{{{NS['ncx']}}}navPoint"):
            text = point.find(".//ncx:navLabel/ncx:text", NS)
            content = point.find("ncx:content", NS)
            if text is None or content is None:
                continue
            target = posixpath.normpath(posixpath.join(base, unquote(content.get("src", "").split("#")[0])))
            labels.setdefault(target, collapse(text.text or ""))
    return labels


def _split_by_headings(chapter: Chapter) -> list[Chapter]:
    """One file holding several chapters becomes several chapters."""
    starts = [
        i for i, b in enumerate(chapter.blocks)
        if b.kind == "h" and looks_like_heading(re.sub(r"\{/?[ib]\}", "", b.text))
    ]
    if len(starts) < 2:
        return [chapter]
    # Text before the first heading is its own chapter (a preface); an image
    # or divider before it just belongs to the first chapter.
    if any(b.kind in ("h", "p") for b in chapter.blocks[: starts[0]]):
        cuts = [0] + starts
    else:
        cuts = [0] + starts[1:]
    bounds = cuts + [len(chapter.blocks)]
    out = []
    for a, b in zip(bounds, bounds[1:]):
        blocks = chapter.blocks[a:b]
        title = next((x.text for x in blocks if x.kind == "h"), "") or chapter.title
        out.append(Chapter(title, blocks))
    return out


# ── plain text and markdown ──────────────────────────────────────────────────

SENTENCE_ENDINGS = (
    ".", "!", "?", "…", "‥", "。", "！", "？", '"', "'", "”", "’", "」", "』",
    "〉", "》", ")", "]", "—", "–", "~",
)
MIN_WRAP = 25
FALLBACK_CHAPTER_CHARS = 20_000


def decode_text(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for encoding in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _soft_wrapped(line: str) -> bool:
    stripped = line.rstrip()
    return bool(stripped) and not stripped.endswith(SENTENCE_ENDINGS) and len(stripped) >= MIN_WRAP


def _paragraphs(lines: list[str], markdown: bool) -> list[str]:
    """Blank lines separate paragraphs; so does any line that looks finished.

    Korean web-novel .txt files are one paragraph per line. Only lines cut at
    a fixed column are joined, which is what the length test detects.
    """
    out: list[str] = []
    current: list[str] = []

    def flush():
        if current:
            joined = collapse(" ".join(current))
            if joined:
                out.append(joined)
            current.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if current and not _soft_wrapped(current[-1]):
            flush()
        current.append(_md_inline(stripped) if markdown else stripped.replace("{", "(").replace("}", ")"))
    flush()
    return out


def _md_inline(text: str) -> str:
    text = text.replace("{", "(").replace("}", ")")
    text = re.sub(r"\*\*(.+?)\*\*", r"{b}\1{/b}", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"{i}\1{/i}", text)
    return text


def read_text(path: Path) -> Book:
    markdown = path.suffix.lower() in (".md", ".markdown")
    text = decode_text(path.read_bytes())
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    chapters: list[Chapter] = []
    title = ""
    buffer: list[str] = []

    def flush():
        paragraphs = _paragraphs(buffer, markdown)
        if paragraphs or title:
            blocks = [Block("h", title)] if title else []
            blocks += [classify(p) for p in paragraphs]
            chapters.append(Chapter(title, blocks))

    for line in lines:
        stripped = line.strip()
        if stripped and looks_like_heading(stripped):
            flush()
            title = re.sub(r"^\#{1,3}\s*", "", stripped)
            buffer = []
        else:
            buffer.append(line)
    flush()

    if len(chapters) <= 1 and (not chapters or not chapters[0].title):
        chapters = _split_by_size(chapters[0].blocks if chapters else [])
    return Book(title=path.stem, chapters=chapters)


def _split_by_size(blocks: list[Block]) -> list[Chapter]:
    chapters, current, size = [], [], 0
    for block in blocks:
        current.append(block)
        size += len(block.text)
        if size >= FALLBACK_CHAPTER_CHARS:
            chapters.append(Chapter(f"Part {len(chapters) + 1}", current))
            current, size = [], 0
    if current:
        chapters.append(Chapter(f"Part {len(chapters) + 1}", current))
    return chapters


# ── DOCX ─────────────────────────────────────────────────────────────────────

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def read_docx(path: Path) -> Book:
    with zipfile.ZipFile(path) as z:
        document = _xml(z.read("word/document.xml"))
        heading_styles: set[str] = set()
        if "word/styles.xml" in z.namelist():
            for style in _xml(z.read("word/styles.xml")).iter(f"{W}style"):
                name = style.find(f"{W}name")
                label = (name.get(f"{W}val") if name is not None else "") or ""
                label = label.lower()
                if label.startswith(("heading", "title", "제목")) or label in ("subtitle",):
                    heading_styles.add(style.get(f"{W}styleId"))
        title = path.stem
        core = "docProps/core.xml"
        author = ""
        if core in z.namelist():
            root = _xml(z.read(core))
            for node in root.iter():
                if node.tag.endswith("}title") and node.text:
                    title = node.text.strip()
                if node.tag.endswith("}creator") and node.text:
                    author = node.text.strip()

    chapters: list[Chapter] = []
    current: Chapter | None = None
    for para in document.iter(f"{W}p"):
        style = para.find(f"{W}pPr/{W}pStyle")
        style_id = style.get(f"{W}val") if style is not None else ""
        outline = para.find(f"{W}pPr/{W}outlineLvl")
        text = _docx_runs(para)
        plain = collapse(re.sub(r"\{/?[ib]\}|\{br\}", "", text))
        if not plain:
            continue
        styled = style_id in heading_styles or (outline is not None and outline.get(f"{W}val") in ("0", "1"))
        if styled or looks_like_heading(plain):
            current = Chapter(plain, [Block("h", tidy_markup(text))])
            chapters.append(current)
            continue
        if current is None:
            current = Chapter("", [])
            chapters.append(current)
        for piece in split_breaks(tidy_markup(repair_markup(text))):
            current.blocks.append(classify(piece))

    if len(chapters) <= 1 and (not chapters or not chapters[0].title):
        chapters = _split_by_size(chapters[0].blocks if chapters else [])
    return Book(title=title, author=author, chapters=chapters)


def _docx_runs(para) -> str:
    out = []
    for run in para.iter(f"{W}r"):
        props = run.find(f"{W}rPr")
        italic = props is not None and _on(props.find(f"{W}i"))
        bold = props is not None and _on(props.find(f"{W}b"))
        pieces = []
        for node in run:
            if node.tag == f"{W}t" and node.text:
                pieces.append(node.text.replace("{", "(").replace("}", ")"))
            elif node.tag == f"{W}tab":
                pieces.append(" ")
            elif node.tag in (f"{W}br", f"{W}cr"):
                pieces.append("{br}")
        text = "".join(pieces)
        if not text:
            continue
        if bold:
            text = "{b}" + text + "{/b}"
        if italic:
            text = "{i}" + text + "{/i}"
        out.append(text)
    joined = "".join(out)
    # Adjacent runs with the same formatting: {/i}{i} is noise.
    return re.sub(r"\{/(i|b)\}\{\1\}", "", joined)


def _on(node) -> bool:
    return node is not None and node.get(f"{W}val", "true") not in ("0", "false", "none")


# ── entry point ──────────────────────────────────────────────────────────────

def read_html(path: Path) -> Book:
    markup = _decode(path.read_bytes())
    blocks = html_blocks(markup)
    chapters = _split_by_headings(Chapter("", blocks))
    title = re.search(r"<title[^>]*>(.*?)</title>", markup, re.S | re.I)
    return Book(title=collapse(unescape(title.group(1))) if title else path.stem, chapters=chapters)


def load(path: Path) -> Book:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"Unsupported file type {suffix!r}. Supported: {', '.join(SUPPORTED)}")
    if suffix == ".epub":
        book = read_epub(path)
    elif suffix == ".docx":
        book = read_docx(path)
    elif suffix in (".html", ".htm", ".xhtml"):
        book = read_html(path)
    else:
        book = read_text(path)
    for chapter in book.chapters:
        if not chapter.title:
            chapter.title = next((b.text for b in chapter.blocks if b.kind == "h"), "")
    if not any(has_hangul(b.text) for c in book.chapters for b in c.blocks):
        raise ValueError(f"{path.name}: no Korean text found — is this the right file?")
    return book
