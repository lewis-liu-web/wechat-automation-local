# -*- coding: utf-8 -*-
"""Cursor coverage audit contract for wechat_bot_monitor.

The audit tracks every cursor advance via _advance_cursor with a reason, and
after a successful save_config verifies that every local_id between the
previous saved cursor and the new cursor is covered by a recorded reason.
Historical cursor positions are accepted via _init_cursor_baseline.
"""

import ast
from pathlib import Path

import pytest


@pytest.fixture
def fresh_audit(monkeypatch):
    """Provide audit helpers with an empty in-process audit map."""
    import wechat_bot_monitor as m
    m._CURSOR_AUDIT.clear()
    calls = []
    monkeypatch.setattr(m, 'log', calls.append)
    return m._advance_cursor, m._audit_cursor_coverage, m._init_cursor_baseline, m._target_key, calls


class TestCursorTraceRecording:
    def test_per_message_reason_requires_single_step(self, fresh_audit):
        advance, audit, init, _, calls = fresh_audit
        t = {'name': 't', 'db': 'd', 'table': 'T', 'username': 'u', 'last_local_id': 10}
        init(t)
        advance(t, 15, 'durable_ingress')
        assert any('cursor_coverage_unexpected_jump' in c and 'reason=durable_ingress' in c for c in calls)
        audit([t], consume=True)
        gap = [c for c in calls if 'cursor_coverage_gap' in c]
        assert len(gap) == 1
        assert 'missing_count=5' in gap[0]
        assert '11,12,13,14,15' in gap[0]

    def test_per_message_reason_single_step_is_silent(self, fresh_audit):
        advance, audit, init, _, calls = fresh_audit
        t = {'name': 't', 'db': 'd', 'table': 'T', 'username': 'u', 'last_local_id': 10}
        init(t)
        advance(t, 11, 'durable_ingress')
        audit([t], consume=True)
        assert not any('cursor_coverage_unexpected_jump' in c for c in calls)
        assert not any('cursor_coverage_gap' in c for c in calls)

    def test_aggregation_reason_allows_range_jump(self, fresh_audit):
        advance, _, init, _, calls = fresh_audit
        t = {'name': 't', 'db': 'd', 'table': 'T', 'username': 'u', 'last_local_id': 10}
        init(t)
        advance(t, 20, 'deferred_flush')
        assert not any('cursor_coverage_unexpected_jump' in c for c in calls)
        import wechat_bot_monitor as m
        trace = m._CURSOR_AUDIT[m._target_key(t)]['trace']
        assert trace == [[11, 20, 'deferred_flush']]

    def test_advance_merges_adjacent_same_reason_intervals(self, fresh_audit):
        advance, _, init, key, _ = fresh_audit
        t = {'name': 't', 'db': 'd', 'table': 'T', 'username': 'u', 'last_local_id': 10}
        import wechat_bot_monitor as m
        init(t)
        advance(t, 11, 'durable_ingress')
        advance(t, 12, 'durable_ingress')
        trace = m._CURSOR_AUDIT[key(t)]['trace']
        assert trace == [[11, 12, 'durable_ingress']]

    def test_advance_splits_on_different_reason(self, fresh_audit):
        advance, _, init, key, _ = fresh_audit
        t = {'name': 't', 'db': 'd', 'table': 'T', 'username': 'u', 'last_local_id': 10}
        import wechat_bot_monitor as m
        init(t)
        advance(t, 11, 'durable_ingress')
        advance(t, 12, 'thin_monitor')
        trace = m._CURSOR_AUDIT[key(t)]['trace']
        assert trace == [[11, 11, 'durable_ingress'], [12, 12, 'thin_monitor']]


