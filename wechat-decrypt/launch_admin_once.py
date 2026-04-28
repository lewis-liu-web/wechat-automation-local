import ctypes, time
from pathlib import Path
py = r"C:\Users\Lewis\AppData\Local\Programs\Python\Python312\python.exe"
script = r"D:\Program Files\GenericAgent-main\temp\wechat-decrypt\admin_extract_and_decrypt.py"
root = r"D:\Program Files\GenericAgent-main\temp\wechat-decrypt"
ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", py, '"'+script+'"', root, 1)
Path(root, "launch_admin_once.ret.txt").write_text(str(ret), encoding="utf-8")
time.sleep(1)
