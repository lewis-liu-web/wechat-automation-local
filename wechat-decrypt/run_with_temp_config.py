"""
临时修改 config.json 执行命令，并在 finally 中无条件恢复原配置。
用法：
  python run_with_temp_config.py --keys-file ..\\some_keys.json -- python decrypt_db.py
注意：不读取/打印密钥内容，只改配置引用；异常/超时/子进程失败后也恢复 config.json。
"""
import argparse, json, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys-file")
    ap.add_argument("--decrypted-dir")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    ns = ap.parse_args()
    if ns.cmd and ns.cmd[0] == "--":
        ns.cmd = ns.cmd[1:]
    if not ns.cmd:
        raise SystemExit("missing command after --")

    original_text = CONFIG.read_text(encoding="utf-8")
    original = json.loads(original_text)
    cfg = dict(original)
    if ns.keys_file is not None:
        cfg["keys_file"] = ns.keys_file
    if ns.decrypted_dir is not None:
        cfg["decrypted_dir"] = ns.decrypted_dir

    try:
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
        print("[temp-config] applied; keys_file ref =", cfg.get("keys_file"), flush=True)
        r = subprocess.run(ns.cmd, cwd=str(ROOT))
        raise SystemExit(r.returncode)
    finally:
        CONFIG.write_text(original_text, encoding="utf-8")
        print("[temp-config] restored config.json", flush=True)

if __name__ == "__main__":
    main()
