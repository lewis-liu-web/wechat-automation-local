import ctypes, pathlib, sys, os

root = pathlib.Path(__file__).resolve().parent
python = pathlib.Path(os.environ.get("LOCALAPPDATA", r"C:\Users\Lewis\AppData\Local")) / "Programs" / "Python" / "Python312" / "python.exe"
if not python.exists():
    python = pathlib.Path(sys.executable)
ret = ctypes.windll.shell32.ShellExecuteW(None, 'runas', str(python), '"' + str(root / 'admin_probe_salts.py') + '"', str(root), 1)
(root / 'launch_probe_admin.ret.txt').write_text(str(ret), encoding='utf-8')
