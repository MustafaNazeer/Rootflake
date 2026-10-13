#!/bin/bash
set -eu
if [ "$#" -eq 0 ]; then
  echo "rootflake-entrypoint: no command given" >&2
  exit 64
fi
case "${ROOTFLAKE_JDK:-}" in
  8|11|17) ;;
  *) echo "rootflake-entrypoint: unsupported ROOTFLAKE_JDK '${ROOTFLAKE_JDK:-}'" >&2; exit 64 ;;
esac
export JAVA_HOME="/opt/jdk/${ROOTFLAKE_JDK}"
export PATH="${JAVA_HOME}/bin:${PATH}"
# RLIMIT_NPROC counts every process of this UID on the host, so it would couple concurrent
# sandboxes under runc. Use it only where no cgroup pids limit exists (Cloud Run).
pids_max=$(cat /sys/fs/cgroup/pids.max 2>/dev/null || echo max)
if [ "$pids_max" = "max" ]; then
  ulimit -u "${ROOTFLAKE_MAX_PROCS:-1024}"
fi
exec "$@"
