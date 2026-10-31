"""Opt-in transport and real Node cancellation contracts."""
import json
import threading
from unittest import mock

from django.http import JsonResponse
from django.middleware.gzip import GZipMiddleware
from django.test import RequestFactory, SimpleTestCase, TransactionTestCase

from botend.services.simc_skill_actor_page import ActorReadControl, _SCAN_SLOTS
from botend.services.simc_skill_actor_stream import actor_json_stream
from botend.tests import test_simc_skill_actor_page as actor_page_tests
from botend.dashboard.api import SimcSkillDamageSnapshotAPIView
from botend.services.simc_skill_snapshot_storage import SkillSnapshotActorStorage as Store


class ActorStreamTransportTests(SimpleTestCase):
    def test_original_deadline_cancels_without_consumer_iteration(self):
        retired = threading.Event()
        def work():
            control = ActorReadControl.current()
            control.admit()
            try:
                control.cancel_event.wait(3)
                return JsonResponse({'success': False, 'code': 'read_cancelled'})
            finally:
                retired.set()
        with mock.patch('botend.services.simc_skill_actor_stream.READ_SECONDS', .03):
            response = actor_json_stream(work)
            try:
                self.assertTrue(retired.wait(3))
                self.assertTrue(response._actor_stream.control.cancel_event.is_set())
                self.assertFalse(json.loads(b''.join(response.streaming_content))['success'])
            finally:
                response.close()

    def test_early_whitespace_heartbeat_identity_and_same_final_json(self):
        finish = threading.Event()
        def work():
            ActorReadControl.current().admit()
            finish.wait(3)
            return JsonResponse({'success': True, 'data': {'value': '事实'}})
        with mock.patch('botend.services.simc_skill_actor_stream.HEARTBEAT_SECONDS', .01):
            response = actor_json_stream(work)
            try:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['X-Accel-Buffering'], 'no')
                self.assertEqual(response['Cache-Control'], 'no-store')
                self.assertEqual(response['Content-Encoding'], 'identity')
                request = RequestFactory().get('/', HTTP_ACCEPT_ENCODING='gzip')
                self.assertIs(GZipMiddleware(lambda r: response).process_response(request, response), response)
                chunks = iter(response.streaming_content)
                first, second = next(chunks), next(chunks)
                self.assertEqual(first, b' ' * 4096)
                self.assertEqual(second, b' ' * 4096)
                with self.assertRaises(ValueError):
                    json.loads(first + second)
                finish.set()
                body = first + second + b''.join(chunks)
                self.assertEqual(json.loads(body), {'success': True, 'data': {'value': '事实'}})
            finally:
                finish.set()
                response.close()

    def test_pre_admission_error_preserves_status_and_total_cap_is_explicit(self):
        response = actor_json_stream(lambda: JsonResponse({'success': False}, status=503))
        self.assertFalse(response.streaming)
        self.assertEqual(response.status_code, 503)
        def work():
            ActorReadControl.current().admit()
            return JsonResponse({'success': True, 'value': 'x' * 8192})
        with mock.patch('botend.services.simc_skill_actor_stream.RESPONSE_BYTES', 8192):
            response = actor_json_stream(work)
            try:
                body = b''.join(response.streaming_content)
                self.assertLessEqual(len(body), 8192)
                self.assertFalse(json.loads(body)['success'])
                self.assertEqual(json.loads(body)['code'], 'page_too_large')
            finally:
                response.close()


