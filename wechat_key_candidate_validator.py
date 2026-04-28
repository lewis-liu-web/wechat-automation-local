"""
候选微信DB key验证器：
- 输入一个或多个32字节hex key
- 遍历 wechat-decrypt/config.json 指定的 db_dir
- 用 key_scan_common.verify_enc_key 对每个DB page1做验证
- 输出可直接转成 all_keys.json 的结果草案

用途：先验证 Hook/共享缓冲 抓到的 raw 32-byte key 是否真能打开哪些 DB。
"""
import argparse, json, os, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
WD = os.path.join(ROOT, 'wechat-decrypt')
if WD not in sys.path:
    sys.path.insert(0, WD)

from config import load_config
from key_scan_common import collect_db_files, cross_verify_keys, save_results, verify_enc_key


def normalize_keys(items):
    out = []
    seen = set()
    for raw in items:
        s = raw.strip().lower().replace('0x', '')
        if len(s) != 64:
            raise ValueError(f'候选key长度不是64hex: {raw}')
        int(s, 16)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def load_candidate_keys(args):
    keys = []
    if args.key:
        keys.extend(args.key)
    if args.key_file:
        with open(args.key_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                keys.append(line)
    if not keys:
        raise SystemExit('请提供 --key 或 --key-file')
    return normalize_keys(keys)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--key', action='append', help='32-byte raw key hex，可重复传多次')
    ap.add_argument('--key-file', help='每行一个64hex key')
    ap.add_argument('--db-dir', help='覆盖 config.json 中的 db_dir')
    ap.add_argument('--out', default='candidate_all_keys.json', help='输出JSON文件名')
    ap.add_argument('--cross', action='store_true', help='对未匹配salt尝试交叉复用已验证key')
    args = ap.parse_args()

    cfg = load_config()
    db_dir = os.path.abspath(args.db_dir or cfg['db_dir'])
    out_file = os.path.abspath(args.out)
    cand_keys = load_candidate_keys(args)

    print('=' * 60)
    print('微信候选DB Key验证器')
    print('=' * 60)
    print('db_dir =', db_dir)
    print('候选key数 =', len(cand_keys))

    db_files, salt_to_dbs = collect_db_files(db_dir)
    print(f'数据库数 = {len(db_files)}, salt数 = {len(salt_to_dbs)}')

    key_map = {}
    for idx, key_hex in enumerate(cand_keys, 1):
        enc_key = bytes.fromhex(key_hex)
        matched = []
        for rel, path, sz, salt_hex, page1 in db_files:
            if salt_hex in key_map:
                continue
            if verify_enc_key(enc_key, page1):
                key_map[salt_hex] = key_hex
                matched.append((salt_hex, rel))
        print(f'[{idx}/{len(cand_keys)}] {key_hex} -> 匹配 {len(matched)} 个salt')
        for salt_hex, rel in matched[:12]:
            print(f'  OK salt={salt_hex} db={rel}')
        if len(matched) > 12:
            print(f'  ... 另有 {len(matched)-12} 个匹配')

    if args.cross and key_map:
        cross_verify_keys(db_files, salt_to_dbs, key_map, print)

    try:
        save_results(db_files, salt_to_dbs, key_map, db_dir, out_file, print)
    except RuntimeError as e:
        if not key_map:
            empty_result = {
                '_db_dir': db_dir,
                '_candidate_key_count': len(cand_keys),
                '_matched_salt_count': 0,
                '_note': str(e),
            }
            with open(out_file, 'w', encoding='utf-8') as f:
                json.dump(empty_result, f, indent=2, ensure_ascii=False)
            print(f'\n未命中真实密钥，已写出空结果文件: {out_file}')
        else:
            raise


if __name__ == '__main__':
    main()
