import ctypes
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parent
script = root / "admin_extract_and_decrypt.py"
ret = ctypes.windll.shell32.ShellExecuteW(
    None,
    "runas",
    sys.executable,
    f'"{script}"',
    str(root),
    1,
)
(root / "launch_admin_once.ret.txt").write_text(str(ret), encoding="utf-8")
time.sleep(1)