class ActorStreamAPITests(TransactionTestCase):
    setUp = actor_page_tests.ActorPageAPITests.setUp

    def response(self, **params):
        request = RequestFactory().get('/api/simc-skill-damage/', {
            'view': 'actor_page', 'snapshot_id': self.snapshot.pk,
            'actor_id': self.row.pk, 'limit': 2, 'stream': '1', **params})
        request.user = self.user
        return SimcSkillDamageSnapshotAPIView.as_view()(request)

    def test_real_api_envelope_busy_validation_and_corrupt_eof(self):
        response = self.response()
        try:
            self.assertTrue(response.streaming)
            data = json.loads(b''.join(response.streaming_content))
            self.assertTrue(data['success'])
            self.assertEqual(data['data']['snapshot']['actor_page']['source_ordinals'], [8, 7])
            self.assertEqual(data['data']['snapshot']['id'], self.snapshot.pk)
            plain = self.response(stream='0')
            try:
                self.assertFalse(plain.streaming)
                self.assertEqual(json.loads(plain.content), data)
            finally:
                plain.close()
        finally:
            response.close()
        for params in ({'limit': 0}, {'cursor': 'bad'}, {'view': 'actor_header_fragment', 'field': ''}):
            response = self.response(**params)
            self.assertEqual(response.status_code, 400)
            self.assertFalse(response.streaming)
            response.close()
        self.assertTrue(_SCAN_SLOTS.acquire(blocking=False))
        try:
            response = self.response()
            self.assertEqual(response.status_code, 503)
            self.assertFalse(response.streaming)
            self.assertEqual(json.loads(response.content)['code'], 'reader_busy')
            response.close()
        finally:
            _SCAN_SLOTS.release()
        from botend.models import SimcSkillDamageSnapshotPart
        last = SimcSkillDamageSnapshotPart.objects.filter(actor=self.row).order_by('-ordinal').first()
        last.text = last.text.replace('事实', '失实', 1)
        last.save(update_fields=['text'])
        response = self.response()
        try:
            self.assertFalse(json.loads(b''.join(response.streaming_content))['success'])
        finally:
            response.close()

    def test_close_before_iteration_during_body_and_diagnostic_kills_node_releases_slot(self):
        import subprocess
        from botend.services.simc_skill_actor_page import ActorReadControl
        original = Store.iterate_json_document
        real_popen = subprocess.Popen
        for phase in ('before_iteration', 'during_body', 'diagnostic'):
            with self.subTest(phase=phase):
                reached, release, stopped = threading.Event(), threading.Event(), threading.Event()
                controls, processes = [], []
                def popen(*args, **kwargs):
                    process = real_popen(*args, **kwargs)
                    processes.append(process)
                    return process
                def chunks(row, document):
                    controls.append(ActorReadControl.current())
                    try:
                        if phase == 'diagnostic':
                            # Parser has fed a real row and is skipping a reference-only footer.
                            yield '{"actions":[{"token":"a"}],"specialization_passive_diagnostics":"'
                            yield 'x' * 1024
                        reached.set()
                        release.wait(3)
                        yield from original(row, document)
                    finally:
                        stopped.set()
                with mock.patch.object(Store, 'iterate_json_document', side_effect=chunks), \
                     mock.patch('botend.services.simc_skill_actor_page.subprocess.Popen', side_effect=popen):
                    response = self.response()
                    try:
                        self.assertTrue(reached.wait(3))
                        self.assertTrue(processes)
                        self.assertIsNone(processes[0].poll())
                        if phase != 'before_iteration':
                            self.assertEqual(next(iter(response.streaming_content)), b' ' * 4096)
                        response.close()
                        self.assertTrue(controls[0].cancel_event.is_set())
                        release.set()
                        self.assertTrue(stopped.wait(3))
                        response._actor_stream.worker.join(3)
                        self.assertFalse(response._actor_stream.worker.is_alive())
                        self.assertIsNotNone(processes[0].poll())
                    finally:
                        release.set()
                        response.close()
                # Latest query can enter immediately after the canceled worker retires.
                latest = self.response()
                try:
                    self.assertTrue(json.loads(b''.join(latest.streaming_content))['success'])
                finally:
                    latest.close()

    def test_real_http_reset_cancels_worker_node_and_scan_slot(self):
        """Actual WSGI socket disconnect, not just a simulated resource.close()."""
        import socket
        import struct
        import subprocess
        from wsgiref.simple_server import make_server, WSGIRequestHandler
        reached, retired = threading.Event(), threading.Event()
        responses, processes = [], []
        real_popen = subprocess.Popen
        def popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process
        def chunks(row, document):
            try:
                yield '{"actions":[{"token":"a"}],"specialization_passive_diagnostics":"'
                yield 'x' * 1024
                reached.set()
                control = ActorReadControl.current()
                control.cancel_event.wait(3)
                control.check()
                yield '"}'
            finally:
                retired.set()
        def application(environ, start_response):
            response = self.response()
            responses.append(response)
            start_response('200 OK', list(response.items()))
            return response
        class QuietHandler(WSGIRequestHandler):
            def log_message(self, *args):
                pass
        server = make_server('127.0.0.1', 0, application, handler_class=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        with mock.patch.object(Store, 'iterate_json_document', side_effect=chunks), \
             mock.patch('botend.services.simc_skill_actor_page.subprocess.Popen', side_effect=popen), \
             mock.patch('botend.services.simc_skill_actor_stream.HEARTBEAT_SECONDS', .01):
            thread.start()
            try:
                client = socket.create_connection(server.server_address, timeout=3)
                client.sendall(b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
                received = b''
                while len(received.partition(b'\r\n\r\n')[2]) < 4096:
                    received += client.recv(16384)
                self.assertTrue(reached.wait(3))
                body = received.partition(b'\r\n\r\n')[2]
                self.assertFalse(body.strip(), body)
                self.assertIsNone(processes[0].poll())
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                client.close()
                self.assertTrue(retired.wait(3))
                responses[0]._actor_stream.worker.join(3)
                self.assertFalse(responses[0]._actor_stream.worker.is_alive())
                self.assertIsNotNone(processes[0].poll())
                self.assertTrue(_SCAN_SLOTS.acquire(blocking=False))
                _SCAN_SLOTS.release()
            finally:
                for response in responses:
                    response.close()
                server.shutdown()
                server.server_close()
                thread.join(3)

    def test_fragment_modes_cancel_and_release_slot_without_materialization(self):
        for params in ({'view': 'actor_fragment', 'source_ordinal': 0},
                       {'view': 'actor_header_fragment', 'field': 'class'},
                       {'view': 'unresolved_fragment'}):
            with self.subTest(params=params):
                reached, retired = threading.Event(), threading.Event()
                original = Store.iterate_json_document
                def chunks(row, document):
                    try:
                        reached.set()
                        control = ActorReadControl.current()
                        control.cancel_event.wait(3)
                        control.check()
                        yield from original(row, document)
                    finally:
                        retired.set()
                with mock.patch.object(Store, 'iterate_json_document', side_effect=chunks):
                    response = self.response(**params)
                    try:
                        self.assertTrue(reached.wait(3))
                        response.close()
                        self.assertTrue(retired.wait(3))
                        response._actor_stream.worker.join(3)
                        self.assertFalse(response._actor_stream.worker.is_alive())
                        self.assertTrue(_SCAN_SLOTS.acquire(blocking=False))
                        _SCAN_SLOTS.release()
                    finally:
                        response.close()
