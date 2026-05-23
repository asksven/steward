import sys
from pathlib import Path

# Make repo root importable so tests can import steward.py as a module.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
