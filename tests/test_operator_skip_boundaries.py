from contextlib import closing
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import jst_auto_print_app as app


def _plan(o_id='6792528', io_id='13543683', token='t' * 32):
    return {'o_id': o_id, 'io_id': io_id, 'claim_token': token,
            'steps': ['GET_WAYBILL', 'PRINT_EXPRESS', 'STOP_BEFORE_PRESHIP'],
            'outbound_identity_unique': True}


def _job(pause_kind='COMPLETION_PROOF_REQUIRED', status='PAUSED'):
    return {'o_id': '6792528', 'io_id': '13543683', 'status': status,
            'step_index': 0, 'pause_kind': pause_kind,
            'pause_reason': '发货操作记录与状态矛盾', 'plan': _plan()}


class _Store:
    def __init__(self, job=None, update_ok=True):
        self.job = job or _job()
        self.update_ok = update_ok
        self.updates = []
        self.exclusions = []

    def latest_problem_job(self):
        return dict(self.job) if self.job else None

    def get_job(self, o_id, io_id):
        return dict(self.job)

    def update_job(self, o_id, io_id, **fields):
        self.updates.append((o_id, io_id, fields))
        return self.update_ok

    def exclude_job(self, o_id, io_id, reason):
        self.exclusions.append((o_id, io_id, reason))


def _engine(store):
    engine = object.__new__(app.AutomationEngine)
    engine.store = store
    engine.workstation_id = 'ws-force-skip'
    engine.closing_event = threading.Event()
    engine.shutdown_event = threading.Event()
    engine.run_event = threading.Event()
    engine.stop_event = threading.Event()
    engine.commit_lock = threading.RLock()
    engine.thread_lock = threading.Lock()
    engine.operator_skip_lock = threading.Lock()
    engine.force_skip_in_progress = False
    engine.thread = None
    engine._settings = lambda: object()
    engine._event = mock.Mock()
    engine.set_status = mock.Mock()
    engine.resume = mock.Mock()
    return engine


