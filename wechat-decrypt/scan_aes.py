
import os, re, sys, ctypes, struct, glob, json, time
from ctypes import wintypes

# Reconfigure stdout to avoid decode errors
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# WinAPI
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80

class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]

kernel32 = ctypes.windll.kernel32
RE_KEY32 = re.compile(rb"(?<![a-zA-Z0-9])[a-zA-Z0-9]{32}(?![a-zA-Z0-9])")
RE_KEY16 = re.compile(rb"(?<![a-zA-Z0-9])[a-zA-Z0-9]{16}(?![a-zA-Z0-9])")

from Crypto.Cipher import AES

def try_key(key_bytes, ciphertext):
    try:
        cipher = AES.new(key_bytes, AES.MODE_ECB)
        dec = cipher.decrypt(ciphertext)
        if dec[:3] == b"\xFF\xD8\xFF":
            return "JPEG"
        if dec[:4] == bytes([0x89, 0x50, 0x4E, 0x47]):
            return "PNG"
        if dec[:4] == b"RIFF":
            return "WEBP"
        if dec[:4] == b"wxgf":
            return "WXGF"
        if dec[:3] == b"GIF":
            return "GIF"
    except Exception:
        pass
    return None

def get_pids():
    import subprocess
    result = subprocess.run(
        ["tasklist.exe", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    pids = []
    for line in result.stdout.strip().split("\n"):
        if "Weixin.exe" in line:
            parts = line.strip('"').split('","')
            if len(parts) >= 2:
                try:
                    pids.append(int(parts[1]))
                except ValueError:
                    pass
    return pids

def scan_pid(pid, ciphertext):
    access = PROCESS_VM_READ | PROCESS_QUERY_INFORMATION
    h_process = kernel32.OpenProcess(access, False, pid)
    if not h_process:
        print(f"[PID {pid}] Cannot open process", flush=True)
        return None
    try:
        address = 0
        mbi = MEMORY_BASIC_INFORMATION()
        rw_regions = []
        all_regions = []
        while address < 0x7FFFFFFFFFFF:
            result = kernel32.VirtualQueryEx(
                h_process, ctypes.c_void_p(address),
                ctypes.byref(mbi), ctypes.sizeof(mbi)
            )
            if result == 0:
                break
            if (mbi.State == MEM_COMMIT and
                mbi.Protect != PAGE_NOACCESS and
                (mbi.Protect & PAGE_GUARD) == 0 and
                mbi.RegionSize <= 50 * 1024 * 1024):
                region = (mbi.BaseAddress, mbi.RegionSize, mbi.Protect)
                all_regions.append(region)
                if (mbi.Protect & (PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY)) != 0:
                    rw_regions.append(region)
            next_addr = address + mbi.RegionSize
            if next_addr <= address:
                break
            address = next_addr

        print(f"[PID {pid}] RW regions: {len(rw_regions)}, total: {len(all_regions)}", flush=True)

        for phase, regions in [("RW", rw_regions), ("ALL", all_regions)]:
            print(f"[PID {pid}] Phase {phase}...", flush=True)
            t0 = time.time()
            for idx, (base_addr, region_size, _) in enumerate(regions):
                if idx % 200 == 0 and idx > 0:
                    elapsed = time.time() - t0
                    print(f"[PID {pid}] {phase} progress {idx}/{len(regions)} ({elapsed:.1f}s)", flush=True)
                try:
                    buffer = ctypes.create_string_buffer(region_size)
                    bytes_read = ctypes.c_size_t(0)
                    ok = kernel32.ReadProcessMemory(
                        h_process, ctypes.c_void_p(base_addr),
                        buffer, region_size, ctypes.byref(bytes_read)
                    )
                    if not ok or bytes_read.value < 32:
                        continue
                    data = buffer.raw[:bytes_read.value]
                except Exception as e:
                    continue

                for m in RE_KEY32.finditer(data):
                    kb = m.group()
                    fmt = try_key(kb[:16], ciphertext)
                    if fmt:
                        key_str = kb[:16].decode("ascii")
                        print(f"[PID {pid}] FOUND 32-char key -> {fmt} : {key_str}", flush=True)
                        return key_str
                    fmt = try_key(kb, ciphertext)
                    if fmt:
                        key_str = kb.decode("ascii")
                        print(f"[PID {pid}] FOUND 32-byte key -> {fmt} : {key_str}", flush=True)
                        return key_str[:16]

                for m in RE_KEY16.finditer(data):
                    kb = m.group()
                    fmt = try_key(kb, ciphertext)
                    if fmt:
                        key_str = kb.decode("ascii")
                        print(f"[PID {pid}] FOUND 16-char key -> {fmt} : {key_str}", flush=True)
                        return key_str
        return None
    finally:
        kernel32.CloseHandle(h_process)

def main():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)
    db_dir = config["db_dir"]
    base_dir = os.path.dirname(db_dir)
    attach_dir = os.path.join(base_dir, "msg", "attach")

    # Find V2 ciphertext
    v2_magic = b"\x07\x08V2\x08\x07"
    pattern = os.path.join(attach_dir, "*", "*", "Img", "*_t.dat")
    dat_files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    ciphertext = None
    for f in dat_files[:100]:
        try:
            with open(f, "rb") as fp:
                header = fp.read(31)
            if header[:6] == v2_magic and len(header) >= 31:
                ciphertext = header[15:31]
                print(f"Test file: {os.path.basename(f)}")
                print(f"Ciphertext: {ciphertext.hex()}")
                break
        except Exception:
            continue
    if not ciphertext:
        print("No V2 files found")
        return

    pids = get_pids()
    print(f"PIDs: {pids}")
    if not pids:
        print("WeChat not running")
        return

    for pid in pids:
        result = scan_pid(pid, ciphertext)
        if result:
            print(f"SUCCESS: AES key = {result}")
            config["image_aes_key"] = result
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            print("Saved to config.json")
            break
    else:
        print("AES key not found in any PID")

if __name__ == "__main__":
    main()
