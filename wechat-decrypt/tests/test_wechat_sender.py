import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SENDER_PATH = ROOT / 'wechat_sender.py'

spec = importlib.util.spec_from_file_location('wechat_sender_under_test', SENDER_PATH)
wechat_sender = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wechat_sender
spec.loader.exec_module(wechat_sender)


def test_target_name_matches_ocr_variants():
    assert wechat_sender.target_name_matches(' bot群聊测试 ', '【bot群聊测试】  12:00')
    assert wechat_sender.target_name_matches('重生之我在重庆移动当牛做马', '重生之我在重庆移动…')
    assert not wechat_sender.target_name_matches('bot群聊测试', '另一个群聊')


def test_target_aliases_keeps_name_username_and_custom_aliases():
    target = {
        'name': '群A',
        'username': '123@chatroom',
        'aliases': ['群A', '备用名'],
    }
    assert wechat_sender.target_aliases(target) == ['群A', '123@chatroom', '备用名']


def test_send_reply_backend_mode_uses_confirmation(monkeypatch):
    calls = []
    monkeypatch.setattr(wechat_sender, 'send_reply_backend', lambda text, log=None: True)

    def confirm(target, before_local_id, text, timeout):
        calls.append((target['name'], before_local_id, text, timeout))
        return True

    result = wechat_sender.send_reply_detailed(
        'hello',
        mode='backend',
        target={'name': '群A'},
        before_local_id=7,
        cfg={'send_confirm_timeout': 0.3},
        confirm=confirm,
    )
    assert result.ok is True
    assert result.confirmed is True
    assert result.attempted == ['backend']
    assert calls == [('群A', 7, 'hello', 0.3)]


def test_send_reply_foreground_confirmation_timeout_sets_reason(monkeypatch):
    monkeypatch.setattr(
        wechat_sender,
        'send_reply_foreground',
        lambda text, target=None, cfg=None, log=None: wechat_sender.SendResult(
            ok=True, mode='foreground', attempted=['physical_input'], reason='sent_physical_foreground'
        ),
    )
    result = wechat_sender.send_reply_detailed(
        'hello',
        mode='foreground',
        target={'name': '群A'},
        before_local_id=7,
        cfg={'send_confirm_timeout': 0.1},
        confirm=lambda target, lid, text, timeout: False,
    )
    assert result.ok is False
    assert result.confirmed is False
    assert 'confirm_timeout' in result.reason


def test_backend_then_foreground_falls_back_after_unconfirmed_backend(monkeypatch):
    monkeypatch.setattr(wechat_sender, 'send_reply_backend', lambda text, log=None: True)
    monkeypatch.setattr(
        wechat_sender,
        'send_reply_foreground',
        lambda text, target=None, cfg=None, log=None: wechat_sender.SendResult(
            ok=True, mode='foreground', attempted=['uia_probe', 'physical_input'], reason='sent_physical_foreground'
        ),
    )
    confirm_results = iter([False, True])
    result = wechat_sender.send_reply_detailed(
        'hello',
        mode='backend_then_foreground',
        target={'name': '群A'},
        before_local_id=1,
        cfg={'send_confirm_timeout': 0.1},
        confirm=lambda target, lid, text, timeout: next(confirm_results),
    )
    assert result.ok is True
    assert result.confirmed is True
    assert result.attempted[0] == 'backend'
    assert 'physical_input' in result.attempted



def test_foreground_skips_search_when_uia_current_chat_verified(monkeypatch):
    monkeypatch.setattr(wechat_sender, 'find_wechat_hwnd', lambda: 123)
    monkeypatch.setattr(wechat_sender.ctypes, 'windll', type('W', (), {'user32': type('U', (), {'SetProcessDPIAware': lambda self: None})()})(), raising=False)

    class FakeWin32Gui:
        @staticmethod
        def ShowWindow(hwnd, flag):
            pass
        @staticmethod
        def SetForegroundWindow(hwnd):
            pass
        @staticmethod
        def GetClientRect(hwnd):
            return (0, 0, 1000, 800)
        @staticmethod
        def ClientToScreen(hwnd, xy):
            return xy

    class FakeWin32Con:
        SW_RESTORE = 9

    class FakeLjq:
        dpi_scale = 1
        calls = []
        @staticmethod
        def Click(x, y):
            FakeLjq.calls.append(('click', x, y))
        @staticmethod
        def Press(key):
            FakeLjq.calls.append(('press', key))

    class FakeClip:
        copied = []
        @staticmethod
        def copy(text):
            FakeClip.copied.append(text)

    import types
    monkeypatch.setitem(sys.modules, 'win32gui', FakeWin32Gui)
    monkeypatch.setitem(sys.modules, 'win32con', FakeWin32Con)
    monkeypatch.setitem(sys.modules, 'ljqCtrl', FakeLjq)
    monkeypatch.setitem(sys.modules, 'pyperclip', FakeClip)
    monkeypatch.setattr(wechat_sender, 'probe_uia_state', lambda target=None, log=None: {'ok': True, 'target_matched': True})
    monkeypatch.setattr(wechat_sender, 'open_chat_from_visible_list', lambda *a, **k: (_ for _ in ()).throw(AssertionError('ocr should be skipped')))
    monkeypatch.setattr(wechat_sender, '_open_chat_by_search', lambda *a, **k: (_ for _ in ()).throw(AssertionError('search should be skipped')))

    result = wechat_sender.send_reply_foreground('hello', target={'name': '群A'}, cfg={'send_strategy': 'current_or_uia_then_ocr_search_physical'})
    assert result.ok is True
    assert 'current_chat_verified' in result.attempted
    assert 'ocr_visible_list' not in result.attempted
    assert 'search_fallback' not in result.attempted
    assert FakeClip.copied == ['hello']
