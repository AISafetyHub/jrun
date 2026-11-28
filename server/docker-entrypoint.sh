#!/bin/sh
# Make airsctl behave as on a jiuding pod: it reads its credentials from
# /etc/accesskey/{user-ak,user-sk} (base64) and its gateway (plus default
# project context) from /etc/downwardapi/annotations. jrun additionally
# passes -u/-P/-p per call (JRUN_AIRSCTL_GATEWAY).
set -eu

: "${AIRS_AK:?AIRS_AK is required}"
: "${AIRS_SK:?AIRS_SK is required}"
: "${JRUN_AIRSCTL_GATEWAY:=http://platform-multi.baai.ac.cn}"
export JRUN_AIRSCTL_GATEWAY

mkdir -p /etc/accesskey /etc/downwardapi
umask 077
printf '%s' "$AIRS_AK" | base64 | tr -d '\n' > /etc/accesskey/user-ak
printf '%s' "$AIRS_SK" | base64 | tr -d '\n' > /etc/accesskey/user-sk
umask 022
{
  echo "gateway-base-url=\"${JRUN_AIRSCTL_GATEWAY}\""
  if [ -n "${JRUN_AIRSCTL_PROJ_ID:-}" ]; then echo "proj.baai.ac.cn/id=\"${JRUN_AIRSCTL_PROJ_ID}\""; fi
  if [ -n "${JRUN_AIRSCTL_PROJSET_ID:-}" ]; then echo "projset.baai.ac.cn/id=\"${JRUN_AIRSCTL_PROJSET_ID}\""; fi
} > /etc/downwardapi/annotations

exec "$@"
