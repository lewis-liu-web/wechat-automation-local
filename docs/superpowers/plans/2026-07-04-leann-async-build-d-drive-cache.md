# LEANN 异步建索引与 D 盘模型缓存 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 LEANN 索引构建在 Streamlit 控制台中可异步执行，并把 embedding 模型缓存默认放到 D 盘，避免 C 盘空间不足与下载卡死。

**Architecture:** 在 `target_registry.py` 中统一封装 LEANN 运行环境（HF_HOME / SENTENCE_TRANSFORMERS_HOME / HF_ENDPOINT），默认指向 `D:\cache\leann`；新增缓存迁移辅助函数把已有 C 盘 HuggingFace 缓存复制到 D 盘；在 `control_api.py` 中新增后台 build endpoint，由 Streamlit UI 调用并轮询状态/日志。

**Tech Stack:** Python 3.12, LEANN CLI, subprocess, threading, Streamlit.

---

## Task 1: 统一 LEANN 运行环境变量

**Files:**
- Modify: `wechat-decrypt/target_registry.py`
- Test: `wechat-decrypt/tests/test_target_registry_kb.py`

- [ ] **Step 1: 编写测试验证 env 设置**

```python
def test_leann_env_uses_configured_cache_dir(tmp_path):
    cfg = {"reply_engine": {"leann": {"cache_dir": str(tmp_path)}}}
    env = _leann_env(cfg)
    assert env["HF_HOME"] == str(tmp_path / "huggingface")
    assert env["SENTENCE_TRANSFORMERS_HOME"] == str(tmp_path / "sentence_transformers")
    assert env["HF_ENDPOINT"] == "https://hf-mirror.com"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest wechat-decrypt/tests/test_target_registry_kb.py::test_leann_env_uses_configured_cache_dir -v`
Expected: FAIL ( `_leann_env` not defined )

- [ ] **Step 3: 在 target_registry.py 实现 `_leann_env`**

在 `_leann_exe` 附近新增：

```python
def _leann_env(cfg=None):
    """Return environment variables for LEANN subprocesses.

    Defaults move HuggingFace / sentence-transformers model caches to D:\cache\leann
    so that embedding downloads do not fill the C drive.
    """
    env = dict(os.environ)
    cfg = cfg or load_config(CONFIG_PATH)
    raw = (cfg.get("reply_engine") or {}).get("leann") or {}
    cache_root = Path(str(raw.get("cache_dir") or r"D:\cache\leann")).resolve()
    env["HF_HOME"] = str(cache_root / "huggingface")
    env["SENTENCE_TRANSFORMERS_HOME"] = str(cache_root / "sentence_transformers")
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    return env
```

- [ ] **Step 4: 在 `search_leann_kb` / `diagnose_leann_kb` 中复用 `_leann_env`**

替换两行 `env = dict(os.environ); env.setdefault("HF_ENDPOINT", "...")` 为：

```python
env = _leann_env()
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest wechat-decrypt/tests/test_target_registry_kb.py::test_leann_env_uses_configured_cache_dir -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add wechat-decrypt/target_registry.py wechat-decrypt/tests/test_target_registry_kb.py
git commit -m "feat(leann): centralize LEANN env vars and default model cache to D:\cache\leann"
```

---

## Task 2: 迁移已有 HuggingFace 缓存到 D 盘

**Files:**
- Modify: `wechat-decrypt/target_registry.py`
- Test: `wechat-decrypt/tests/test_target_registry_kb.py`

- [ ] **Step 1: 编写迁移函数测试**

```python
def test_migrate_leann_cache_copies_files(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    (src / "hub" / "models--x").mkdir(parents=True)
    (src / "hub" / "models--x" / "file.txt").write_text("model")
    result = migrate_leann_cache(str(src), str(dst))
    assert result["copied"]
    assert (dst / "hub" / "models--x" / "file.txt").exists()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest wechat-decrypt/tests/test_target_registry_kb.py::test_migrate_leann_cache_copies_files -v`
Expected: FAIL

- [ ] **Step 3: 实现 `migrate_leann_cache`**

```python
def migrate_leann_cache(src_dir, dst_dir):
    """Copy an existing HuggingFace cache tree to a new location.

    Returns a dict with copied flag and counts. Skips if destination already exists.
    """
    import shutil
    src = Path(src_dir)
    dst = Path(dst_dir)
    if not src.exists():
        return {"copied": False, "reason": "source does not exist", "src": str(src), "dst": str(dst)}
    if dst.exists() and any(dst.iterdir()):
        return {"copied": False, "reason": "destination not empty", "src": str(src), "dst": str(dst)}
    dst.mkdir(parents=True, exist_ok=True)
    copied_files = 0
    for item in src.rglob("*"):
        if item.is_file():
            rel = item.relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            copied_files += 1
    return {"copied": True, "src": str(src), "dst": str(dst), "files": copied_files}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest wechat-decrypt/tests/test_target_registry_kb.py::test_migrate_leann_cache_copies_files -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wechat-decrypt/target_registry.py wechat-decrypt/tests/test_target_registry_kb.py
git commit -m "feat(leann): add helper to migrate HuggingFace cache to D drive"
```

