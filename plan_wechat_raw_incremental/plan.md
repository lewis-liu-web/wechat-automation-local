# 微信原始库增量同步/解密计划

## 当前步骤
S0_PLAN_CONFIRM：已完成只读探索，等待用户确认是否按“目标库级准实时刷新”实施。

## 背景
用户要求：低频同步不能满足及时回复，需要推进原始微信库增量同步/解密，目标是不卡且接近实时回复。

## 已验证事实
- 当前 read-only 监听器仍在运行：`wechat_bot_monitor.py` PID `28384`。
- 原始微信 DB 目录：`E:\document\wechat\xwechat_files\wxid_o0k0isuoveu322_e414\db_storage`
- 当前目标库：
  - `message/message_0.db`
  - 同目录存在 `message_0.db-wal` 与 `message_0.db-shm`
- 原始 `message_0.db-wal` mtime 与大小显示：最新写入可能在 WAL 中。
- 现有 `admin_extract_and_decrypt.py` 每次先跑 `find_all_keys_windows.py` 再跑 `decrypt_db.py`，过重。
- `decrypt_db.py` 有可复用核心函数 `decrypt_database(db_path, out_path, enc_key)`，但 main 当前默认 `os.walk(DB_DIR)` 全库解密。
- 密钥文件 `all_keys.json` 存在；不得读取内容，只引用路径。

## 实施原则
- 不读取密钥内容。
- 不破坏现有 read-only 监听器。
- 默认无参数行为保持兼容，避免影响旧全量流程。
- 解密失败不得覆盖旧明文库。
- 每步改前读文件、改后验证。
- UI/微信发送相关如需操作，遵守 ljqCtrl_sop；当前阶段主要是 DB/脚本改造。

## 执行计划

### S1 备份与基线
1. 读取并备份：
   - `wechat-decrypt/decrypt_db.py`
   - `wechat-decrypt/wechat_bot_monitor.py`
   - 必要时 `wechat-decrypt/admin_extract_and_decrypt.py`
2. 记录当前明文库 mtime、last_local_id、监听器 PID。

验收：
- 备份文件存在。
- 当前监听器未被误杀。

### S2 改造 decrypt_db.py 单库解密能力
1. 增加 CLI 参数：
   - `--db message/message_0.db`
   - 可选 `--out-dir`
   - 可选 `--keys-file`
2. 无参数时保持原全库解密行为。
3. 单库模式只解密目标相对路径，不跑 key 扫描。

验收：
- `python decrypt_db.py --db message/message_0.db` 可运行。
- 不读取/打印密钥内容。
- 输出目标明文库路径正确。
- 默认 `python decrypt_db.py` 行为不被破坏。

### S3 新增轻量刷新脚本
新建：
- `wechat-decrypt/fast_refresh_targets.py`

能力：
1. 读取 `wechat_bot_targets.json`。
2. 汇总目标库集合，例如 `message/message_0.db`。
3. 检查原始 `.db/.db-wal/.db-shm` mtime/size 指纹。
4. 有变化才调用单库解密。
5. 解密成功才刷新状态；失败保留旧明文库。
6. 不调用 `find_all_keys_windows.py`。

验收：
- 无变化时快速退出。
- 有变化时只处理目标库。
- 日志可追踪耗时和处理库。

### S4 接入监听器
在 `wechat_bot_monitor.py` 每轮读明文 DB 前，调用轻量刷新逻辑，或以子进程调用 `fast_refresh_targets.py --once`。

验收：
- 监听器日志出现 `fast-refresh` 相关耗时。
- 不再依赖手动 `admin_extract_and_decrypt.py`。
- 没有新消息时循环低成本。

### S5 真实测试
1. 启动/重启监听器。
2. 用户在测试群 @ 小助手。
3. 观察：
   - 原始库 mtime
   - 明文库 mtime
   - 监听器 new/hit/reply 日志
   - 总延迟

验收：
- 能自动刷新并回复。
- 总延迟满足“接近实时”，目标先定为 3~8 秒内；若达不到，再研究 WAL 处理或更深增量。

## 风险与后续
- 如果现有逐页解密器不合并 WAL，最新消息可能仍需等待微信 checkpoint；届时需要进一步研究 SQLCipher/WCDB WAL 合并。
- 不建议第一阶段直接做页级/行级原始增量，成本高且风险大。
- 若 key 失效，才手动或按需运行 key 扫描。