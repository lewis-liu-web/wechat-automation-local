import os, sys, subprocess, traceback, json, time, ctypes
from pathlib import Path
ROOT=Path(__file__).resolve().parent
LOG=ROOT/'admin_extract_and_decrypt.log'
DONE=ROOT/'admin_extract_and_decrypt.done'

def log(msg):
    with LOG.open('a',encoding='utf-8') as f:
        f.write(str(msg)+'\n')
        f.flush()

def main():
    if DONE.exists(): DONE.unlink()
    log('START '+time.strftime('%Y-%m-%d %H:%M:%S'))
    log('is_admin='+str(bool(ctypes.windll.shell32.IsUserAnAdmin())))
    # 只运行本仓库脚本；key_scan_common 已脱敏控制台输出，不打印完整密钥
    for cmd in ([sys.executable,'find_all_keys_windows.py'], [sys.executable,'decrypt_db.py']):
        log('RUN '+' '.join(cmd))
        r=subprocess.run(cmd,cwd=str(ROOT),capture_output=True,text=True,timeout=900)
        log('RET '+str(r.returncode))
        log('STDOUT_TAIL\n'+r.stdout[-12000:])
        log('STDERR_TAIL\n'+r.stderr[-4000:])
        if r.returncode!=0:
            break
    # 输出目录只记录文件名和大小，不记录聊天内容
    out=ROOT/'decrypted'
    if out.exists():
        for p in sorted(out.rglob('*.db')):
            log('DBOUT '+str(p.relative_to(ROOT))+' '+str(p.stat().st_size))
    log('END '+time.strftime('%Y-%m-%d %H:%M:%S'))
    DONE.write_text('done',encoding='utf-8')

try:
    main()
except Exception:
    log('EXCEPTION\n'+traceback.format_exc())
    DONE.write_text('exception',encoding='utf-8')
    raise
