---
name: translate-novel
description: Translate a whole Korean novel (.epub, .txt, .docx, .html, .md) into English end to end — build a glossary and style rules, translate every chapter with subagents, copyedit the whole book, and compile a validated EPUB. Resumable. Use when the user gives a Korean novel file to translate, or names a novel project in novels/ to continue.
argument-hint: "<novel file or project slug> [auto] [workers=N] [copyedit=proof|line|none] [calibrate=off] [until=lore|translate|edit|build]"
allowed-tools: Bash(python3 -m kt *) Bash(python -m kt *)
---

# Translate a novel

You are the orchestrator. You never translate or edit prose yourself and you
never read chunk files: you run `python3 -m kt …`, dispatch subagents, read
their short reports, and make decisions. Keep your own context small — it has
to last the whole book. Everything is on disk, so if the session ends (usage
limit, closed window), running this skill again with the slug resumes.

Arguments: `$ARGUMENTS`

* A file path → a new project (phase 0). A slug → resume from `kt status`.
* `auto` → do not stop at the checkpoints; take the recommended choices.
* `workers=N` → subagents per wave (default 2, sized for a Pro plan's usage
  limits; parallel waves finish sooner but spend the same usage in total,
  so raise it on Max). 1 gives every chunk the previous chunk's English.
* `copyedit=proof|line|none` → skip the copyedit question.
* `calibrate=off` → skip the calibration step (for ablation runs only; the
  copyedit choice then falls back to `copyedit=` or proof).
* `until=…` → stop after that phase.

Subagents (in `.claude/agents/`): **novel-lorekeeper** (glossary + style,
Opus), **novel-translator** (Sonnet), **novel-editor** (Sonnet; dispatched
with `model: "opus"` for a line edit). Give each a self-contained task
message: they do not see this conversation.

## Phase 0 — Ingest

```
python3 -m kt init "<file>"            # prints the slug
```
Check the output: chapter count and titles should look like the book. If
chapters look wrong (one giant chapter, hundreds of tiny ones), tell the
person what you see before continuing. Titles in the first line tell you
whether headings were detected.

If the person has a folder from the earlier handoff workflow for this novel, import it
now: `python3 -m kt import-handoff <slug> <folder>`.

## Phase 1 — Lore

Dispatch **novel-lorekeeper**: "Mode: bootstrap. Slug: <slug>." If the person
supplied an existing glossary, style notes or instructions for this novel,
include their paths and tell the lorekeeper to convert them into glossary.md
and style.md (book-specific lessons go under Lessons from review).

Checkpoint (skip if `auto`): show the person the lorekeeper's summary — the
cast with pronouns, the title, and the open questions — and ask whether to
change anything. Apply answers by dispatching the lorekeeper in resolve
mode, or with small edits to glossary.md yourself.

## Phase 2 — Translate

**Calibration first.** `python3 -m kt next <slug> --workers 1 --max-chunks 3`
→ one **novel-translator** on those chunks. Then **novel-lorekeeper**:
"Mode: calibrate. Slug: <slug>. Chunks: <those ids>." Its lessons are in
style.md before the rest of the book is translated — this is the cheapest
quality gain in the whole pipeline. With `calibrate=off`, skip this and go
straight to the waves.

**Copyedit choice.** The calibration reply ends with a copyedit
recommendation (proof / line / none). Unless `copyedit=` was given or the
book already has one (`kt meta` shows it), ask the person — show the
recommendation and its reason, and what each costs:
  * proof — continuity, grammar, punctuation, obvious translationese;
    Sonnet; much lighter than the translation (most lines pass untouched).
  * line — full line edit of rhythm and voice; Opus; roughly as much usage
    as the translation itself.
  * none — straight to the build after the audit.
With `auto`, take the recommendation. Record it:
`python3 -m kt meta <slug> --copyedit <mode>`.

**Then waves**, until `kt next` says everything is translated:

1. `python3 -m kt next <slug> --workers <N>` → N assignments.
2. Dispatch one **novel-translator** per assignment, all in one message so
   they run together. Task text: "Slug: <slug>. Chunks: <ids>. Translate them
   in order following your brief." Wait for every one to report.
3. `python3 -m kt merge <slug>` → new glossary rows, conflicts, doubts.
   Conflicts, pronoun questions and doubts that matter → one
   **novel-lorekeeper** in resolve mode with the merge output pasted in.
   Trivial ones (a new minor name) need nothing.
4. `python3 -m kt check <slug> <this wave's ids>` → any ERROR: dispatch a
   **novel-translator** in repair mode with the check output pasted in.
5. Every ~30 chunks: **novel-lorekeeper** in update mode for the recent
   chapter range.
6. Every few waves, one line to the person: "Translated 42/180 chunks
   (chapter 38)."

Translators report problems; read their last lines and act on anything
flagged "orchestrator must decide".

## Phase 3 — Consistency audit

```
python3 -m kt merge <slug>
python3 -m kt audit <slug>             # full report in reports/audit.md
```
Read only the summary counts and, if needed, `grep` reports/audit.md for a
code. Act on it:
* `untranslated`, `missing-segments`, `stray-line`, `hangul`, `echo`,
  `repeated-*` → **novel-translator** repair mode on those chunks.
* `name-variant` → confirm with `kt replace … --dry-run`, then replace
  (`--word`), or hand to the lorekeeper if the right form is unclear.
* `pronoun-drift`, `not-in-glossary`, `heading-format` → lorekeeper resolve
  mode with the lines pasted in.
* Translationese rates above ~5 per 10k words → mention them in the editor
  task text for the copyedit.
Re-run the audit until it has no errors.

## Phase 4 — Copyedit

1. `python3 -m kt edit-next <slug> --workers <N>` (snapshots the unedited
   translation to en_draft/ the first time).
2. One **novel-editor** per assignment, all in one message: "Slug: <slug>.
   Range: <first>-<last>. Mode: <proof|line>. Copyedit following your
   brief." For `line`, dispatch with the model override `opus`. Add the
   audit's high translationese rates and any known problem areas.
   Mode `none` → skip this phase.
3. After each wave: if editors report recurring defects, **novel-lorekeeper**
   in lessons mode with the report paths — later editor waves then get the
   updated style sheet.
4. Repeat until `edit-next` says everything is edited, then run
   `python3 -m kt audit <slug>` again and fix anything new.

## Phase 5 — Build

```
python3 -m kt build <slug>
```
For an EPUB source the English is written into the original pages, so the
publisher's formatting — styled system windows, fonts, images, tables —
survives exactly; `--fresh` builds a clean new book instead. TXT/DOCX
sources always get a clean book, with bracketed system messages boxed. The
build curls quotes, normalises ellipses, validates every page as XML and
checks the package structure; if epubcheck is installed it runs that too.
Fix anything it reports (usually a segment with broken `{i}` markers —
repair mode), and rebuild.

Tell the person: the EPUB path, chapters, word count (from the audit), what
the copyedit changed (en_draft/ holds the pre-edit version), and anything
left for them to decide — open doubts in reports/merge_log.md, author
inconsistencies the editors reported.

## Rules

* Never read `ko/`, `en/` or the chapter JSON yourself; use `kt status`,
  `kt check`, `kt audit` counts, and the subagents' reports.
* Never run two waves that touch the same chunks at the same time.
* If a subagent fails or runs out of turns, `kt next` / `kt edit-next` will
  hand its unfinished chunks out again; do not redo finished ones.
* When the session is getting long, finish the current wave, say where you
  stopped, and tell the person to run `/translate-novel <slug>` to continue.
* Previews at any time: `python3 -m kt build <slug> --allow-partial`.
