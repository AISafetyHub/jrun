"""HTTP front end (stdlib ThreadingHTTPServer) and the ``jrun-server`` entry point.

Protocol (docs/remote-service.md): JSON ``POST`` requests under ``/v1/``, the
caller's jiuding token in ``X-Jrun-Airs-Token``; errors are
``{"error": "..."}`` with 400/401/403/404/413/500.
"""

import argparse
import json
import logging
import shutil
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jrun_server.access import AuthError, Forbidden
from jrun_server.runner import JrunError, JrunRunner
from jrun_server.service import BadRequest, Service
from jrun_server.settings import Settings

TOKEN_HEADER = "X-Jrun-Airs-Token"
MAX_BODY = 2 * 1024 * 1024
ROUTES = {
    "/v1/whoami": lambda s, token, body: s.whoami(token),
    "/v1/submit": lambda s, token, body: s.submit(token, body),
    "/v1/jobs/status": lambda s, token, body: s.status(token, body),
    "/v1/jobs/stop": lambda s, token, body: s.stop(token, body),
    "/v1/jobs/remove": lambda s, token, body: s.remove(token, body),
}

log = logging.getLogger("jrun_server")


def make_handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "jrun-server/1"

        def log_message(self, fmt, *args):  # route access logs through logging
            log.info("%s %s", self.address_string(), fmt % args)

        def _reply(self, code: int, data: dict):
            payload = json.dumps(data, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == "/healthz":
                self._reply(200, {"ok": True})
            else:
                self._reply(404, {"error": f"unknown path {self.path}"})

        def do_POST(self):
            route = ROUTES.get(self.path)
            if route is None:
                self._reply(404, {"error": f"unknown path {self.path}"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._reply(413, {"error": "request body too large"})
                return
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
            except ValueError as e:
                self._reply(400, {"error": f"invalid JSON body: {e}"})
                return
            token = self.headers.get(TOKEN_HEADER)
            try:
                self._reply(200, route(service, token, body))
            except BadRequest as e:
                self._reply(400, {"error": str(e)})
            except AuthError as e:
                self._reply(401, {"error": str(e)})
            except Forbidden as e:
                self._reply(403, {"error": str(e)})
            except JrunError as e:
                log.warning("%s failed: %s", self.path, e)
                self._reply(500, {"error": str(e)})
            except Exception as e:  # noqa: BLE001 - never drop the connection silently
                log.exception("%s crashed", self.path)
                self._reply(500, {"error": f"internal error: {e}"})

    return Handler


def startup_checks(settings: Settings, runner: JrunRunner, which=shutil.which) -> dict:
    """Fail fast on a misconfigured service; returns the service account."""
    if which(settings.jrun_bin) is None:
        raise SystemExit(f"jrun-server: '{settings.jrun_bin}' not found on PATH")
    if which("airsctl") is None:
        raise SystemExit("jrun-server: airsctl not found on PATH")
    try:
        account = runner.run_json(["whoami", "--json"])
    except JrunError as e:
        raise SystemExit(f"jrun-server: the service key (AIRS_AK/AIRS_SK) does not work: {e}")
    joined = {p.get("name") for p in account.get("projsets") or []}
    missing = [ps for ps in settings.project_sets if ps not in joined]
    if missing:
        raise SystemExit(f"jrun-server: service account '{account.get('alias')}' is not a "
                         f"member of {', '.join(missing)}")
    return account


def main(argv=None):
    parser = argparse.ArgumentParser(prog="jrun-server",
                                     description="jrun remote-submission service")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--data", default=None, help="data directory (default /data)")
    parser.add_argument("--skip-checks", action="store_true",
                        help="skip the startup checks (tests/debugging)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = Settings.from_env()
    except ValueError as e:
        raise SystemExit(f"jrun-server: {e}")
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    if args.data:
        from pathlib import Path
        settings.data_dir = Path(args.data)
    runner = JrunRunner(settings.jrun_bin, timeout=settings.jrun_timeout)
    if not args.skip_checks:
        account = startup_checks(settings, runner)
        log.info("submitting as %s (id=%s) into %s; categories: %s",
                 account.get("alias"), account.get("id"),
                 ", ".join(settings.project_sets), ", ".join(settings.categories))
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    service = Service(settings, runner)
    httpd = ThreadingHTTPServer((settings.host, settings.port), make_handler(service))
    log.info("listening on %s:%d", settings.host, settings.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
