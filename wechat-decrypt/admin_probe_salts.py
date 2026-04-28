import ctypes, ctypes.wintypes as wt, os, sys, json, time, traceback, re
from pathlib import Path
import psutil
from key_scan_common import collect_db_files
ROOT=Path(__file__).resolve().parent
LOG=ROOT/'admin_probe_salts.log'; DONE=ROOT/'admin_probe_salts.done'
DB_DIR=json.load(open(ROOT/'config.json',encoding='utf-8'))['db_dir']
def log(s):
    with LOG.open('a',encoding='utf-8') as f: f.write(str(s)+'\n'); f.flush()
PROCESS_QUERY_INFORMATION=0x0400; PROCESS_VM_READ=0x0010
MEM_COMMIT=0x1000
PAGE_READABLE=0xEE
class MBI(ctypes.Structure):
    _fields_=[('BaseAddress',ctypes.c_void_p),('AllocationBase',ctypes.c_void_p),('AllocationProtect',wt.DWORD),('RegionSize',ctypes.c_size_t),('State',wt.DWORD),('Protect',wt.DWORD),('Type',wt.DWORD)]
k32=ctypes.windll.kernel32
k32.OpenProcess.argtypes=[wt.DWORD,wt.BOOL,wt.DWORD]; k32.OpenProcess.restype=wt.HANDLE
k32.VirtualQueryEx.argtypes=[wt.HANDLE,ctypes.c_void_p,ctypes.POINTER(MBI),ctypes.c_size_t]; k32.VirtualQueryEx.restype=ctypes.c_size_t
k32.ReadProcessMemory.argtypes=[wt.HANDLE,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.POINTER(ctypes.c_size_t)]; k32.ReadProcessMemory.restype=wt.BOOL
k32.CloseHandle.argtypes=[wt.HANDLE]

def find_pids():
    return [p.info['pid'] for p in psutil.process_iter(['pid','name']) if (p.info.get('name') or '').lower()=='weixin.exe']

def scan_pid(pid, salts):
    hp=k32.OpenProcess(PROCESS_QUERY_INFORMATION|PROCESS_VM_READ, False, pid)
    if not hp:
        log(f'PID {pid} open failed err={k32.GetLastError()}'); return {}
    pats=[]
    for s in salts:
        b=bytes.fromhex(s)
        h=s.encode('ascii')
        w=s.encode('utf-16le')
        pats.append((s,'raw',b)); pats.append((s,'hex_ascii',h)); pats.append((s,'hex_wide',w))
    found={}
    mbi=MBI(); addr=0; max_addr=0x7FFFFFFFFFFF; regions=0; readable=0; bytes_read=0
    while addr<max_addr and len(found)<len(salts):
        if not k32.VirtualQueryEx(hp, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)): break
        base=mbi.BaseAddress or 0; size=mbi.RegionSize or 0
        if mbi.State==MEM_COMMIT and (mbi.Protect & PAGE_READABLE) and size and size<256*1024*1024:
            readable+=1
            buf=ctypes.create_string_buffer(size); rd=ctypes.c_size_t()
            if k32.ReadProcessMemory(hp, ctypes.c_void_p(base), buf, size, ctypes.byref(rd)) and rd.value:
                data=buf.raw[:rd.value]; bytes_read+=rd.value
                for salt,kind,pat in pats:
                    if salt in found: continue
                    off=data.find(pat)
                    if off>=0:
                        found[salt]=(kind, base+off)
                        log(f'FOUND_SALT pid={pid} kind={kind} salt={salt} addr=0x{base+off:016X}')
            regions+=1
        nxt=base+size
        if nxt<=addr: break
        addr=nxt
    k32.CloseHandle(hp)
    log(f'PID {pid} regions={regions} readable={readable} bytes={bytes_read} found={len(found)}')
    return found

def main():
    if LOG.exists(): LOG.unlink()
    if DONE.exists(): DONE.unlink()
    log('START '+time.strftime('%F %T'))
    log('is_admin='+str(bool(ctypes.windll.shell32.IsUserAnAdmin())))
    db_files,salt_to_dbs=collect_db_files(DB_DIR)
    salts=list(salt_to_dbs.keys())
    log(f'db_files={len(db_files)} salts={len(salts)}')
    pids=find_pids(); log('pids='+repr(pids))
    allfound={}
    for pid in pids:
        f=scan_pid(pid,salts)
        allfound.update({k:(pid,)+v for k,v in f.items()})
    log('SUMMARY found_salts='+str(len(allfound))+'/'+str(len(salts)))
    for s,v in allfound.items(): log(f'SUM {s} pid={v[0]} kind={v[1]} addr=0x{v[2]:016X}')
    DONE.write_text('done',encoding='utf-8')
try:
    main()
except Exception:
    log('EXCEPTION\n'+traceback.format_exc()); DONE.write_text('exception',encoding='utf-8'); raise
