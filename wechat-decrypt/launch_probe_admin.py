import ctypes, pathlib
root=pathlib.Path(__file__).resolve().parent
ret=ctypes.windll.shell32.ShellExecuteW(None,'runas',r'C:\Users\Lewis\AppData\Local\Programs\Python\Python312\python.exe', '"'+str(root/'admin_probe_salts.py')+'"', str(root), 1)
(root/'launch_probe_admin.ret.txt').write_text(str(ret),encoding='utf-8')
