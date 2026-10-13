# Sandbox image for running target project tests. Decisions: docs/adr/0005-local-sandbox-hardening.md
# No apt: every download is pinned by checksum, and the JVMs carry their own time zone data.

ARG UBUNTU_DIGEST=sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55

FROM ubuntu@${UBUNTU_DIGEST} AS fetch
ADD --checksum=sha256:9c70e102f527ac674ac2fe9c7d47b9a04e2d19842ba5ab8e9b33f368bbadfaea \
    https://github.com/adoptium/temurin8-binaries/releases/download/jdk8u504-b01/OpenJDK8U-jdk_x64_linux_hotspot_8u504b01.tar.gz \
    /fetch/jdk8.tgz
ADD --checksum=sha256:5c3f68887c325d36d852ba534303e1f5f1f5cae7d6cc1e951d73e0d8e98a058d \
    https://github.com/adoptium/temurin11-binaries/releases/download/jdk-11.0.32.1%2B1/OpenJDK11U-jdk_x64_linux_hotspot_11.0.32.1_1.tar.gz \
    /fetch/jdk11.tgz
ADD --checksum=sha256:3808d1d15e3ec6bd5b84057fb5d84c33d8a1536a258146bcea2e603fc726e08e \
    https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.20.1%2B1/OpenJDK17U-jdk_x64_linux_hotspot_17.0.20.1_1.tar.gz \
    /fetch/jdk17.tgz
# Apache publishes SHA-512 only; this SHA-256 was computed from a tarball that passed its SHA-512.
ADD --checksum=sha256:80ffca22aed9e8b9713a232f3394fd81d7f20322df75efdb2b047dbd3e3a23bb \
    https://archive.apache.org/dist/maven/maven-3/3.9.16/binaries/apache-maven-3.9.16-bin.tar.gz \
    /fetch/maven.tgz
RUN set -eu; \
    for v in 8 11 17; do mkdir -p /opt/jdk/$v; tar -xzf /fetch/jdk$v.tgz -C /opt/jdk/$v --strip-components=1; done; \
    mkdir -p /opt/maven; tar -xzf /fetch/maven.tgz -C /opt/maven --strip-components=1

ARG DEPENDENCY_PLUGIN=org.apache.maven.plugins:maven-dependency-plugin:3.11.0
RUN set -eu; \
    for artifact in edu.illinois.cs:idflakies-maven-plugin:2.0.0 edu.illinois:nondex-maven-plugin:2.2.5; do \
      JAVA_HOME=/opt/jdk/17 /opt/maven/bin/mvn -B -q --strict-checksums -Dmaven.repo.local=/opt/m2-tail \
        ${DEPENDENCY_PLUGIN}:get -Dartifact=$artifact; \
    done; \
    cd /opt/m2-tail; \
    echo "cb7c1c531fe08467f3249edac8d676e469e19a4a97f5b3e3dad3af1475064a54  edu/illinois/cs/idflakies-maven-plugin/2.0.0/idflakies-maven-plugin-2.0.0.jar" | sha256sum -c -; \
    echo "4d1441d0fdbfc6b8c47ae368af7aabac2627bbb26db90053b6c02d61f99470c6  edu/illinois/nondex-maven-plugin/2.2.5/nondex-maven-plugin-2.2.5.jar" | sha256sum -c -; \
    # Maven only serves an artifact offline to the repository id it came from unless these
    # markers are gone, and target projects may name Central differently.
    find /opt/m2-tail -name '_remote.repositories' -delete

FROM ubuntu@${UBUNTU_DIGEST}
RUN set -eu; \
    if id ubuntu >/dev/null 2>&1; then userdel -r ubuntu; fi; \
    groupadd --gid 10001 sandbox; \
    useradd --uid 10001 --gid 10001 --home-dir /home/sandbox --create-home --shell /usr/sbin/nologin sandbox

COPY --from=fetch /opt/jdk /opt/jdk
COPY --from=fetch /opt/maven /opt/maven
COPY --from=fetch /opt/m2-tail /opt/m2-tail
COPY entrypoint.sh /usr/local/bin/rootflake-entrypoint
RUN set -eu; \
    chmod 0555 /usr/local/bin/rootflake-entrypoint; \
    chmod -R a-w /opt/jdk /opt/maven /opt/m2-tail; \
    find / -xdev -perm /6000 -type f -exec chmod a-s {} +

ENV PATH=/opt/maven/bin:$PATH \
    MAVEN_OPTS=-Xmx768m \
    ROOTFLAKE_JDK=17
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/rootflake-entrypoint"]
