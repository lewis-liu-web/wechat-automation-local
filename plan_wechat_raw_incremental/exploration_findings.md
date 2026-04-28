# 原始微信库增量同步/解密探索结论

## 环境现状

- 当前 read-only 监听器仍在运行：`wechat_bot_monitor.py` PID `28384`。
- 当前微信原始库目录：
  - `E:\document\wechat\xwechat_files\wxid_o0k0isuoveu322_e414\db_storage`
- 项目解密输出目录：
  - `./wechat-decrypt/decrypted`
- 密钥文件：
  - `all_keys.json` 存在，大小 `2934` bytes；未读取内容。
- 目标消息库元信息：
  - 原始 `message/message_0.db`：`299008` bytes，mtime `2026-04-27 13:57:12`
  - 原始 `message/message_0.db-wal`：`547992` bytes，mtime `2026-04-27 13:57:12`
  - 原始 `message/message_0.db-shm`：`32768` bytes，mtime `2026-04-27 08:12:54`
  - 明文 `decrypted/message/message_0.db`：`299008` bytes，mtime `2026-04-27 13:57:00`
- 结论：微信最新写入主要可能在 WAL 里；只复制/只解密 `.db` 主文件可能丢最近消息，必须考虑 `.db-wal/.db-shm` 三件套一致性。

## 关键发现

1. `admin_extract_and_decrypt.py` 现在固定顺序执行：
   - `find_all_keys_windows.py`
   - `decrypt_db.py`
2. `find_all_keys_windows.py` 会扫描 Weixin.exe 内存提取全部 DB key，耗时和侵入性都比单库解密高；如果 `all_keys.json` 仍有效，不应每轮都跑。
3. `decrypt_db.py` 已经有核心函数：
   - `decrypt_database(db_path, out_path, enc_key)`
4. 但 `decrypt_db.py main()` 当前是：
   - 读取 `all_keys.json`
   - `os.walk(DB_DIR)` 扫全部 db
   - 解密所有能匹配 key 的库
5. 因此最小改造很明确：
   - 保留 `decrypt_database()`
   - 给 `decrypt_db.py` 增加 `--db message/message_0.db` 或类似目标参数
   - 只对目标库执行解密
6. 由于当前库很小，单库解密理论上可做到秒级甚至亚秒级；真正瓶颈更可能是：
   - WAL 一致性
   - 解密后明文库是否包含最新 WAL 内容
   - 监听器触发节流和 UI 回复耗时

## 风险/不确定点

- SQLCipher/WCDB 的 WAL 文件是否能被当前逐页解密器直接合并，目前还未验证。
- 现有 `decrypt_database()` 只按单一 `.db` 文件逐页解密输出，不处理 `-wal` 帧；如果最新消息只在 WAL，中间可能仍然延迟到 checkpoint 后才进入 `.db`。
- 若直接复制微信正在写的三件套，存在一致性风险；需要采用“快照目录 + 校验 + 原子替换/失败不覆盖”的策略。
- 如果 key 失效，单库解密会失败；此时才需要重新跑 `find_all_keys_windows.py`，不应每轮跑。
- 真正“页级/行级原始增量”实现成本较高，容易卡在 WAL 帧、SQLCipher page MAC、checkpoint 和版本兼容上，不建议第一阶段直接做。

## 建议实现路径

### 第一阶段：目标库级准实时刷新

目标：先让监听器不依赖手动全量同步，而是自动刷新目标 `message_N.db`，延迟控制在几秒内。

步骤：

1. 改造 `decrypt_db.py`
   - 增加 CLI 参数：
     - `--db message/message_0.db`
     - 可选 `--out-dir`
     - 可选 `--keys-file`
   - 默认无参数时保持原全库行为，避免破坏旧流程。
2. 新增或改造轻量同步器
   - 监控配置中涉及的目标库集合，例如：
     - `message/message_0.db`
   - 检测原始 `.db/.db-wal/.db-shm` mtime/size 变化。
   - 有变化时先只解密目标库。
3. 改造监听器
   - 在每轮读明文库前，调用轻量刷新逻辑。
   - 或者单独后台刷新，监听器只读明文库。
4. 安全策略
   - 解密成功才替换明文输出。
   - 解密失败保留旧明文库。
   - 不每轮扫描 key；只有 key 缺失/失效才提示或手动刷新 key。
5. 验证
   - 单独跑 `decrypt_db.py --db message/message_0.db` 计时。
   - 对比原全量解密耗时。
   - 发一条测试消息，观察原始库 mtime、明文库 mtime、监听命中和回复耗时。

### 第二阶段：明文层业务增量

目标：解密可以仍是单库级，但业务读取严格按 `local_id > last_local_id`，不全量扫描消息表。

### 第三阶段：原始页/WAL 级增量，仅作为后续优化

如果第一阶段仍然慢，再研究 SQLCipher WAL 帧级处理，不作为当前优先项。

## 推荐本轮执行范围

本轮建议只做“低风险最小闭环”：

1. 备份现有 `decrypt_db.py` 和 `wechat_bot_monitor.py`。
2. 给 `decrypt_db.py` 增加 `--db` 单库过滤能力。
3. 新建 `admin_decrypt_one_db.py` 或 `fast_refresh_targets.py`，只调用单库解密，不跑 key 扫描。
4. 先命令行验证单库刷新耗时。
5. 再把轻量刷新接入监听器。
6. 完成后测试真实群 @ 回复。