# Deployment Notes

This project is intended for local Windows WeChat automation and local database tooling. Deployment means preparing the local runtime environment, private configuration, and validation steps; it does not imply publishing secrets or runtime data.

## Preconditions

- Python environment with project dependencies installed.
- Local WeChat desktop installation and accessible runtime data.
- Extracted/decrypted database material where required by the selected workflow.
- Private configuration files created locally and kept out of Git.

## Private files

The following files or file classes are local-only and must not be committed:

- `wechat-decrypt/wechat_bot_targets.json`
- key files, extracted candidate keys, decrypted databases, exported chats
- runtime logs, screenshots, temporary replies, backups, caches
- any user-specific WeChat profile/database paths

## Validation

From the repository root or `wechat-decrypt/`, run the relevant tests before using changed code:

```bash
cd wechat-decrypt
python -m pytest
```

For MCP/config related changes, include the MCP server search/config tests. For sender/monitor changes, validate with an isolated target before enabling broader automation.

## Local runtime sequence

1. Prepare local private config and key/database prerequisites.
2. Verify decrypt/database access with the tooling described in `wechat-decrypt/README.md`.
3. Run tests.
4. Start the selected local component manually, for example the bot monitor or MCP server, only after confirming target configuration.
5. Observe logs and database confirmation for the test target before expanding scope.

## Operational safety

- Keep database receive polling as the primary listener unless a validated replacement is explicitly approved.
- Treat UI automation sender changes as high-impact: probe first, integrate second.
- Avoid changing active business logic without confirmation.
- Do not run destructive cleanup, history-changing Git commands, or push without explicit approval.

## Rollback

Use Git diff/status to review changes before and after deployment. Revert only the specific files involved in a failed change; do not remove private runtime files or secrets as part of code rollback.
