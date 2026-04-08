import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
HOOKS_PARENT = PROJECT_ROOT / ".claude"
if str(HOOKS_PARENT) not in sys.path:
    sys.path.insert(0, str(HOOKS_PARENT))
