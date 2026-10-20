"""Write packaging/icon.ico from the app's drawn icon. Run from the project root."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtGui import QGuiApplication  # noqa: E402

from rlutils.ui import icon_pixmap  # noqa: E402

app = QGuiApplication(sys.argv)
target = Path(__file__).resolve().parent / "icon.ico"
if not icon_pixmap(256).save(str(target), "ICO"):
    sys.exit("could not write icon.ico")
print(target)
