"""Stack-neutral ingestion library for the Forest Archive Portal.

Nothing here knows about a search engine, a static site generator, or a host.
That is deliberate: the architecture decision is deferred until Phase 0 discovery
is written up (CLAUDE.md §4), and the normalized record format is what keeps that
decision reversible.
"""

__all__ = ["records", "state"]
