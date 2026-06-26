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

## Admin command & dedicated-agent conventions
- `target.category=admin` plus optional `target.admin_senders` (list of wxids or nicknames/remarks) enables slash-command control for that target. An empty `admin_senders` means every sender in the target is treated as an admin. Matching is case-insensitive and works on both wxid and display name.
- Admin commands are recognized in `wechat_bot_monitor.py` before trigger/session handling. New command-like behavior meant to be controlled by admins must route through `admin_commands.handle_admin_command`.
- Supported commands: `/help`, `/mute`/`/stop`, `/continue`/`/unmute`/`/resume`, `/mode <customer_service|group_assistant>`.
- `target.admin_muted=True` pauses new automatic replies for that target. It does not cancel already-queued agent jobs.
- `target.dedicated_agent_instance_id` binds a target's deep-agent tasks to a single registered agent instance. The instance id is validated against `cfg.agent_provider.instances`; invalid/stale ids fall back to the general pool.
- Dedicated-agent filtering happens in `agent_jobs.claim_next_job` / `agent_jobs.claim_dispatchable` by matching `payload.dedicated_agent_instance_id` against the caller's `instance_id`.


## Safety rules
- Do not commit secrets, keys, decrypted databases, private chat exports, screenshots, logs, or runtime artifacts.
- Do not read or move secret/key files unless explicitly required; reference paths only.
- Do not change active bot business logic without user confirmation.
- Before code changes: explain intent and expected user impact.
- After code changes: run relevant validation.
- After completing each coding or documentation task, refresh the codebase-memory-mcp index for `wechat-decrypt` so architecture/search context stays current.

## Git rules
- `git push` only after explicit user confirmation.
- Do not run `git commit`, `git reset`, or other history-changing commands unless explicitly requested.
- Commit messages should be concise English descriptions of intent.

## Workspace layout
- `wechat-decrypt/`: main implementation and WeChat DB tooling.
- `docs/`: project notes, status snapshots, design decisions.
- `pywechat_probe/`, `source_read/`: research/probe material; keep source-like notes, ignore generated artifacts.
- Runtime outputs, images, logs, DBs, temp replies, backups, caches should stay untracked.


## Development vs runtime directories

- `wechat-automation-local/` is the git repository and development workspace.
- `E:\projects\GA-projects\CD-only\wechat-decrypt` is the current runtime/test deployment copy.
- Source of truth is the git repo; deploy by syncing `wechat-decrypt/` to `CD-only/wechat-decrypt/`.
- Always start `control_api` and `wechat_bot_monitor` from the **runtime directory** (`CD-only/wechat-decrypt/`).
- Do not manually start the monitor from the git repo directory; otherwise `STOP_FILE`, config paths, and log files drift between the two copies and stop/restart commands fail silently.
- After code changes: sync to `CD-only`, restart `control_api`, and verify `/status` before asking the user to test the UI.


## Sync to runtime rules

To prevent cursor resets, config loss, and accidental message replay:

1. **Stop runtime services before syncing.**
   - Place `STOP_FILE` (or `wechat_bot_monitor.stop`, per config) and wait for `wechat_bot_monitor.py` to exit.
   - Stop `control_api.py` if its code changed.

2. **Use a robocopy command that never overwrites runtime state / config.**
   Example (PowerShell):
   ```powershell
   robocopy "E:\projects\GA-projects\wechat-automation-local\wechat-decrypt" "E:\projects\GA-projects\CD-only\wechat-decrypt" /E /R:0 /W:0 `
     /XD __pycache__ .pytest_cache logs temp data decrypted decoded_images wechat_auto `
     /XF *.sqlite *.db *.log STOP_FILE wechat_bot_monitor.stop wechat_bot.stop `
          wechat_bot_targets.json wechat_bot_targets.example.json `
          config.json config.example.json `
          wechat_bot_candidates.json wechat_decrypted_targets.json `
          fast_refresh_state.json all_keys.json agent_jobs.db
   ```
   Never use `/MIR` on the runtime directory.

3. **Preserve the knowledge-base layout.**
   - The canonical root is `wiki/`.
   - Scene KBs live under `wiki/scenes/<kb>/`.
   - Do not keep a top-level `scenes/` directory in runtime.

4. **After syncing, verify message cursors before starting the monitor.**
   - For each target in `wechat_bot_targets.json` / `wechat_decrypted_targets.json`, ensure `last_local_id` is >= the current `MAX(local_id)` in the corresponding decrypted message table.
   - If a config file was accidentally overwritten and the cursor is stale, set it to the DB max before starting the monitor to avoid replaying history.

5. **Restart in the right order.**
   - Start `control_api` from `CD-only/wechat-decrypt/` and verify `/status`.
   - Remove any stale `STOP_FILE`.
   - Start `wechat_bot_monitor.py` from `CD-only/wechat-decrypt/`.