class OperatorSkipBoundaryTests(unittest.TestCase):
    def test_every_pause_type_can_be_forced_including_unknown_types(self):
        for kind in ('COMPLETION_PROOF_REQUIRED', 'NETWORK_TRANSIENT',
                     'BACKEND_SCHEMA', 'PRINT_SERVICE_OFFLINE', 'NO_PRINT_BOUNDARY',
                     'POLICY_NO_WRITE', 'OPERATOR_PAUSED', 'OPERATOR_STOPPED',
                     'SAFETY_ENVIRONMENT', 'UNEXPECTED_ERROR', '', 'FUTURE_ERROR'):
            with self.subTest(kind=kind):
                store = _Store(_job(kind))
                engine = _engine(store)
                planner = mock.Mock()
                with mock.patch.object(app, 'PlannerClient', return_value=planner):
                    result = engine.skip_current_and_continue(('6792528', '13543683'))
                self.assertEqual(result, ('6792528', '13543683'))
                planner.force_skip.assert_called_once()
                planner.renew.assert_not_called()
                planner.complete.assert_not_called()
                self.assertEqual(store.updates[-1][2]['status'], 'SKIPPED_OPERATOR')
                self.assertEqual(len(store.exclusions), 1)
                engine.resume.assert_called_once()

    def test_running_action_and_missing_or_stale_claim_can_be_forced(self):
        for plan in ({}, _plan(token='expired')):
            store = _Store(_job(status='RUNNING'))
            store.job['plan'] = plan
            engine = _engine(store)
            with mock.patch.object(app, 'PlannerClient') as cls:
                engine.skip_current_and_continue()
            cls.return_value.force_skip.assert_called_once()
            self.assertTrue(engine.stop_event.is_set())
            self.assertEqual(store.updates[-1][2]['status'], 'SKIPPED_OPERATOR')

    def test_active_worker_is_stopped_and_joined_before_backend_exclusion(self):
        store = _Store(_job(status='RUNNING'))
        engine = _engine(store)
        stopped = threading.Event()
        def worker():
            engine.stop_event.wait(2)
            stopped.set()
        engine.thread = threading.Thread(target=worker)
        engine.thread.start()
        planner = mock.Mock()
        def force_skip(*args):
            self.assertTrue(stopped.is_set())
            self.assertFalse(engine.thread.is_alive())
            self.assertTrue(engine.force_skip_in_progress)
            with self.assertRaisesRegex(RuntimeError, '保存强制跳过'):
                app.AutomationEngine.resume(engine)
        planner.force_skip.side_effect = force_skip
        with mock.patch.object(app, 'PlannerClient', return_value=planner):
            engine.skip_current_and_continue()
        self.assertFalse(engine.force_skip_in_progress)
        engine.resume.assert_called_once()

    def test_backend_failure_leaves_local_state_and_does_not_resume(self):
        store = _Store()
        engine = _engine(store)
        planner = mock.Mock()
        planner.force_skip.side_effect = app.TransientAPIError('timeout')
        with mock.patch.object(app, 'PlannerClient', return_value=planner):
            with self.assertRaises(app.TransientAPIError):
                engine.skip_current_and_continue()
        self.assertEqual(store.updates, [])
        self.assertEqual(store.exclusions, [])
        engine.resume.assert_not_called()
        self.assertFalse(engine.force_skip_in_progress)
        self.assertTrue(engine.stop_event.is_set())

    def test_local_failure_after_remote_commit_can_be_retried(self):
        store = _Store(update_ok=False)
        engine = _engine(store)
        with mock.patch.object(app, 'PlannerClient') as cls:
            with self.assertRaisesRegex(RuntimeError, '本地任务状态写入失败'):
                engine.skip_current_and_continue()
        cls.return_value.force_skip.assert_called_once()
        engine.resume.assert_not_called()
        self.assertEqual(store.exclusions, [])

    def test_changed_or_invalid_confirmation_never_skips_another_pair(self):
        for identity in (('1', '2'), ('6792528',)):
            engine = _engine(_Store())
            with mock.patch.object(app, 'PlannerClient') as cls:
                with self.assertRaises(RuntimeError):
                    engine.skip_current_and_continue(identity)
            cls.assert_not_called()
            self.assertFalse(engine.stop_event.is_set())

    def test_can_select_a_problem_while_other_jobs_are_running(self):
        engine = _engine(_Store())
        engine.run_event.set()
        self.assertEqual(engine.operator_skip_target()['o_id'], '6792528')

    def test_closed_engine_cannot_restart_from_force_skip(self):
        engine = _engine(_Store())
        engine.shutdown_event.set()
        with self.assertRaisesRegex(RuntimeError, '关闭'):
            engine.skip_current_and_continue()
        engine.resume.assert_not_called()

    def test_store_prioritizes_actual_running_error_over_old_paused_or_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = app.EventStore(root / 'events.sqlite3', root / 'events.jsonl')
            for o_id, io_id in (('101', '201'), ('102', '202'), ('103', '203')):
                store.save_job(_plan(o_id, io_id))
            store.update_job('101', '201', step_index=0, status='PAUSED',
                             pause_kind='OPERATOR_PAUSED', pause_reason='旧异常')
            store.event('BLOCKED', 'AUTO_PAUSE', '旧异常', o_id='101', io_id='201')
            store.update_job('102', '202', step_index=1, status='RUNNING')
            store.event('BLOCKED', 'COMPLETION_PROOF_REQUIRED', '当前提交结果不明',
                        o_id='102', io_id='202')
            target = store.latest_problem_job()
            self.assertEqual((target['o_id'], target['io_id']), ('102', '202'))
            self.assertEqual(target['pause_reason'], '当前提交结果不明')
            store.update_job('102', '202', step_index=1, status='SKIPPED_OPERATOR')
            self.assertEqual(store.latest_problem_job()['o_id'], '101')

    def test_store_selects_pending_job_with_an_exact_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = app.EventStore(root / 'events.sqlite3', root / 'events.jsonl')
            store.save_job(_plan('101', '201'))
            self.assertIsNone(store.latest_problem_job())
            store.event('BLOCKED', 'AUTO_PAUSE', '页面不可用', o_id='101', io_id='201')
            self.assertEqual(store.latest_problem_job()['o_id'], '101')


if __name__ == '__main__':
    unittest.main()
