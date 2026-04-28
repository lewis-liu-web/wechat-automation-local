#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dry-run wiki/knowledge retrieval for WeChat reply engine without sending messages."""
import argparse, json
from pathlib import Path
from reply_engine import retrieve_knowledge, generate_reply

ROOT = Path(__file__).resolve().parent

def load_config(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def find_target(cfg, name):
    for t in cfg.get('targets', []):
        if name in (t.get('name'), t.get('username'), t.get('table')):
            return t
    raise SystemExit(f'target not found: {name}')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=str(ROOT/'wechat_bot_targets.json'))
    ap.add_argument('--target', required=True)
    ap.add_argument('--query', required=True)
    ap.add_argument('--llm', action='store_true', help='call configured LLM/subagent; default only retrieves knowledge and fallback')
    args = ap.parse_args()
    cfg = load_config(args.config)
    target = find_target(cfg, args.target)
    # Use stripped query so hit display matches what generate_reply actually sees.
    stripped = args.query
    for trig in (cfg.get('default_triggers') or []):
        stripped = stripped.replace(trig, ' ')
    import re
    stripped = re.sub(r'\s+', ' ', stripped).strip()
    hits = retrieve_knowledge(stripped, cfg, target)
    print('target:', target.get('name'))
    print('query (raw):', repr(args.query))
    print('query (stripped):', repr(stripped))
    print('knowledge_bases:', target.get('knowledge_bases', []))
    print('hits:', len(hits))
    for h in hits:
        print(f'  [{h.scope}] {h.label} score={h.score} path={h.rel_path}')
    run_cfg = cfg if args.llm else dict(cfg, reply_engine={**(cfg.get('reply_engine') or {}), 'use_subagent': False})
    decision = generate_reply(args.query, target, run_cfg)
    print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
