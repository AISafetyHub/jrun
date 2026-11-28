"""Fake urllib opener for REST client tests (no network)."""

import io
import json
import urllib.error


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    """Returns queued payloads (or raises queued exceptions) per open() call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, FakeResponse):
            return response
        return FakeResponse(response)


def make_http_error(code, payload):
    return urllib.error.HTTPError(
        url="https://x", code=code, msg="err", hdrs=None,
        fp=io.BytesIO(json.dumps(payload).encode()),
    )
