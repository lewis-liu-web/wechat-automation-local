# WeChat 4.1.x DB Key Hook 交付说明

## 一、交付目标
本交付将“Hook取微信 DB raw key → 验证候选 key → 输出兼容 all_keys.json 结果 → 复用现有解库/监听链”收束为可交接状态。

当前交付**已完成并可直接使用**的部分：
1. Hook 输出协议定义
2. Hook 输出样例
3. Hook 输出适配器
4. 候选 key 验证器
5. Hook 侧最小 key 落盘骨架
6. Hook 安装/模式扫描骨架
7. 模拟链路本地实跑

当前交付**需接入你的现有注入工程**的部分：
1. 真实 Weixin.dll pattern/mask/offset
2. 真实 hook 框架（MinHook/Detours/自有 trampoline）
3. 真实命中点寄存器转发到 `OnWeChatDbKeyPoint(rdx, rip)`

---

## 二、交付文件

### Python / 协议 / 样例
- `wechat_hook_protocol.md`
- `wechat_hook_capture_sample.jsonl`
- `wechat_hook_output_adapter.py`
- `wechat_key_candidate_validator.py`
- `extracted_candidate_keys.txt`
- `hook_verified_all_keys.json`

### C++ Hook 骨架
- `wechat_dbkey_hook_skeleton.cpp`
- `wechat_dbkey_hook_install_skeleton.cpp`

---

## 三、最小闭环

### 第1步：Hook 写出 JSONL
目标文件：
- `wechat_hook_capture.jsonl`

每行示例：
```json
{"ts":"2026-04-27T12:00:00.123Z","pid":1234,"tid":5678,"module":"Weixin.dll","rip":"0x7FF612345678","key_ptr":"0x1ABCDEF0010","key":"00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"}
```

### 第2步：适配 + 验证
```bash
python wechat_hook_output_adapter.py wechat_hook_capture.jsonl --cross --out hook_verified_all_keys.json
```

### 第3步：复用解库链
把生成结果接给 `wechat-decrypt` 现有流程：
```bash
python wechat-decrypt/decrypt_db.py
python wechat-decrypt/monitor_web.py
```

---

## 四、各文件职责

### 1) `wechat_hook_output_adapter.py`
输入：
- 纯 64hex 列表
- JSONL
- 普通日志文本（内含 64hex）

输出：
- `extracted_candidate_keys.txt`
- 自动调用验证器

### 2) `wechat_key_candidate_validator.py`
职责：
- 读取候选 raw 32-byte key
- 遍历 `wechat-decrypt/config.json` 里的 `db_dir`
- 使用 `wechat-decrypt/key_scan_common.py` 做 page1/HMAC 验证
- 输出兼容 `all_keys.json` 的结果草案

补充行为：
- 若 0 命中，不再抛异常中断，而是输出空结果 JSON，便于 Hook 联调

### 3) `wechat_dbkey_hook_skeleton.cpp`
职责：
- 从 `RDX + 0x08` 安全读取 32 字节
- 过滤全零值
- 线程安全追加写 `wechat_hook_capture.jsonl`

### 4) `wechat_dbkey_hook_install_skeleton.cpp`
职责：
- 定位 `Weixin.dll`
- 模式扫描
- 计算 hook 地址
- 安装/卸载 hook（需替换占位实现）
- 命中后转发 `RDX/RIP` 给 `OnWeChatDbKeyPoint`

---

## 五、当前已验证状态

### 已实跑验证
- `wechat_hook_output_adapter.py` 语法通过
- `wechat_key_candidate_validator.py` 语法通过
- 模拟样例 `wechat_hook_capture_sample.jsonl` 可跑通
- 实际数据库目录存在，已扫描到 17 个 DB
- 随机样例 key 不会命中真实 salt，最终输出空结果 JSON（符合预期）

### 当前结果文件示例
```json
{
  "_db_dir": "E:\document\wechat\xwechat_files\lewis4438136_3297\db_storage",
  "_candidate_key_count": 3,
  "_matched_salt_count": 0,
  "_note": "未能从任何微信进程中提取到密钥"
}
```

---

## 六、接入指南（可交付重点）

### 必换项A：真实 pattern/mask/offset
文件：
- `wechat_dbkey_hook_install_skeleton.cpp`

替换区域：
- `kPat_414`
- `kMask_414`
- `kPat_41614`
- `kMask_41614`
- `hook_offset_from_match`

### 必换项B：真实 Hook 框架
替换函数：
- `InstallInlineHook(...)`
- `RemoveInlineHook(...)`
- `BuildOrGetDetourStub()`

### 必接项C：命中寄存器转发
命中点必须能把：
- `RDX`
- `RIP`

传给：
```cpp
OnWeChatDbKeyPoint(rdxValue, ripValue);
```

---

## 七、推荐接法

### 推荐优先级
1. 若命中点可映射到“标准函数入口”，优先用 MinHook/Detours
2. 若命中点在函数中段，则：
   - asm stub / trampoline
   - 或硬件断点 / VEH
3. 只要最终能稳定拿到命中瞬间 `RDX`，本交付的 Python 后链即可复用

---

## 八、交付边界
本次交付已做到“可交接、可联调、可直接接入你的注入工程”的状态，但**尚未强行伪造不可验证的可编译 Hook 工程**。原因是以下两项依赖现场真实工程：
1. 真实签名字节与偏移
2. 具体 hook 框架与 x64 detour stub 实现

这两项不应臆造，否则会产生“看似完整、实际不可用”的伪交付。

---

## 九、建议上线顺序
1. 在注入工程中替换真实签名与 hook 安装逻辑
2. 让目标进程开始写 `wechat_hook_capture.jsonl`
3. 本地执行：
   ```bash
   python wechat_hook_output_adapter.py wechat_hook_capture.jsonl --cross --out hook_verified_all_keys.json
   ```
4. 验证是否出现真实 DB 命中条目
5. 再接 `decrypt_db.py / monitor_web.py`

---

## 十、验收标准
满足以下条件即可视为上线成功：
- `wechat_hook_capture.jsonl` 持续产出真实 64hex key
- `hook_verified_all_keys.json` 出现非空 DB 映射
- `wechat-decrypt/decrypt_db.py` 能成功解至少一个核心 DB
- `monitor_web.py` 能对新消息链路正常工作
