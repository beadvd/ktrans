"""python3 -m kt <command> …

Every command prints something short and useful to a model reading it: the
subagents and the orchestrating session call these through Bash, so output
size is context size. Long output goes to reports/, with a one-line pointer.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

from . import build as build_mod
from . import check as check_mod
from . import glossary, packet, project as project_mod, segfile
from .project import Project

# Default size of one translator subagent's assignment. Three typical chapters:
# enough to amortise reading the agent brief and style sheet, small enough
# that the subagent's own context stays well under 100k tokens.
BATCH_CHARS = 20_000
BATCH_MAX_CHUNKS = 4
# Copyedit batches are measured in English words the editor must read.
EDIT_BATCH_WORDS = 14_000


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m kt", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="ingest a novel (.epub .txt .docx .html .md) and plan chunks")
    p.add_argument("file")
    p.add_argument("--slug", default="")
    p.add_argument("--max-chars", type=int, default=project_mod.MAX_CHUNK_CHARS)
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("status", help="progress and the next step")
    p.add_argument("book", nargs="?")

    p = sub.add_parser("candidates", help="likely names/terms not yet in the glossary")
    p.add_argument("book", nargs="?")
    p.add_argument("--top", type=int, default=80)
    p.add_argument("--chapters", default="", help="limit to a range, e.g. 1-10")

    p = sub.add_parser("source", help="print Korean text of chapters, compactly (for the lore pass)")
    p.add_argument("book", nargs="?")
    p.add_argument("--chapters", default="1-3")
    p.add_argument("--max-chars", type=int, default=40_000)

    p = sub.add_parser("packet", help="everything a translator needs for one chunk")
    p.add_argument("book")
    p.add_argument("chunk")
    p.add_argument("--no-style", action="store_true", help="omit style.md (already read this session)")

    p = sub.add_parser("brief", help="style + glossary for an edit batch")
    p.add_argument("book")
    p.add_argument("chunks", nargs="+")

    p = sub.add_parser("show", help="aligned Korean/English for a chunk")
    p.add_argument("book")
    p.add_argument("chunk")
    p.add_argument("segments", nargs="?", default="", help="e.g. 12-15,40")

    p = sub.add_parser("check", help="validate translated chunks (exit 1 on ERROR)")
    p.add_argument("book")
    p.add_argument("chunks", nargs="*", help="ids or ranges; default: all translated")

    p = sub.add_parser("checklog", help="issue counts per code: first check of each chunk vs latest")
    p.add_argument("book", nargs="?")
    p.add_argument("--chunks", default="", help="limit to ids/ranges, e.g. c0004-c0040")

    p = sub.add_parser("next", help="the next translation assignments")
    p.add_argument("book", nargs="?")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--batch-chars", type=int, default=BATCH_CHARS)
    p.add_argument("--max-chunks", type=int, default=BATCH_MAX_CHUNKS)

    p = sub.add_parser("merge", help="fold translators' notes/ into the glossary; report conflicts")
    p.add_argument("book", nargs="?")

    p = sub.add_parser("audit", help="whole-book consistency report → reports/audit.md")
    p.add_argument("book", nargs="?")

    p = sub.add_parser("replace", help="book-wide find/replace in the English")
    p.add_argument("book")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--regex", action="store_true")
    p.add_argument("--word", action="store_true", help="whole words only")
    p.add_argument("--chunks", default="", help="limit to ids/ranges")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("edit-next", help="the next copyedit assignments")
    p.add_argument("book", nargs="?")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--batch-words", type=int, default=EDIT_BATCH_WORDS)

    p = sub.add_parser("mark-edited", help="record chunks as copyedited")
    p.add_argument("book")
    p.add_argument("chunks", nargs="+")

    p = sub.add_parser("snapshot", help="copy en/ to en_draft/ (before the copyedit)")
    p.add_argument("book", nargs="?")

    p = sub.add_parser("meta", help="show or set English title/author")
    p.add_argument("book", nargs="?")
    p.add_argument("--title-en")
    p.add_argument("--author-en")
    p.add_argument("--copyedit", choices=["proof", "line", "none"], help="copyedit depth for this book")

    p = sub.add_parser("build", help="assemble the English EPUB")
    p.add_argument("book", nargs="?")
    p.add_argument("--allow-partial", action="store_true")
    p.add_argument("--output")
    p.add_argument("--straight-quotes", action="store_true")
    p.add_argument("--fresh", action="store_true",
                   help="EPUB sources: build a clean new book instead of writing into the original pages")

    p = sub.add_parser("import-handoff", help="import replies from an earlier handoff-workflow folder")
    p.add_argument("book")
    p.add_argument("folder")
    p.add_argument("--overwrite", action="store_true")

    p = sub.add_parser("validate", help="check an EPUB file")
    p.add_argument("epub")

    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    return COMMANDS[args.command](args) or 0


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_init(args) -> int:
    proj = project_mod.init(Path(args.file), slug=args.slug, max_chars=args.max_chars, force=args.force)
    meta = proj.meta
    chars = sum(c["chars"] for c in meta["chapters"])
    split = sum(1 for c in proj.chunks if c.parts > 1 and c.part == 1)
    empty = [c["n"] for c in meta["chapters"] if c["segments"] == 0]
    print(f"Project: {proj.path}  (slug: {proj.slug})")
    print(f"Title: {meta['title_ko']}  ·  Author: {meta['author_ko'] or '—'}")
    print(f"{len(meta['chapters'])} chapters · {len(proj.chunks)} chunks · {chars:,} Korean characters")
    if split:
        print(f"{split} long chapter(s) split into parts at ≤{args.max_chars:,} chars")
    if empty:
        print(f"Chapters with no text (images only?): {empty[:10]}")
    sizes = sorted(c.chars for c in proj.chunks)
    print(f"Chunk size: median {sizes[len(sizes) // 2]:,}, max {sizes[-1]:,} chars")
    print(f"First chapter titles: " + " | ".join(c["title_ko"][:24] for c in meta["chapters"][:5]))
    print(f"Next: python3 -m kt candidates {proj.slug}")
    return 0


def cmd_status(args) -> int:
    proj = Project.find(args.book)
    chunks = proj.chunks
    done = [c for c in chunks if proj.is_translated(c.id)]
    edited = set(proj.state.get("edited", []))
    terms = glossary.load(proj.glossary_file)
    notes = list((proj.path / "notes").glob("c*.md"))
    title = proj.meta.get("title_en") or proj.meta.get("title_ko")
    print(f"{proj.slug}: {title}")
    print(f"  translated  {len(done)}/{len(chunks)} chunks "
          f"(chapters {len({c.chapter for c in done})}/{len(proj.meta['chapters'])})")
    print(f"  copyedited  {len(edited)}/{len(chunks)}")
    print(f"  glossary    {len(terms)} entries · style.md {'set' if _style_filled(proj) else 'NOT FILLED IN'}")
    if notes:
        print(f"  unmerged notes: {len(notes)} → python3 -m kt merge {proj.slug}")
    outputs = sorted((proj.path / "out").glob("*.epub"))
    if outputs:
        print(f"  built       {outputs[-1]}")
    if not terms or not _style_filled(proj):
        step = "lore: build glossary.md + style.md (kt candidates / kt source)"
    elif len(done) < len(chunks):
        first = next(c for c in chunks if not proj.is_translated(c.id))
        step = f"translate from {first.id} (chapter {first.chapter}) — kt next"
    elif len(edited) < len(chunks):
        step = "audit + copyedit — kt audit, then kt edit-next"
    else:
        step = "build — kt build"
    print(f"  next step   {step}")
    return 0


def _style_filled(proj: Project) -> bool:
    if not proj.style_file.is_file():
        return False
    text = proj.style_file.read_text("utf-8")
    return "Genre / setting:" not in text or bool(re.search(r"Genre / setting:[ \t]*\S", text))


def _chapter_range(spec: str, total: int) -> tuple[int, int]:
    if not spec:
        return 1, total
    a, _, b = spec.partition("-")
    return int(a), int(b or a)


def cmd_candidates(args) -> int:
    proj = Project.find(args.book)
    lo, hi = _chapter_range(args.chapters, len(proj.meta["chapters"]))
    texts = ["\n".join(proj.source_segments(c).values()) for c in proj.chunks if lo <= c.chapter <= hi]
    found = glossary.candidates(texts, glossary.load(proj.glossary_file), top=args.top)
    print(f"{len(found)} candidates (count · distinct forms · first context), chapters {lo}–{hi}:")
    for cand in found:
        print(f"  {cand.stem}\t{cand.count}\t{cand.forms}\t…{cand.example}…")
    return 0


def cmd_source(args) -> int:
    proj = Project.find(args.book)
    lo, hi = _chapter_range(args.chapters, len(proj.meta["chapters"]))
    budget = args.max_chars
    for chunk in proj.chunks:
        if not lo <= chunk.chapter <= hi:
            continue
        parsed = segfile.read(proj.ko(chunk.id))
        print(f"## {chunk.label()}")
        for n in parsed.order:
            text = parsed.segments[n]
            budget -= len(text)
            if budget < 0:
                print("… (truncated at --max-chars)")
                return 0
            print(text)
    return 0


def cmd_packet(args) -> int:
    proj = Project.find(args.book)
    print(packet.translator_packet(proj, proj.chunk(args.chunk), with_style=not args.no_style), end="")
    return 0


def cmd_brief(args) -> int:
    proj = Project.find(args.book)
    print(packet.editor_brief(proj, proj.resolve(args.chunks)), end="")
    return 0


def cmd_show(args) -> int:
    proj = Project.find(args.book)
    numbers = packet.parse_numbers(args.segments) if args.segments else None
    print(packet.aligned(proj, proj.chunk(args.chunk), numbers), end="")
    return 0


def cmd_check(args) -> int:
    proj = Project.find(args.book)
    chunks = proj.resolve(args.chunks) if args.chunks else [c for c in proj.chunks if proj.is_translated(c.id)]
    entries = glossary.load(proj.glossary_file)
    report = check_mod.Report()
    for chunk in chunks:
        own = check_mod.check_chunk(proj, chunk, entries=entries)
        check_mod.log_run(proj, chunk.id, own)
        report.issues.extend(own.issues)
    label = f"{chunks[0].id}–{chunks[-1].id}" if len(chunks) > 1 else chunks[0].id
    print(f"check {label}: {report.errors} error(s), {report.warnings} warning(s)")
    if report.issues:
        print(report.text())
    return 1 if report.errors else 0


def cmd_checklog(args) -> int:
    proj = Project.find(args.book)
    only = {c.id for c in proj.resolve([args.chunks])} if args.chunks else None
    first, last, chunks = check_mod.summarise_log(proj, only)
    if not chunks:
        print("No checks logged yet (reports/check_log.jsonl is written by kt check).")
        return 0
    print(f"check log: {chunks} chunk(s) · first check of each chunk vs its latest")
    print(f"  {'first':>5}  {'latest':>6}  code")
    order = {"ERROR": 0, "WARN": 1, "NOTE": 2}
    for key in sorted(first.keys() | last.keys(), key=lambda k: (order.get(k.split()[0], 3), k)):
        print(f"  {first[key]:5}  {last[key]:6}  {key}")
    return 0


def cmd_next(args) -> int:
    proj = Project.find(args.book)
    pending = [c for c in proj.chunks if not proj.is_translated(c.id)]
    if not pending:
        print("All chunks are translated. Next: python3 -m kt merge, then python3 -m kt audit.")
        return 0
    batches: list[list] = []
    current: list = []
    size = 0
    all_ids = [c.id for c in proj.chunks]
    chapter_chars: dict[int, int] = {}
    for c in pending:
        chapter_chars[c.chapter] = chapter_chars.get(c.chapter, 0) + c.chars
    for chunk in pending:
        contiguous = current and all_ids.index(chunk.id) == all_ids.index(current[-1].id) + 1
        # Only close a batch at a chapter start, so the parts of a split
        # chapter stay with one translator and each sees the previous part's
        # English. A hard cap keeps a pathological chapter from running away.
        full = current and (
            (chunk.part == 1 and (size + chapter_chars[chunk.chapter] > args.batch_chars
                                  or len(current) >= args.max_chunks))
            or len(current) >= args.max_chunks + 3
        )
        if current and (full or not contiguous):
            batches.append(current)
            current, size = [], 0
            if len(batches) >= args.workers:
                break
        current.append(chunk)
        size += chunk.chars
    if current and len(batches) < args.workers:
        batches.append(current)
    remaining = len(pending) - sum(len(b) for b in batches)
    print(f"{len(batches)} assignment(s); {remaining} chunk(s) remain after these.")
    for i, batch in enumerate(batches, 1):
        chapters = sorted({c.chapter for c in batch})
        span = f"{chapters[0]}" if len(chapters) == 1 else f"{chapters[0]}–{chapters[-1]}"
        print(f"  [{i}] {' '.join(c.id for c in batch)}   (chapters {span}, {sum(c.chars for c in batch):,} chars)")
    return 0


def cmd_merge(args) -> int:
    proj = Project.find(args.book)
    result = glossary.merge_notes(proj.glossary_file, proj.path / "notes")
    print(f"merged {result.files} note file(s): {len(result.added)} new glossary row(s), "
          f"{len(result.conflicts)} conflict(s), {len(result.doubts)} doubt(s)")
    for entry in result.added[:40]:
        print(f"  + {entry.korean} → {entry.english} ({entry.type or '?'}{', ' + entry.pronoun if entry.pronoun else ''})")
    log = []
    for existing, proposed, chunk in result.conflicts:
        line = (f"CONFLICT {chunk}: {existing.korean} is '{existing.english}' ({existing.pronoun or '-'}) "
                f"in glossary; translator used '{proposed.english}' ({proposed.pronoun or '-'})")
        print("  " + line)
        log.append(line)
    for chunk, doubt in result.doubts:
        line = f"DOUBT {chunk}: {doubt}"
        print("  " + line[:220])
        log.append(line)
    if log:
        with open(proj.reports / "merge_log.md", "a", encoding="utf-8") as fh:
            fh.write("\n".join(f"- {l}" for l in log) + "\n")
    if result.conflicts:
        print("Resolve conflicts: keep the glossary spelling and fix the chunk with "
              "`python3 -m kt replace <book> OLD NEW --chunks <id>`, or change the glossary and replace book-wide.")
    return 0


def cmd_audit(args) -> int:
    proj = Project.find(args.book)
    report, markdown = check_mod.audit(proj)
    path = proj.reports / "audit.md"
    path.write_text(markdown, encoding="utf-8")
    counts: dict[str, int] = {}
    for issue in report.issues:
        counts[f"{issue.level} {issue.code}"] = counts.get(f"{issue.level} {issue.code}", 0) + 1
    print(f"audit: {report.errors} error(s), {report.warnings} warning(s) → {path}")
    for key, n in sorted(counts.items()):
        print(f"  {n:5}  {key}")
    return 1 if report.errors else 0


def cmd_replace(args) -> int:
    proj = Project.find(args.book)
    chunks = proj.resolve([args.chunks]) if args.chunks else proj.chunks
    pattern = args.old if args.regex else re.escape(args.old)
    if args.word:
        pattern = rf"(?<![\w-]){pattern}(?![\w-])"
    regex = re.compile(pattern)
    total = 0
    for chunk in chunks:
        path = proj.en(chunk.id)
        if not path.is_file():
            continue
        text = path.read_text("utf-8")
        # Never touch the [[n]] prefixes.
        lines = text.split("\n")
        changed = 0
        for i, line in enumerate(lines):
            m = segfile.LINE.match(line)
            if not m:
                continue
            body, count = regex.subn(args.new, m.group(2))
            if count:
                lines[i] = f"[[{m.group(1)}]] {body}"
                changed += count
        if changed:
            total += changed
            print(f"  {chunk.id}: {changed}")
            if not args.dry_run:
                path.write_text("\n".join(lines), encoding="utf-8")
    print(f"{'would replace' if args.dry_run else 'replaced'} {total} occurrence(s)")
    return 0


def cmd_edit_next(args) -> int:
    proj = Project.find(args.book)
    edited = set(proj.state.get("edited", []))
    untranslated = [c.id for c in proj.chunks if not proj.is_translated(c.id)]
    if untranslated:
        print(f"Warning: {len(untranslated)} chunk(s) are still untranslated; they are skipped.")
    pending = [c for c in proj.chunks if c.id not in edited and proj.is_translated(c.id)]
    if not pending:
        print("Everything is copyedited. Next: python3 -m kt audit, then python3 -m kt build.")
        return 0
    if not (proj.path / "en_draft").exists():
        _snapshot(proj)
        print("(snapshot of the pre-edit translation saved to en_draft/)")
    batches, current, words = [], [], 0
    for chunk in pending:
        count = sum(len(t.split()) for t in segfile.read(proj.en(chunk.id)).segments.values())
        # Keep whole chapters together; a batch may run over rather than split one.
        boundary = not current or chunk.chapter != current[-1].chapter
        if current and boundary and words + count > args.batch_words:
            batches.append(current)
            current, words = [], 0
            if len(batches) >= args.workers:
                break
        current.append(chunk)
        words += count
    if current and len(batches) < args.workers:
        batches.append(current)
    left = len(pending) - sum(len(b) for b in batches)
    print(f"{len(batches)} edit assignment(s); {left} chunk(s) remain after these.")
    for i, batch in enumerate(batches, 1):
        print(f"  [{i}] {batch[0].id}-{batch[-1].id}   (chapters {batch[0].chapter}–{batch[-1].chapter}, {len(batch)} chunks)")
    return 0


def cmd_mark_edited(args) -> int:
    proj = Project.find(args.book)
    state = proj.state
    edited = set(state.get("edited", []))
    chunks = proj.resolve(args.chunks)
    edited |= {c.id for c in chunks}
    state["edited"] = sorted(edited)
    proj.save_state(state)
    print(f"marked {len(chunks)} chunk(s) edited; {len(edited)}/{len(proj.chunks)} total")
    return 0


def _snapshot(proj: Project) -> Path:
    target = proj.path / "en_draft"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(proj.path / "en", target)
    return target


def cmd_snapshot(args) -> int:
    proj = Project.find(args.book)
    print(f"copied en/ → {_snapshot(proj)}")
    return 0


def cmd_meta(args) -> int:
    proj = Project.find(args.book)
    if args.title_en is not None:
        proj.meta["title_en"] = args.title_en
    if args.author_en is not None:
        proj.meta["author_en"] = args.author_en
    if args.copyedit is not None:
        proj.meta["copyedit"] = args.copyedit
    if args.title_en is not None or args.author_en is not None or args.copyedit is not None:
        proj.save_meta()
    m = proj.meta
    print(f"title_ko: {m['title_ko']}\nauthor_ko: {m['author_ko']}\ntitle_en: {m['title_en']}\n"
          f"author_en: {m['author_en']}\ncopyedit: {m.get('copyedit', '(not chosen)')}")
    return 0


def cmd_build(args) -> int:
    proj = Project.find(args.book)
    inplace = (proj.path / "source" / "regions.json").is_file() and not args.fresh
    builder = build_mod.build_inplace if inplace else build_mod.build
    result = builder(proj, allow_partial=args.allow_partial,
                     output=Path(args.output) if args.output else None,
                     quotes=not args.straight_quotes)
    print("mode: " + ("original EPUB, formatting preserved" if inplace else "clean rebuild"))
    build_mod.write_report(proj, result)
    size = result.path.stat().st_size / 1_048_576
    print(f"built {result.path}  ({result.chapters} chapters, {size:.2f} MB)")
    if result.untranslated:
        print(f"  partial: {len(result.untranslated)} chunk(s) left in Korean")
    for problem in result.problems[:20]:
        print(f"  ! {problem}")
    return _validate(result.path) or (1 if result.problems else 0)


def cmd_import_handoff(args) -> int:
    from .handoff import import_folder

    proj = Project.find(args.book)
    result = import_folder(proj, Path(args.folder), overwrite=args.overwrite)
    print(f"{result.pairs} translated paragraphs found in {args.folder}")
    print(f"imported {len(result.imported)} chunk(s) completely"
          + (f": {result.imported[0]}–{result.imported[-1]}" if result.imported else ""))
    if result.skipped_existing:
        print(f"skipped {result.skipped_existing} chunk(s) already translated here (use --overwrite)")
    for chunk_id, found, total in result.partial[:20]:
        print(f"  partial {chunk_id}: {found}/{total} segments matched — left for a translator")
    if result.imported:
        print(f"Next: python3 -m kt check {proj.slug}")
    return 0


def cmd_validate(args) -> int:
    return _validate(Path(args.epub))


def _validate(path: Path) -> int:
    problems = build_mod.validate(path)
    if problems:
        print("structure: " + str(len(problems)) + " problem(s)")
        for problem in problems[:20]:
            print(f"  ✗ {problem}")
    else:
        print("structure: ok (zip layout, OPF, manifest, spine, nav, every XHTML well-formed)")
    official = build_mod.epubcheck(path)
    if official is None:
        print("epubcheck: not installed (optional: brew install epubcheck, or set EPUBCHECK_JAR)")
    else:
        ok, output = official
        print("epubcheck: " + ("passed" if ok else "FAILED"))
        if not ok:
            print(output)
    return 1 if problems or (official and not official[0]) else 0


COMMANDS = {
    "init": cmd_init, "status": cmd_status, "candidates": cmd_candidates, "source": cmd_source,
    "packet": cmd_packet, "brief": cmd_brief, "show": cmd_show, "check": cmd_check,
    "checklog": cmd_checklog,
    "next": cmd_next, "merge": cmd_merge, "audit": cmd_audit, "replace": cmd_replace,
    "edit-next": cmd_edit_next, "mark-edited": cmd_mark_edited, "snapshot": cmd_snapshot,
    "meta": cmd_meta, "build": cmd_build, "validate": cmd_validate,
    "import-handoff": cmd_import_handoff,
}
