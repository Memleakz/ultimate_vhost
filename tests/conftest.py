import sys
from pathlib import Path

# Add src/lib/ to sys.path so that `vhost_helper` is importable in tests
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
