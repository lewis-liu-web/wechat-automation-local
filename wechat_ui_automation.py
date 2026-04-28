
"""
wechat_ui_automation.py

安全微信自动化工具（UI/OCR 方案）：
- 不注入进程
- 不读取微信内存
- 不解密数据库
- 只对当前用户已打开的微信窗口做窗口截图/OCR/物理点击/剪贴板输入

命令：
  python wechat_ui_automation.py status
  python wechat_ui_automation.py read --out wechat_visible.json
  python wechat_ui_automation.py search "文件传输助手"
  python wechat_ui_automation.py send "你好" --no-enter   # 只粘贴不回车
  python wechat_ui_automation.py send "你好"              # 粘贴并回车发送
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import win32con
import win32gui
import win32process
from PIL import ImageGrab

sys.path.append(str(Path(__file__).resolve().parent.parent / 'memory'))
try:
    import ocr_utils
except Exception as e:
    ocr_utils = None
    OCR_IMPORT_ERROR = repr(e)
else:
    OCR_IMPORT_ERROR = None

try:
    import ljqCtrl
except Exception as e:
    ljqCtrl = None
    LJQ_IMPORT_ERROR = repr(e)
else:
    LJQ_IMPORT_ERROR = None


def find_wechat_window():
    found = []
    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd) or ''
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            pid = 0
        if title == '微信' or '微信' in title or 'WeChat' in title:
            rect = win32gui.GetWindowRect(hwnd)
            if rect[2] - rect[0] > 300 and rect[3] - rect[1] > 300:
                found.append({'hwnd': hwnd, 'title': title, 'pid': pid, 'rect': rect})
    win32gui.EnumWindows(cb, None)
    if not found:
        raise RuntimeError('未找到已打开的微信主窗口')
    # 优先标题精确为“微信”的最大窗口
    found.sort(key=lambda x: ((x['title'] == '微信'), (x['rect'][2]-x['rect'][0])*(x['rect'][3]-x['rect'][1])), reverse=True)
    return found[0]


def activate(hwnd):
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.35)


def client_bbox(hwnd):
    left_top = win32gui.ClientToScreen(hwnd, (0, 0))
    rc = win32gui.GetClientRect(hwnd)
    right_bottom = win32gui.ClientToScreen(hwnd, (rc[2], rc[3]))
    return (*left_top, *right_bottom)


def screenshot(hwnd, out='wechat_current_client.png'):
    bbox = client_bbox(hwnd)
    img = ImageGrab.grab(bbox)
    img.save(out)
    return img, bbox


def ocr_crop(img, box):
    if ocr_utils is None:
        raise RuntimeError('ocr_utils 导入失败: ' + str(OCR_IMPORT_ERROR))
    crop = img.crop(box)
    r = ocr_utils.ocr_image(crop, enhance=False)
    lines = [str(x).strip() for x in r.get('lines', []) if str(x).strip()]
    details = []
    for d in r.get('details', []) or []:
        details.append({
            'text': d.get('text', ''),
            'conf': float(d.get('conf', 0) or 0),
            'bbox': d.get('bbox'),
        })
    return {'lines': lines, 'details': details}


def read_visible(save_images=True):
    win = find_wechat_window()
    hwnd = win['hwnd']
    activate(hwnd)
    img, bbox = screenshot(hwnd)
    w, h = img.size

    # 微信常见布局：左导航约55px，会话列表到约300px，聊天区域在右侧。
    regions = {
        'search': (55, 0, min(310, w), min(80, h)),
        'conversation_list': (55, 70, min(330, w), h),
        'chat_header': (300, 0, w, min(90, h)),
        'chat_messages': (300, 80, w, max(80, h - 160)),
        'chat_input': (300, max(0, h - 170), w, h),
        'full_client': (0, 0, w, h),
    }
    out = {
        'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'window': {**win, 'rect': list(win['rect'])},
        'client_bbox_screen': list(bbox),
        'client_size': [w, h],
        'regions': {},
        'note': 'UI/OCR读取的是当前屏幕可见内容；不是数据库全量历史。',
    }
    for name, box in regions.items():
        try:
            out['regions'][name] = {'box': list(box), **ocr_crop(img, box)}
        except Exception as e:
            out['regions'][name] = {'box': list(box), 'error': repr(e), 'lines': [], 'details': []}
        if save_images:
            try:
                img.crop(box).save(f'wechat_region_{name}.png')
            except Exception:
                pass
    return out


def physical_click_client(hwnd, x, y):
    # win32 坐标在未全局 DPI aware 时通常是逻辑坐标；ljqCtrl.Click 需要物理坐标。
    sx, sy = win32gui.ClientToScreen(hwnd, (int(x), int(y)))
    if ljqCtrl is None:
        raise RuntimeError('ljqCtrl 导入失败: ' + str(LJQ_IMPORT_ERROR))
    scale = float(getattr(ljqCtrl, 'dpi_scale', 1.0) or 1.0)
    px, py = sx / scale, sy / scale
    activate(hwnd)
    ljqCtrl.Click(px, py)
    time.sleep(0.25)


def search_chat(keyword):
    import pyperclip
    win = find_wechat_window(); hwnd = win['hwnd']
    activate(hwnd)
    # 搜索框通常位于客户端 (80, 38) 附近
    physical_click_client(hwnd, 150, 38)
    pyperclip.copy(keyword)
    ljqCtrl.Press('ctrl+a')
    ljqCtrl.Press('ctrl+v')
    time.sleep(0.8)
    # 点击第一条搜索结果常在 (170, 115) 附近；若没有结果也只是点击搜索面板
    physical_click_client(hwnd, 170, 115)
    time.sleep(0.8)
    return read_visible(save_images=True)


def send_text(text, enter=True):
    import pyperclip
    win = find_wechat_window(); hwnd = win['hwnd']
    activate(hwnd)
    rc = win32gui.GetClientRect(hwnd)
    w, h = rc[2], rc[3]
    # 输入区在右下，点击输入框中央偏左
    physical_click_client(hwnd, max(360, int(w*0.55)), max(520, h - 85))
    pyperclip.copy(text)
    ljqCtrl.Press('ctrl+v')
    if enter:
        time.sleep(0.1)
        ljqCtrl.Press('enter')
    time.sleep(0.5)
    return read_visible(save_images=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('status')
    p_read = sub.add_parser('read'); p_read.add_argument('--out', default='wechat_visible.json')
    p_search = sub.add_parser('search'); p_search.add_argument('keyword'); p_search.add_argument('--out', default='wechat_visible.json')
    p_send = sub.add_parser('send'); p_send.add_argument('text'); p_send.add_argument('--no-enter', action='store_true'); p_send.add_argument('--out', default='wechat_visible.json')
    args = ap.parse_args()

    if args.cmd == 'status':
        win = find_wechat_window(); activate(win['hwnd'])
        img, bbox = screenshot(win['hwnd'])
        print(json.dumps({'ok': True, 'window': {**win, 'rect': list(win['rect'])}, 'client_bbox_screen': bbox, 'client_size': img.size, 'ocr_ok': ocr_utils is not None, 'ljq_ok': ljqCtrl is not None}, ensure_ascii=False, indent=2))
        return
    if args.cmd == 'read':
        out = read_visible(); Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
        print(Path(args.out).resolve())
        print('\n'.join(out['regions']['conversation_list']['lines'][:20]))
        print('--- chat visible ---')
        print('\n'.join(out['regions']['chat_messages']['lines'][:40]))
        return
    if args.cmd == 'search':
        out = search_chat(args.keyword); Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
        print(Path(args.out).resolve())
        print('--- chat visible ---')
        print('\n'.join(out['regions']['chat_messages']['lines'][:40]))
        return
    if args.cmd == 'send':
        out = send_text(args.text, enter=not args.no_enter); Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
        print(Path(args.out).resolve())
        print('sent' if not args.no_enter else 'pasted_only')

if __name__ == '__main__':
    main()
