# AGENTS.md

## Project purpose
This workspace is a local WeChat automation/research project. The current production path is under `wechat-decrypt/`.

## Current architecture snapshot
- Receive path: decrypted local WeChat DB incremental polling via `wechat-decrypt/wechat_bot_monitor.py` and `fast_refresh_targets.py`.
- Receive path decision: keep DB incremental polling as the primary listener; it is faster and more complete than UIA polling for multi-target monitoring.
- Send path: foreground sender currently tries visible conversation OCR first, falls back to WeChat search, then uses `ljqCtrl` clipboard/Enter/click style physical sending and DB confirmation.
- Next optimization direction: build UIA/pywechat-style probes and sender as an isolated layer first, then prefer UIA sender with OCR/search/physical fallback.

## Safety rules
- Do not commit secrets, keys, decrypted databases, private chat exports, screenshots, logs, or runtime artifacts.
- Do not read or move secret/key files unless explicitly required; reference paths only.
- Do not change active bot business logic without user confirmation.
- Before code changes: explain intent and expected user impact.
- After code changes: run relevant validation.

## Git rules
- `git push` only after explicit user confirmation.
- Do not run `git commit`, `git reset`, or other history-changing commands unless explicitly requested.
- Commit messages should be concise English descriptions of intent.

## Workspace layout
- `wechat-decrypt/`: main implementation and WeChat DB tooling.
- `docs/`: project notes, status snapshots, design decisions.
- `pywechat_probe/`, `plan_*`, `source_read/`: research/probe material; keep source-like notes, ignore generated artifacts.
- Runtime outputs, images, logs, DBs, temp replies, backups, caches should stay untracked.
