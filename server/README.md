# jrun-server

The jrun remote-submission service. It runs with one account's AK/SK, for example a
full-time employee's key that is allowed to use the **public** pool. It submits jobs for
callers who prove their jiuding identity with their own token, after it checks that they
can access the target project set and queue. Every platform action goes through the
`jrun` CLI (`--json`); this package does not import jrun.

Full documentation (protocol, access checks, security): [`../docs/remote-service.md`](../docs/remote-service.md).

## Deploy

```bash
cp /client-tools/airsctl ./airsctl     # from a jiuding pod (gitignored)
cp .env.example .env                   # AIRS_AK / AIRS_SK / JRUN_SERVE_PROJECT_SETS
docker compose up -d --build           # JRUN_REF=<branch> to pin the jrun version
```

or

```bash
docker build -t jrun-server .
docker run -d -p 8080:8080 -v jrun-data:/data \
  -e AIRS_AK=... -e AIRS_SK=... -e JRUN_SERVE_PROJECT_SETS=baai-safety jrun-server
```

Put TLS in front of it: callers send their 2-hour platform token.

## Use it from jrun

Add a `remote:` block to the config. Public jobs then go through the service, and
everything else is submitted locally as before:

```yaml
remote:
  url: https://jrun-svc.example.com
```

## Develop

```bash
uv run --extra dev pytest              # in server/
cd .. && uv run pytest server/tests    # also checks allowlists against jrun's models
```
