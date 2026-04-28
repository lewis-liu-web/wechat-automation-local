# Current Status Snapshot - 2026-04-28

## Why this snapshot exists
The local workspace has accumulated code, probes, screenshots, logs, decrypted/runtime outputs, and backups. Git is being introduced to make future changes reversible and auditable.

## Active implementation state
- Main active project: `wechat-decrypt/`.
- Bot monitor: `wechat-decrypt/wechat_bot_monitor.py`.
- Reply engine: `wechat-decrypt/reply_engine.py`.
- Target config path used by code: `wechat-decrypt/wechat_bot_targets.json` (private, ignored by Git).
- Runtime logs and decrypted DBs are private runtime artifacts and should not be tracked.

## Receive-side assessment
- Current receive side uses decrypted local DB incremental polling plus fast refresh.
- Empty cycles observed at millisecond level.
- Keep this as the primary listener. UIA/pywechat-style polling is not expected to beat it for multi-target background monitoring or historical context.

## Send-side assessment
- Current send path is foreground UI automation.
- It now attempts visible conversation OCR first, then falls back to WeChat search.
- It uses physical/clipboard sending and DB confirmation.
- Search is considered a fallback, not the desired common path.

## Planned optimization sequence
1. Build isolated UIA probe: detect WeChat window, session list item, current chat title, and `chat_input_field`.
2. Build isolated UIA sender for test group only.
3. Integrate sender priority: UIA current chat -> UIA session list -> OCR visible list -> search -> physical fallback.
4. Keep DB receive path; optimize LLM/template/rule latency separately.

## Interaction constraint
The user explicitly asked not to directly change business logic without confirmation. Future optimization changes should be proposed first, then applied after confirmation.
