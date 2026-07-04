"""Unit tests for message_aggregator image task detection and flush behavior."""

import sys
import time
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
    def _turn(self, text_parts, image_paths=None, trigger_matched=False, in_session=False, event_context=None, target=None, config=None):
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
            target=target or {},
            config=config or {},
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

    def test_custom_image_task_markers_via_target_policy(self):
        turn = self._turn(
            ['lewis4438136:\n请修复这张图'],
            target={'policy': {'image_task_markers': ['修复']}},
        )
        self.assertTrue(turn.has_image_task_description())

    def test_custom_image_task_markers_via_config(self):
        turn = self._turn(
            ['lewis4438136:\n请调色这张图'],
            config={'image_task_markers': ['调色']},
        )
        self.assertTrue(turn.has_image_task_description())


class AggregationPipelineTests(unittest.TestCase):
    def setUp(self):
        with agg._BUFFER_LOCK:
            agg._buffers.clear()
            agg._window_opened_at.clear()
            agg._window_last_at.clear()
            agg._window_meta.clear()

    def _event(self, local_id, content='', image_path=None, **raw_overrides):
        return agg.MessageEvent(
            chat_id='chat',
            sender_id='sender',
            local_id=local_id,
            timestamp=time.time(),
            msg_type='image' if image_path else 'text',
            content=content,
            image_path=image_path,
            raw={'local_id': local_id, 'message_content': content, **raw_overrides},
        )

    def test_image_plus_description_merge(self):
        config = {'max_aggregated_messages': 2}
        e1 = self._event(1, image_path='/tmp/meter.jpg')
        self.assertIsNone(agg.ingest_event(e1, config=config))
        e2 = self._event(2, content='帮我看看这个')
        turn = agg.ingest_event(e2, config=config)
        self.assertIsNotNone(turn)
        self.assertEqual(turn.image_paths, ['/tmp/meter.jpg'])
        self.assertIn('帮我看看这个', turn.combined_text())
        self.assertTrue(turn.has_image_task_description())

    def test_trigger_inheritance_across_messages(self):
        config = {'max_aggregated_messages': 2}
        agg.ingest_event(self._event(1, image_path='/tmp/meter.jpg'), config=config)
        turn = agg.ingest_event(
            self._event(2, content='@bot 这个怎么处理'),
            trigger_matched=True,
            config=config,
        )
        self.assertIsNotNone(turn)
        self.assertTrue(turn.trigger_matched)
        self.assertEqual(turn.image_paths, ['/tmp/meter.jpg'])
        self.assertIn('这个怎么处理', turn.combined_text())
    def test_non_trigger_messages_are_buffered_and_flushed(self):
        config = {'max_aggregated_messages': 2}
        e1 = self._event(1, content='帮我查一下这个订单')
        e2 = self._event(2, content='小助手')
        self.assertIsNone(agg.ingest_event(e1, config=config))
        turn = agg.ingest_event(e2, config=config)
        self.assertIsNotNone(turn)
        self.assertEqual(len(turn.text_parts), 2)
        combined = turn.combined_text()
        self.assertIn('帮我查一下这个订单', combined)
        self.assertIn('小助手', combined)

    def test_trigger_inheritance_on_time_flush(self):
        agg.ingest_event(self._event(1, content='对了'))
        time.sleep(agg._DEBOUNCE_SECONDS + 0.2)
        turn = agg.ingest_event(
            self._event(2, content='@bot 套餐怎么设置'),
            trigger_matched=True,
        )
        # The stale window is flushed WITHOUT inheriting the new event's trigger.
        self.assertIsNotNone(turn)
        self.assertFalse(turn.trigger_matched)
        self.assertIn('对了', turn.combined_text())
        self.assertNotIn('套餐怎么设置', turn.combined_text())
        # The triggering event starts a fresh window with its own trigger flag.
        self.assertTrue(agg.has_open_window('chat', 'sender'))
        meta = agg._window_meta.get(agg._window_key('chat', 'sender')) or {}
        self.assertTrue(meta.get('trigger_matched'))

    def test_termination_word_flush(self):
        agg.ingest_event(self._event(1, content='帮我查一下'))
        turn = agg.ingest_event(self._event(2, content='好了'))
        self.assertIsNotNone(turn)
        self.assertIn('帮我查一下', turn.combined_text())
        self.assertNotIn('好了', turn.combined_text())
        # The terminating event starts a fresh window.
        self.assertTrue(agg.has_open_window('chat', 'sender'))

    def test_termination_words_configurable(self):
        agg.ingest_event(self._event(1, content='first'))
        turn = agg.ingest_event(
            self._event(2, content='stop'),
            config={'termination_words': ['stop']},
        )
        self.assertIsNotNone(turn)
        self.assertIn('first', turn.combined_text())
        self.assertNotIn('stop', turn.combined_text())

    def test_max_aggregated_messages_flush(self):
        config = {'max_aggregated_messages': 3}
        agg.ingest_event(self._event(1, content='msg1'), config=config)
        agg.ingest_event(self._event(2, content='msg2'), config=config)
        turn = agg.ingest_event(self._event(3, content='msg3'), config=config)
        self.assertIsNotNone(turn)
        self.assertEqual(len(turn.text_parts), 3)
        self.assertIn('msg1', turn.combined_text())
        self.assertIn('msg2', turn.combined_text())
        self.assertIn('msg3', turn.combined_text())

    def test_max_aggregated_messages_via_target_policy(self):
        target = {'policy': {'max_aggregated_messages': 2}}
        agg.ingest_event(self._event(1, content='a'), target=target)
        turn = agg.ingest_event(self._event(2, content='b'), target=target)
        self.assertIsNotNone(turn)
        self.assertEqual(len(turn.text_parts), 2)

    def test_to_generate_reply_message_includes_aggregation_metadata(self):
        config = {'max_aggregated_messages': 2}
        agg.ingest_event(self._event(1, content='hello'), config=config)
        turn = agg.ingest_event(self._event(2, content='world', image_path='/tmp/x.jpg'), config=config)
        msg = turn.to_generate_reply_message()
        self.assertTrue(msg['is_aggregated'])
        self.assertEqual(msg['aggregated_local_ids'], [1, 2])
        self.assertEqual(msg['text_parts_count'], 2)
        self.assertEqual(msg['session_image_paths'], ['/tmp/x.jpg'])

    def test_to_generate_reply_message_not_aggregated_for_single_event(self):
        config = {'max_aggregated_messages': 1}
        turn = agg.ingest_event(self._event(1, content='hello'), config=config)
        self.assertIsNotNone(turn)
        msg = turn.to_generate_reply_message()
        self.assertFalse(msg['is_aggregated'])
        self.assertEqual(msg['aggregated_local_ids'], [1])
        self.assertEqual(msg['text_parts_count'], 1)


    def test_to_generate_reply_message_copies_mention_name_from_event_context(self):
        event_context = {"sender_display_name": "飞扬的跟屁虫"}
        config = {"max_aggregated_messages": 1}
        turn = agg.ingest_event(
            self._event(1, content="hello"),
            config=config,
            event_context=event_context,
        )
        self.assertIsNotNone(turn)
        msg = turn.to_generate_reply_message()
        self.assertEqual(msg["mention_name"], "飞扬的跟屁虫")
        self.assertEqual(msg["sender_display_name"], "飞扬的跟屁虫")

    def test_to_generate_reply_message_preserves_aggregator_context(self):
        """Regression: context_messages must come from the aggregated window,
        not be replaced later by monitor's build_event_context flat rows.
        Each context entry must carry enough sender metadata for prompt building.
        """
        config = {"max_aggregated_messages": 2}
        e1 = self._event(1, content="前面这条", real_sender_id=7, status=1)
        e2 = self._event(2, content="@bot 后面这条", real_sender_id=7, status=1)
        self.assertIsNone(agg.ingest_event(e1, config=config))
        turn = agg.ingest_event(e2, trigger_matched=True, config=config)
        self.assertIsNotNone(turn)
        msg = turn.to_generate_reply_message()
        self.assertEqual(len(msg["context_messages"]), 2)
        self.assertEqual(msg["context_messages"][0]["local_id"], 1)
        self.assertEqual(msg["context_messages"][1]["local_id"], 2)
        self.assertEqual(msg["context_messages"][0].get("real_sender_id"), 7)
        self.assertEqual(msg["context_messages"][1].get("real_sender_id"), 7)
        self.assertEqual(msg["event_context"], turn.event_context)

if __name__ == '__main__':
    unittest.main()
