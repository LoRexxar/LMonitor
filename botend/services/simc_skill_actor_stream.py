"""Opt-in JSON whitespace keepalive around the unchanged bounded response.

Only the final JSON carries facts. Admission errors retain their HTTP status;
post-admission errors use success:false under the already committed HTTP 200.
"""
import logging
import queue
import threading

from django.db import connections
from django.http import JsonResponse, StreamingHttpResponse

from botend.services.simc_skill_actor_page import ActorPageError, ActorReadControl, RESPONSE_BYTES

logger = logging.getLogger(__name__)
HEARTBEAT_SECONDS = 2
READ_SECONDS = 300
PAD = b' ' * 4096
# Reserve enough space for a valid explicit error, even at the transport cap.
ERROR_RESERVE = 1024


def _error(code, message):
    return JsonResponse({'success': False, 'code': code, 'error': message}, status=503)


class _ActorJSONStream:
    """A closable object, not an unstarted generator whose finally won't run."""
    def __init__(self, work):
        self.control = ActorReadControl()
        self.result = queue.Queue(maxsize=1)
        self.done = threading.Event()
        self.closed = threading.Event()
        self.timer = threading.Timer(READ_SECONDS, self.control.cancel)
        self.timer.daemon = True
        self.worker = threading.Thread(target=self._run, args=(work,), daemon=True,
                                       name='actor-json-read')
        self.timer.start()
        self.worker.start()

    def _run(self, work):
        response = None
        try:
            with self.control.activate():
                self.control.check()
                response = work()
                self.control.check()
        except ActorPageError as exc:
            if response is not None:
                response.close()
            response = _error(exc.code, str(exc))
        except Exception:
            logger.exception('Bounded actor stream worker failed')
            response = _error('page_unavailable', '技能快照数据暂时不可用')
        finally:
            try:
                if response is not None:
                    if self.closed.is_set():
                        response.close()
                    else:
                        self.result.put_nowait(response)
                        # Close may race the initial closed check and queue put.
                        if self.closed.is_set():
                            self._discard_result()
            finally:
                try:
                    connections.close_all()
                finally:
                    self.done.set()
                    self.timer.cancel()

    def _discard_result(self):
        try:
            self.result.get_nowait().close()
        except queue.Empty:
            pass

    def close(self):
        self.closed.set()
        self.control.cancel()
        self.timer.cancel()
        # Never wait for a blocked database read on the HTTP disconnect thread.
        self._discard_result()

    def __iter__(self):
        response = None
        sent = 0
        try:
            # Flush headers immediately even when the worker hasn't read a byte.
            yield PAD
            sent += len(PAD)
            while not self.closed.is_set():
                try:
                    response = self.result.get(timeout=HEARTBEAT_SECONDS)
                    break
                except queue.Empty:
                    if self.control.cancel_event.is_set():
                        response = _error('read_cancelled', '技能读取已取消或达到原有时间上限')
                        break
                    if sent + len(PAD) + ERROR_RESERVE <= RESPONSE_BYTES:
                        yield PAD
                        sent += len(PAD)
                    else:
                        self.control.cancel()
                        response = _error('page_too_large', '技能页与保活数据超过安全字节上限')
                        break
            if self.closed.is_set():
                return
            body = response.content
            if sent + len(body) > RESPONSE_BYTES:
                response.close()
                response = _error('page_too_large', '技能页与保活数据超过安全字节上限')
                body = response.content
            # Never truncate JSON or emit an unverified partial business result.
            yield body
        finally:
            if response is not None:
                response.close()
            self.close()


def actor_json_stream(work):
    stream = _ActorJSONStream(work)
    # Quick validation/busy responses are not admitted into a 200 stream.
    while not stream.control.admitted_event.is_set():
        if stream.done.wait(.01):
            if not stream.control.admitted_event.is_set():
                response = stream.result.get_nowait()
                stream.close()
                return response
        if stream.control.cancel_event.is_set():
            stream.close()
            return _error('read_cancelled', '技能读取已取消或达到原有时间上限')
    response = StreamingHttpResponse(stream, content_type='application/json')
    response['X-Accel-Buffering'] = 'no'
    response['Cache-Control'] = 'no-store'
    response['Content-Encoding'] = 'identity'
    response._actor_stream = stream
    return response
