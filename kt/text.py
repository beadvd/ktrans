"""Small text helpers shared by every module."""

from __future__ import annotations

import re

HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
HANGUL_RUN = re.compile(r"[가-힣]+")
WHITESPACE = re.compile(r"\s+")

# Inline formatting survives the round trip as brace markers rather than HTML,
# because Korean web novels use angle brackets as prose (<상태창>, <공지>) and
# those must come out as literal text, not tags.
INLINE_TAGS = ("i", "b")
MARKUP = re.compile(r"\{(/?)(i|b)\}|\{br\}")


def has_hangul(text: str) -> bool:
    return bool(HANGUL.search(text or ""))


def collapse(text: str) -> str:
    return WHITESPACE.sub(" ", text or "").strip()


def strip_markup(text: str) -> str:
    return MARKUP.sub(lambda m: " " if m.group(0) == "{br}" else "", text or "")


def markup_balanced(text: str) -> bool:
    """True when every {i}/{b} is closed, in order, and none nest wrongly."""
    stack: list[str] = []
    for m in MARKUP.finditer(text or ""):
        if m.group(0) == "{br}":
            continue
        closing, tag = m.group(1), m.group(2)
        if not closing:
            stack.append(tag)
        elif not stack or stack.pop() != tag:
            return False
    return not stack


def repair_markup(text: str) -> str:
    """Drop markers that cannot be paired, keeping the words."""
    if markup_balanced(text):
        return text
    return MARKUP.sub(lambda m: m.group(0) if m.group(0) == "{br}" else "", text)


def tidy_markup(text: str) -> str:
    """Remove empty pairs and leading/trailing line breaks."""
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\{(i|b)\}(\s*)\{/\1\}", r"\2", text)
    text = re.sub(r"^(?:\s*\{br\}\s*)+|(?:\s*\{br\}\s*)+$", "", text)
    return text.strip()


def word_count(text: str) -> int:
    return len(strip_markup(text).split())


# ── quotes ───────────────────────────────────────────────────────────────────

def smart_quotes(text: str) -> str:
    """Straight quotes to typographic ones, conservatively.

    Opening after start/space/opening punctuation, closing otherwise; an
    apostrophe inside or after a word is always ’. Already-curly text is left
    alone, so running this twice changes nothing.
    """
    if '"' not in text and "'" not in text:
        return text
    out = []
    for i, ch in enumerate(text):
        prev = text[i - 1] if i else ""
        nxt = text[i + 1] if i + 1 < len(text) else ""
        opening = (not prev) or prev.isspace() or prev in "([{—–-‘“}"
        if ch == '"':
            out.append("“" if opening else "”")
        elif ch == "'":
            # 'em, 'til and '90s open with an apostrophe, not a quote.
            if opening and not (nxt.isdigit() or _elision(text, i)):
                out.append("‘")
            else:
                out.append("’")
        else:
            out.append(ch)
    return "".join(out)


def normalise_ellipses(text: str, style: str = "...") -> str:
    """"……", "……." , "…" and "...." all become one ellipsis in the book's style.

    Done at build time so no translator has to spend attention on it.
    """
    text = re.sub(r"(?:…|‥|\.{3,})+(?:\.(?!\.))?", "\u0000", text)
    return text.replace("\u0000", "..." if style == "..." else "…")


_ELISIONS = ("em ", "em.", "em,", "cause ", "til ", "tis ", "twas ", "n ", "bout ")


def _elision(text: str, i: int) -> bool:
    tail = text[i + 1 : i + 8].lower() + " "
    return tail.startswith(_ELISIONS)


def hangul_sample(text: str, width: int = 24) -> str:
    m = HANGUL.search(text)
    if not m:
        return ""
    return text[max(0, m.start() - width) : m.end() + width]


def levenshtein(a: str, b: str, limit: int = 3) -> int:
    """Edit distance, giving up early past `limit`."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            value = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            cur.append(value)
            best = min(best, value)
        if best > limit:
            return limit + 1
        prev = cur
    return prev[-1]
