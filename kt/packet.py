"""What a translator or editor subagent reads, rendered compactly.

A subagent's context is re-read on every turn it takes, so everything here is
sized deliberately: only the glossary rows that occur in the chunk, the style
sheet once per subagent rather than once per chunk, and a short tail of the
previous chunk's finished English so the voice carries across the seam.
"""

from __future__ import annotations

from . import glossary, segfile
from .project import Chunk, Project

CONTEXT_CHARS = 1500
KOREAN_CONTEXT_CHARS = 600


def previous_context(project: Project, chunk: Chunk) -> tuple[str, str]:
    """(kind, text): the end of the previous chunk, English if it exists."""
    chunks = project.chunks
    index = next(i for i, c in enumerate(chunks) if c.id == chunk.id)
    if index == 0:
        return "", ""
    prior = chunks[index - 1]
    if project.is_translated(prior.id):
        parsed = segfile.read(project.en(prior.id))
        return "english", _tail([parsed.segments[n] for n in parsed.order], CONTEXT_CHARS)
    parsed = segfile.read(project.ko(prior.id))
    return "korean", _tail([parsed.segments[n] for n in parsed.order], KOREAN_CONTEXT_CHARS)


def _tail(paragraphs: list[str], limit: int) -> str:
    out: list[str] = []
    size = 0
    for text in reversed(paragraphs):
        if size + len(text) > limit and out:
            break
        out.append(text)
        size += len(text)
    return "\n".join(reversed(out))


def style_text(project: Project) -> str:
    return project.style_file.read_text("utf-8").strip() if project.style_file.is_file() else "(no style sheet yet)"


def translator_packet(project: Project, chunk: Chunk, *, with_style: bool = True) -> str:
    source = project.ko(chunk.id).read_text("utf-8")
    entries = glossary.relevant(glossary.load(project.glossary_file), source)
    kind, context = previous_context(project, chunk)
    chapter = project.meta["chapters"][chunk.chapter - 1]
    rel = project.path

    out = [
        f"=== PACKET {chunk.label()} ===",
        f"Book: {project.meta.get('title_en') or project.meta.get('title_ko')}  ·  chapter title (Korean): {chapter.get('title_ko', '')}",
        f"Write the English to:  {rel / 'en' / (chunk.id + '.txt')}",
        f"Write notes (only if needed) to:  {rel / 'notes' / (chunk.id + '.md')}",
        "",
    ]
    if with_style:
        out += ["--- STYLE SHEET (style.md) ---", style_text(project), ""]
    out += [
        f"--- GLOSSARY: {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} occurring in this chunk (authoritative) ---",
        glossary.table(entries),
        "",
    ]
    if context:
        if kind == "english":
            out += ["--- PREVIOUS ENGLISH (end of the preceding chunk; continue its voice; do NOT translate or repeat it) ---"]
        else:
            out += ["--- PREVIOUS KOREAN (not yet translated; context for names and tense only; do NOT translate it) ---"]
        out += [context, ""]
    else:
        out += ["--- (this is the first chunk of the book) ---", ""]
    out += ["--- SOURCE: translate every [[n]] line ---", source.strip(), "=== END PACKET ==="]
    return "\n".join(out) + "\n"


def editor_brief(project: Project, chunks: list[Chunk]) -> str:
    """Style sheet plus the glossary rows relevant to a batch of chunks."""
    source = "\n".join(project.ko(c.id).read_text("utf-8") for c in chunks)
    entries = glossary.relevant(glossary.load(project.glossary_file), source)
    first = chunks[0]
    kind, context = previous_context(project, first)
    out = [
        f"=== EDIT BRIEF {chunks[0].id}–{chunks[-1].id} (chapters {chunks[0].chapter}–{chunks[-1].chapter}) ===",
        "Files to edit (in order):",
        *[f"  {project.en(c.id)}" for c in chunks],
        "",
        "--- STYLE SHEET (style.md) ---",
        style_text(project),
        "",
        f"--- GLOSSARY: {len(entries)} entries occurring in these chapters ---",
        glossary.table(entries),
        "",
    ]
    if context and kind == "english":
        out += ["--- END OF THE PRECEDING CHAPTER (already edited or pending; for continuity only) ---", context, ""]
    out.append("=== END BRIEF ===")
    return "\n".join(out) + "\n"


def aligned(project: Project, chunk: Chunk, numbers: list[int] | None = None) -> str:
    ko = segfile.read(project.ko(chunk.id)).segments
    en = segfile.read(project.en(chunk.id)).segments if project.is_translated(chunk.id) else {}
    out = [f"# {chunk.label()}"]
    for n in sorted(ko):
        if numbers and n not in numbers:
            continue
        out.append(f"[[{n}]] KO: {ko[n]}")
        out.append(f"{' ' * (len(str(n)) + 4)} EN: {en.get(n, '(missing)')}")
    return "\n".join(out) + "\n"


def parse_numbers(spec: str) -> list[int]:
    numbers: list[int] = []
    for piece in spec.split(","):
        piece = piece.strip()
        if not piece:
            continue
        a, _, b = piece.partition("-")
        numbers += list(range(int(a), int(b or a) + 1))
    return numbers
