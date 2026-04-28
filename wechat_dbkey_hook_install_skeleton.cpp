// wechat_dbkey_hook_install_skeleton.cpp
// 完整接线骨架（示意）：
// 1) 定位 Weixin.dll 基址/大小
// 2) 用 pattern+mask 扫描版本特征
// 3) 按 offset 计算命中地址/或直接取匹配地址
// 4) 安装 hook（此处保留为伪接口）
// 5) 命中时抓 RIP/RDX，并转给 OnWeChatDbKeyPoint
//
// 说明：这里不绑定具体框架（MinHook/Detours/手写trampoline），
//       但把“怎么串起来”完整定型，便于你直接替换 InstallInlineHook / RemoveInlineHook。

#include <windows.h>
#include <psapi.h>
#include <stdint.h>
#include <vector>
#include <string>
#include <stdio.h>
#include <string.h>

#pragma comment(lib, "Psapi.lib")

extern "C" void OnWeChatDbKeyPoint(uint64_t rdxValue, uint64_t ripValue); // 来自 wechat_dbkey_hook_skeleton.cpp

struct PatternSpec {
    const char* version;
    const uint8_t* bytes;
    const char* mask;   // 'x' = exact, '?' = wildcard
    ptrdiff_t hook_offset_from_match; // 若要从匹配起点偏移到真正Hook点
};

// ===== 这里替换成你已确认的真实版本签名 =====
static const uint8_t kPat_414[] = { 0x48, 0x8B, 0xD1, 0x48, 0x8D, 0x4A, 0x08 };
static const char*   kMask_414 = "xxxxxxx";
static const uint8_t kPat_41614[] = { 0x48, 0x8B, 0xD1, 0x48, 0x8D, 0x4A, 0x08 };
static const char*   kMask_41614 = "xxxxxxx";

static PatternSpec g_specs[] = {
    { "4.1.4.x",   kPat_414,   kMask_414,   0 },
    { "4.1.6.14", kPat_41614, kMask_41614, 0 },
};

static void* g_hookTarget = nullptr;
static void* g_trampoline = nullptr;

static bool get_module_range(const wchar_t* moduleName, uint8_t*& base, size_t& size) {
    HMODULE mods[1024]; DWORD cbNeeded = 0;
    if (!EnumProcessModules(GetCurrentProcess(), mods, sizeof(mods), &cbNeeded)) return false;
    size_t n = cbNeeded / sizeof(HMODULE);
    wchar_t nameBuf[MAX_PATH];
    for (size_t i = 0; i < n; ++i) {
        if (!GetModuleBaseNameW(GetCurrentProcess(), mods[i], nameBuf, MAX_PATH)) continue;
        if (_wcsicmp(nameBuf, moduleName) == 0) {
            MODULEINFO mi{};
            if (!GetModuleInformation(GetCurrentProcess(), mods[i], &mi, sizeof(mi))) return false;
            base = (uint8_t*)mi.lpBaseOfDll;
            size = (size_t)mi.SizeOfImage;
            return true;
        }
    }
    return false;
}

static uint8_t* find_pattern(uint8_t* base, size_t size, const uint8_t* pat, const char* mask) {
    size_t patLen = strlen(mask);
    for (size_t i = 0; i + patLen <= size; ++i) {
        bool ok = true;
        for (size_t j = 0; j < patLen; ++j) {
            if (mask[j] == 'x' && base[i + j] != pat[j]) {
                ok = false;
                break;
            }
        }
        if (ok) return base + i;
    }
    return nullptr;
}

// ==== 下面两个函数由你替换成真实Hook框架实现 ====
static bool InstallInlineHook(void* target, void* detour, void** original_trampoline) {
    // MinHook 版大致会是：MH_CreateHook(target, detour, original_trampoline); MH_EnableHook(target);
    // Detours 版则是 DetourTransactionBegin/Attach/Commit
    // 这里只做占位，返回 false 以提示“还没接真实框架”
    (void)target; (void)detour; (void)original_trampoline;
    return false;
}

static void RemoveInlineHook(void* target) {
    (void)target;
}

// ===== 命中回调桥 =====
extern "C" void __stdcall OnWeChatDbKeyPoint_Bridge(uint64_t rdxValue, uint64_t ripValue) {
    OnWeChatDbKeyPoint(rdxValue, ripValue);
}

// ===== 裸桥/汇编桥：示意 =====
// 你的真实detour需要：
// 1) 保存必要寄存器
// 2) 取命中瞬间 RDX
// 3) 取当前 RIP（可用目标地址常量、返回地址或上下文推导）
// 4) 调 OnWeChatDbKeyPoint_Bridge(rdx, rip)
// 5) 跳回 trampoline
//
// 因为 x64 MSVC 不支持 inline asm，这里只保留“伪签名”。
// 实战里通常：
// - 用 MinHook/Detours 直接 hook 一个有标准函数签名的函数；或
// - 写独立 asm stub；或
// - VEH/硬件断点方案。
static void* BuildOrGetDetourStub() {
    return nullptr; // 这里替换成真实 detour stub 地址
}

static bool InstallWeChatDbKeyHook() {
    uint8_t* base = nullptr;
    size_t size = 0;
    if (!get_module_range(L"Weixin.dll", base, size)) {
        OutputDebugStringA("[dbkey-hook] Weixin.dll not found\n");
        return false;
    }

    char msg[256];
    sprintf_s(msg, "[dbkey-hook] Weixin.dll base=%p size=%zu\n", base, size);
    OutputDebugStringA(msg);

    for (const auto& spec : g_specs) {
        uint8_t* m = find_pattern(base, size, spec.bytes, spec.mask);
        if (!m) continue;

        uint8_t* hookAddr = m + spec.hook_offset_from_match;
        sprintf_s(msg, "[dbkey-hook] matched version=%s match=%p hook=%p\n", spec.version, m, hookAddr);
        OutputDebugStringA(msg);

        void* detour = BuildOrGetDetourStub();
        if (!detour) {
            OutputDebugStringA("[dbkey-hook] detour stub not built yet\n");
            return false;
        }

        if (!InstallInlineHook(hookAddr, detour, &g_trampoline)) {
            OutputDebugStringA("[dbkey-hook] InstallInlineHook failed\n");
            return false;
        }

        g_hookTarget = hookAddr;
        OutputDebugStringA("[dbkey-hook] hook installed\n");
        return true;
    }

    OutputDebugStringA("[dbkey-hook] no known pattern matched\n");
    return false;
}

static void UninstallWeChatDbKeyHook() {
    if (g_hookTarget) {
        RemoveInlineHook(g_hookTarget);
        g_hookTarget = nullptr;
        g_trampoline = nullptr;
    }
}

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID reserved) {
    (void)hModule; (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hModule);
        InstallWeChatDbKeyHook();
    } else if (reason == DLL_PROCESS_DETACH) {
        UninstallWeChatDbKeyHook();
    }
    return TRUE;
}
