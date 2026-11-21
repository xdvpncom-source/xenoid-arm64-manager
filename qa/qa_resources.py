"""Compatibility entry point for the host QA resource timer and CLI.

The runtime has a unique module name so the controller can also ship it beside
its separate qa_resources settings validator without an import collision.
"""
from pathlib import Path
import sys

_DIRECTORY = str(Path(__file__).resolve().parent)
if _DIRECTORY not in sys.path:
    sys.path.insert(0, _DIRECTORY)
from qa_resource_runtime import *

if __name__ == "__main__":
    raise SystemExit(main())
