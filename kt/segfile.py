"""The line format every chunk is read and written in.

    # c0012 · Chapter 12 · part 1/2
    [[1]] 제12화 재회
    [[2]] 그는 고개를 들었다.
    ~~~ scene break ~~~
    [[3]] "뭐?"

One segment per line, numbered. The English file has the same numbers with
English after them. Chosen over JSON because a model writes it without
escaping every quotation mark in the dialogue, it is about a third cheaper in
tokens, and an editor can fix a line with a plain find-and-replace because
the `[[n]]` prefix makes every line unique.

Lines starting with `#` or `~~~` are comments and structure hints. A blank
line is ignored. Anything else is a stray line — almost always a paragraph the
model broke in two — and is reported, not silently glued back on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

LINE = re.compile(r"^\s*\[\[(\d+)\]\]\s?(.*)$")
LEFTOVER = re.compile(r"\[\[\d+\]\]")


@dataclass
class Parsed:
    segments: dict[int, str] = field(default_factory=dict)
    order: list[int] = field(default_factory=list)
    duplicates: list[int] = field(default_factory=list)
    stray: list[tuple[int, str]] = field(default_factory=list)  # (line no, text)


def parse(text: str) -> Parsed:
    out = Parsed()
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("~~~"):
            continue
        m = LINE.match(line)
        if not m:
            out.stray.append((lineno, stripped))
            continue
        n, body = int(m.group(1)), m.group(2).strip()
        if n in out.segments:
            out.duplicates.append(n)
            continue
        out.segments[n] = body
        out.order.append(n)
    return out


def read(path: Path) -> Parsed:
    return parse(Path(path).read_text(encoding="utf-8"))


def render(segments: list[tuple[int, str]], header: str = "") -> str:
    lines = [f"# {header}"] if header else []
    lines += [f"[[{n}]] {text}" for n, text in segments]
    return "\n".join(lines) + "\n"
