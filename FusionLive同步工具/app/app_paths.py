"""Program locations; user data remains beside the root launcher."""
from pathlib import Path
import sys
CODE = Path(__file__).resolve().parent
HOME = CODE.parent
if str(HOME) not in sys.path:
    sys.path.append(str(HOME))
