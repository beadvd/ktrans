"""Assembling the English EPUB, and checking it is one.

The book is built fresh rather than by patching the source's XHTML: EPUB 3
with an EPUB 2 NCX for older readers, one XHTML file per chapter, a cover if
the source had one, images carried over, and a stylesheet that reads like an
English trade paperback. Every document is parsed as XML before it is zipped,
so a reader never meets a malformed page.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from . import segfile
from .project import Project
from .text import has_hangul, normalise_ellipses, repair_markup, smart_quotes, strip_markup

STYLESHEET = """\
body { margin: 0 5%; font-family: Georgia, "Times New Roman", serif; line-height: 1.55; }
h1 { font-size: 1.4em; text-align: center; margin: 2.5em 0 1.8em; font-weight: normal; letter-spacing: 0.02em; page-break-before: always; }
h2 { font-size: 1.1em; text-align: center; margin: -1em 0 1.8em; font-weight: normal; font-style: italic; }
p { margin: 0; text-indent: 1.4em; text-align: justify; orphans: 2; widows: 2; }
h1 + p, h2 + p, p.sep + p, div.img + p, p.first { text-indent: 0; }
p.sep { text-align: center; text-indent: 0; margin: 1.2em 0; letter-spacing: 0.4em; }
div.img { text-align: center; margin: 1em 0; page-break-inside: avoid; }
div.img img { max-width: 100%; max-height: 95vh; }
p.sys { text-indent: 0; text-align: center; margin: 0.6em 8%; padding: 0.35em 0.6em; border: 1px solid #7a8a99; border-radius: 4px; font-family: "Helvetica Neue", Arial, sans-serif; font-size: 0.9em; }
p.sys + p.sys { margin-top: -0.3em; }
.untranslated { color: #8a5a00; }
section.titlepage { text-align: center; margin-top: 30%; }
section.titlepage h1 { page-break-before: avoid; font-size: 2em; margin: 0 0 0.6em; }
section.titlepage p { text-indent: 0; text-align: center; }
"""

# A paragraph that is entirely one bracketed message: [Quest Complete],
# 【Skill acquired】, <Status>. Web-novel system windows; boxed in fresh builds.
SYSTEM_WINDOW = re.compile(r"^\s*(?:\[[^\]]*\]|【[^】]*】|<[^>]*>|〈[^〉]*〉|《[^》]*》)\s*$", re.S)

MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
         ".webp": "image/webp", ".svg": "image/svg+xml"}


@dataclass
class BuildResult:
    path: Path
    chapters: int
    untranslated: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def render_inline(text: str, quotes: bool = True, ellipsis: str = "...") -> str:
    """Segment text → XHTML: typography, escaping, then our markers into tags."""
    text = normalise_ellipses(repair_markup(text), ellipsis)
    if quotes:
        text = smart_quotes(text)
    out = html.escape(text, quote=False)
    out = re.sub(r"\{(/?)(i|b)\}", lambda m: f"<{m.group(1)}{'em' if m.group(2) == 'i' else 'strong'}>", out)
    return out.replace("{br}", "<br/>")


def xhtml(title: str, body: str, css: str = "../style.css") -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en">\n'
        f"<head>\n<meta charset=\"utf-8\"/>\n<title>{html.escape(title)}</title>\n"
        f'<link rel="stylesheet" type="text/css" href="{css}"/>\n</head>\n'
        f"<body>\n{body}\n</body>\n</html>\n"
    )


def build(project: Project, *, allow_partial: bool = False, output: Path | None = None,
          quotes: bool = True) -> BuildResult:
    meta = project.meta
    ellipsis = meta.get("ellipsis", "...")
    title = meta.get("title_en") or meta.get("title_ko") or project.slug
    author = meta.get("author_en") or meta.get("author_ko") or ""
    untranslated = [c.id for c in project.chunks if not project.is_translated(c.id)]
    if untranslated and not allow_partial:
        raise SystemExit(
            f"{len(untranslated)} chunk(s) are not translated yet ({', '.join(untranslated[:8])}…). "
            "Translate them, or build a preview with --allow-partial (keeps their Korean)."
        )

    # English per chapter, from however many chunks the chapter was split into.
    english: dict[int, dict[int, str]] = {}
    for chunk in project.chunks:
        target = english.setdefault(chunk.chapter, {})
        if project.is_translated(chunk.id):
            target.update(segfile.read(project.en(chunk.id)).segments)

    images_dir = project.path / "source" / "images"
    files: dict[str, bytes] = {}
    manifest: list[tuple[str, str, str, str]] = []  # id, href, media-type, properties
    spine: list[str] = []
    toc: list[tuple[str, str]] = []  # (label, href)
    problems: list[str] = []

    cover = meta.get("cover") or ""
    if cover and (images_dir / cover).is_file():
        suffix = Path(cover).suffix.lower()
        files[f"OEBPS/images/{cover}"] = (images_dir / cover).read_bytes()
        manifest.append(("cover-image", f"images/{cover}", MEDIA.get(suffix, "image/jpeg"), "cover-image"))
        body = f'<section epub:type="cover" class="cover"><div class="img"><img src="../images/{html.escape(cover)}" alt="Cover"/></div></section>'
        files["OEBPS/text/cover.xhtml"] = xhtml("Cover", body).encode()
        manifest.append(("cover", "text/cover.xhtml", "application/xhtml+xml", ""))
        spine.append("cover")

    title_body = (
        f'<section class="titlepage" epub:type="titlepage"><h1>{render_inline(title, quotes)}</h1>'
        + (f"<p>{render_inline(author, quotes)}</p>" if author else "")
        + (f"<p><em>Translated from the Korean</em></p>")
        + "</section>"
    )
    files["OEBPS/text/title.xhtml"] = xhtml(title, title_body).encode()
    manifest.append(("titlepage", "text/title.xhtml", "application/xhtml+xml", ""))
    spine.append("titlepage")

    used_images: set[str] = set()
    for chapter in meta["chapters"]:
        number = chapter["n"]
        blocks = project.chapter_blocks(number)
        translated = english.get(number, {})
        parts: list[str] = []
        label = ""
        headings = 0
        for block in blocks:
            kind = block["k"]
            if kind == "sep":
                parts.append('<p class="sep">* * *</p>')
                continue
            if kind == "img":
                src = block.get("src", "")
                if src and (images_dir / src).is_file():
                    used_images.add(src)
                    parts.append(f'<div class="img"><img src="../images/{html.escape(src)}" alt=""/></div>')
                continue
            text = translated.get(block["n"])
            css = ""
            if not text:
                text, css = block.get("t", ""), ' class="untranslated"'
            elif has_hangul(text):
                problems.append(f"chapter {number} [[{block['n']}]]: Hangul in the English")
            if kind == "h":
                tag = "h1" if headings == 0 else "h2"
                headings += 1
                if not label:
                    label = strip_markup(text).strip()
                parts.append(f"<{tag}{css}>{render_inline(text, quotes, ellipsis)}</{tag}>")
            else:
                if not css and SYSTEM_WINDOW.match(strip_markup(text)):
                    css = ' class="sys"'
                parts.append(f"<p{css}>{render_inline(text, quotes, ellipsis)}</p>")
        if not label:
            label = f"Chapter {number}"
            parts.insert(0, f"<h1>{label}</h1>")
        name = f"ch{number:04d}.xhtml"
        body = f'<section epub:type="chapter" id="ch{number:04d}">\n' + "\n".join(parts) + "\n</section>"
        document = xhtml(label, body)
        try:
            ET.fromstring(document.encode("utf-8"))
        except ET.ParseError as exc:
            problems.append(f"{name}: not well-formed XML ({exc})")
            continue
        files[f"OEBPS/text/{name}"] = document.encode("utf-8")
        manifest.append((f"ch{number:04d}", f"text/{name}", "application/xhtml+xml", ""))
        spine.append(f"ch{number:04d}")
        toc.append((label, f"text/{name}"))

    for index, name in enumerate(sorted(used_images - {cover}), 1):
        files[f"OEBPS/images/{name}"] = (images_dir / name).read_bytes()
        manifest.append((f"img{index:04d}", f"images/{name}", MEDIA.get(Path(name).suffix.lower(), "image/jpeg"), ""))

    identifier = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, 'kt:' + project.slug)}"
    files["OEBPS/style.css"] = STYLESHEET.encode()
    manifest.append(("css", "style.css", "text/css", ""))
    files["OEBPS/nav.xhtml"] = _nav(title, toc).encode()
    manifest.append(("nav", "nav.xhtml", "application/xhtml+xml", "nav"))
    files["OEBPS/toc.ncx"] = _ncx(title, identifier, toc).encode()
    manifest.append(("ncx", "toc.ncx", "application/x-dtbncx+xml", ""))
    files["OEBPS/content.opf"] = _opf(title, author, identifier, manifest, spine, bool(cover and "cover" in spine)).encode()

    output = Path(output) if output else project.path / "out" / f"{_filename(title)}.epub"
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".epub.tmp")
    with zipfile.ZipFile(tmp, "w") as z:
        # mimetype first and stored, or readers reject the file.
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER, compress_type=zipfile.ZIP_DEFLATED)
        for name, data in files.items():
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    tmp.replace(output)
    return BuildResult(output, len(toc), untranslated, problems)


CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def _filename(title: str) -> str:
    name = re.sub(r"[^\w\s-]", "", title).strip()
    return re.sub(r"\s+", "_", name)[:80] or "book"


def _opf(title, author, identifier, manifest, spine, has_cover) -> str:
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    esc = html.escape
    items = "\n".join(
        f'    <item id="{i}" href="{esc(h)}" media-type="{m}"' + (f' properties="{p}"' if p else "") + "/>"
        for i, h, m, p in manifest
    )
    refs = "\n".join(f'    <itemref idref="{i}"/>' for i in spine)
    creator = f"    <dc:creator>{esc(author)}</dc:creator>\n" if author else ""
    cover_meta = '    <meta name="cover" content="cover-image"/>\n' if has_cover else ""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="en">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{identifier}</dc:identifier>
    <dc:title>{esc(title)}</dc:title>
{creator}    <dc:language>en</dc:language>
    <meta property="dcterms:modified">{modified}</meta>
{cover_meta}  </metadata>
  <manifest>
{items}
  </manifest>
  <spine toc="ncx">
{refs}
  </spine>
</package>
"""


def _nav(title, toc) -> str:
    items = "\n".join(f'      <li><a href="{html.escape(h)}">{html.escape(l)}</a></li>' for l, h in toc)
    body = (
        f'<nav epub:type="toc" id="toc">\n    <h1>Contents</h1>\n    <ol>\n{items}\n    </ol>\n  </nav>'
    )
    return xhtml(title, body, css="style.css")


def _ncx(title, identifier, toc) -> str:
    points = "\n".join(
        f'    <navPoint id="np{i}" playOrder="{i}"><navLabel><text>{html.escape(l)}</text></navLabel>'
        f'<content src="{html.escape(h)}"/></navPoint>'
        for i, (l, h) in enumerate(toc, 1)
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="{identifier}"/></head>
  <docTitle><text>{html.escape(title)}</text></docTitle>
  <navMap>
{points}
  </navMap>
</ncx>
"""


# ── validation ───────────────────────────────────────────────────────────────

def validate(path: Path) -> list[str]:
    """Structural checks any reader relies on. Empty list means it is sound."""
    problems: list[str] = []
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return ["not a zip file"]
    with z:
        infos = z.infolist()
        if not infos or infos[0].filename != "mimetype":
            problems.append("mimetype is not the first entry")
        elif infos[0].compress_type != zipfile.ZIP_STORED:
            problems.append("mimetype is compressed")
        elif z.read("mimetype") != b"application/epub+zip":
            problems.append("mimetype has the wrong content")
        names = set(z.namelist())
        try:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            opf_path = container.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile").get("full-path")
            opf = ET.fromstring(z.read(opf_path))
        except (KeyError, ET.ParseError, AttributeError) as exc:
            return problems + [f"container/OPF unreadable: {exc}"]
        base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
        ns = "{http://www.idpf.org/2007/opf}"
        ids = {}
        for item in opf.iter(f"{ns}item"):
            href = base + item.get("href")
            ids[item.get("id")] = item
            if href not in names:
                problems.append(f"manifest item missing from zip: {href}")
            elif item.get("media-type") == "application/xhtml+xml":
                try:
                    ET.fromstring(z.read(href))
                except ET.ParseError as exc:
                    problems.append(f"{href}: {exc}")
        for ref in opf.iter(f"{ns}itemref"):
            if ref.get("idref") not in ids:
                problems.append(f"spine refers to unknown id {ref.get('idref')}")
        if not any("nav" in (i.get("properties") or "") for i in ids.values()):
            problems.append("no EPUB 3 nav document")
    return problems


def epubcheck(path: Path) -> tuple[bool, str] | None:
    """Run the official validator if it is installed; None if it is not."""
    import os

    jar = os.environ.get("EPUBCHECK_JAR")
    if shutil.which("epubcheck"):
        command = ["epubcheck", str(path)]
    elif jar and Path(jar).is_file() and shutil.which("java"):
        command = ["java", "-jar", jar, str(path)]
    else:
        return None
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return done.returncode == 0, (done.stdout + done.stderr)[-3000:]


def write_report(project: Project, result: BuildResult) -> None:
    data = {"output": str(result.path), "chapters": result.chapters,
            "untranslated": result.untranslated, "problems": result.problems}
    (project.reports / "build.json").write_text(json.dumps(data, indent=1), encoding="utf-8")


# ── in place: the source EPUB with its Korean replaced ───────────────────────
#
# For an EPUB source this is the default. The English is written into the
# original pages region by region (see ingest._Extractor), so every class,
# stylesheet, font, table and boxed system window survives exactly as the
# publisher made it. Only the text inside each paragraph element changes.

MEDIA_TAG = re.compile(r"<svg\b.*?</svg\s*>|<(?:img|image|hr)\b[^>]*>", re.S | re.I)
XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.I)


def build_inplace(project: Project, *, allow_partial: bool = False, output: Path | None = None,
                  quotes: bool = True) -> BuildResult:
    meta = project.meta
    ellipsis = meta.get("ellipsis", "...")
    title = meta.get("title_en") or meta.get("title_ko") or project.slug
    author = meta.get("author_en") or ""
    untranslated = [c.id for c in project.chunks if not project.is_translated(c.id)]
    if untranslated and not allow_partial:
        raise SystemExit(
            f"{len(untranslated)} chunk(s) are not translated yet ({', '.join(untranslated[:8])}…). "
            "Translate them, or build a preview with --allow-partial (keeps their Korean)."
        )
    layout = json.loads((project.path / "source" / "regions.json").read_text("utf-8"))
    documents: list[str] = layout["documents"]
    regions: dict[str, list] = layout["regions"]

    english: dict[int, dict[int, str]] = {}
    for chunk in project.chunks:
        if project.is_translated(chunk.id):
            english.setdefault(chunk.chapter, {}).update(segfile.read(project.en(chunk.id)).segments)

    # (document, region) → the blocks read from it, in order, with their English.
    by_region: dict[tuple[int, int], list[dict]] = {}
    labels: dict[str, str] = {}  # document href → English chapter label
    heading_en: dict[str, str] = {}  # Korean heading → English, for TOC labels
    for chapter in meta["chapters"]:
        translated = english.get(chapter["n"], {})
        for block in project.chapter_blocks(chapter["n"]):
            loc = block.get("loc")
            if not loc:
                continue
            entry = dict(block)
            if "n" in block:
                entry["en"] = translated.get(block["n"])
            by_region.setdefault((loc[0], loc[1]), []).append(entry)
            if block["k"] == "h" and entry.get("en"):
                label = strip_markup(normalise_ellipses(entry["en"], ellipsis)).strip()
                labels.setdefault(documents[loc[0]], label)
                heading_en.setdefault(_plain(block["t"]), label)

    source = project.path / meta["source"]
    problems: list[str] = []
    output = Path(output) if output else project.path / "out" / f"{_filename(title)}.epub"
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".epub.tmp")
    chapters = 0
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(tmp, "w") as out:
        out.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        names = src.namelist()
        opf_path = _opf_path(src)
        nav_docs = [n for n in names if n.lower().endswith((".xhtml", ".html", ".htm")) and n not in regions]
        for name in names:
            if name == "mimetype":
                continue
            data = src.read(name)
            if name in regions:
                doc_index = documents.index(name)
                text = _decode_doc(data)
                new = _rewrite_document(text, regions[name], doc_index, by_region, quotes, ellipsis,
                                        labels.get(name, ""))
                if _well_formed(text) and not _well_formed(new):
                    problems.append(f"{name}: rewriting broke the XML; kept the Korean page")
                    new = text
                data = new.encode("utf-8")
                chapters += 1
            elif name == opf_path:
                data = _rewrite_opf(_decode_doc(data), title, author).encode("utf-8")
            elif name.lower().endswith(".ncx"):
                data = _rewrite_ncx(_decode_doc(data), name, labels, heading_en, title).encode("utf-8")
            elif name in nav_docs and b"<nav" in data:
                data = _rewrite_nav(_decode_doc(data), name, labels, heading_en).encode("utf-8")
            out.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    tmp.replace(output)
    return BuildResult(output, chapters, untranslated, problems)


def _plain(text: str) -> str:
    return re.sub(r"\s+", "", strip_markup(text))


def _decode_doc(data: bytes) -> str:
    from .ingest import _decode

    return _decode(data)


def _well_formed(text: str) -> bool:
    try:
        ET.fromstring(XML_DECL.sub("", text).encode("utf-8"))
        return True
    except ET.ParseError:
        return False


def _opf_path(z: zipfile.ZipFile) -> str:
    container = ET.fromstring(z.read("META-INF/container.xml"))
    node = container.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
    return node.get("full-path") if node is not None else ""


def _to_utf8(text: str) -> str:
    """Declare what we write: UTF-8, English."""
    text = XML_DECL.sub('<?xml version="1.0" encoding="utf-8"?>', text, count=1)
    text = re.sub(r'(charset=)(["\'])[\w-]+\2', r'\1\2utf-8\2', text, flags=re.I)  # <meta charset="…">
    text = re.sub(r'(charset=)(?!["\'])[\w-]+', r'\1utf-8', text, flags=re.I)  # content="…; charset=…"
    return re.sub(r'((?:xml:)?lang=)(["\'])ko(?:-KR)?\2', r'\1\2en\2', text, flags=re.I)


def _rewrite_document(text, doc_regions, doc_index, by_region, quotes, ellipsis, label) -> str:
    pieces: list[str] = []
    cursor = 0
    for region_index, (start, end) in enumerate(doc_regions):
        entries = by_region.get((doc_index, region_index), [])
        text_entries = [e for e in entries if "n" in e]
        if not text_entries or any(not e.get("en") for e in text_entries):
            continue  # nothing to translate here, or not translated yet: keep it
        original = text[start:end]
        media = MEDIA_TAG.findall(original)
        images = sum(1 for e in entries if e["k"] == "img" or (e["k"] == "sep" and e.get("src") == "hr"))
        parts: list[str] = []
        if media and images != len(media):
            parts += media  # can't pair them up; keep every image, then the text
            media = []
        previous_text = False
        for entry in entries:
            if entry["k"] in ("img",) or (entry["k"] == "sep" and entry.get("src") == "hr"):
                if media:
                    parts.append(media.pop(0))
                previous_text = False
                continue
            rendered = render_inline(entry.get("en") or entry.get("t", ""), quotes, ellipsis)
            if previous_text:
                parts.append("<br/>")
            parts.append(rendered)
            previous_text = True
        # Keep the whitespace that sat inside the element, for tidy source.
        lead = re.match(r"\s*", original).group(0)
        trail = re.search(r"\s*$", original).group(0)
        pieces.append(text[cursor:start] + lead + "".join(parts) + trail)
        cursor = end
    pieces.append(text[cursor:])
    result = "".join(pieces)
    if label:
        result = re.sub(r"(<title[^>]*>)(.*?)(</title>)",
                        lambda m: m.group(1) + (html.escape(label) if has_hangul(m.group(2)) else m.group(2)) + m.group(3),
                        result, count=1, flags=re.S | re.I)
    return _to_utf8(result)


def _rewrite_opf(text: str, title: str, author: str) -> str:
    text = re.sub(r"(<dc:title\b[^>]*>)(.*?)(</dc:title>)", lambda m: m.group(1) + html.escape(title) + m.group(3),
                  text, count=1, flags=re.S)
    if author:
        text = re.sub(r"(<dc:creator\b[^>]*>)(.*?)(</dc:creator>)",
                      lambda m: m.group(1) + html.escape(author) + m.group(3), text, count=1, flags=re.S)
    text = re.sub(r"(<dc:language\b[^>]*>)\s*ko(?:-KR)?\s*(</dc:language>)", r"\1en\2", text, flags=re.I)
    return _to_utf8(text)


def _label_for(target: str, current: str, labels, heading_en) -> str | None:
    import posixpath
    from urllib.parse import unquote

    href = posixpath.normpath(posixpath.join(posixpath.dirname(target), unquote(current.split("#")[0])))
    return labels.get(href)


def _rewrite_nav(text: str, name: str, labels, heading_en) -> str:
    def swap(m: re.Match) -> str:
        inner = m.group(3)
        if not has_hangul(inner):
            return m.group(0)
        label = heading_en.get(_plain(re.sub(r"<[^>]+>", "", inner))) or _label_for(name, m.group(2), labels, heading_en)
        return m.group(1) + html.escape(label) + m.group(4) if label else m.group(0)

    text = re.sub(r'(<a\b[^>]*href="([^"]*)"[^>]*>)(.*?)(</a>)', swap, text, flags=re.S | re.I)
    return _to_utf8(text)


def _rewrite_ncx(text: str, name: str, labels, heading_en, title: str) -> str:
    def swap(m: re.Match) -> str:
        inner = m.group(2)
        if not has_hangul(inner):
            return m.group(0)
        label = heading_en.get(_plain(html.unescape(inner))) or _label_for(name, m.group(4), labels, heading_en)
        return m.group(1) + html.escape(label) + m.group(3) + m.group(4) + m.group(5) if label else m.group(0)

    text = re.sub(r'(<navLabel>\s*<text>)(.*?)(</text>\s*</navLabel>\s*<content\s+src=")([^"]+)(")', swap, text, flags=re.S)
    text = re.sub(r"(<docTitle>\s*<text>)(.*?)(</text>)", lambda m: m.group(1) + html.escape(title) + m.group(3),
                  text, count=1, flags=re.S)
    return _to_utf8(text)