class TestCursorCoverageAudit:
    def test_no_gap_when_trace_covers_full_range(self, fresh_audit):
        advance, audit, init, _, calls = fresh_audit
        t = {'name': 't', 'db': 'd', 'table': 'T', 'username': 'u', 'last_local_id': 10}
        init(t)
        advance(t, 15, 'deferred_flush')
        audit([t], consume=True)
        assert not any('cursor_coverage_gap' in c for c in calls)

    def test_gap_logged_when_local_id_missing(self, fresh_audit):
        advance, audit, init, _, calls = fresh_audit
        t = {'name': 't', 'db': 'd', 'table': 'T', 'username': 'u', 'last_local_id': 10}
        init(t)
        advance(t, 15, 'deferred_flush')
        import wechat_bot_monitor as m
        m._CURSOR_AUDIT[m._target_key(t)]['trace'] = [[11, 13, 'deferred_flush']]
        audit([t], consume=True)
        gap = [c for c in calls if 'cursor_coverage_gap' in c]
        assert len(gap) == 1
        assert 'missing_count=2' in gap[0]
        assert '14,15' in gap[0]

    def test_gap_sample_is_globally_capped_at_50(self, fresh_audit):
        advance, audit, init, key, calls = fresh_audit
        t = {'name': 't', 'db': 'd', 'table': 'T', 'username': 'u', 'last_local_id': 10}
        init(t)
        advance(t, 200, 'deferred_flush')
        import wechat_bot_monitor as m
        m._CURSOR_AUDIT[key(t)]['trace'] = [[11, 20, 'deferred_flush'], [150, 170, 'deferred_flush']]
        audit([t], consume=True)
        gap = [c for c in calls if 'cursor_coverage_gap' in c][0]
        assert 'missing_count=159' in gap
        sample_str = gap.split('sample=')[1]
        sample_ids = [int(x) for x in sample_str.split(',')]
        assert len(sample_ids) == 50
        assert all(11 <= x <= 200 for x in sample_ids)

    def test_consume_advances_baseline_and_clears_trace(self, fresh_audit):
        advance, audit, init, key, _ = fresh_audit
        t = {'name': 't', 'db': 'd', 'table': 'T', 'username': 'u', 'last_local_id': 10}
        import wechat_bot_monitor as m
        init(t)
        advance(t, 15, 'durable_ingress')
        audit([t], consume=True)
        state = m._CURSOR_AUDIT[key(t)]
        assert state['prev'] == 15
        assert state['trace'] == []

    def test_historical_cursor_baseline_avoids_false_gap(self, fresh_audit):
        _, audit, init, _, calls = fresh_audit
        t = {'name': 't', 'db': 'd', 'table': 'T', 'username': 'u', 'last_local_id': 100}
        init(t)
        audit([t], consume=True)
        assert not any('cursor_coverage_gap' in c for c in calls)

    def test_new_target_gets_baseline_without_gap(self, fresh_audit):
        _, audit, init, key, calls = fresh_audit
        import wechat_bot_monitor as m
        t = {'name': 't', 'db': 'd', 'table': 'T', 'username': 'u', 'last_local_id': 200}
        assert key(t) not in m._CURSOR_AUDIT
        init(t)
        audit([t], consume=True)
        assert not any('cursor_coverage_gap' in c for c in calls)
        assert m._CURSOR_AUDIT[key(t)]['prev'] == 200

    def test_no_save_audit_logs_but_preserves_baseline(self, fresh_audit):
        advance, audit, init, key, _ = fresh_audit
        t = {'name': 't', 'db': 'd', 'table': 'T', 'username': 'u', 'last_local_id': 10}
        import wechat_bot_monitor as m
        init(t)
        advance(t, 15, 'deferred_flush')
        audit([t], consume=False)
        state = m._CURSOR_AUDIT[key(t)]
        assert state['prev'] == 10
        assert len(state['trace']) == 1


class TestCursorMutationContract:
    def test_last_local_id_only_mutated_in_advance_cursor(self):
        source = (Path(__file__).parent.parent / 'wechat_bot_monitor.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        def enclosing_func(node):
            while node is not None:
                node = parents.get(node)
                if isinstance(node, ast.FunctionDef):
                    return node.name
            return None

        violators = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and target.slice.value == 'last_local_id':
                        func = enclosing_func(node)
                        if func != '_advance_cursor':
                            violators.append((node.lineno, func))
        assert violators == []
