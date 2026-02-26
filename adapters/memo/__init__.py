"""
adapters/memo/ — Memo Protocol integration for Cleo.

Packages Cleo agent memories (episodes, cases, patterns, KB notes)
into Memo MemoryObject format for export / upload to the Memo platform.

Public API:
    MemoConfig      — configuration from agents.yaml
    MemoExporter    — batch / selective export pipeline
    MemoImporter    — Memo Skill pull → Cleo skill directory injection
    MemoClient      — Memo REST API HTTP client
"""

from adapters.memo.config import MemoConfig  # noqa: F401

__all__ = ["MemoConfig"]
