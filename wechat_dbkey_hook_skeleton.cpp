// wechat_dbkey_hook_skeleton.cpp
// 最小 Hook 输出器骨架（伪可编译示意）：
// - 命中 Weixin.dll 指定偏移/签名后，取 RDX+0x08 的32字节key
// - 以 JSONL 追加写入 wechat_hook_capture.jsonl
// 注意：此文件只负责“取值+落盘协议”，不包含具体注入/Detours/MinHook接线细节。

#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <string>
#include <sstream>
#include <iomanip>

static CRITICAL_SECTION g_fileLock;
static bool g_inited = false;
static const wchar_t* kOutFile = L"wechat_hook_capture.jsonl";

static std::string hex_encode(const uint8_t* p, size_t n) {
    std::ostringstream oss;
    oss << std::hex << std::setfill('0');
    for (size_t i = 0; i < n; ++i) oss << std::setw(2) << (unsigned)p[i];
    return oss.str();
}

static std::string json_escape(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 8);
    for (char c : s) {
        switch (c) {
        case '\\': out += "\\\\"; break;
        case '"': out += "\\\""; break;
        case '\r': out += "\\r"; break;
        case '\n': out += "\\n"; break;
        case '\t': out += "\\t"; break;
        default: out += c; break;
        }
    }
    return out;
}

static std::string iso8601_utc_now() {
    SYSTEMTIME st;
    GetSystemTime(&st);
    char buf[64];
    sprintf_s(buf, "%04u-%02u-%02uT%02u:%02u:%02u.%03uZ",
        st.wYear, st.wMonth, st.wDay,
        st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
    return buf;
}

static bool safe_copy_32(const void* src, uint8_t out[32]) {
    __try {
        memcpy(out, src, 32);
        return true;
    }
    __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

static bool all_zero_32(const uint8_t k[32]) {
    for (int i = 0; i < 32; ++i) if (k[i] != 0) return false;
    return true;
}

static void append_jsonl(uint32_t pid, uint32_t tid, uint64_t rip, uint64_t keyPtr, const uint8_t key[32]) {
    EnterCriticalSection(&g_fileLock);
    HANDLE h = CreateFileW(kOutFile, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                           OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h != INVALID_HANDLE_VALUE) {
        std::ostringstream oss;
        oss << "{"
            << "\"ts\":\"" << iso8601_utc_now() << "\"," 
            << "\"pid\":" << pid << ","
            << "\"tid\":" << tid << ","
            << "\"module\":\"Weixin.dll\"," 
            << "\"rip\":\"0x" << std::hex << rip << std::dec << "\"," 
            << "\"key_ptr\":\"0x" << std::hex << keyPtr << std::dec << "\"," 
            << "\"key\":\"" << hex_encode(key, 32) << "\""
            << "}\r\n";
        std::string line = oss.str();
        DWORD written = 0;
        WriteFile(h, line.data(), (DWORD)line.size(), &written, NULL);
        CloseHandle(h);
    }
    LeaveCriticalSection(&g_fileLock);
}

extern "C" void OnWeChatDbKeyPoint(uint64_t rdxValue, uint64_t ripValue) {
    // 约定：命中点拿到寄存器快照后，调用此函数
    // 关键：真实 key 指针 = RDX + 0x08
    uint64_t keyPtr = rdxValue + 0x08;
    uint8_t key[32] = {0};
    if (!safe_copy_32((const void*)keyPtr, key)) return;
    if (all_zero_32(key)) return;

    append_jsonl(GetCurrentProcessId(), GetCurrentThreadId(), ripValue, keyPtr, key);
}

BOOL APIENTRY DllMain(HMODULE hModule, DWORD ul_reason_for_call, LPVOID lpReserved) {
    if (ul_reason_for_call == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hModule);
        InitializeCriticalSection(&g_fileLock);
        g_inited = true;
    } else if (ul_reason_for_call == DLL_PROCESS_DETACH) {
        if (g_inited) DeleteCriticalSection(&g_fileLock);
    }
    return TRUE;
}
