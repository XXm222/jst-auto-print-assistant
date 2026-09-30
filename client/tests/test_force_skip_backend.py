import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import jst_auto_print_app as app

SERVER_DIR = Path(__file__).resolve().parents[2] / 'server'
sys.path.insert(0, str(SERVER_DIR))
try:
    import jst_print_api_server as api
    from jst_lease_store import LeaseStore, LeaseConflict
finally:
    sys.path.remove(str(SERVER_DIR))


class ForceSkipBackendTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'leases.sqlite3'
        self.store = LeaseStore(self.path, ttl_seconds=300)
        self.body = {'workstation_id': 'ws-force-skip', 'o_id': '6840357',
                     'io_id': '13585554', 'reason': '人工强制跳过：状态与发货记录矛盾'}

    def tearDown(self):
        self.directory.cleanup()

    def skip(self, body=None):
        return api.process_request('/jst-print-api/v1/order/force-skip',
                                   body or self.body, store=self.store)

    def test_no_lease_or_completion_proof_is_needed_and_record_survives_restart(self):
        with mock.patch.object(api, 'run_planner') as planner:
            result = self.skip()
        planner.assert_not_called()
        self.assertTrue(result['permanently_excluded'])
        self.assertEqual(result['completion_reason'], 'OPERATOR_SKIPPED')
        reopened = LeaseStore(self.path, ttl_seconds=300)
        self.assertIsNone(reopened.claim('6840357', '13585554', 'ws-other'))
        self.assertIsNotNone(reopened.claim('6840357', '13585555', 'ws-other'))
        with sqlite3.connect(self.path) as conn:
            saved = conn.execute('SELECT reason FROM operator_skips').fetchone()
        self.assertEqual(saved[0], self.body['reason'])

    def test_current_lease_is_revoked_even_when_claimed_by_another_workstation(self):
        lease = self.store.claim('6840357', '13585554', 'ws-other')
        self.skip()
        with self.assertRaises(LeaseConflict):
            self.store.renew(lease.o_id, lease.io_id, lease.workstation_id, lease.claim_token)
        with self.assertRaises(LeaseConflict):
            self.store.require_active(lease.o_id, lease.io_id,
                                      lease.workstation_id, lease.claim_token)
        self.assertEqual(self.store.active_for_workstation('ws-other'), [])
        self.assertIsNone(self.store.claim('6840357', '13585554', 'ws-other'))

    def test_expired_and_released_leases_can_be_forced(self):
        now = [1800000000.0]
        store = LeaseStore(self.path, ttl_seconds=300, clock=lambda: now[0])
        for index, state in enumerate(('EXPIRED', 'RELEASED')):
            o_id, io_id = str(100 + index), str(200 + index)
            lease = store.claim(o_id, io_id, 'ws-old')
            if state == 'RELEASED':
                store.release(o_id, io_id, lease.workstation_id, lease.claim_token)
            now[0] += 301
            store.force_skip(o_id, io_id, 'ws-operator', state)
            self.assertIsNone(store.claim(o_id, io_id, 'ws-new'))

    def test_retries_preserve_original_audit_and_completed_print_proof(self):
        lease = self.store.claim('6840357', '13585554', 'ws-original')
        self.store.complete(lease.o_id, lease.io_id, lease.workstation_id,
                            lease.claim_token, 'PRINTED')
        first = self.skip()
        retry = self.skip(dict(self.body, reason='网络重试'))
        self.assertEqual(first['skipped_at'], retry['skipped_at'])
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute('SELECT reason FROM operator_skips').fetchall()
            completed_reason = conn.execute('SELECT completion_reason FROM leases').fetchone()[0]
        self.assertEqual(rows, [(self.body['reason'],)])
        self.assertEqual(completed_reason, 'PRINTED')

    def test_exclusion_is_independent_of_local_200_pair_limit_and_lease_rows(self):
        for index in range(205):
            self.store.force_skip(str(1000 + index), str(2000 + index), 'ws-operator', '问题单')
        with sqlite3.connect(self.path) as conn:
            conn.execute('DELETE FROM leases')
        self.assertIsNone(self.store.claim('1000', '2000', 'ws-new'))
        self.assertIsNone(self.store.claim('1204', '2204', 'ws-new'))

    def test_planning_with_empty_local_excludes_never_returns_forced_pair(self):
        self.skip()
        candidate = {'o_id': '6840357', 'io_id': '13585554',
                     'current_carrier_id': api.TARGET_CARRIER_ID,
                     'current_carrier': api.PRINT_PROFILE_CARRIERS[api.TARGET_CARRIER_ID],
                     'steps': ['GET_WAYBILL', 'PRINT_EXPRESS', 'STOP_BEFORE_PRESHIP'],
                     'items': [{'product_id': 'sku-test'}]}
        raw = {'generated_at': api.utc_text(), 'scope': {}, 'counts': {'selected': 1},
               'selected': [candidate], 'blocked_preview': [], 'blocked_reason_counts': {},
               'guardrails': []}
        with mock.patch.object(api, 'get_plan_pool', return_value=raw):
            result = api._plan({'workstation_id': 'ws-new-client', 'exclude': [],
                                'print_profile': api.TARGET_CARRIER_ID}, self.store)
        self.assertEqual(result['selected'], [])

    def test_invalid_identity_or_reason_is_rejected_before_any_db_write(self):
        for changed in ({'o_id': '../oops'}, {'io_id': True}, {'reason': ''},
                        {'reason': ' '}, {'reason': 'x' * 2001}, {'extra': 1}):
            with self.subTest(changed=changed):
                with self.assertRaises(api.APIError):
                    self.skip(dict(self.body, **changed))
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM operator_skips').fetchone()[0], 0)

    def test_client_round_trip_requires_exact_durable_backend_acknowledgement(self):
        client = object.__new__(app.PlannerClient)
        client.workstation_id = 'ws-force-skip'
        client._post = lambda endpoint, payload, **kw: api.process_request(
            '/jst-print-api/v1/' + endpoint, payload, store=self.store)
        client.force_skip('6840357', '13585554', '客户端强制跳过')
        self.assertIsNone(self.store.claim('6840357', '13585554', 'ws-next'))
        good = self.skip()
        for change in ({'o_id': '999'}, {'io_id': '999'}, {'ok': False},
                       {'permanently_excluded': False}, {'api_schema_version': True},
                       {'completion_reason': 'PRINTED'}):
            client._post = lambda *args, **kw: dict(good, **change)
            with self.assertRaises(app.BackendSchemaError):
                client.force_skip('6840357', '13585554', '问题单')

    def test_http_endpoint_requires_bearer_authentication(self):
        token = 'test-only-token-' + 'x' * 40
        with mock.patch.object(api, 'TOKEN', token), mock.patch.object(
                api, 'get_lease_store', return_value=self.store):
            server = api.BoundedThreadingHTTPServer(('127.0.0.1', 0), api.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f'http://127.0.0.1:{server.server_port}/jst-print-api/v1/order/force-skip'
                payload = json.dumps(self.body).encode()
                headers = {'User-Agent': 'JSTAutoPrint/0.5.25', 'Content-Type': 'application/json'}
                request = urllib.request.Request(url, data=payload, headers=headers)
                with self.assertRaises(urllib.error.HTTPError) as exc:
                    urllib.request.urlopen(request, timeout=3)
                self.assertEqual(exc.exception.code, 401)
                exc.exception.close()
                request = urllib.request.Request(url, data=payload,
                          headers=dict(headers, Authorization='Bearer ' + token))
                with urllib.request.urlopen(request, timeout=3) as response:
                    self.assertTrue(json.load(response)['permanently_excluded'])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
