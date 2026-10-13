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
if [ -n "${ROOTFLAKE_PROXY:-}" ]; then
  if ! [[ "$ROOTFLAKE_PROXY" =~ ^(rootflake-px-[a-z0-9]{8,40}):([0-9]{1,5})$ ]]; then
    echo "rootflake-entrypoint: malformed ROOTFLAKE_PROXY" >&2
    exit 64
  fi
  mkdir -p "$HOME/.m2"
  cat > "$HOME/.m2/settings.xml" <<SETTINGS
<settings><proxies>
<proxy><id>https</id><active>true</active><protocol>https</protocol><host>${BASH_REMATCH[1]}</host><port>${BASH_REMATCH[2]}</port></proxy>
<proxy><id>http</id><active>true</active><protocol>http</protocol><host>${BASH_REMATCH[1]}</host><port>${BASH_REMATCH[2]}</port></proxy>
</proxies></settings>
SETTINGS
fi
export JAVA_HOME="/opt/jdk/${ROOTFLAKE_JDK}"
export PATH="${JAVA_HOME}/bin:${PATH}"
# RLIMIT_NPROC counts every process of this UID on the host, so it would couple concurrent
# sandboxes under runc. Use it only where no cgroup pids limit exists (Cloud Run).
pids_max=$(cat /sys/fs/cgroup/pids.max 2>/dev/null || echo max)
if [ "$pids_max" = "max" ]; then
  ulimit -u "${ROOTFLAKE_MAX_PROCS:-1024}"
fi
exec "$@"
