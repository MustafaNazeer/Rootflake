#!/bin/bash
# Runs inside the download container only, behind the allowlist proxy.
set -eu
dependency_plugin=org.apache.maven.plugins:maven-dependency-plugin:3.11.0
mvn "${dependency_plugin}:go-offline" test-compile
# go-offline does not fetch the Surefire provider, which Surefire resolves only when it
# runs tests, so fetch every provider for each Surefire version the project resolved.
surefire_dir=/m2/repo/org/apache/maven/plugins/maven-surefire-plugin
if [ -d "$surefire_dir" ]; then
  for version_dir in "$surefire_dir"/*/; do
    version=$(basename "$version_dir")
    if ! [[ "$version" =~ ^[0-9]+(\.[0-9]+){1,3}(-M[0-9]+)?$ ]]; then
      echo "rootflake-fetch: skipping unexpected Surefire version '$version'" >&2
      continue
    fi
    for provider in surefire-junit3 surefire-junit4 surefire-junit47 surefire-junit-platform surefire-testng; do
      mvn -q "${dependency_plugin}:get" "-Dartifact=org.apache.maven.surefire:${provider}:${version}" \
        || echo "rootflake-fetch: ${provider} ${version} not available" >&2
    done
  done
fi
