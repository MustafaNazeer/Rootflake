# Allowlist CONNECT proxy for the dependency download network. Standard library only.
FROM python@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170
COPY src/rootflake/sandbox/proxy.py /app/proxy.py
RUN set -eu; \
    chmod 0444 /app/proxy.py; \
    find / -xdev -perm /6000 -type f -exec chmod a-s {} +
USER 10002:10002
ENTRYPOINT ["python", "-I", "/app/proxy.py"]
