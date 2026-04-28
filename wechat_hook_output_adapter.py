"""
Hook输出适配器：
- 输入 Hook 产出的 JSONL / 纯文本 key 列表
- 去重并抽取64hex候选 key
- 调用 wechat_key_candidate_validator.py 完成 DB 校验

支持的输入行示例：
1) 纯64hex
2) {"key":"64hex","pid":1234,"addr":"0x..."}
3) 其它含 64hex 的日志行（会正则提取）
"""
import argparse, json, os, re, subprocess, sys

HEX64_RE = re.compile(r'(?i)(?:0x)?([0-9a-f]{64})')
ROOT = os.path.dirname(os.path.abspath(__file__))
VALIDATOR = os.path.join(ROOT, 'wechat_key_candidate_validator.py')


def extract_keys_from_line(line):
    line = line.strip()
    if not line:
        return []
    out = []
    if line.startswith('{') and line.endswith('}'):
        try:
            obj = json.loads(line)
            for k in ('key', 'enc_key', 'db_key', 'raw_key'):
                v = obj.get(k)
                if isinstance(v, str):
                    m = HEX64_RE.fullmatch(v.strip())
                    if m:
                        out.append(m.group(1).lower())
        except Exception:
            pass
    for m in HEX64_RE.finditer(line):
        out.append(m.group(1).lower())
    # dedup preserve order
    seen = set(); ret = []
    for k in out:
        if k not in seen:
            seen.add(k); ret.append(k)
    return ret


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('hook_output', help='Hook 输出文件：txt / log / jsonl')
    ap.add_argument('--db-dir')
    ap.add_argument('--out', default='hook_verified_all_keys.json')
    ap.add_argument('--cross', action='store_true')
    ap.add_argument('--dump-keys', default='extracted_candidate_keys.txt', help='抽取出的候选key文本')
    args = ap.parse_args()

    seen = set(); keys = []
    with open(args.hook_output, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            for k in extract_keys_from_line(line):
                if k not in seen:
                    seen.add(k)
                    keys.append(k)

    if not keys:
        raise SystemExit('未从 Hook 输出中提取到任何64hex key')

    dump_path = os.path.abspath(args.dump_keys)
    with open(dump_path, 'w', encoding='utf-8') as f:
        for k in keys:
            f.write(k + '\n')
    print(f'已抽取候选key {len(keys)} 个 -> {dump_path}')

    cmd = [sys.executable, VALIDATOR, '--key-file', dump_path, '--out', args.out]
    if args.db_dir:
        cmd += ['--db-dir', args.db_dir]
    if args.cross:
        cmd.append('--cross')

    print('执行验证:', ' '.join(cmd))
    raise SystemExit(subprocess.call(cmd, cwd=ROOT))


if __name__ == '__main__':
    main()
