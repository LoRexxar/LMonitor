"""Small shared HTTP boundary with structured, credential-free provider errors."""
import ssl

import requests


class AIHTTPError(Exception):
    def __init__(self, provider, status_code, body=None):
        self.status_code = status_code
        self.body = body if isinstance(body, dict) else {}
        _, code = error_details(self)
        # Do not expose response bodies: providers may echo an Authorization value.
        super().__init__(f"{provider} HTTP status={status_code}" + (f", code={code}" if code else ""))


def error_details(error):
    """Read SDK/HTTP exception fields, never infer status/code from its string."""
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    body = getattr(error, "body", None)
    if not isinstance(body, dict) and response is not None:
        try:
            body = response.json()
        except Exception:
            body = None
    detail = body.get("error", body) if isinstance(body, dict) else {}
    code = detail.get("code") if isinstance(detail, dict) else None
    if code is None:
        code = getattr(error, "code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    return status, str(code) if code is not None else ""


def is_certificate_error(error):
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        if "CERTIFICATE_VERIFY_FAILED" in str(current) or "certificate verify failed" in str(current).lower():
            return True
        for child in (getattr(current, "__cause__", None), getattr(current, "__context__", None),
                      getattr(current, "reason", None), *getattr(current, "args", ())):
            if isinstance(child, BaseException):
                pending.append(child)
    return False


def is_transient_connection_error(error):
    if is_certificate_error(error):
        return False
    if isinstance(error, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    # Both Zhipu SDK generations expose these OpenAI-style error classes.
    names = {cls.__name__ for cls in type(error).__mro__}
    return bool(names & {"APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout",
                         "ReadTimeout", "WriteTimeout", "PoolTimeout", "RemoteProtocolError"})


def post_completion(*, provider, base_url, api_key, payload, timeout):
    response = requests.post(
        base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload, timeout=timeout, allow_redirects=False,
    )
    try:
        body = response.json()
    except ValueError:
        body = None
    if not 200 <= response.status_code < 300:
        raise AIHTTPError(provider, response.status_code, body)
    if not isinstance(body, dict):
        raise ValueError(f"{provider} HTTP returned invalid JSON object")
    if body.get("error"):
        raise AIHTTPError(provider, response.status_code, body)
    return body
