# jrun remote-submission service.
#
# Build context: this directory (server/). Copy the airsctl binary from a
# jiuding pod first:   cp /client-tools/airsctl server/airsctl
#
#   docker build -t jrun-server server/
#   docker build --build-arg JRUN_REF=<branch-or-tag> -t jrun-server server/
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The latest jrun CLI (the service only drives the `jrun` command).
ARG JRUN_REF=main
ARG JRUN_SOURCE=git+https://github.com/AISafetyHub/jrun@${JRUN_REF}
RUN pip install --no-cache-dir "${JRUN_SOURCE}"

COPY airsctl /usr/local/bin/airsctl
RUN chmod 0755 /usr/local/bin/airsctl

COPY pyproject.toml /opt/jrun-server/
COPY jrun_server /opt/jrun-server/jrun_server
RUN pip install --no-cache-dir /opt/jrun-server

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

ENV JRUN_AIRSCTL_GATEWAY=http://platform-multi.baai.ac.cn \
    JRUN_SERVE_DATA=/data \
    JRUN_SERVE_PORT=8080 \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('JRUN_SERVE_PORT','8080'))"
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["jrun-server"]
