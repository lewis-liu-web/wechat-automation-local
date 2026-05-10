"""
自动检测当前活跃微信数据目录

原理：扫描微信数据根目录下所有账号子目录，
      通过最近文件活动时间判断哪个是当前活跃账号。

用法：
    from find_active_wechat_dir import get_active_wechat_dir
    data_dir, info = get_active_wechat_dir()
"""
import os
from datetime import datetime

# 用户微信数据根目录（可扩展为自动探测）
WECHAT_BASE_DIRS = [
    r"E:\document\wechat\xwechat_files",
    r"E:\WeChat Files",
    r"C:\Users\%USERNAME%\Documents\WeChat Files",
    r"C:\Users\%USERNAME%\Documents\xwechat_files",
]


def expand_env(path: str) -> str:
    """展开环境变量"""
    return os.path.expandvars(path)


def get_dir_activity_score(data_dir: str) -> tuple:
    """
    计算目录活跃度得分。
    返回 (得分, 最近修改时间, 说明)
    得分越高越可能是活跃目录。
    """
    if not os.path.isdir(data_dir):
        return (-1, 0, "目录不存在")

    required_subdirs = ["db_storage", "msg", "config"]
    missing = [d for d in required_subdirs if not os.path.isdir(os.path.join(data_dir, d))]
    if missing:
        return (-1, 0, f"缺少子目录: {missing}")

    # 检查几个关键位置的最近修改时间
    check_paths = []

    # 1. db_storage 目录（加密数据库）
    db_storage = os.path.join(data_dir, "db_storage")
    if os.path.isdir(db_storage):
        check_paths.append(db_storage)
        # 检查里面的文件
        try:
            for f in os.listdir(db_storage):
                check_paths.append(os.path.join(db_storage, f))
        except:
            pass

    # 2. msg/attach 目录（图片附件）
    attach_dir = os.path.join(data_dir, "msg", "attach")
    if os.path.isdir(attach_dir):
        check_paths.append(attach_dir)
        try:
            for root, dirs, files in os.walk(attach_dir):
                for f in files[-10:]:  # 只检查最后几个，避免遍历太多
                    check_paths.append(os.path.join(root, f))
                if len(check_paths) > 50:
                    break
        except:
            pass

    # 3. config 目录
    config_dir = os.path.join(data_dir, "config")
    if os.path.isdir(config_dir):
        check_paths.append(config_dir)

    # 计算最近修改时间
    latest_mtime = 0
    latest_path = ""
    for path in check_paths:
        try:
            mtime = os.path.getmtime(path)
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_path = path
        except:
            continue

    # 计算得分：最近修改时间距离现在越近，得分越高
    # 同时根据文件数量加权
    now = datetime.now().timestamp()
    age_seconds = now - latest_mtime if latest_mtime else 999999999

    # 如果最近1小时内有活动，高分；最近1天内，中分；否则低分
    if age_seconds < 3600:
        score = 1000
    elif age_seconds < 86400:
        score = 500
    elif age_seconds < 7 * 86400:
        score = 100
    else:
        score = 10

    # 额外加分：如果有 V2 .dat 文件，说明是活跃的图片存储
    try:
        v2_count = 0
        for root, dirs, files in os.walk(os.path.join(data_dir, "msg", "attach")):
            for f in files:
                if f.endswith('.dat'):
                    filepath = os.path.join(root, f)
                    try:
                        with open(filepath, 'rb') as fp:
                            header = fp.read(6)
                        if header == b'\x07\x08V2\x08\x07':
                            v2_count += 1
                    except:
                        pass
            if v2_count > 5:
                break
        score += min(v2_count * 10, 200)
    except:
        pass

    mtime_str = datetime.fromtimestamp(latest_mtime).strftime('%Y-%m-%d %H:%M:%S') if latest_mtime else "N/A"
    info = f"最近活动: {mtime_str} @ {os.path.basename(latest_path) or data_dir}"

    return (score, latest_mtime, info)


def get_active_wechat_dir() -> tuple:
    """
    自动检测并返回当前活跃微信数据目录。

    Returns:
        (data_dir: str, info: str)
        如果找不到，data_dir 为 None
    """
    candidates = []

    for base in WECHAT_BASE_DIRS:
        base = expand_env(base)
        if not os.path.isdir(base):
            continue

        for entry in os.listdir(base):
            entry_path = os.path.join(base, entry)
            if not os.path.isdir(entry_path):
                continue
            # 微信目录特征：wxid_ 开头 或 包含数字后缀
            # 但也可能是其他格式，所以不做严格过滤
            score, mtime, info = get_dir_activity_score(entry_path)
            if score > 0:
                candidates.append((score, mtime, entry_path, info))

    if not candidates:
        return None, "未找到任何微信数据目录"

    # 按得分排序，取最高
    candidates.sort(key=lambda x: (-x[0], -x[1]))
    best = candidates[0]

    summary = f"选中: {best[2]}\n得分: {best[0]} | {best[3]}"
    if len(candidates) > 1:
        summary += "\n其他候选:"
        for c in candidates[1:4]:
            summary += f"\n  {c[2]} (得分{c[0]}, {c[3]})"

    return best[2], summary


def auto_sync_config(config_path: str = None) -> str:
    """
    自动检测活跃目录并同步到 config.json
    Returns: 操作结果说明
    """
    import json

    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), "config.json")

    data_dir, info = get_active_wechat_dir()
    if data_dir is None:
        return f"❌ 自动检测失败: {info}"

    # 读取现有配置
    config = {}
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

    # 检查是否需要更新
    new_db_dir = os.path.join(data_dir, "db_storage")
    old_db_dir = config.get('db_dir', '')

    config['db_dir'] = new_db_dir
    config['_auto_detected_dir'] = data_dir
    config['_last_sync_time'] = datetime.now().isoformat()

    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    if old_db_dir != new_db_dir:
        return f"✅ 已自动同步配置\n旧 db_dir: {old_db_dir}\n新 db_dir: {new_db_dir}\n{info}"
    else:
        return f"✅ 配置已是最新，无需更新\n当前 db_dir: {new_db_dir}\n{info}"


if __name__ == "__main__":
    print("=" * 60)
    print("微信活跃数据目录自动检测")
    print("=" * 60)

    data_dir, info = get_active_wechat_dir()
    if data_dir:
        print(f"\n{info}")
        print(f"\n完整路径: {data_dir}")

        # 自动同步
        result = auto_sync_config()
        print(f"\n{result}")
    else:
        print(f"\n❌ {info}")