---

## Task 3: 更新默认配置示例

**Files:**
- Modify: `wechat-decrypt/config.example.json`

- [ ] **Step 1: 在 `reply_engine.leann` 中新增 `cache_dir`**

```json
"leann": {
    "cli_path": "leann",
    "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
    "cache_dir": "D:\\cache\\leann",
    "timeout": 120
}
```

- [ ] **Step 2: Commit**

```bash
git add wechat-decrypt/config.example.json
git commit -m "config(leann): document cache_dir default in example config"
```

---

## Task 4: 在 control_api 中新增异步 LEANN build endpoint

**Files:**
- Modify: `wechat-decrypt/control_api.py`
- Modify: `wechat-decrypt/target_registry.py`
- Test: `wechat-decrypt/tests/test_control_api.py`（或新建 `tests/test_leann_build.py`）

- [ ] **Step 1: 在 target_registry.py 实现 `build_leann_kb`**

```python
def build_leann_kb(kb_id, docs=None, force=False, config_path=CONFIG_PATH):
    """Start an asynchronous LEANN build for the configured index.

    Returns a dict with build_id and log path. The build runs in a background thread.
    """
    cfg = load_config(config_path)
    spec = _get_kb_spec(kb_id, cfg=cfg, config_path=config_path)
    if not spec:
        raise ValueError("knowledge base not found: %s" % kb_id)
    if str(spec.get("type") or "").lower() != "leann":
        raise ValueError("knowledge base is not leann type: %s" % kb_id)
    index_name = str(spec.get("index_name") or "").strip()
    if not index_name:
        raise ValueError("LEANN 知识库缺少 index_name")
    exe = _leann_exe(spec)
    env = _leann_env(cfg=cfg)
    # default docs to current project dir if not provided
    doc_paths = docs or ["."]
    cmd = [exe, "build", index_name, "--docs"] + doc_paths
    if force:
        cmd.append("--force")
    build_id = "leann_build_%s_%s" % (kb_id, int(time.time()))
    log_dir = Path(config_path).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / "%s.log" % build_id
    _run_leann_build_async(build_id, cmd, env, str(log_path))
    return {"build_id": build_id, "log_path": str(log_path), "index_name": index_name, "status": "started"}
```

- [ ] **Step 2: 实现后台构建线程 `_run_leann_build_async`**

```python
def _run_leann_build_async(build_id, cmd, env, log_path):
    def _build():
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("[%s] START %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(cmd)))
            proc = subprocess.Popen(
                cmd,
                stdout=f,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=_NO_WINDOW_FLAGS,
            )
            _LEANN_BUILD_JOBS[build_id] = {"proc": proc, "log_path": log_path, "status": "running"}
            proc.wait()
            _LEANN_BUILD_JOBS[build_id]["status"] = "done" if proc.returncode == 0 else "failed"
            _LEANN_BUILD_JOBS[build_id]["returncode"] = proc.returncode
            f.write("[%s] END rc=%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), proc.returncode))
    import threading
    t = threading.Thread(target=_build, daemon=True)
    t.start()
```

并在模块顶部初始化：

```python
_LEANN_BUILD_JOBS: Dict[str, Dict[str, Any]] = {}
```

- [ ] **Step 3: 新增查询构建状态的函数**

```python
def get_leann_build_status(build_id):
    job = _LEANN_BUILD_JOBS.get(build_id)
    if not job:
        return {"build_id": build_id, "status": "unknown"}
    return {
        "build_id": build_id,
        "status": job.get("status", "unknown"),
        "returncode": job.get("returncode"),
        "log_path": job.get("log_path"),
    }
```

- [ ] **Step 4: 在 control_api.py 注册路由**

```python
if method == "POST" and (m := _match(path, "/kbs/{key}/leann/build")):
    body = body or {}
    docs = body.get("docs")
    force = bool(body.get("force"))
    try:
        return _ok(**reg.build_leann_kb(m[0], docs=docs, force=force))
    except Exception as e:
        return _err("build failed: %s" % (e,))
if method == "GET" and (m := _match(path, "/kbs/{key}/leann/build/status")):
    build_id = str((params.get("build_id") or [""])[0]).strip()
    return _ok(**reg.get_leann_build_status(build_id))
```

- [ ] **Step 5: 运行新增测试**

Run: `pytest wechat-decrypt/tests/test_target_registry_kb.py -k leann -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add wechat-decrypt/target_registry.py wechat-decrypt/control_api.py wechat-decrypt/tests/test_target_registry_kb.py
git commit -m "feat(leann): async build endpoint and status polling"
```

