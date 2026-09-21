"""The item model: what a produced summary IS, independent of its folder name.

A summary is not an "article numbered 12 in a library" -- it is an item, and the
three modules here describe it without ever parsing its name:

    zoombie.item.paths      where the parts live (``.data/``, ``.data/img/``)
    zoombie.item.registry   the naming conventions, MEASURED from a workspace
    zoombie.item.meta       ``item.json`` -- number, date, title, source
    zoombie.item.scan       one walk of a workspace, shared by items/index

The split exists for one reason: the folder name is the user's to choose, so no
part of the toolchain may depend on its shape. Metadata lives in ``item.json``;
recognition is evidence-based; naming follows whatever the workspace already does.
"""

from __future__ import annotations

from . import meta, paths, registry, scan

__all__ = ["meta", "paths", "registry", "scan"]
