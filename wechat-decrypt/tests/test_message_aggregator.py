"""Unit tests for message_aggregator image task detection."""

import sys
import unittest
from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('message_aggregator_under_test', ROOT / 'message_aggregator.py')
assert spec is not None and spec.loader is not None
agg = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = agg
spec.loader.exec_module(agg)


class ImageTaskDescriptionTests(unittest.TestCase):
    def _turn(self, text_parts, image_paths=None, trigger_matched=False, in_session=False, event_context=None):
        return agg.AggregatedTurn(
            chat_id='chat',
            sender_id='sender',
            start_local_id=1,
            end_local_id=2,
            start_time=0.0,
            end_time=1.0,
            text_parts=text_parts,
            image_paths=image_paths or ['/tmp/x.jpg'],
            trigger_matched=trigger_matched,
            in_session=in_session,
            event_context=event_context or {},
        )

    def test_trigger_matched_with_any_text_counts_as_task(self):
        turn = self._turn(['lewis4438136:\n@飞扬的跟屁虫 这是你的头像，你觉得怎么样'], trigger_matched=True)
        self.assertTrue(turn.has_image_task_description())

    def test_in_session_with_any_text_counts_as_task(self):
        turn = self._turn(['lewis4438136:\n@飞扬的跟屁虫 这是你的头像，你觉得怎么样'], in_session=True)
        self.assertTrue(turn.has_image_task_description())

    def test_event_context_trigger_matched_counts_as_task(self):
        turn = self._turn(
            ['lewis4438136:\n@飞扬的跟屁虫 这是你的头像，你觉得怎么样'],
            trigger_matched=False,
            event_context={'trigger_matched': True},
        )
        self.assertTrue(turn.has_image_task_description())

    def test_plain_question_without_trigger_still_detected(self):
        turn = self._turn(['lewis4438136:\n这是你的头像，你觉得怎么样'], trigger_matched=False, in_session=False)
        self.assertTrue(turn.has_image_task_description())

    def test_no_text_no_trigger_no_marker_returns_false(self):
        turn = self._turn(['<bytes 683>'], trigger_matched=False, in_session=False)
        self.assertFalse(turn.has_image_task_description())

    def test_marker_analysis_still_works(self):
        turn = self._turn(['lewis4438136:\n分析一下这张图'], trigger_matched=False, in_session=False)
        self.assertTrue(turn.has_image_task_description())


if __name__ == '__main__':
    unittest.main()
