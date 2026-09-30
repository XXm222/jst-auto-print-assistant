import unittest

import jst_auto_print_app as app


class _Store:
    def __init__(self, *, has_other=True, update_ok=True):
        self.has_other = has_other
        self.update_ok = update_ok
        self.updates = []

    def update_job(self, o_id, io_id, **fields):
        self.updates.append((str(o_id), str(io_id), dict(fields)))
        return self.update_ok

    def has_runnable_job_except(self, o_id, io_id):
        self.queried = (str(o_id), str(io_id))
        return self.has_other


def _engine(store):
    engine = object.__new__(app.AutomationEngine)
    engine.store = store
    engine.events = []
    engine.pauses = []
    engine.statuses = []
    engine._event = lambda *args, **kwargs: engine.events.append((args, kwargs))
    engine._auto_pause = lambda reason, plan=None: engine.pauses.append((reason, plan))
    engine.set_status = engine.statuses.append
    return engine


class QueueResilienceTests(unittest.TestCase):
    @staticmethod
    def _job():
        return {
            "o_id": "101",
            "io_id": "201",
            "status": "PREPARING",
            "step_index": 1,
            "plan": {"o_id": "101", "io_id": "201"},
        }

    def test_one_preclick_order_conflict_does_not_interrupt_other_runnable_jobs(self):
        store = _Store(has_other=True)
        engine = _engine(store)

        engine._pause_resumable_job(
            self._job(), "实时重量已变化", pause_kind=app.SKIPPABLE_PAUSE_KIND
        )

        self.assertEqual(engine.pauses, [])
        self.assertEqual(store.updates[0][2]["status"], "PAUSED")
        self.assertEqual(store.queried, ("101", "201"))
        self.assertEqual(engine.events[0][0][1], "JOB_CONFLICT_DEFERRED")

    def test_queue_pauses_when_only_the_conflicting_order_remains(self):
        store = _Store(has_other=False)
        engine = _engine(store)

        engine._pause_resumable_job(
            self._job(), "实时重量已变化", pause_kind=app.SKIPPABLE_PAUSE_KIND
        )

        self.assertEqual(len(engine.pauses), 1)
        self.assertEqual(engine.pauses[0][0], "实时重量已变化")

    def test_global_environment_pause_never_gets_deferred(self):
        store = _Store(has_other=True)
        engine = _engine(store)

        engine._pause_resumable_job(
            self._job(), "仓库页面无法证明", pause_kind="SAFETY_ENVIRONMENT"
        )

        self.assertEqual(len(engine.pauses), 1)
        self.assertFalse(hasattr(store, "queried"))


if __name__ == "__main__":
    unittest.main()
