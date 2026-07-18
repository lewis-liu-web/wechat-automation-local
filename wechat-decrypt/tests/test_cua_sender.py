"""Unit tests for CUA sender helpers in wechat_sender.

Covers:
- _cua_find_wechat_window: off-screen filter, placeholder-bar filter,
  app_name filter, and bring_to_front retry when no usable candidate.
- _cua_switch_to_chat: UIA-tree direct-click on a visible ListItem by
  id, and search-box fallback (no Enter press).

All tests stub out _cua_run so no real cua-driver process is spawned.
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
SENDER_PATH = ROOT / 'wechat_sender.py'

spec = importlib.util.spec_from_file_location('wechat_sender_under_test', SENDER_PATH)
assert spec is not None and spec.loader is not None
sender = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sender
spec.loader.exec_module(sender)


def _list_windows_payload(items):
    return json.dumps({'windows': items})


def _weixin_window(pid, window_id, width, height, x=0, y=0, on_screen=True, title='微信'):
    return {
        'app_name': 'Weixin.exe',
        'bounds': {'width': width, 'height': height, 'x': x, 'y': y},
        'is_on_screen': on_screen,
        'pid': pid,
        'title': title,
        'window_id': window_id,
    }


class _FakeResult:
    def __init__(self, returncode, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _state_with_tree(tree):
    return json.dumps({
        'element_count': 100,
        'screenshot_width': 1432,
        'screenshot_height': 1146,
        'pid': 9616,
        'window_id': 724684,
        'tree_markdown': tree,
    })


# Tree fixtures used by SwitchChatTests.  TREE_MAIN contains a session_item
# whose displayed text AND id both contain "bot群聊测试".  TREE_NO_MATCH
# replaces BOTH occurrences so neither the id-based nor the text-based
# _find_session_listitem path can match it.  TREE_DROPDOWN extends
# TREE_NO_MATCH with a search-result row whose id is search_result_0 and
# whose displayed text contains "bot群聊测试" again -- this is the
# post-search-dropdown state that the fallback path is supposed to click.
TREE_MAIN_LINES = [
    '- [0] Window "Weixin"',
    '  - [1] Group',
    '    - [2] Custom',
    '      - [3] Group',
    '        - [4] ToolBar',
    '          - [5] Button "微信"',
    '        - [41] Group',
    '          - [42] Group',
    '            - [43] Custom',
    '              - [44] Custom',
    '                - [45] Custom',
    '                  - [52] Group',
    '                    - [53] Group',
    '                      - [54] Group',
    '                        - [55] Group',
    '                          - [56] Button "快捷操作"',
    '                          - [62] Edit "搜索" [actions=[invoke,set_value,text]]',
    '                          - [68] List "会话" [id=session_list]',
    '                            - [69] ListItem "bot群聊测试聊天" [id=session_item_bot群聊测试 actions=[invoke,select,set_value]]',
    '                            - [70] ListItem "家人们" [id=session_item_家人们 actions=[invoke,select,set_value]]',
    '                            - [71] ListItem "公众号" [id=session_item_公众号 actions=[invoke,select,set_value]]',
]
TREE_MAIN = '\n'.join(TREE_MAIN_LINES)
TREE_NO_MATCH = TREE_MAIN.replace('bot群聊测试聊天', '其他群聊天').replace('session_item_bot群聊测试', 'session_item_other_chat')
TREE_DROPDOWN = TREE_NO_MATCH + '\n                            - [99] ListItem "search_result_0 bot群聊测试 群主" [id=search_result_0 actions=[invoke,select]]'


class FindWeChatWindowTests(unittest.TestCase):
    """Verify the off-screen and placeholder-bar filter logic."""

    def test_picks_main_window_drops_offscreen_placeholder(self):
        payload = _list_windows_payload([
            _weixin_window(9616, 724684, 967, 770, x=896, y=333, title='微信'),
            _weixin_window(9616, 461212, 181, 25, x=-31991, y=-32000, on_screen=True, title='微信'),
        ])
        with patch.object(sender, '_cua_run', return_value=_FakeResult(0, payload)):
            result = sender._cua_find_wechat_window()
        self.assertEqual(result, (9616, 724684))

    def test_rejects_window_smaller_than_placeholder_threshold(self):
        payload = _list_windows_payload([
            _weixin_window(9616, 461212, 181, 25, x=100, y=100, title='微信'),
        ])
        with patch.object(sender, '_cua_run', return_value=_FakeResult(0, payload)):
            result = sender._cua_find_wechat_window()
        self.assertIsNone(result)

    def test_rejects_negative_position_even_if_large(self):
        payload = _list_windows_payload([
            _weixin_window(9616, 555, 1200, 800, x=-2000, y=100, title='微信'),
        ])
        with patch.object(sender, '_cua_run', return_value=_FakeResult(0, payload)):
            result = sender._cua_find_wechat_window()
        self.assertIsNone(result)

    def test_returns_none_when_no_wechat_window(self):
        payload = _list_windows_payload([
            {'app_name': 'chrome.exe', 'bounds': {'width': 800, 'height': 600, 'x': 100, 'y': 100},
             'is_on_screen': True, 'pid': 1, 'title': 'Chrome', 'window_id': 1},
        ])
        with patch.object(sender, '_cua_run', return_value=_FakeResult(0, payload)):
            result = sender._cua_find_wechat_window()
        self.assertIsNone(result)

    def test_drops_empty_title_qt_child_surfaces(self):
        payload = _list_windows_payload([
            {'app_name': 'Weixin.exe', 'bounds': {'width': 1600, 'height': 947, 'x': 50, 'y': 50},
             'is_on_screen': True, 'pid': 9616, 'title': '', 'window_id': 100},
            _weixin_window(9616, 724684, 967, 770, x=896, y=333, title='微信'),
        ])
        with patch.object(sender, '_cua_run', return_value=_FakeResult(0, payload)):
            result = sender._cua_find_wechat_window()
        self.assertEqual(result, (9616, 724684))

    def test_restore_main_window_called_when_no_usable_candidate(self):
        minimised = _list_windows_payload([
            _weixin_window(9616, 461212, 181, 25, x=-31991, y=-32000, title='微信'),
        ])
        main = _list_windows_payload([
            _weixin_window(9616, 724684, 967, 770, x=896, y=333, title='微信'),
        ])
        calls = []
        list_w_count = {'n': 0}
        def fake_run(cmd, timeout=10, log=None):
            calls.append(cmd[1])
            if cmd[1] == 'list_windows':
                list_w_count['n'] += 1
                return _FakeResult(0, minimised if list_w_count['n'] == 1 else main)
            if cmd[1] == 'bring_to_front':
                return _FakeResult(0, '{}')
            return _FakeResult(0, '{}')
        with patch.object(sender, '_cua_run', side_effect=fake_run), \
             patch.object(sender.time, 'sleep', lambda *a, **k: None), \
             patch('win32gui.ShowWindow', return_value=True) as mock_show:
            result = sender._cua_find_wechat_window()
        self.assertEqual(result, (9616, 724684))
        mock_show.assert_called_once()

    def test_bring_to_front_fallback_when_restore_fails(self):
        minimised = _list_windows_payload([
            _weixin_window(9616, 461212, 181, 25, x=-31991, y=-32000, title='微信'),
        ])
        main = _list_windows_payload([
            _weixin_window(9616, 724684, 967, 770, x=896, y=333, title='微信'),
        ])
        calls = []
        list_w_count = {'n': 0}
        def fake_run(cmd, timeout=10, log=None):
            calls.append(cmd[1])
            if cmd[1] == 'list_windows':
                list_w_count['n'] += 1
                # Restore failed: still minimised on retry, so bring_to_front path runs.
                return _FakeResult(0, minimised if list_w_count['n'] <= 2 else main)
            if cmd[1] == 'bring_to_front':
                return _FakeResult(0, '{}')
            return _FakeResult(0, '{}')
        with patch.object(sender, '_cua_run', side_effect=fake_run), \
             patch.object(sender.time, 'sleep', lambda *a, **k: None), \
             patch('win32gui.ShowWindow', side_effect=Exception('bad hwnd')):
            result = sender._cua_find_wechat_window()
        self.assertEqual(result, (9616, 724684))
        self.assertIn('bring_to_front', calls)

    def test_ignores_wechat_appex_mini_program_windows(self):
        payload = _list_windows_payload([
            {'app_name': 'WeChatAppEx.exe', 'bounds': {'width': 1400, 'height': 1000, 'x': 100, 'y': 100},
             'is_on_screen': True, 'pid': 30448, 'title': '微信', 'window_id': 920854},
            _weixin_window(9616, 724684, 967, 770, x=896, y=333, title='微信'),
        ])
        with patch.object(sender, '_cua_run', return_value=_FakeResult(0, payload)):
            result = sender._cua_find_wechat_window()
        self.assertEqual(result, (9616, 724684))


class SwitchChatTests(unittest.TestCase):
    """Verify the UIA-tree based chat switching helper."""

    def test_direct_click_visible_listitem(self):
        calls = []
        def fake_run(cmd, timeout=10, log=None):
            calls.append(cmd)
            if cmd[1] == 'get_window_state':
                return _FakeResult(0, _state_with_tree(TREE_MAIN))
            return _FakeResult(0, '{}')
        with patch.object(sender, '_cua_run', side_effect=fake_run), \
             patch.object(sender, '_uia_current_chat_matches', return_value=True):
            ok = sender._cua_switch_to_chat(9616, 724684, 'bot群聊测试')
        self.assertTrue(ok)
        click_calls = [c for c in calls if c[1] == 'click']
        self.assertEqual(len(click_calls), 1)
        click_args = json.loads(click_calls[0][2])
        self.assertEqual(click_args.get('element_index'), 69)
        press_calls = [c for c in calls if c[1] == 'press_key']
        type_calls = [c for c in calls if c[1] == 'type_text']
        self.assertEqual(press_calls, [])
        self.assertEqual(type_calls, [])

    def test_no_enter_press_in_search_fallback(self):
        calls = []
        # 1: tree read in switch_to_chat (no_match list, direct-click miss)
        # 2: cache refresh in _cua_click_element for search Edit
        # 3: cache refresh in _cua_set_value
        # 4: tree2 read for the dropdown
        # 5: cache refresh in _cua_click_element for dropdown ListItem (unused)
        state_iter = iter([_state_with_tree(TREE_NO_MATCH)] * 3 +
                           [_state_with_tree(TREE_DROPDOWN)] +
                           [_state_with_tree(TREE_NO_MATCH)])
        def fake_run(cmd, timeout=10, log=None):
            calls.append((cmd[1], cmd[2] if len(cmd) > 2 else None))
            if cmd[1] == 'get_window_state':
                return _FakeResult(0, next(state_iter))
            if cmd[1] == 'set_value':
                return _FakeResult(0, '{}')
            return _FakeResult(0, '{}')
        with patch.object(sender, '_cua_run', side_effect=fake_run), \
             patch.object(sender.time, 'sleep', lambda *a, **k: None), \
             patch.object(sender, '_uia_current_chat_matches', return_value=True):
            ok = sender._cua_switch_to_chat(9616, 724684, 'bot群聊测试')
        self.assertTrue(ok)
        press_calls = [c for c in calls if c[0] == 'press_key']
        self.assertEqual(press_calls, [], 'search fallback must not press Enter')
        # set_value should be the preferred text-write path now (Qt
        # accessibility honours ValuePattern.SetValue).
        set_value_calls = [c for c in calls if c[0] == 'set_value']
        self.assertGreaterEqual(len(set_value_calls), 1)
        set_args = json.loads(set_value_calls[0][1])
        self.assertEqual(set_args.get('value'), 'bot群聊测试')
        click_calls = [c for c in calls if c[0] == 'click']
        self.assertGreaterEqual(len(click_calls), 1)

    def test_returns_false_when_target_missing_and_no_dropdown(self):
        def fake_run(cmd, timeout=10, log=None):
            if cmd[1] == 'get_window_state':
                return _FakeResult(0, _state_with_tree(TREE_NO_MATCH))
            return _FakeResult(0, '{}')
        with patch.object(sender, '_cua_run', side_effect=fake_run):
            ok = sender._cua_switch_to_chat(9616, 724684, '不存在的群')
        self.assertFalse(ok)

    def test_aliases_tried_in_order(self):
        # target.name doesn't appear in tree, but target.username does.
        target = {'name': 'fancy display name', 'username': 'bot群聊测试'}
        def fake_run(cmd, timeout=10, log=None):
            if cmd[1] == 'get_window_state':
                return _FakeResult(0, _state_with_tree(TREE_MAIN))
            return _FakeResult(0, '{}')
        with patch.object(sender, '_cua_run', side_effect=fake_run), \
             patch.object(sender, '_uia_current_chat_matches', return_value=True):
            ok = sender._cua_switch_to_chat(9616, 724684, target)
        self.assertTrue(ok)




class SeparateWindowTests(unittest.TestCase):
    """Verify separate-chat-window CUA mode avoids in-window chat switching."""

    def test_window_title_override_takes_priority_over_name(self):
        payload = _list_windows_payload([
            _weixin_window(9616, 111, 1400, 1000, title='微信'),
            _weixin_window(9616, 222, 900, 700, title='bot群聊测试'),
            _weixin_window(9616, 333, 900, 700, title='测试群'),
        ])
        target = {'name': 'bot群聊测试', 'cua_window_title': '测试群'}
        with patch.object(sender, '_cua_run', return_value=_FakeResult(0, payload)):
            result = sender._cua_find_separate_chat_window(target)
        self.assertEqual(result, (9616, 333))

    def test_separate_window_title_fields_all_recognized(self):
        for field in ('cua_window_title', 'separate_window_title', 'window_title'):
            payload = _list_windows_payload([
                _weixin_window(9616, 111, 1400, 1000, title='微信'),
                _weixin_window(9616, 222, 900, 700, title='override-title'),
            ])
            target = {'name': 'bot群聊测试', field: 'override-title'}
            with patch.object(sender, '_cua_run', return_value=_FakeResult(0, payload)):
                result = sender._cua_find_separate_chat_window(target)
            self.assertEqual(result, (9616, 222), field)

    def test_falls_back_to_pop_out_chat_when_no_window_found(self):
        state = {
            'element_count': 20,
            'screenshot_width': 900,
            'screenshot_height': 700,
            'bounds': {'width': 900, 'height': 700, 'x': 0, 'y': 0},
            'tree_markdown': '- [44] Edit "输入" [id=chat_input_field actions=[invoke,set_value,text]]',
        }
        with patch.object(sender, '_cua_find_separate_chat_window', return_value=None) \
             as find_mock, \
             patch.object(sender, '_cua_pop_out_chat', return_value=(9616, 222)) as pop_mock, \
             patch.object(sender, '_cua_get_window_state', return_value=state), \
             patch.object(sender, '_cua_save_foreground_window', side_effect=AssertionError('separate_window must not save foreground itself')), \
             patch.object(sender, '_cua_restore_foreground_window', side_effect=AssertionError('separate_window must not restore foreground itself')), \
             patch.object(sender, '_cua_set_input_value_uiautomation', return_value=True), \
             patch.object(sender, '_cua_press_key', return_value=True), \
             patch.object(sender.time, 'sleep', lambda *a, **k: None):
            result = sender.send_reply_cua_separate_window('hello', target={'name': 'bot群聊测试'})
        self.assertTrue(result.ok)
        self.assertEqual(result.attempted, [
            'cua_find_separate_window',
            'cua_pop_out_chat',
            'cua_get_window_state',
            'cua_set_input_value_uiautomation',
            'cua_press_enter',
        ])
        pop_mock.assert_called_once()

    def test_finds_target_window_by_title(self):
        payload = _list_windows_payload([
            _weixin_window(9616, 111, 1400, 1000, title='微信'),
            _weixin_window(9616, 222, 900, 700, title='bot群聊测试'),
            _weixin_window(9616, 333, 900, 700, title='文件传输助手'),
        ])
        with patch.object(sender, '_cua_run', return_value=_FakeResult(0, payload)):
            result = sender._cua_find_separate_chat_window({'name': 'bot群聊测试'})
        self.assertEqual(result, (9616, 222))

    def test_finds_target_window_even_when_cli_reports_qt_placeholder_bounds(self):
        payload = _list_windows_payload([
            _weixin_window(15324, 19664330, 183, 26, x=-31992, y=-32000, title='微信'),
            _weixin_window(15324, 3869176, 183, 26, x=-31992, y=-32000, title='bot群聊测试'),
        ])
        with patch.object(sender, '_cua_run', return_value=_FakeResult(0, payload)):
            result = sender._cua_find_separate_chat_window({'name': 'bot群聊测试'})
        self.assertEqual(result, (15324, 3869176))


    def test_separate_window_uses_uiautomation_for_input(self):
        calls = []
        state = {
            'element_count': 20,
            'screenshot_width': 900,
            'screenshot_height': 700,
            'bounds': {'width': 900, 'height': 700, 'x': 0, 'y': 0},
            'tree_markdown': '- [44] Edit \"输入\" [id=chat_input_field actions=[invoke,set_value,text]]',
        }
        def record(name, value=True):
            def inner(*args, **kwargs):
                calls.append((name, args, kwargs))
                return value
            return inner
        with patch.object(sender, '_cua_find_separate_chat_window', return_value=(9616, 222)), \
             patch.object(sender, '_cua_get_window_state', return_value=state), \
             patch.object(sender, '_cua_save_foreground_window', return_value=11111), \
             patch.object(sender, '_cua_restore_foreground_window', return_value=True), \
             patch.object(sender, '_cua_set_input_value_uiautomation', side_effect=record('uia_set_value')), \
             patch.object(sender, '_cua_set_value', side_effect=AssertionError('must not use cua set_value')), \
             patch.object(sender, '_cua_press_key', side_effect=record('press_key')), \
             patch.object(sender.time, 'sleep', lambda *a, **k: None):
            result = sender.send_reply_cua_separate_window('hello', target={'name': 'bot群聊测试'})
        self.assertTrue(result.ok)
        self.assertEqual(result.mode, 'cua_separate_window')
        self.assertEqual(result.attempted, [
            'cua_find_separate_window',
            'cua_get_window_state',
            'cua_set_input_value_uiautomation',
            'cua_press_enter',
        ])
        self.assertEqual([c[0] for c in calls], ['uia_set_value', 'press_key'])

    def test_separate_window_minimised_sends_in_background(self):
        state = {
            'element_count': 20,
            'screenshot_width': 900,
            'screenshot_height': 700,
            'bounds': {'width': 183, 'height': 26, 'x': -31992, 'y': -32000},
            'tree_markdown': '- [44] Edit "输入" [id=chat_input_field actions=[invoke,set_value,text]]',
        }
        with patch.object(sender, '_cua_find_separate_chat_window', return_value=(9616, 222)), \
             patch.object(sender, '_cua_get_window_state', return_value=state), \
             patch.object(sender, '_cua_save_foreground_window', side_effect=AssertionError('separate_window must not save foreground itself')), \
             patch.object(sender, '_cua_restore_foreground_window', side_effect=AssertionError('separate_window must not restore foreground itself')), \
             patch.object(sender, '_cua_set_input_value_uiautomation', return_value=True), \
             patch.object(sender, '_cua_press_key', return_value=True), \
             patch('wechat_sender.ctypes.windll', MagicMock()) as mock_windll, \
             patch.object(sender.time, 'sleep', lambda *a, **k: None):
            mock_windll.user32.IsIconic.return_value = 1
            mock_windll.user32.ShowWindow.side_effect = AssertionError('must not restore minimised window in background path')
            result = sender.send_reply_cua_separate_window('hello', target={'name': 'bot群聊测试'})
        self.assertTrue(result.ok)
        self.assertEqual(result.mode, 'cua_separate_window')
        self.assertFalse(result.detail.get('fg_restored', True))
        self.assertEqual(result.attempted, [
            'cua_find_separate_window',
            'cua_get_window_state',
            'cua_set_input_value_uiautomation',
            'cua_press_enter',
        ])

    def test_separate_window_falls_back_to_cua_set_value(self):
        calls = []
        state = {
            'element_count': 20,
            'screenshot_width': 900,
            'screenshot_height': 700,
            'bounds': {'width': 900, 'height': 700, 'x': 0, 'y': 0},
            'tree_markdown': '- [44] Edit \"输入\" [id=chat_input_field actions=[invoke,set_value,text]]',
        }
        def record(name, value=True):
            def inner(*args, **kwargs):
                calls.append(name)
                return value
            return inner
        with patch.object(sender, '_cua_find_separate_chat_window', return_value=(9616, 222)), \
             patch.object(sender, '_cua_get_window_state', return_value=state), \
             patch.object(sender, '_cua_save_foreground_window', return_value=11111), \
             patch.object(sender, '_cua_restore_foreground_window', return_value=True), \
             patch.object(sender, '_cua_set_input_value_uiautomation', return_value=False), \
             patch.object(sender, '_cua_set_value', side_effect=record('set_value')), \
             patch.object(sender, '_cua_press_key', side_effect=record('press_key')), \
             patch.object(sender.time, 'sleep', lambda *a, **k: None):
            result = sender.send_reply_cua_separate_window('hello', target={'name': 'bot群聊测试'})
        self.assertTrue(result.ok)
        self.assertEqual(result.attempted, [
            'cua_find_separate_window',
            'cua_get_window_state',
            'cua_set_input_value',
            'cua_press_enter',
        ])
        self.assertEqual(calls, ['set_value', 'press_key'])

    def test_separate_window_fails_when_input_not_found(self):
        state = {
            'element_count': 20,
            'screenshot_width': 900,
            'screenshot_height': 700,
            'bounds': {'width': 900, 'height': 700, 'x': 0, 'y': 0},
            'tree_markdown': '- [44] Button "发送"',
        }
        with patch.object(sender, '_cua_find_separate_chat_window', return_value=(9616, 222)), \
             patch.object(sender, '_cua_get_window_state', return_value=state), \
             patch.object(sender, '_cua_save_foreground_window', return_value=11111), \
             patch.object(sender, '_cua_restore_foreground_window', return_value=True), \
             patch.object(sender, '_cua_set_input_value_uiautomation', return_value=False), \
             patch.object(sender, '_cua_set_value', return_value=False), \
             patch.object(sender.time, 'sleep', lambda *a, **k: None):
            result = sender.send_reply_cua_separate_window('hello', target={'name': 'bot群聊测试'})
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'cua_input_not_found')

class CuaRunTests(unittest.TestCase):
    """Verify _cua_run forwards subprocess safety flags."""

    def test_cua_run_passes_stdin_devnull_and_no_window(self):
        import subprocess
        fake_result = _FakeResult(0, stdout='ok')
        with patch.object(subprocess, 'run', return_value=fake_result) as mock_run:
            result = sender._cua_run(['cua-driver', 'list_windows', '--json'], timeout=25)
        self.assertIs(result, fake_result)
        mock_run.assert_called_once()
        call_args, call_kwargs = mock_run.call_args
        self.assertEqual(call_args[0], ['cua-driver', 'list_windows', '--json'])
        self.assertEqual(call_kwargs.get('timeout'), 25)
        self.assertIs(call_kwargs.get('stdin'), subprocess.DEVNULL)
        self.assertTrue(call_kwargs.get('capture_output'))
        self.assertTrue(call_kwargs.get('text'))
        if sys.platform == 'win32':
            self.assertEqual(call_kwargs.get('creationflags'), subprocess.CREATE_NO_WINDOW)
        else:
            self.assertNotIn('creationflags', call_kwargs)


if __name__ == '__main__':
    unittest.main()
