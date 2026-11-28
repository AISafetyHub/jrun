"""Shared fakes for the jrun-server tests."""

from jrun_server.runner import JrunError

CALLER_TOKEN = "caller-token"
CALLER = {"id": "42", "alias": "intern1", "identity_type": "院外", "active": True,
          "projsets": [{"id": "ps-1", "name": "baai-safety"},
                       {"id": "ps-2", "name": "other-ps"}]}


class FakeRunner:
    """Scripted jrun: ``responses`` maps a command prefix (tuple) to a dict,
    an exception, or a callable(args, cwd, token) -> dict."""

    def __init__(self, responses=None):
        self.responses = dict(responses or {})
        self.calls = []

    def run_json(self, args, *, cwd=None, token=None):
        self.calls.append({"args": list(args), "cwd": cwd, "token": token})
        best = None
        for prefix in self.responses:
            if tuple(args[:len(prefix)]) == prefix and (best is None or len(prefix) > len(best)):
                best = prefix
        if best is None:
            raise JrunError(args, f"unexpected jrun call: {args}")
        response = self.responses[best]
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(args, cwd, token)
        return response

    def calls_for(self, *prefix):
        return [c for c in self.calls if tuple(c["args"][:len(prefix)]) == prefix]


def queues(*names, models=("NVIDIA_H100",)):
    return {"queues": [{"queue_name": n, "resources": [{"model": m} for m in models]}
                       for n in names]}
