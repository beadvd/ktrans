"""Synthetic inputs. All Korean text here is written for the tests."""

from __future__ import annotations

import struct
import zipfile
import zlib
from pathlib import Path

import pytest

CHAPTERS = [
    ("제1화 낯선 성", [
        "유현은 성문 앞에 서서 한참 동안 위를 올려다보았다.",
        "\"여기가 맞아?\"",
        "세라가 {i}조용히{/i} 고개를 끄덕였다.",
        "* * *",
        "밤이 되자 성 안의 등불이 하나둘 꺼졌다. 유현은 잠들지 못했다.",
        "……",
    ]),
    ("제2화 첫 번째 시험", [
        "아침이 밝았다. 세라는 이미 마당에 나와 있었다.",
        "\"오늘은 검을 들어.\"",
        "유현은 대답 대신 검을 쥐었다. 손바닥이 땀으로 젖어 있었다.",
    ]),
]


def tiny_png() -> bytes:
    """A valid 1×1 PNG, so epubcheck has nothing to complain about."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff")) + chunk(b"IEND", b""))


def make_epub(path: Path, chapters=CHAPTERS, split_second_file: bool = False,
              system_window: bool = False) -> Path:
    def page(title, paragraphs, index):
        body = [f'<h2 class="chapter-title">{title}</h2>']
        for p in paragraphs:
            p = p.replace("{i}", "<em>").replace("{/i}", "</em>")
            body.append(f'<p class="txt">{p}</p>' if p != "* * *" else '<p class="sep">* * *</p>')
        if system_window and index == 2:
            body.append('<div class="sys-window"><p class="sys">[퀘스트 완료]</p></div>')
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ko">'
            f'<head><title>{title}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>'
            f"<body>{''.join(body)}</body></html>"
        )

    items, refs, navs = [], [], []
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/images/cover.png", tiny_png())
        z.writestr("OEBPS/style.css", ".sys-window { border: 2px solid #468; } p.txt { text-indent: 1em; }")
        items.append('<item id="css" href="style.css" media-type="text/css"/>')
        items.append('<item id="cov" href="images/cover.png" media-type="image/png" properties="cover-image"/>')
        for i, (title, paragraphs) in enumerate(chapters, 1):
            z.writestr(f"OEBPS/ch{i}.xhtml", page(title, paragraphs, i))
            items.append(f'<item id="c{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>')
            refs.append(f'<itemref idref="c{i}"/>')
            navs.append(f'<li><a href="ch{i}.xhtml">{title}</a></li>')
        if split_second_file:
            z.writestr("OEBPS/ch2b.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>그날 밤 유현은 오래 걸었다.</p></body></html>')
            items.append('<item id="c2b" href="ch2b.xhtml" media-type="application/xhtml+xml"/>')
            refs.append('<itemref idref="c2b"/>')
        z.writestr("OEBPS/nav.xhtml",
                   '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
                   '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
                   '<head><title>목차</title></head><body>'
                   f'<nav epub:type="toc"><ol>{"".join(navs)}</ol></nav></body></html>')
        items.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0" encoding="utf-8"?>\n'
                   '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id" xml:lang="ko">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<dc:identifier id="id">urn:uuid:12345678-1234-5678-1234-567812345678</dc:identifier>'
                   '<dc:title>낯선 성의 검사</dc:title><dc:creator>김작가</dc:creator><dc:language>ko</dc:language>'
                   '<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata>'
                   f'<manifest>{"".join(items)}</manifest><spine>{"".join(refs)}</spine></package>')
    return path


ENGLISH = {
    1: {1: "Chapter 1: The Strange Castle",
        2: "Yoo-hyun stood before the castle gate and looked up for a long while.",
        3: "\"Is this the place?\"",
        4: "Sera nodded {i}quietly{/i}.",
        5: "When night fell, the lamps inside the castle went out one by one. Yoo-hyun could not sleep.",
        6: "…"},
    2: {1: "Chapter 2: The First Trial",
        2: "Morning came. Sera was already out in the courtyard.",
        3: "\"Pick up the sword today.\"",
        4: "Instead of answering, Yoo-hyun gripped the sword. His palm was damp with sweat."},
}


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.setenv("KT_ROOT", str(tmp_path / "novels"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write_english(project, english=ENGLISH):
    for chunk in project.chunks:
        lines = [f"[[{n}]] {english[chunk.chapter][n]}" for n in range(chunk.first, chunk.last + 1)]
        project.en(chunk.id).write_text("\n".join(lines) + "\n", encoding="utf-8")
