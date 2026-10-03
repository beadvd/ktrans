"""kt — the deterministic half of a Korean→English novel translation pipeline.

Claude Code does the translating. This package does everything that should
not be left to a model: reading EPUB/TXT/DOCX, splitting the book into
chunks sized for a subagent, rendering compact per-chunk packets, checking
what comes back, keeping the glossary, and assembling a valid EPUB.

Standard library only, so it runs anywhere Claude Code does with no install.
"""

__version__ = "1.0.0"
