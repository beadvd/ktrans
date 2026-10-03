# ktrans

Multi-agent Korean → English literary translation, run by Claude Code.
`/translate-novel <file>` does the whole job; see
`.claude/skills/translate-novel/SKILL.md`.

* `kt/` is the toolkit (`python3 -m kt …`, standard library only). It
  does every deterministic step — ingest, chunking, packets, checks, glossary
  merge, EPUB build. Models never do those by hand.
* Source texts live in `novels/<slug>/` (git-ignored — never commit or push
  source or translated text). Translations are plain files: `en/cNNNN.txt`,
  one `[[n]] English` line per source paragraph.
* Tests: `python3 -m pytest -q`.
