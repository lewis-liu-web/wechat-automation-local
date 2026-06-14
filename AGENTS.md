# AGENTS.md

## Project purpose
This workspace is a local WeChat automation/research project. The current production path is under `wechat-decrypt/`.

## Current architecture snapshot
- Receive path: decrypted local WeChat DB incremental polling via `wechat-decrypt/wechat_bot_monitor.py` and `fast_refresh_targets.py`.
- Receive path decision: keep DB incremental polling as the primary listener; it is faster and more complete than UIA polling for multi-target monitoring.
- Send path: multiple sender backends available, selected via `send_mode` config:
  - `foreground` (default): UIA visible list → OCR → search → physical clipboard/Enter/click. May steal focus.
  - `backend`: Clipboard + PostMessage without focus steal. Less reliable for Qt WeChat.
  - `cua`: **CUA driver background sender** — uses `cua-driver` CLI to interact via UIA/PostMessage without bringing window to foreground. Works with Qt-based WeChat 4.x. Tested and functional.
  - `backend_then_foreground` / `cua_then_foreground`: Try background first, fallback to foreground if DB confirmation fails.
- Next optimization direction: CUA sender is now implemented and tested. Consider making `cua` the default after extended production validation.

## Safety rules
- Do not commit secrets, keys, decrypted databases, private chat exports, screenshots, logs, or runtime artifacts.
- Do not read or move secret/key files unless explicitly required; reference paths only.
- Do not change active bot business logic without user confirmation.
- Before code changes: explain intent and expected user impact.
- After code changes: run relevant validation.
- After completing each coding or documentation task, refresh the CodeGraph index so architecture/search context stays current.

## Git rules
- `git push` only after explicit user confirmation.
- Do not run `git commit`, `git reset`, or other history-changing commands unless explicitly requested.
- Commit messages should be concise English descriptions of intent.

## Workspace layout
- `wechat-decrypt/`: main implementation and WeChat DB tooling.
- `docs/`: project notes, status snapshots, design decisions.
- `pywechat_probe/`, `source_read/`: research/probe material; keep source-like notes, ignore generated artifacts.
- Runtime outputs, images, logs, DBs, temp replies, backups, caches should stay untracked.
