"""
run_wechat_hook_pipeline_check.py

交付包一键自检/联调入口：
1. 检查关键文件是否存在
2. 检查 wechat-decrypt/config.json 与 db_dir
3. 编译检查 Python 脚本
4. 使用样例 JSONL 跑通 adapter -> validator -> result JSON
5. 输出验收摘要

注意：本脚本不做进程注入、不读取密钥、不绕过访问控制；只验证本交付包后链路是否可运行。
"""
import json
import os
import py_compile
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WD = ROOT / "wechat-decrypt"
CONFIG = WD / "config.json"

REQUIRED_FILES = [
    "wechat_hook_protocol.md",
    "DELIVERY_WECHAT_DBKEY_HOOK.md",
    "wechat_hook_capture_sample.jsonl",
    "wechat_hook_output_adapter.py",
    "wechat_key_candidate_validator.py",
    "wechat_dbkey_hook_skeleton.cpp",
    "wechat_dbkey_hook_install_skeleton.cpp",
]

PY_FILES = [
    "wechat_hook_output_adapter.py",
    "wechat_key_candidate_validator.py",
]


def ok(msg):
    print(f"[OK] {msg}")


def warn(msg):
    print(f"[WARN] {msg}")


def fail(msg):
    print(f"[FAIL] {msg}")


def load_config():
    if not CONFIG.exists():
        fail(f"缺少配置文件: {CONFIG}")
        return None
    with CONFIG.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    ok(f"读取配置: {CONFIG}")
    return cfg


def count_dbs(db_dir):
    n = 0
    samples = []
    for root, _, files in os.walk(db_dir):
        for name in files:
            if name.lower().endswith(".db"):
                n += 1
                if len(samples) < 8:
                    samples.append(os.path.join(root, name))
    return n, samples


def main():
    print("=" * 72)
    print("WeChat DB Key Hook 交付包后链路自检")
    print("=" * 72)

    missing = []
    for rel in REQUIRED_FILES:
        p = ROOT / rel
        if p.exists():
            ok(f"文件存在: {rel}")
        else:
            missing.append(rel)
            fail(f"文件缺失: {rel}")

    if missing:
        print("\n关键文件缺失，停止。")
        return 2

    for rel in PY_FILES:
        try:
            py_compile.compile(str(ROOT / rel), doraise=True)
            ok(f"Python语法通过: {rel}")
        except Exception as e:
            fail(f"Python语法失败: {rel}: {e}")
            return 3

    cfg = load_config()
    if not cfg:
        return 4

    db_dir = cfg.get("db_dir")
    if not db_dir:
        fail("config.json 缺少 db_dir")
        return 5
    if not os.path.exists(db_dir):
        fail(f"db_dir 不存在: {db_dir}")
        return 6

    db_count, samples = count_dbs(db_dir)
    ok(f"db_dir存在: {db_dir}")
    ok(f"发现DB数量: {db_count}")
    for s in samples:
        print(f"      {s}")

    out_file = ROOT / "hook_verified_all_keys.check.json"
    cmd = [
        sys.executable,
        str(ROOT / "wechat_hook_output_adapter.py"),
        str(ROOT / "wechat_hook_capture_sample.jsonl"),
        "--cross",
        "--out",
        str(out_file),
    ]
    print("\n执行样例链路:")
    print(" ".join(cmd))
    res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=180)
    print("\n--- stdout ---")
    print(res.stdout)
    print("--- stderr ---")
    print(res.stderr)

    if res.returncode != 0:
        fail(f"样例链路失败，返回码={res.returncode}")
        return 7

    if not out_file.exists():
        fail(f"未生成结果文件: {out_file}")
        return 8

    with out_file.open("r", encoding="utf-8") as f:
        result = json.load(f)

    matched = result.get("_matched_salt_count")
    candidates = result.get("_candidate_key_count")
    ok(f"生成结果文件: {out_file.name}")
    if candidates is not None:
        ok(f"候选key数: {candidates}")
    if matched is not None:
        ok(f"命中salt数: {matched}（样例随机key通常为0，属预期）")

    print("\n验收结论:")
    print("- 后链路可运行")
    print("- 样例输入可被抽取、去重、验证并输出JSON")
    print("- 若换成真实授权Hook输出，继续使用同一命令验证")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
