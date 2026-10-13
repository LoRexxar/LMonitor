"""Thread orchestration/rate-budget contracts, independent from DB tests."""
import threading
from concurrent.futures import ThreadPoolExecutor as RealPool
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from botend.management.commands.sync_player_raiderio_rosters import collect_candidates
from botend.services import raiderio_run_roster as service


class RosterBatchTests(SimpleTestCase):
    def test_pacing_global_budget_and_stop_while_waiting(self):
        event = threading.Event()
        with patch.object(service, '_last_request', None), \
                patch.object(service.time, 'monotonic', side_effect=[10, 10, 10.1, 10.25]), \
                patch.object(service.time, 'sleep') as sleep:
            self.assertTrue(service._pace(event))
            self.assertTrue(service._pace(event))
            self.assertAlmostEqual(sleep.call_args.args[0], .15)
        event.set()
        with patch.object(service, '_last_request', None):
            self.assertFalse(service._pace(event))

    def test_bounded_workers_shared_stop_no_queued_tail_and_own_collectors(self):
        module = 'botend.management.commands.sync_player_raiderio_rosters'
        barrier = threading.Barrier(4)
        events, threads, sessions = [], set(), []
        submitted = []
        lock = threading.Lock()

        class TrackedPool(RealPool):
            def submit(self, fn, *args, **kwargs):
                submitted.append(args[0]['run_id'])
                return super().submit(fn, *args, **kwargs)

        class Worker:
            def __init__(self, stop_event):
                self.stop = stop_event
                self.close = Mock()
                sessions.append(self)
                events.append(stop_event)

            def refresh(self, run, season, force=False):
                with lock:
                    threads.add(threading.get_ident())
                barrier.wait(timeout=5)
                self.stop.set()
                return {'status': 'rate_limited' if run['id'] == 1 else 'ready'}

        candidates = [{'run_id': i, 'run': {'id': i}, 'conflict': False} for i in range(1, 101)]
        with patch(module + '.RosterCollector', Worker), patch(module + '.close_old_connections') as close, \
                patch(module + '.ThreadPoolExecutor', TrackedPool):
            results = list(collect_candidates(candidates, 'season-mn-2', workers=4, force=False))
        self.assertEqual(len(submitted), 4)
        self.assertEqual(len(results), 100)
        self.assertEqual(sum(r['status'] == 'batch_stopped' for r in results), 96)
        self.assertEqual(len(threads), 4)
        self.assertEqual(len({id(event) for event in events}), 1)
        self.assertEqual(close.call_count, 8)
        for session in sessions:
            session.close.assert_called_once()
