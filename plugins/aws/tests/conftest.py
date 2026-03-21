from __future__ import annotations

import sys
import os

# Ensure the plugin source is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Ensure the main server source is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))
# Ensure the abstractions package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "packages", "abstractions", "src"))
