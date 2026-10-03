"""The glossary: a Markdown table a person or a model can edit.

    | 한국어 | English | type | pronoun | notes |
    |---|---|---|---|---|
    | 유현 / 현이 | Yoo-hyun | person | he | protagonist; 현이 is his nickname |

A Markdown table because it renders in VS Code's preview, diffs cleanly, and
Claude edits it reliably with a one-line Edit. Rows are matched to a chunk by
substring on any alias, so each translator sees only the rows that matter —
on a 300-chapter novel with a 400-row glossary that is the difference between
a packet of 2k tokens and one of 15k.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

COLUMNS = ("한국어", "English", "type", "pronoun", "notes")


@dataclass
class Entry:
    korean: str
    english: str
    type: str = ""
    pronoun: str = ""
    notes: str = ""

    @property
    def aliases(self) -> list[str]:
        return [a.strip() for a in re.split(r"\s*/\s*|\s*,\s*", self.korean) if a.strip()]

    @property
    def english_forms(self) -> list[str]:
        """The canonical English and any listed alternates ("Lu / Lucas")."""
        return [e.strip() for e in re.split(r"\s*/\s*", self.english) if e.strip()]

    def row(self) -> str:
        cells = [self.korean, self.english, self.type, self.pronoun, self.notes]
        return "| " + " | ".join(c.replace("|", "/").strip() for c in cells) + " |"

    def occurs_in(self, text: str) -> bool:
        return any(alias in text for alias in self.aliases)


def parse(text: str) -> list[Entry]:
    entries: list[Entry] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or set("".join(cells)) <= set("-: "):
            continue  # separator row
        if cells[0] in ("한국어", "Korean", "korean"):
            continue  # header
        cells += [""] * (5 - len(cells))
        korean, english = cells[0].strip("`"), cells[1].strip("`")
        if not korean:
            continue
        entries.append(Entry(korean, english, cells[2].lower(), cells[3].lower(), " | ".join(cells[4:]).strip(" |")))
    return entries


def load(path: Path) -> list[Entry]:
    path = Path(path)
    return parse(path.read_text("utf-8")) if path.is_file() else []


def append(path: Path, entries: list[Entry]) -> None:
    text = path.read_text("utf-8") if path.is_file() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    text += "".join(e.row() + "\n" for e in entries)
    path.write_text(text, encoding="utf-8")


def relevant(entries: list[Entry], text: str) -> list[Entry]:
    return [e for e in entries if e.english and e.occurs_in(text)]


def table(entries: list[Entry]) -> str:
    if not entries:
        return "(no glossary entries occur in this text)"
    head = "| " + " | ".join(COLUMNS) + " |\n|---|---|---|---|---|\n"
    return head + "\n".join(e.row() for e in entries)


# ── candidate proper nouns ───────────────────────────────────────────────────

PARTICLES = tuple(sorted((
    "이라고는", "이라는", "라는", "이라고", "라고", "에게서", "으로써", "으로서",
    "에서는", "에게는", "한테는", "으로는", "이라도", "부터는", "까지는", "이었다",
    "이었고", "였다", "에서", "에게", "한테", "께서", "으로", "이라", "처럼", "보다",
    "부터", "까지", "마다", "조차", "밖에", "이나", "이란", "라도", "이는", "이가",
    "이를", "이의", "이와", "이도", "이랑", "랑", "하고", "님은", "님이", "님을", "님의",
    "님께", "님", "씨", "은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "로",
    "도", "만", "께", "야", "아", "여", "들",
), key=len, reverse=True))

STOPWORDS = frozenset("""
그것 이것 저것 그녀 그들 우리 당신 자신 사람 여자 남자 아이 엄마 아빠 친구 사랑 마음
생각 이제 지금 오늘 내일 어제 여기 거기 저기 그런 이런 저런 그래 그러 정말 진짜 그냥
조금 많이 다시 처음 마지막 하지만 그러나 그리고 그래서 때문 처럼 같이 함께 혼자 모두
전부 안에 밖에 위에 아래 앞에 뒤에 옆에 사이 동안 잠시 결국 물론 역시 당연 분명 아마
혹시 만약 이렇 저렇 그렇 어떻 무엇 어디 언제 어느 어떤 있다 없다 되다 하다 보다 오다
가다 주다 받다 말다 알다 모르 원하 필요 가능 확실 정확 얼굴 목소리 머리 순간 표정
이야기 자리 이유 방법 문제 상황 모습 정도 시간 세상 그런데 그렇게 이렇게 아니 아직
정도로 하나 모든 무슨 누구 자기 그게 이게 저게 거야 거지 건가 것이 것은 것을 수가
있었다 없었다 했다 한다 된다 그대로 모두가 다들 너무 아주 매우 가장 더욱 계속 바로
""".split())

HANGUL_RUN = re.compile(r"[가-힣]{2,12}")


def strip_particle(token: str) -> str:
    for particle in PARTICLES:
        if token.endswith(particle) and len(token) - len(particle) >= 2:
            return token[: -len(particle)]
    return token


@dataclass
class Candidate:
    stem: str
    count: int
    forms: int
    example: str


def candidates(texts: list[str], known: list[Entry], top: int = 80, min_count: int = 4) -> list[Candidate]:
    """Likely proper nouns and recurring terms not yet in the glossary.

    A real name takes many particles (유현이, 유현은, 유현을…) while a common
    word inflects too, so the ranking uses both frequency and the number of
    distinct forms. It is a shortlist for a model to judge, not a verdict.
    """
    covered = {alias for e in known for alias in e.aliases}
    counts: Counter[str] = Counter()
    forms: defaultdict[str, set[str]] = defaultdict(set)
    example: dict[str, str] = {}
    for text in texts:
        for m in HANGUL_RUN.finditer(text):
            stem = strip_particle(m.group(0))
            if not 2 <= len(stem) <= 6 or stem in STOPWORDS or stem in covered:
                continue
            if any(stem in alias or alias in stem for alias in covered if len(alias) >= 2):
                continue
            counts[stem] += 1
            forms[stem].add(m.group(0))
            if stem not in example:
                start = max(0, m.start() - 25)
                example[stem] = text[start : m.end() + 25].replace("\n", " ")
    ranked = [
        Candidate(stem, n, len(forms[stem]), example[stem])
        for stem, n in counts.items()
        if n >= min_count and len(forms[stem]) >= 2
    ]
    ranked.sort(key=lambda c: -(c.count * (1 + 0.3 * (c.forms - 1))))
    # Drop a stem that is a prefix of a stronger one (유현 vs 유현이가 stems).
    out: list[Candidate] = []
    for cand in ranked:
        if any(cand.stem != o.stem and cand.stem.startswith(o.stem) and len(cand.stem) - len(o.stem) == 1 for o in out):
            continue
        out.append(cand)
        if len(out) >= top:
            break
    return out


# ── merging translator notes ─────────────────────────────────────────────────

@dataclass
class MergeResult:
    added: list[Entry]
    conflicts: list[tuple[Entry, Entry, str]]  # (existing, proposed, chunk)
    doubts: list[tuple[str, str]]  # (chunk, line)
    files: int


def read_notes(path: Path) -> tuple[list[Entry], list[str]]:
    """New-term rows and doubt bullet lines from one translator note."""
    text = path.read_text("utf-8")
    terms = parse(text)
    doubts = []
    section = ""
    for line in text.splitlines():
        if line.startswith("#"):
            section = line.lower()
            continue
        if ("doubt" in section or "question" in section or "flag" in section) and line.strip().startswith(("-", "*")):
            doubts.append(line.strip()[1:].strip())
    return terms, doubts


def merge_notes(glossary_path: Path, notes_dir: Path) -> MergeResult:
    existing = load(glossary_path)
    by_alias = {alias: e for e in existing for alias in e.aliases}
    merged_dir = notes_dir / "merged"
    merged_dir.mkdir(parents=True, exist_ok=True)

    added: list[Entry] = []
    conflicts: list[tuple[Entry, Entry, str]] = []
    doubts: list[tuple[str, str]] = []
    files = 0
    for note in sorted(notes_dir.glob("c*.md")):
        files += 1
        terms, note_doubts = read_notes(note)
        chunk = note.stem
        doubts += [(chunk, d) for d in note_doubts]
        for term in terms:
            if not term.english:
                continue
            match = next((by_alias[a] for a in term.aliases if a in by_alias), None)
            if match is None:
                if not term.notes:
                    term.notes = f"from {chunk}"
                added.append(term)
                for alias in term.aliases:
                    by_alias[alias] = term
            elif match.english.lower() != term.english.lower() or (
                term.pronoun and match.pronoun and term.pronoun != match.pronoun
            ):
                conflicts.append((match, term, chunk))
        note.replace(merged_dir / note.name)

    if added:
        append(glossary_path, added)
    return MergeResult(added, conflicts, doubts, files)
