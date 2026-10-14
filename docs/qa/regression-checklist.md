# Regression checklist

What to run, and what must hold, before a change to the sandbox runner in `src/rootflake/sandbox/` is merged. It covers the runner as it exists today: the container argument builder and checker, the Surefire report reader and parser, the single test executor, the dependency download path with its allowlist proxy, the workspace copy manager, and the runc versus runsc measurement. Items for code that does not exist yet are listed at the end so they stay visible.

## Commands

From the repository root:

```
uv run ruff check
uv run ruff format --check
uv run pytest -q          # run it three times in a row
uv run pytest -q -m docker
docker ps -a --filter name=rootflake --format '{{.Names}}'
docker network ls --filter name=rootflake --format '{{.Name}}'
```

The tests marked `docker` need a local Docker daemon and the `rootflake-sandbox:dev` image. They skip without them, and a skip is not a pass: on a machine that has both, they must run.

## Suite

- [ ] `ruff check` and `ruff format --check` report nothing.
- [ ] Three consecutive `pytest` runs report the same number of passed tests, with no failures, errors, or unexpected skips. Any test whose result differs between runs is a defect in the suite, fixed before merge.
- [ ] `pytest -m docker` runs both workspace tests against the real image and they pass.
- [ ] After the suite, the two `docker` listing commands print nothing: no container or network named `rootflake` survives a run.

## Run outcomes

- [ ] A run counts as passed only when Maven exits 0 and the Surefire report has exactly one matching `testcase` with no `failure`, `error`, or `skipped` child (`tests/unit/test_runner_results.py`, `test_pass_needs_exit_status_and_report_to_agree`).
- [ ] A run counts as failed only when Maven exits 1 and the report says failed, and a failed run is never retried (`tests/unit/test_executor.py`, `test_a_failing_run_is_not_retried`).
- [ ] Every other combination (missing, skipped, ambiguous, or unreadable report; exit status and report disagreeing; any other exit status) is infrastructure, and an infrastructure result is retried exactly once (`test_an_infrastructure_result_is_retried_once`, `test_two_infrastructure_results_end_as_infrastructure`, `test_missing_report_file_is_infrastructure`).
- [ ] A run past its wall time limit stops its container, is retried once on a fresh container name, and ends as timeout, never as failed (`test_a_timeout_stops_the_container_and_is_retried_once`, `test_each_attempt_uses_a_fresh_container_name`, `test_docker_runner_kills_a_process_that_outlives_its_timeout`).
- [ ] The timeout must lie in (0, 6 hours] (`test_timeout_must_be_bounded`).
- [ ] A retry is judged on its own result: a timeout followed by a pass ends as passed, and an infrastructure result followed by a failure ends as failed (`test_a_timeout_then_a_pass_ends_as_passed`, `test_an_infrastructure_result_then_a_failure_ends_as_failed`).
- [ ] A report whose identity, ctime and size are unchanged since before the attempt reads as missing, so a stale pass or failure left by an earlier run, or by the first attempt of a retry, is never counted (`test_a_stale_passing_report_is_not_a_pass`, `test_a_stale_failing_report_is_not_a_failure`, `test_the_retry_ignores_the_report_its_first_attempt_left`, and the stamp tests in `tests/unit/test_report_reader.py`).
- [ ] The container arguments are checked before anything runs (`test_run_test_checks_the_arguments_before_running_anything`).

## Reports written by code under test

- [ ] Any DTD, entity declaration, or DOCTYPE makes a report unreadable, including one hidden behind UTF-16 or a UTF-8 BOM (`tests/unit/test_runner_results.py`, `tests/unit/test_xmlsafe.py`).
- [ ] Reports over 8 MiB and malformed reports are unreadable; a failure nested in another `testcase` does not leak into the target test.
- [ ] The report reader refuses symlinked files and directories, FIFOs, and paths outside the workspace (`tests/unit/test_report_reader.py`).

## Captured output

- [ ] Output keeps at most the first 16 KiB and the last 48 KiB of each stream, marks the cut, and strips terminal controls, C1 controls, and bidi marks (`tests/unit/test_executor.py`, the `sanitize_output` and `DockerRunner` tests).
- [ ] Invalid UTF-8 never raises.

## Container arguments

- [ ] Built arguments pass the checker, and the checker rejects every forbidden flag, any missing or replaced hardening flag, a third mount, a mount source outside the managed roots or through a symlink, an unpinned or unexpected image, and unchecked environment values (`tests/unit/test_container_args.py`).
- [ ] Offline arguments fail the download check and download arguments fail the offline check; proxy variables are refused in offline runs.

## Dependency download

- [ ] A download ends as ok, failed, timeout, or infrastructure, and the proxy, both networks, and the download container are removed on every path, including when a removal itself fails or raises (`tests/unit/test_download.py`).
- [ ] An unexpected member on the internal network stops the download.
- [ ] The proxy tunnels only allowlisted host and port pairs to public IPv4 addresses, refuses plain HTTP, oversized headers, silent clients, and hanging lookups, and caps connections (`tests/unit/test_proxy.py`).
- [ ] The POM allowlist reads only HTTPS repositories, skips symlinks and `.git`, refuses DTDs, and caps hosts, POM count, and POM size (`tests/unit/test_pom_repos.py`).

## Workspace copies

- [ ] Copy, unlock, and remove containers run only in their exact hardened form, and every failure, timeout, or runner error is recorded rather than raised (`tests/unit/test_workspace.py`).
- [ ] A download copy is never handed out for test runs, and keyed directories with the wrong mode, owner, or a planted symlink are refused.

## Runtime measurement

- [ ] Only fixture projects under `tests/fixtures/maven/` can be measured, by name, with no symlinks anywhere in the tree (`tests/unit/test_measure.py`).
- [ ] The Maven repository a measurement loads is derived from the fixture's content hash and filled only by its own download; any other repository is refused, and a failed download stops the measurement.
- [ ] Normal runs cannot choose a runtime; only the measurement runner swaps the single checked `--runtime` value.
- [ ] Retried runs are counted but left out of timing statistics.

## Not covered yet

These must gain tests before the code that needs them lands.

- A build failure has no outcome of its own. A Maven build failure today ends as infrastructure.
- The runner records a timeout as its own outcome; the gate that counts it as infrastructure does not exist yet.
- Budget exhaustion, patch validation (empty, malformed, or out of workspace patches), the same JVM double run, the time zone sweep, and the fresh state reset between reruns.
- Integration tests under runsc.
