"""The item model: what a produced summary IS, independent of its folder name.

A summary is not an "article numbered 12 in a library" -- it is an item, and the
modules here describe it without ever parsing its name:

    zoombie.item.paths      where the parts live (``summary.md``, ``img/``)
    zoombie.item.registry   the naming conventions, MEASURED from a workspace
    zoombie.item.scan       one walk of a workspace

A finished item is deliberately small: its document, the media it was made from,
and the figures the document inlines. There is no ``.data/`` sidecar directory and
no ``item.json``: the folder name is the user's to choose, recognition is by the
document's presence, and any metadata the toolchain once stored is either
re-derived from the document or was throwaway by design.
"""

from __future__ import annotations

from . import paths, registry, scan

__all__ = ["paths", "registry", "scan"]
