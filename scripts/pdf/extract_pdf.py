#!/usr/bin/env python3
"""Standalone PDF -> Markdown extractor for the zoombie toolchain.

The implementation lives in ``zoombie/lib/pdf.py`` so the CLI can import it
directly instead of spawning a second interpreter. This wrapper exists so the
extractor is still runnable on its own (and so the installed layout keeps a
``pdf/extract_pdf.py`` path), with identical flags and JSON output.

A skill never invokes this directly, and never re-assembles its arguments by
hand: it calls the zoombie CLI, which owns the ASCII-path isolation.

Exit codes
----------
0  Markdown written (possibly with skipped scanned pages, reported in JSON)
1  A real failure (unreadable PDF, OCR requested but Tesseract missing, ...)
"""

from __future__ import annotations

import os
import sys

# Allow a direct `python extract_pdf.py` from a checkout (scripts/pdf/ sits
# beside scripts/zoombie/) as well as from the installed layout
# (bin/zoombie/pdf/ sits beside bin/zoombie/zoombie/).
_HERE = os.path.dirname(os.path.abspath(__file__))
for candidate in (os.path.dirname(_HERE), _HERE):
    if os.path.isdir(os.path.join(candidate, "zoombie")):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)
        break

try:
    from zoombie.lib import pdf
except ImportError:  # pragma: no cover - only when run without the package
    sys.stderr.write(
        "extract_pdf.py: the zoombie package could not be imported. Run it through "
        "the zoombie CLI, or ensure scripts/ is on PYTHONPATH.\n"
    )
    raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(pdf.main())
