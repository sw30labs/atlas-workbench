"""Command-line interface for Contingency Atlas.

    atlas up          # first run: create data dir, load demo, open dashboard
    atlas doctor      # is everything healthy?
    atlas --help

The CLI is a thin, friendly layer over the existing engine: it resolves one
data directory, seeds the fictional demo corpus, and starts the loopback
dashboard.  No research behaviour lives here.
"""

from contingency_atlas.cli.main import main

__all__ = ["main"]
