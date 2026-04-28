# WeChat DB Key Hook 最小落地协议

## 1. Hook 命中目标
- 模块：`Weixin.dll`
- 版本：4.1.4 ~ 4.1.6.14（已分别有 pattern/mask/offset）
- 命中后：从 `RDX + 0x08` 读取 32 字节 raw DB key

## 2. 推荐日志输出格式
建议 Hook 每次命中写一行 JSON（JSONL）：

```json
{"ts":"2026-04-27T12:00:00.123","pid":1234,"tid":5678,"module":"Weixin.dll","rip":"0x7FF612345678","key_ptr":"0x1ABCDEF0010","key":"00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"}
```

字段约束：
- `ts`: ISO 时间字符串
- `pid`: 进程ID
- `tid`: 线程ID
- `module`: 固定写 `Weixin.dll`
- `rip`: 当前命中地址，16进制字符串
- `key_ptr`: `RDX+0x08` 的地址，16进制字符串
- `key`: 32字节原始DB key，64hex

## 3. 最小伪代码
```c
on_hook_hit(ctx) {
    uint8_t* p = (uint8_t*)(ctx->Rdx + 0x08);
    uint8_t key[32];
    safe_read(key, p, 32);
    if (!looks_nonzero(key, 32)) return;
    append_jsonl(log_path, {
        "ts": now_iso8601_ms(),
        "pid": GetCurrentProcessId(),
        "tid": GetCurrentThreadId(),
        "module": "Weixin.dll",
        "rip": hex(ctx->Rip),
        "key_ptr": hex((uintptr_t)p),
        "key": hex_encode(key, 32),
    });
}
```

## 4. 落地命令
### 4.1 抽取与验证
```bash
python wechat_hook_output_adapter.py wechat_hook_capture.jsonl --cross
```

### 4.2 解库
```bash
python wechat-decrypt/decrypt_db.py
```

### 4.3 如需指定DB目录
```bash
python wechat_hook_output_adapter.py wechat_hook_capture.jsonl --db-dir "D:\WeChatData\Msg" --cross
```

## 5. 排错要点
- 如果一直无命中：优先复核版本签名是否匹配当前 Weixin.dll
- 如果有 key 但全验不过：
  1. 复核是否真的取的是 `RDX+0x08`
  2. 复核读取长度是否固定 32 字节
  3. 复核是否命中的是 DB 打开/加解密路径而非邻近无关键函数
- 如果同一 key 命中多个 DB：正常，后续由 salt/HMAC 验证筛真
- 如果 key 频繁重复：正常，适配器会去重
