# WeChat Reply LLM+Wiki V1 Plan

## Goal
Replace fixed template replies with a local wiki + strong boundary reply engine, keeping the existing fast-refresh monitor and WeChat GUI sender.

## Confirmed requirements
- Trigger words only:
  - `@飞扬的小助理`
  - `飞扬的小助理`
  - `小助理`
  - `小助手`
- Use local wiki first; online wiki reserved for later.
- Assistant identity: 飞扬的小助理, not 飞扬/扬叔 himself.
- Must not promise, authorize, quote, decide, or perform high-risk actions on behalf of 飞扬/扬叔.
- User prefers `llm_nos / minimax-anthropic` for this task.

## Exploration result
- Current monitor sends fixed text at `wechat_bot_monitor.py` lines around 304-313.
- `llm_nos`, `llm-nos`, `nos`, `ga_llm`, `minimax`, `anthropic` executables were not found in PATH.
- keychain has no stored names.
- Environment has `MINIMAX_API_KEY` and `MINIMAX_API_HOST`, but no known text chat endpoint/config was found.
- Therefore V1 implements a pluggable provider and safe fallback; once `llm_nos` invocation is provided, only provider function/config needs changing.

## Implementation V1
1. Add `reply_engine.py`
   - strip trigger words
   - pre-boundary check
   - keyword local wiki retrieval
   - optional external provider stub
   - safe heuristic fallback
   - post-boundary check
   - max length clamp
2. Add local wiki:
   - `wiki/core/assistant_identity.md`
   - `wiki/core/reply_boundary.md`
   - `wiki/core/allowed_actions.md`
   - `wiki/core/forbidden_actions.md`
   - `wiki/local/faq.md`
3. Patch `wechat_bot_monitor.py`
   - defaults update
   - import `generate_reply`
   - replace fixed template with `ReplyDecision`
4. Patch config triggers.
5. Verify syntax and dry-run one synthetic trigger.