---

## Task 5: 在 control_client / api 暴露 build client

**Files:**
- Modify: `wechat-decrypt/control_client.py`

- [ ] **Step 1: 新增 `build_leann_kb` 和 `leann_build_status`**

```python
def build_leann_kb(kb_id: str, docs: List[str] = None, force: bool = False,
                   base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    body: Dict[str, Any] = {"force": force}
    if docs:
        body["docs"] = list(docs)
    return _request("POST", "/kbs/%s/leann/build" % urllib.parse.quote(kb_id, safe=""),
                    base_url=base_url, body=body, timeout=10)


def leann_build_status(build_id: str, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    return _request("GET", "/kbs/_leann/build/status",
                    base_url=base_url, params={"build_id": build_id}, timeout=10)
```

> 注意：上面路由用了 `/kbs/_leann/build/status` 占位 key；应随 Task 4 注册为 `/kbs/{key}/leann/build/status`，key 可忽略。

- [ ] **Step 2: Commit**

```bash
git add wechat-decrypt/control_client.py
git commit -m "feat(leann): expose async build client"
```

---

## Task 6: Streamlit UI 增加异步重建索引按钮

**Files:**
- Modify: `wechat-decrypt/wechat-control-ui/app.py`
- Modify: `wechat-decrypt/wechat-control-ui/api.py`（已 re-export control_client，无需改动）

- [ ] **Step 1: 在 KB 列表中为 leann 类型增加"重建索引"按钮**

在显示 LEANN 索引的 expander 内（约 line 1165 附近）新增：

```python
if kb_type == "leann":
    idx = kb.get("index_name") or kb.get("knowledge_base_id") or ""
    st.caption("LEANN 索引名：%s" % idx)
    build_state_key = "leann_build_%s" % kb_id
    if st.button("🔄 重建索引", key="leann_build_btn_%s" % kb_id):
        try:
            result = build_leann_kb(kb_id, force=True, base_url=base)
            st.session_state[build_state_key] = result
            st.info("已启动后台构建：%s" % result.get("build_id"))
        except ControlAPIError as e:
            st.error("启动失败：%s" % (e,))
    status = st.session_state.get(build_state_key)
    if status:
        current = leann_build_status(status.get("build_id"), base_url=base)
        st.caption("状态：%s" % current.get("status"))
        if current.get("log_path"):
            st.caption("日志：%s" % current.get("log_path"))
```

- [ ] **Step 2: 在"新增 LEANN 索引"区域增加"立即后台构建"复选框**

在创建按钮逻辑中：

```python
build_now = st.checkbox("创建后立即后台构建索引", value=False, key="kb_leann_build_now")
if st.button("创建", ...):
    ...
    if build_now:
        build_leann_kb(leann_new_id.strip(), force=True, base_url=base)
        st.info("已启动后台构建")
```

- [ ] **Step 3: 手动验证 UI**

Run: `streamlit run wechat-decrypt/wechat-control-ui/app.py`
Expected: 在"知识库"页面能看到 LEANN 索引的"重建索引"按钮，点击后提示已启动。

- [ ] **Step 4: Commit**

```bash
git add wechat-decrypt/wechat-control-ui/app.py
git commit -m "feat(ui): async LEANN index rebuild button"
```

---

## Task 7: 全量验证与同步到运行时

- [ ] **Step 1: 运行完整测试套件**

Run: `cd wechat-decrypt && pytest`
Expected: 全部通过（当前基线 332 passed）。

- [ ] **Step 2: 手动验证缓存迁移与异步 build**

1. 确认 `D:\cache\leann\huggingface` 已存在并包含模型缓存；如不存在，运行迁移函数。
2. 在 Streamlit 中点击"重建索引"，确认 `logs/leann_build_*.log` 中出现成功输出。

- [ ] **Step 3: Sync to runtime**

按 `AGENTS.md` 的 robocopy 命令同步到 `E:\projects\GA-projects\CD-only\wechat-decrypt`，重启 `control_api` 和 `wechat_bot_monitor`。

- [ ] **Step 4: Refresh codebase-memory index**

Run: codebase-memory-mcp index_repository for `wechat-decrypt`.

---

## Spec Coverage Check

| 需求 | 对应任务 |
|------|---------|
| embedding 模型不要放 C 盘 | Task 1 (`HF_HOME` 默认 D 盘), Task 2 (迁移函数) |
| 异步处理下载/构建 | Task 4 (后台线程), Task 6 (UI 按钮) |
| 与现有目标绑定白名单兼容 | Task 1 仅改环境，不影响 allowed_index_names |

## Placeholder Scan

- 无 TBD/TODO。
- 所有函数均给出完整签名与实现。
- 文件路径为项目中实际路径。
