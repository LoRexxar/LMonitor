"""Consumer transport doubles test wiring, not real OSS acceptance."""
import base64
import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from django.test import SimpleTestCase
import simc_agent_consumer as agent
import simc_conditional_evidence as codec


class ConditionalConsumerTests(SimpleTestCase):
    def consumer(self, root, transport=None):
        c = agent.SimcAgentConsumer(agent.AgentConfig(simc_path='/bin/true', token_path=str(Path(root)/'token')), transport=transport or MagicMock())
        c.agent_token = 'test-token'
        return c

    def metadata(self, c):
        return dict(lease_token='lease', instance_id=c.instance_id, completion_id='a'*32,
                    status='completed', stdout='', stderr='', report=None, conditional_evidence=None)

    def test_durable_restart_upload_order_and_original_instance(self):
        with tempfile.TemporaryDirectory() as root:
            c = self.consumer(root)
            blob = codec.encode('prepared\r\n', '{ "sim": {} }\n')
            m = self.metadata(c)
            c._save_completion_outbox(7, m, 'simc_task_1_run_7.html', b'<html>ok</html>', evidence_bytes=blob)
            recovered = self.consumer(root)
            events = []
            def request(*, path, payload, authorization):
                events.append(path.rsplit('/', 2)[1])
                if path.endswith('/evidence-upload/'):
                    self.assertEqual(set(payload), {'lease_token','instance_id','size','sha256','content_md5'})
                    self.assertEqual(payload['instance_id'], m['instance_id'])
                    self.assertEqual(payload['sha256'], hashlib.sha256(blob).hexdigest())
                    return dict(protocol_version=1, object_key='simc_conditional_evidence/v1/test.gz', method='PUT', url='https://bucket.example/test', headers={'Content-Length':str(len(blob))})
                if path.endswith('/complete/'):
                    self.assertEqual(set(payload['conditional_evidence']), {'protocol_version','object_key','size','sha256'})
                return {}
            recovered.transport.json.side_effect = request
            recovered.transport.put_bytes.side_effect = lambda **kw: (events.append('put'), self.assertEqual(kw['body'], blob))
            recovered._upload_report = lambda *a, **kw: (events.append('html') or {'object_key':'html','size':15,'sha256':'b'*64})
            self.assertTrue(recovered.flush_completion_outbox())
            self.assertEqual(events, ['evidence-upload','put','html','complete'])
            self.assertFalse(list(recovered.completion_outbox_path.glob('*.json')))

    def test_same_process_json_and_prepare_authorization(self):
        with tempfile.TemporaryDirectory() as root:
            c = self.consumer(root)
            original = '# lmonitor_equipment_control_v1=test\n'
            job = dict(run_id=7,lease_token='lease',input=original,input_hash=hashlib.sha256(original.encode()).hexdigest(), output_filename='simc_task_1_run_7.html', timeout_seconds=10,lease_expires_at='2999-01-01T00:00:00+00:00',conditional_authorization={'approved':'independent'})
            c.transport.json.return_value = {'lease_expires_at':job['lease_expires_at']}
            captured = {}
            def spawn(command, **kw):
                captured['command'] = command
                work = Path(kw['cwd'])
                (work/job['output_filename']).write_bytes(b'<html>ok</html>')
                for arg in command:
                    if arg.startswith('json2='):
                        (work/arg.split('=',1)[1]).write_bytes(b'{ "sim": {} }\n')
                p = MagicMock(returncode=0)
                p.communicate.return_value = (b'', b'')
                return p
            def save(*args, **kw):
                captured['evidence'] = codec.decode(kw['evidence_bytes'])
                self.assertTrue(Path(captured['work']).exists())
                return original_save(*args, **kw)
            original_save = c._save_completion_outbox
            def prepare(code, binary, directory, **kw):
                self.assertEqual(kw['conditional_authorization'], job['conditional_authorization'])
                captured['work'] = str(directory)
                return 'prepared\n'
            with patch('simc_equipment_control.prepare_control_input', side_effect=prepare), patch('simc_agent_consumer.subprocess.Popen', side_effect=spawn) as popen, patch.object(c, '_save_completion_outbox', side_effect=save), patch.object(c, 'flush_completion_outbox', return_value=False):
                c.execute_job(job)
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(captured['evidence'], {'prepared_input':'prepared\n','report_json':'{ "sim": {} }\n'})
            self.assertTrue(any(x.startswith('json2=') for x in captured['command']))
            self.assertFalse(Path(captured['work']).exists())

    def test_pending_cancelled_and_terminal_replay(self):
        for response in (agent.APIError('offline'),
                         agent.APIError('cancelled',409,{'code':'run_cancelled'}),
                         {'already_completed':True}):
            with self.subTest(response=type(response).__name__), tempfile.TemporaryDirectory() as root:
                c = self.consumer(root)
                c._save_completion_outbox(7,self.metadata(c),'simc_task_1_run_7.html',b'<html>ok</html>',evidence_bytes=codec.encode('p','{}'))
                if isinstance(response, Exception):
                    c.transport.json.side_effect = response
                else:
                    c.transport.json.return_value = response
                retained = isinstance(response, agent.APIError) and response.status is None
                self.assertEqual(c.flush_completion_outbox(), not retained)
                self.assertEqual(bool(list(c.completion_outbox_path.glob('*.json'))), retained)
                c.transport.put_bytes.assert_not_called()

    def test_uncertain_put_and_failed_completion_remain_retryable(self):
        with tempfile.TemporaryDirectory() as root:
            c = self.consumer(root)
            blob = codec.encode('p','{}')
            c._save_completion_outbox(7,self.metadata(c),'simc_task_1_run_7.html',b'<html>ok</html>',evidence_bytes=blob)
            ticket = dict(protocol_version=1,object_key='simc_conditional_evidence/v1/test.gz',method='PUT',url='https://bucket.example/x',headers={})
            c.transport.json.return_value = ticket
            c.transport.put_bytes.side_effect = agent.APIError('response lost')
            c._upload_report = MagicMock(return_value={'object_key':'html','size':15,'sha256':'a'*64})
            with patch.object(c,'_completion_json',return_value=False):
                self.assertFalse(c.flush_completion_outbox())
            entry = next(c.completion_outbox_path.glob('*.json'))
            self.assertIsNone(c._load_completion_outbox(entry)[1]['conditional_evidence'])
            with patch.object(c,'_completion_json',return_value=True):
                self.assertTrue(c.flush_completion_outbox())
            self.assertFalse(entry.exists())
            self.assertEqual(c.transport.put_bytes.call_count,2)

    def test_html_retry_preserves_artifacts_and_idempotency_after_restart(self):
        # Storage/control doubles exercise the real consumer/outbox, not real OSS.
        for failure in ('html_offline', 'put_response_lost', 'completion_response_lost'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                c = self.consumer(root)
                c.stop_event.set()  # Do not sleep between bounded transport retries.
                metadata = self.metadata(c)
                html, evidence = b'<html>ok</html>', codec.encode('p', '{}')
                name = 'simc_task_1_run_7.html'
                c._save_completion_outbox(7, metadata, name, html, evidence_bytes=evidence)
                stored, writes, attempts, publications = {}, [], [], []
                recovering = False

                def request(*, path, payload, authorization):
                    self.assertEqual(payload['instance_id'], metadata['instance_id'])
                    self.assertEqual(payload['lease_token'], metadata['lease_token'])
                    if path.endswith('/complete/'):
                        self.assertEqual(payload['status'], 'completed')
                        self.assertEqual(payload['completion_id'], metadata['completion_id'])
                        if not recovering and failure != 'completion_response_lost':
                            raise agent.APIError('not ready', 422)
                        self.assertEqual(stored, {'evidence': evidence, 'html': html})
                        if not publications:
                            publications.append(payload['completion_id'])
                        if not recovering:
                            raise agent.APIError('completion response lost')
                        return {}
                    if publications:
                        return {'already_completed': True}
                    kind = 'evidence' if path.endswith('/evidence-upload/') else 'html'
                    key = ('simc_conditional_evidence/v1/test.gz' if kind == 'evidence'
                           else 'simc_agent_results/' + name)
                    return dict(protocol_version=1, object_key=key, method='PUT',
                                url='https://bucket.example/' + kind,
                                headers={'Content-Length': str(payload['size']),
                                         'x-oss-forbid-overwrite': 'true'})

                def put(*, url, body, headers):
                    kind = url.rsplit('/', 1)[1]
                    attempts.append(kind)
                    self.assertEqual(headers['Content-Length'], str(len(body)))
                    self.assertEqual(headers['x-oss-forbid-overwrite'], 'true')
                    if kind == 'html' and not recovering and failure == 'html_offline':
                        raise agent.APIError('offline')
                    if kind in stored:
                        raise agent.APIError('forbid overwrite', 409)
                    stored[kind] = body
                    writes.append(kind)
                    if not recovering and failure == 'put_response_lost':
                        raise agent.APIError('PUT response lost')

                c.transport.json.side_effect = request
                c.transport.put_bytes.side_effect = put
                self.assertFalse(c.flush_completion_outbox())
                entry = next(c.completion_outbox_path.glob('*.json'))
                _, pending, saved_name, saved_html, saved_evidence = c._load_completion_outbox(
                    entry, include_evidence=True)
                self.assertEqual((saved_name, saved_html, saved_evidence), (name, html, evidence))
                self.assertEqual(pending['status'], 'completed')
                self.assertEqual(pending['completion_id'], metadata['completion_id'])
                before = attempts.count('html')
                recovering = True
                restarted = self.consumer(root, c.transport)
                restarted.stop_event.set()
                self.assertTrue(restarted.flush_completion_outbox())
                self.assertFalse(entry.exists())
                self.assertEqual(writes.count('html'), 1)
                self.assertEqual(writes.count('evidence'), 1)
                self.assertEqual(publications, [metadata['completion_id']])
                if failure == 'html_offline':
                    self.assertGreater(attempts.count('html'), before)
                if failure == 'completion_response_lost':
                    self.assertEqual(attempts.count('html'), before)

    def test_missing_json_fails_without_evidence_or_upload(self):
        with tempfile.TemporaryDirectory() as root:
            c = self.consumer(root)
            code = '# lmonitor_equipment_control_v1=test\n'
            job = dict(run_id=7,lease_token='lease',input=code,input_hash=hashlib.sha256(code.encode()).hexdigest(),output_filename='simc_task_1_run_7.html',timeout_seconds=10,lease_expires_at='2999-01-01T00:00:00+00:00',conditional_authorization={})
            c.transport.json.return_value = {'lease_expires_at':job['lease_expires_at']}
            def spawn(*args, **kw):
                (Path(kw['cwd'])/job['output_filename']).write_bytes(b'<html>ok</html>')
                p = MagicMock(returncode=0)
                p.communicate.return_value = (b'',b'')
                return p
            with patch('simc_equipment_control.prepare_control_input',return_value='p'), patch('simc_agent_consumer.subprocess.Popen',side_effect=spawn):
                c.execute_job(job)
            payload = c.transport.json.call_args.kwargs['payload']
            self.assertEqual(payload['status'],'failed')
            self.assertNotIn('conditional_evidence',payload)
            c.transport.put_bytes.assert_not_called()

    def test_bounds_and_legacy_outbox(self):
        with tempfile.TemporaryDirectory() as root:
            c = self.consumer(root)
            path = Path(root)/'oversized'
            path.write_bytes(b'1234')
            with self.assertRaises(agent.APIError):
                c._bounded_read(path,3)
            m = self.metadata(c)
            del m['conditional_evidence']
            m['status'] = 'failed'
            c._save_completion_outbox(7,m)
            entry = next(c.completion_outbox_path.glob('*.json'))
            self.assertEqual(set(json.loads(entry.read_text())),{'run_id','metadata','report_name','report_bytes'})
            self.assertTrue(self.consumer(root).flush_completion_outbox())
            self.assertFalse(entry.exists())

    def test_evidence_transport_accepts_exact_signed_length(self):
        t = agent.HTTPTransport('https://control.example', 1)
        with patch.object(t, '_request') as request:
            t.put_bytes(url='https://bucket.example/private', body=b'abc', headers={'Content-Length':'3','Content-MD5':'exact'})
        self.assertEqual(request.call_args.args[0].data, b'abc')
        self.assertEqual(request.call_args.args[0].get_header('Content-length'), '3')
        with self.assertRaises(agent.APIError):
            t.put_bytes(url='https://bucket.example/private', body=b'abc', headers={'Content-Length':'4'})
