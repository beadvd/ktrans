"""Carrying over work done with the earlier handoff workflow.

The old workflow exported `chunk_NNN.json` (Korean under numeric keys, inline
formatting as ⟦n⟧…⟦/n⟧ markers) and Claude wrote `chunk_NNN.reply.json`
beside each. Chunk boundaries and keys there have nothing to do with the
chunks here, so replies are matched on the Korean text itself, normalised.
A chunk is imported only when every one of its segments is found; partial
matches are reported and left for a translator, so nothing half-done is ever
mistaken for finished.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import segfile
from .project import Project

MARKER_ANY = re.compile(r"[⟦〚]\s*/?\s*\d+\s*/?\s*[⟧〛]")
MARKER_PAIR = re.compile(r"[⟦〚]\s*(\d+)\s*[⟧〛](.*?)[⟦〚]\s*/\s*\d+\s*[⟧〛]", re.S)
OUR_MARKUP = re.compile(r"\{/?[ib]\}|\{br\}")
FENCE = re.compile(r"\A\s*```(?:json)?\s*|\s*```\s*\Z")


def _key(korean: str) -> str:
    text = OUR_MARKUP.sub("", MARKER_ANY.sub("", korean))
    return re.sub(r"\s+", "", text)


def _parse_reply(text: str) -> dict:
    cleaned = FENCE.sub("", text.strip())
    for candidate in (cleaned, cleaned[cleaned.find("{"): cleaned.rfind("}") + 1]):
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except ValueError:
            continue
    return {}


def load_pairs(folder: Path) -> dict[str, str]:
    """Normalised Korean → English, from every answered chunk in the folder."""
    pairs: dict[str, str] = {}
    for source in sorted(Path(folder).glob("chunk_*.json")):
        if ".reply" in source.name:
            continue
        reply = next((p for p in (source.with_name(source.stem + ".reply.json"),
                                  source.with_name(source.stem + ".reply.txt")) if p.is_file()), None)
        if reply is None:
            continue
        try:
            korean = json.loads(source.read_text("utf-8"))
        except ValueError:
            continue
        korean = korean.get("translate", korean)
        english = _parse_reply(reply.read_text("utf-8"))
        for k, ko in korean.items():
            en = english.get(str(k))
            if isinstance(ko, str) and isinstance(en, str) and en.strip():
                pairs[_key(ko)] = en.strip()
    return pairs


def convert_markers(english: str, source: str) -> str:
    """⟦n⟧…⟦/n⟧ → the source segment's {i}/{b} in order, when counts agree."""
    tags = re.findall(r"\{(i|b)\}", source)
    found = MARKER_PAIR.findall(english)
    if tags and len(tags) == len(found):
        it = iter(tags)
        english = MARKER_PAIR.sub(lambda m: (lambda t: f"{{{t}}}{m.group(2)}{{/{t}}}")(next(it)), english)
    return re.sub(r"\s{2,}", " ", MARKER_ANY.sub("", english)).strip()


@dataclass
class ImportResult:
    imported: list[str] = field(default_factory=list)
    partial: list[tuple[str, int, int]] = field(default_factory=list)  # id, found, total
    skipped_existing: int = 0
    pairs: int = 0


def import_folder(project: Project, folder: Path, overwrite: bool = False) -> ImportResult:
    pairs = load_pairs(folder)
    result = ImportResult(pairs=len(pairs))
    for chunk in project.chunks:
        if project.is_translated(chunk.id) and not overwrite:
            result.skipped_existing += 1
            continue
        source = segfile.read(project.ko(chunk.id))
        lines, found = [], 0
        for n in source.order:
            english = pairs.get(_key(source.segments[n]))
            if english is not None:
                found += 1
                lines.append((n, convert_markers(english, source.segments[n])))
        if found == len(source.order) and found:
            project.en(chunk.id).write_text(segfile.render(lines, f"{chunk.id} · imported"), encoding="utf-8")
            result.imported.append(chunk.id)
        elif found:
            result.partial.append((chunk.id, found, len(source.order)))
    return result
