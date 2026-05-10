"""
配置加载器 - 从 config.json 读取路径配置
首次运行时自动检测微信数据目录，检测失败则提示手动配置
"""
import glob
import json
import os
import platform
import sys
from datetime import datetime

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

_SYSTEM = platform.system().lower()

if _SYSTEM == "linux":
    _DEFAULT_TEMPLATE_DIR = os.path.expanduser("~/Documents/xwechat_files/your_wxid/db_storage")
    _DEFAULT_PROCESS = "wechat"
elif _SYSTEM == "darwin":
    # macOS 使用独立的 C 扫描器 (find_all_keys_macos.c)，此处仅提供 config 默认值
    _DEFAULT_TEMPLATE_DIR = os.path.expanduser("~/Documents/xwechat_files/your_wxid/db_storage")
    _DEFAULT_PROCESS = "WeChat"
else:
    _DEFAULT_TEMPLATE_DIR = r"D:\xwechat_files\your_wxid\db_storage"
    _DEFAULT_PROCESS = "Weixin.exe"

_DEFAULT = {
    "db_dir": _DEFAULT_TEMPLATE_DIR,
    "keys_file": "all_keys.json",
    "decrypted_dir": "decrypted",
    "decoded_image_dir": "decoded_images",
    "wechat_process": _DEFAULT_PROCESS,
}


def _choose_candidate(candidates):
    """在多个候选目录中选择一个。"""
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        if not sys.stdin.isatty():
            return candidates[0]
        print("[!] 检测到多个微信数据目录（请选择当前正在运行的微信账号）:")
        for i, c in enumerate(candidates, 1):
            print(f"    {i}. {c}")
        print("    0. 跳过，稍后手动配置")
        try:
            while True:
                choice = input("请选择 [0-{}]: ".format(len(candidates))).strip()
                if choice == "0":
                    return None
                if choice.isdigit() and 1 <= int(choice) <= len(candidates):
                    return candidates[int(choice) - 1]
                print("    无效输入，请重新选择")
        except (EOFError, KeyboardInterrupt):
            print()
            return None
    return None



def _auto_detect_db_dir_windows():
    r"""从微信本地配置自动检测 Windows db_storage 路径。

    策略：
      1. 读取 %APPDATA%\\Tencent\\xwechat\\config\\*.ini 获取数据根目录。
      2. 在根目录下搜索所有 xwechat_files\\*\\db_storage。
      3. 如果 ini 未命中，回退扫描常见盘符（C/D/E）下可能路径。
      4. 对每个候选目录计算活跃度得分（按最近活动时间 + V2图片文件数排序）。
      5. 若只有一个高活跃候选，直接选中；若多个，列表供用户选择。
    """
    appdata = os.environ.get("APPDATA", "")
    config_dir = os.path.join(appdata, "Tencent", "xwechat", "config")
    seen = set()
    candidates = []

    # 1) 从 ini 读取
    if os.path.isdir(config_dir):
        for ini_file in glob.glob(os.path.join(config_dir, "*.ini")):
            try:
                content = None
                for enc in ("utf-8", "gbk"):
                    try:
                        with open(ini_file, "r", encoding=enc) as f:
                            content = f.read(1024).strip()
                        break
                    except UnicodeDecodeError:
                        continue
                if not content or any(c in content for c in "\n\r\x00"):
                    continue
                if os.path.isdir(content):
                    pattern = os.path.join(content, "xwechat_files", "*", "db_storage")
                    for match in glob.glob(pattern):
                        normalized = os.path.normcase(os.path.normpath(match))
                        if os.path.isdir(match) and normalized not in seen:
                            seen.add(normalized)
                            candidates.append(match)
            except OSError:
                continue

    # 2) 回退扫描常见路径
    if not candidates:
        fallback_roots = []
        for drive in ["C:", "D:", "E:", os.path.splitdrive(os.getcwd())[0] + ":"]:
            for rel in [
                r"Users\*\Documents\WeChat Files",
                r"Users\*\Documents\xwechat_files",
                r"WeChat Files",
                r"xwechat_files",
                r"document\wechat\xwechat_files",
            ]:
                pattern = os.path.join(drive, rel, "*", "db_storage")
                for match in glob.glob(pattern):
                    normalized = os.path.normcase(os.path.normpath(match))
                    if os.path.isdir(match) and normalized not in seen:
                        seen.add(normalized)
                        candidates.append(match)

    if not candidates:
        return None

    # 4) 按 message 目录 mtime 降序（活跃账号优先）
    def _mtime(path):
        msg_dir = os.path.join(path, "message")
        target = msg_dir if os.path.isdir(msg_dir) else path
        try:
            return os.path.getmtime(target)
        except OSError:
            return 0

    candidates.sort(key=_mtime, reverse=True)

    # 单一候选直接返回
    if len(candidates) == 1:
        mtime_str = datetime.fromtimestamp(_mtime(candidates[0])).strftime('%Y-%m-%d %H:%M:%S')
        print(f"[+] 自动检测到微信数据目录: {candidates[0]} (message 目录最近修改: {mtime_str})")
        return candidates[0]

    # 非交互环境直接选最活跃的
    if not sys.stdin.isatty():
        mtime_str = datetime.fromtimestamp(_mtime(candidates[0])).strftime('%Y-%m-%d %H:%M:%S')
        print(f"[+] 自动选择最活跃目录: {candidates[0]} (message 目录最近修改: {mtime_str})")
        return candidates[0]

    # 交互环境展示候选列表
    print("[!] 检测到多个微信数据目录（按 message 目录最近修改时间排序）:")
    for i, c in enumerate(candidates, 1):
        mtime_str = datetime.fromtimestamp(_mtime(c)).strftime('%Y-%m-%d %H:%M:%S')
        marker = " ← 推荐" if i == 1 else ""
        print(f"    {i}. {c}  (message 修改: {mtime_str}){marker}")
    print("    0. 跳过，稍后手动配置")
    try:
        while True:
            choice = input("请选择 [0-{}]: ".format(len(candidates))).strip()
            if choice == "0":
                return None
            if choice.isdigit() and 1 <= int(choice) <= len(candidates):
                return candidates[int(choice) - 1]
            print("    无效输入，请重新选择")
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _auto_detect_db_dir_linux():
    """自动检测 Linux 微信 db_storage 路径。

    优先搜索当前用户的 home 目录。以 sudo 运行时通过 SUDO_USER 回退到
    实际用户的 home，避免只搜索 /root 而遗漏真实数据目录。
    """
    seen = set()
    candidates = []
    search_roots = [
        os.path.expanduser("~/Documents/xwechat_files"),
    ]
    # sudo 运行时，~ 展开为 /root；回退到实际用户的 home
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        # 验证 SUDO_USER 是合法系统用户，防止路径注入
        import pwd
        try:
            sudo_home = pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            sudo_home = None
        if sudo_home:
            fallback = os.path.join(sudo_home, "Documents", "xwechat_files")
            if fallback not in search_roots:
                search_roots.append(fallback)

    for root in search_roots:
        if not os.path.isdir(root):
            continue
        pattern = os.path.join(root, "*", "db_storage")
        for match in glob.glob(pattern):
            normalized = os.path.normcase(os.path.normpath(match))
            if os.path.isdir(match) and normalized not in seen:
                seen.add(normalized)
                candidates.append(match)

    # 早期 Linux 微信版本（wine/容器方案）使用的数据路径
    old_path = os.path.expanduser("~/.local/share/weixin/data/db_storage")
    if os.path.isdir(old_path):
        normalized = os.path.normcase(os.path.normpath(old_path))
        if normalized not in seen:
            candidates.append(old_path)

    # 优先使用最近活跃账号：按 message 目录 mtime 降序（近似排序，best-effort）
    def _mtime(path):
        msg_dir = os.path.join(path, "message")
        target = msg_dir if os.path.isdir(msg_dir) else path
        try:
            return os.path.getmtime(target)
        except OSError:
            return 0

    candidates.sort(key=_mtime, reverse=True)
    return _choose_candidate(candidates)


