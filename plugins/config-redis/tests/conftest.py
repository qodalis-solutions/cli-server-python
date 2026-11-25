from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(__file__)
# The plugin source, the server, the abstractions, and the repository root (the shared store contract in tests/)
for path in (
    os.path.join(_HERE, ".."),
    os.path.join(_HERE, "..", "..", "..", "src"),
    os.path.join(_HERE, "..", "..", "..", "packages", "abstractions", "src"),
    os.path.join(_HERE, "..", "..", ".."),
):
    sys.path.insert(0, os.path.abspath(path))
