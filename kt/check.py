"""Mechanical checks: per chunk after translating, and across the whole book.

A prompt asks a model to behave; this asserts that it did. Everything here is
deterministic and cheap, so a translator runs it on every chunk before
reporting back, and an ERROR means the chunk is not done.

It cannot judge accuracy or prose. That is what the editor pass is for.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import glossary, segfile
from .project import Chunk, Project
from .text import HANGUL, hangul_sample, levenshtein, markup_balanced, strip_markup, word_count

CJK_BRACKETS = re.compile(r"[「」『』【】〈〉《》]")
PRONOUN_META = re.compile(r"\((?:he|she|they)(?:/(?:he|she|they))*\)", re.I)
MARKDOWN = re.compile(r"\*\*|__|`|^#{1,6}\s|(?<!\w)\*[^*\s][^*]{0,80}\*(?!\w)")
TRANSLATOR_NOTE = re.compile(r"\b(?:TN|T/N|Translator'?s? note)\s*[:：]|\(\s*note\s*:", re.I)
SENTENCE = re.compile(r"[^.!?…]+[.!?…]+[\"'”’]?")
MARKER = re.compile(r"\{/?[ib]\}")
# A paragraph that ends: letter + closing quote. "Of course, Mira" needs its full stop.
DIALOGUE_UNFINISHED = re.compile(r"[A-Za-z][\"\u201d]\s*$")

# Phrases a Korean→English draft over-produces. Counted, not banned: the
# editor decides. Rates are per 10,000 words.
TRANSLATIONESE = {
    "could not help but": r"\bcould(?:n't| not) help but\b",
    "let out a (sigh/laugh/…)": r"\blet out an? \w+",
    "a moment later / for a moment": r"\bfor a moment\b|\ba moment later\b",
    "in that moment / at that moment": r"\b(?:in|at) that moment\b",
    "it was at that moment that": r"\bit was (?:at|in) that moment\b",
    "somehow": r"\bsomehow\b",
    "as if / as though": r"\bas (?:if|though)\b",
    "the corners of (his/her) mouth": r"\bcorners? of (?:his|her|their) (?:mouth|lips)\b",
    "clicked (his/her) tongue": r"\bclicked (?:his|her|their) tongue\b",
    "(his/her) expression hardened / darkened": r"\bexpression (?:hardened|darkened|stiffened)\b",
    "unable to hide": r"\bunable to hide\b",
    "nodded (his/her) head": r"\bnodded (?:his|her|their) head\b",
    "inwardly / inner thoughts": r"\binwardly\b|\binner thoughts?\b",
    "heart pounded / thumped": r"\bheart (?:pounded|thumped|raced)\b",
    "welled up (often objectless)": r"\bwell(?:ed|s|ing) up\b",
    "the very": r"\bthe very\b",
    "ha / haa / heok (transliterated)": r"\b(?:haa+|heok|keuk|kuk|hmph)\b",
}


@dataclass
class Issue:
    level: str  # ERROR | WARN | NOTE
    code: str
    where: str
    detail: str


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)

    def add(self, level: str, code: str, where: str, detail: str) -> None:
        self.issues.append(Issue(level, code, where, detail))

    @property
    def errors(self) -> int:
        return sum(1 for i in self.issues if i.level == "ERROR")

    @property
    def warnings(self) -> int:
        return sum(1 for i in self.issues if i.level == "WARN")

    def text(self, limit_per_code: int = 8) -> str:
        if not self.issues:
            return "clean"
        order = {"ERROR": 0, "WARN": 1, "NOTE": 2}
        shown: Counter[str] = Counter()
        lines = []
        for issue in sorted(self.issues, key=lambda i: (order[i.level], i.where)):
            shown[issue.code] += 1
            if shown[issue.code] == limit_per_code + 1:
                lines.append(f"  …more {issue.code} (see full report)")
            if shown[issue.code] > limit_per_code:
                continue
            lines.append(f"  {issue.level:5} {issue.code:20} {issue.where:14} {issue.detail}")
        return "\n".join(lines)


# ── one chunk ────────────────────────────────────────────────────────────────

def check_chunk(project: Project, chunk: Chunk, report: Report | None = None,
                entries: list[glossary.Entry] | None = None) -> Report:
    report = report or Report()
    ko = segfile.read(project.ko(chunk.id))
    if not project.is_translated(chunk.id):
        report.add("ERROR", "missing-file", chunk.id, f"{project.en(chunk.id)} does not exist")
        return report
    en = segfile.read(project.en(chunk.id))
    where = chunk.id

    missing = [n for n in ko.order if n not in en.segments]
    extra = [n for n in en.order if n not in ko.segments]
    if missing:
        report.add("ERROR", "missing-segments", where, f"{len(missing)} missing: {_ranges(missing)}")
    if extra:
        report.add("ERROR", "extra-segments", where, f"numbers not in the source: {_ranges(extra)}")
    if en.duplicates:
        report.add("ERROR", "duplicate-number", where, f"numbered twice: {_ranges(en.duplicates)}")
    for lineno, text in en.stray[:5]:
        report.add("ERROR", "stray-line", f"{where}:L{lineno}", f"line without [[n]] — split paragraph or commentary? {text[:60]!r}")
    order = [n for n in en.order if n in ko.segments]
    if order != sorted(order):
        report.add("ERROR", "out-of-order", where, "segments are not in source order")

    ko_words = 0
    for n in ko.order:
        source = ko.segments[n]
        target = en.segments.get(n)
        if target is None:
            continue
        at = f"{where}[[{n}]]"
        if not target.strip():
            report.add("ERROR", "empty", at, "empty translation")
            continue
        if HANGUL.search(target):
            report.add("ERROR", "hangul", at, f"…{hangul_sample(target)}…")
        if "[[" in target or "]]" in target:
            report.add("ERROR", "leftover-marker", at, "[[ ]] inside the text — two segments merged?")
        if not markup_balanced(target):
            report.add("ERROR", "markup", at, "unbalanced {i}/{b} markers")
        if len(MARKER.findall(source)) != len(MARKER.findall(target)):
            report.add("WARN", "markup-count", at, "source emphasis markers not reproduced one-for-one")
        if PRONOUN_META.search(target):
            report.add("ERROR", "pronoun-meta", at, "glossary pronoun leaked into the prose")
        if TRANSLATOR_NOTE.search(target):
            report.add("ERROR", "translator-note", at, "translator's note in the prose — put it in notes/")
        if MARKDOWN.search(target):
            report.add("ERROR", "markdown", at, "Markdown would print literally — use {i}…{/i}")
        if "~" in target or "\uff5e" in target:
            report.add("WARN", "tilde", at, "tilde in English — a drawn-out word takes a hyphen (ve-ery)")
        if DIALOGUE_UNFINISHED.search(target):
            report.add("WARN", "quote-punctuation", at, "dialogue ends without punctuation before the closing quote")
        if CJK_BRACKETS.search(target) and not CJK_BRACKETS.search(source):
            report.add("WARN", "cjk-bracket", at, "CJK bracket that is not in the source")
        ko_len = len(strip_markup(source).strip())
        en_len = len(strip_markup(target).strip())
        ko_words += ko_len
        if ko_len >= 40:
            ratio = en_len / ko_len
            if ratio < 0.9:
                report.add("WARN", "short", at, f"English is {ratio:.1f}× the Korean length — omission?")
            elif ratio > 5.5:
                report.add("WARN", "long", at, f"English is {ratio:.1f}× the Korean length — addition?")

    texts = [en.segments[n] for n in en.order if n in ko.segments]
    _duplicate_sentences(texts, [n for n in en.order if n in ko.segments], where, report)
    _dash_rate("\n".join(texts), where, report)
    _glossary_presence(entries if entries is not None else glossary.load(project.glossary_file),
                       "\n".join(ko.segments.values()), "\n".join(texts), where, report)
    return report


def _ranges(numbers: list[int]) -> str:
    numbers = sorted(set(numbers))
    out, start = [], None
    for i, n in enumerate(numbers):
        if start is None:
            start = n
        if i + 1 == len(numbers) or numbers[i + 1] != n + 1:
            out.append(f"{start}" if start == n else f"{start}–{n}")
            start = None
    text = ", ".join(out)
    return text if len(text) < 80 else text[:77] + "…"


def _norm(sentence: str) -> str:
    sentence = re.sub(r"[‘’“”'\"]", "", strip_markup(sentence))
    sentence = re.sub(r"[^\w\s]", " ", sentence)
    return " ".join(sentence.lower().split())


def _duplicate_sentences(texts: list[str], numbers: list[int], where: str, report: Report) -> None:
    """A sentence or paragraph emitted twice by accident — the echo defect."""
    bodies: dict[str, int] = {}
    for n, text in zip(numbers, texts):
        key = _norm(text)
        if len(key) > 60 and key in bodies:
            report.add("ERROR", "repeated-paragraph", f"{where}[[{n}]]", f"same text as [[{bodies[key]}]]")
        bodies.setdefault(key, n)
    seen: dict[str, int] = {}
    for n, text in zip(numbers, texts):
        for sentence in SENTENCE.findall(strip_markup(text)):
            key = _norm(sentence)
            if len(key.split()) < 7:
                continue
            if key in seen and seen[key] != n:
                report.add("ERROR", "repeated-sentence", f"{where}[[{n}]]",
                           f"also in [[{seen[key]}]]: {sentence.strip()[:60]!r}")
            seen.setdefault(key, n)


def _dash_rate(text: str, where: str, report: Report, limit: float = 4.0) -> None:
    words = max(word_count(text), 1)
    dashes = text.count("—") + text.count(" -- ")
    rate = dashes / words * 1000
    if dashes >= 4 and rate > limit:
        report.add("WARN", "em-dash-rate", where, f"{dashes} em-dashes in {words} words ({rate:.1f}/1000; aim ≤ 3)")


def _glossary_presence(entries, ko_text: str, en_text: str, where: str, report: Report) -> None:
    """A Korean term occurs in the chunk but its English never does.

    Chunk-level on purpose: a single paragraph may legitimately replace a name
    with "he", but a whole chunk that mentions 유현 five times and never says
    Yoo-hyun has almost certainly spelled him some other way.
    """
    lowered = en_text.lower()
    for entry in entries:
        if not entry.english or not entry.occurs_in(ko_text):
            continue
        if entry.type in ("address", "other") or entry.english.startswith(("(", "—", "-")):
            continue
        occurrences = sum(ko_text.count(a) for a in entry.aliases)
        if occurrences < 2:
            continue
        forms = [f.lower() for f in entry.english_forms]
        if not any(f in lowered for f in forms):
            report.add("WARN", "glossary", where,
                       f"{entry.korean} ×{occurrences} in Korean but '{entry.english}' never appears")


# ── the whole book ───────────────────────────────────────────────────────────

CAPITALISED = re.compile(r"\b([A-Z][a-z]{2,15}(?:-[a-z]{2,10})?)\b")
COMMON = frozenset("""
The She Her Hers His Him They Them Their There These Those This That What Which Who Whom
Why How When Where While And But For Nor Yet Not Now Then Than All Any Both Each Even
Ever Every Few Here However You Your Yours One Once Only Other Some Still Such Sure After
Again Against Almost Alone Along Already Also Although Always Another Anyone Anything
Around Because Been Before Being Below Beside Between Beyond Cannot Could Despite Down
During Either Else Enough Everyone Everything Except Finally First From Give Going Gone
Good Great Instead Into Just Keep Last Late Later Left Less Let Like Little Long Look Made
Make Many Maybe Mean Might More Most Much Must Near Need Never New Next Nobody None
Nothing Off Often Old Our Ours Out Over Own Perhaps Please Quite Rather Really Right Same
Second Seemed Several Shall Should Since Slowly Small Something Sometimes Soon Suddenly
Take Tell Though Three Through Thus Together Too Took Toward Truly Turn Two Under Until
Upon Very Want Was Were Whatever Whether Will With Within Without Would Yes Chapter Part
Prologue Epilogue Sir Lady Lord Madam Miss Mister Mom Dad God Well Oh Ah Huh Hey Wait
Come Stop Look Okay Thank Thanks Sorry Hello Goodbye Did Does Don Isn Didn Wasn Aren Can
Hmm Its Are Had Has Have Were Just Since Still Is It In On At As If So No To Of Do Be My Me
""".split())
PRONOUNS = {"he": "he", "him": "he", "his": "he", "himself": "he",
            "she": "she", "her": "she", "hers": "she", "herself": "she"}


def audit(project: Project) -> tuple[Report, str]:
    """Every per-chunk check plus the checks only the whole book can do.

    Returns the report and a Markdown summary written to reports/audit.md.
    """
    report = Report()
    entries = glossary.load(project.glossary_file)
    chunks = [c for c in project.chunks if project.is_translated(c.id)]
    untranslated = [c.id for c in project.chunks if not project.is_translated(c.id)]
    if untranslated:
        report.add("ERROR", "untranslated", "book", f"{len(untranslated)} chunk(s): {', '.join(untranslated[:12])}")

    english: dict[str, dict[int, str]] = {}
    for chunk in chunks:
        check_chunk(project, chunk, report, entries)
        english[chunk.id] = segfile.read(project.en(chunk.id)).segments

    _echoes(project, chunks, english, report)
    _name_variants(entries, english, report)
    _pronoun_drift(entries, english, report)
    _headings(project, chunks, english, report)
    _quote_mix(english, report)
    rates = _translationese(english)

    words = sum(word_count(t) for seg in english.values() for t in seg.values())
    summary = [
        f"# Audit — {project.meta.get('title_en') or project.meta.get('title_ko')}",
        "",
        f"- {len(chunks)}/{len(project.chunks)} chunks translated, {words:,} English words",
        f"- {report.errors} error(s), {report.warnings} warning(s)",
        "",
        "## Issues",
        "",
        "```",
        report.text(limit_per_code=400),
        "```",
        "",
        "## Translationese (per 10,000 words — anything above ~5 deserves a look)",
        "",
        *[f"- {name}: {rate:.1f}" for name, rate in rates if rate >= 1.0],
    ]
    return report, "\n".join(summary) + "\n"


def _echoes(project, chunks, english, report) -> None:
    """The same long English paragraph twice, from different Korean."""
    seen: dict[str, tuple[str, int, str]] = {}
    for chunk in chunks:
        ko = project.source_segments(chunk)
        for n, text in english[chunk.id].items():
            key = _norm(text)
            if len(key) < 100:
                continue
            if key in seen:
                other_chunk, other_n, other_ko = seen[key]
                if _norm(other_ko) != _norm(ko.get(n, "")):
                    report.add("ERROR", "echo", f"{chunk.id}[[{n}]]",
                               f"same English as {other_chunk}[[{other_n}]] but different Korean")
            else:
                seen[key] = (chunk.id, n, ko.get(n, ""))


def _name_variants(entries, english, report) -> None:
    """Lucas vs Lukas; Lu vs Lou. Capitalised words close to a glossary name."""
    counts: Counter[str] = Counter()
    where: dict[str, str] = {}
    for chunk_id, segments in english.items():
        for text in segments.values():
            for word in CAPITALISED.findall(strip_markup(text)):
                counts[word] += 1
                where.setdefault(word, chunk_id)
    names = {form for e in entries if e.english for form in re.findall(r"[A-Z][a-z]+(?:-[a-z]+)?", e.english)}
    for name in sorted(names):
        for word, count in counts.items():
            if word in names or word in COMMON or word == name:
                continue
            limit = 1 if len(name) <= 5 else 2
            if levenshtein(word.lower(), name.lower(), limit) <= limit and word[0] == name[0]:
                report.add("WARN", "name-variant", where[word],
                           f"'{word}' ×{count} looks like glossary name '{name}' ×{counts.get(name, 0)}")
    # Frequent names absent from the glossary: candidates to add.
    unknown = [(w, c) for w, c in counts.most_common(200) if w not in COMMON and w not in names and c >= 15]
    for word, count in unknown[:15]:
        report.add("NOTE", "not-in-glossary", where[word], f"'{word}' ×{count} — a name the glossary lacks?")


def _pronoun_drift(entries, english, report) -> None:
    """Paragraphs naming exactly one glossary person, scored by pronoun.

    Noisy by nature (other people are in the scene), so it only speaks up when
    the opposite pronoun dominates a sizeable share of those paragraphs.
    """
    persons = [e for e in entries if e.type.startswith("person") and e.pronoun in ("he", "she") and e.english]
    if not persons:
        return
    first_names = {e.english.split()[0]: e for e in persons}
    tally: dict[str, Counter] = defaultdict(Counter)
    samples: dict[str, list[str]] = defaultdict(list)
    for chunk_id, segments in english.items():
        for n, text in segments.items():
            plain = strip_markup(text)
            present = [name for name in first_names if re.search(rf"\b{re.escape(name)}\b", plain)]
            if len(present) != 1:
                continue
            words = Counter(PRONOUNS[w] for w in re.findall(r"\b(he|him|his|himself|she|her|hers|herself)\b", plain.lower()))
            if not words:
                continue
            name = present[0]
            major = words.most_common(1)[0][0]
            tally[name][major] += 1
            if major != first_names[name].pronoun and len(samples[name]) < 4:
                samples[name].append(f"{chunk_id}[[{n}]]")
    for name, counter in tally.items():
        expected = first_names[name].pronoun
        total = sum(counter.values())
        wrong = total - counter[expected]
        if total >= 6 and wrong / total > 0.4:
            report.add("WARN", "pronoun-drift", "book",
                       f"{name} is '{expected}' but {wrong}/{total} single-name paragraphs lean the other way, e.g. {', '.join(samples[name])}")


def _headings(project, chunks, english, report) -> None:
    """Chapter headings should share one format."""
    shapes: Counter[str] = Counter()
    examples: dict[str, str] = {}
    for chunk in chunks:
        if chunk.part != 1:
            continue
        blocks = project.chapter_blocks(chunk.chapter)
        first_heading = next((b["n"] for b in blocks if b["k"] == "h"), None)
        if first_heading is None:
            continue
        text = english[chunk.id].get(first_heading, "")
        shape = re.sub(r"\d+", "N", strip_markup(text).split(":")[0]).strip()[:20]
        shapes[shape] += 1
        examples.setdefault(shape, f"{chunk.id}: {text[:50]}")
    if len(shapes) > 1:
        top = shapes.most_common(1)[0][0]
        for shape, count in shapes.items():
            if shape != top and count <= max(2, shapes[top] // 10):
                report.add("WARN", "heading-format", examples[shape].split(":")[0],
                           f"heading '{examples[shape].split(': ', 1)[1]}' differs from the usual '{top}…'")


def _quote_mix(english, report) -> None:
    straight = curly = 0
    for segments in english.values():
        for text in segments.values():
            straight += text.count('"')
            curly += text.count("“") + text.count("”")
    if straight and curly and min(straight, curly) > 20:
        report.add("NOTE", "quote-style", "book",
                   f"{straight} straight and {curly} curly double quotes — the build converts both to curly; fine unless deliberate")


def _translationese(english) -> list[tuple[str, float]]:
    text = "\n".join(t for seg in english.values() for t in seg.values())
    words = max(word_count(text), 1)
    rates = []
    for name, pattern in TRANSLATIONESE.items():
        count = len(re.findall(pattern, text, re.I))
        rates.append((name, count / words * 10_000))
    return sorted(rates, key=lambda r: -r[1])


# ── run log, for evaluation ──────────────────────────────────────────────────

LOG_NAME = "check_log.jsonl"


def log_run(project: Project, chunk_id: str, report: Report) -> None:
    """Append one chunk's issue counts to reports/check_log.jsonl.

    Translators check every chunk straight after writing it, so a chunk's
    first entry measures the raw draft and its last entry the current state.
    """
    counts = Counter(f"{i.level} {i.code}" for i in report.issues)
    entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "chunk": chunk_id, "counts": dict(sorted(counts.items()))}
    with (project.reports / LOG_NAME).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def summarise_log(project: Project, only: set[str] | None = None) -> tuple[Counter[str], Counter[str], int]:
    """Issue counts summed over each chunk's first and latest logged check."""
    path = project.reports / LOG_NAME
    first: dict[str, dict] = {}
    last: dict[str, dict] = {}
    if path.is_file():
        for line in path.read_text("utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                if only is not None and entry["chunk"] not in only:
                    continue
                first.setdefault(entry["chunk"], entry["counts"])
                last[entry["chunk"]] = entry["counts"]
    return (sum((Counter(c) for c in first.values()), Counter()),
            sum((Counter(c) for c in last.values()), Counter()), len(first))