def auto_detect_db_dir():
    if _SYSTEM == "windows":
        return _auto_detect_db_dir_windows()
    if _SYSTEM == "linux":
        return _auto_detect_db_dir_linux()
    return None


def default_config():
    """Return a fresh copy of the platform-specific default config."""
    return dict(_DEFAULT)


def _with_resolved_paths(cfg):
    """Return cfg with project-relative paths resolved to absolute paths."""
    resolved = dict(cfg)
    base = os.path.dirname(os.path.abspath(__file__))
    for key in ("keys_file", "decrypted_dir", "decoded_image_dir"):
        if key in resolved and not os.path.isabs(resolved[key]):
            resolved[key] = os.path.join(base, resolved[key])

    db_dir = resolved.get("db_dir", "")
    if db_dir and os.path.basename(db_dir) == "db_storage":
        resolved["wechat_base_dir"] = os.path.dirname(db_dir)
    else:
        resolved["wechat_base_dir"] = db_dir

    if "decoded_image_dir" not in resolved:
        resolved["decoded_image_dir"] = os.path.join(base, "decoded_images")
    return resolved


def load_config(config_file=CONFIG_FILE, *, exit_on_missing=True, write_missing=True):
    """Load config.json and resolve relative paths.

    The default behavior is kept CLI-friendly: missing/unresolved db_dir exits with
    instructions. Tests and import-only tools can pass ``exit_on_missing=False`` to
    inspect safe defaults without terminating the process.
    """
    cfg = {}
    if os.path.exists(config_file):
        try:
            with open(config_file, encoding="utf-8") as f:
                cfg = json.load(f)
        except json.JSONDecodeError:
            print(f"[!] {config_file} 格式损坏，将使用默认配置")
            cfg = {}
    # db_dir 缺失或仍为模板值时，尝试自动检测
    db_dir = cfg.get("db_dir", "")
    if not db_dir or db_dir == _DEFAULT_TEMPLATE_DIR or "your_wxid" in db_dir:
        detected = auto_detect_db_dir()
        if detected:
            print(f"[+] 自动检测到微信数据目录: {detected}")
            cfg = {**_DEFAULT, **cfg, "db_dir": detected}
            if write_missing:
                with open(config_file, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=4, ensure_ascii=False)
                print(f"[+] 已保存到: {config_file}")
        else:
            cfg = {**_DEFAULT, **cfg}
            if write_missing and not os.path.exists(config_file):
                with open(config_file, "w", encoding="utf-8") as f:
                    json.dump(_DEFAULT, f, indent=4, ensure_ascii=False)
            print(f"[!] 未能自动检测微信数据目录")
            print(f"    请手动编辑 {config_file} 中的 db_dir 字段")
            if _SYSTEM == "linux":
                print("    Linux 默认路径类似: ~/Documents/xwechat_files/<wxid>/db_storage")
            else:
                print(f"    路径可在 微信设置 → 文件管理 中找到")
            if exit_on_missing:
                sys.exit(1)
    else:
        cfg = {**_DEFAULT, **cfg}

    return _with_resolved_paths(cfg)
