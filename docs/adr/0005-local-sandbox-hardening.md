# ADR 0005: Local sandbox hardening

Status: Proposed
Date: 2026-10-12

The direction below was agreed on 2026-10-12. This ADR moves to Accepted only after the gVisor measurement in Decision 1 passes.

## Context

Rootflake builds and runs the tests of open source Java projects it did not write, and applies patches an LLM wrote. The [threat model](../security/threat-model.md) lists what can go wrong and the [sandbox hardening checklist](../security/sandbox-hardening-checklist.md) lists the controls (`SH-NN`) that answer it. Four questions were left open there. This ADR settles three of them for **local Docker execution only**:

* **OD-2.** How strong the local container runtime needs to be, given that plain Docker containers share the host kernel (TM-02).
* **OD-3.** Which files a patch may touch, in particular build files (TM-08, and the shortcut checker in the gate).
* **OD-4.** Whether, and how, network egress is restricted during the dependency download step (TM-01, TM-05).

It also fixes the exact `docker run` flag set the runner uses, the relation between JVM heap and container memory, the starting resource caps, and the tests that must pass inside the sandbox before any target project runs.

OD-1, cutting the network for target code on Cloud Run Jobs, is a separate decision and is not covered here. Nothing in this ADR changes the rule that no target test code runs on Cloud Run until OD-1 is decided and tested.

### The machine this applies to

Bulk sandbox work runs on a personal laptop. It was inspected on 2026-10-12 with read only commands (`docker info`, `dpkg -l`, `sysctl`, `id`, `ls`); nothing was installed or reconfigured.

| Fact | Value |
|---|---|
| OS and kernel | Ubuntu 25.10, Linux 6.17.0-41-generic, x86_64 |
| CPU and memory | 8 CPUs, 14.9 GiB |
| Docker | Ubuntu's `docker.io` package, 29.1.3; `runc` 1.3.4; `containerd` 2.2.1 |
| Docker runtimes registered | `io.containerd.runc.v2`, `runc` (default `runc`); no `/etc/docker/daemon.json` |
| Docker security options | `apparmor`, `seccomp` (builtin profile), `cgroupns`; no `userns`, not rootless |
| cgroups | v2, systemd driver; root controllers `cpuset cpu io memory hugetlb pids rdma misc dmem`; controllers delegated to the user slice: `cpu memory pids` |
| gVisor | not installed (`which runsc` finds nothing) |
| Rootless prerequisites | `/etc/subuid` and `/etc/subgid` give the user 65536 IDs; `uidmap` (which provides `newuidmap`) is not installed; Ubuntu's `docker.io` ships no `dockerd-rootless-setuptool.sh` |
| User namespaces | `kernel.apparmor_restrict_unprivileged_userns = 1` |
| KVM | `/dev/kvm` present, user in the `kvm` group, hardware virtualization flags present, not itself a VM |
| Docker access | the user is in the `docker` group |

The last row matters on its own. Docker's post install guide warns: "The `docker` group grants root-level privileges to the user." Any process running as this user, including Rootflake's pipeline process, can therefore get root on the host through the Docker socket. That is not something a container runtime choice fixes; it is recorded under Consequences.

## Decision 1 (OD-2): local runtime strength

### Options

Every option below **includes every checklist control** (SH-01 to SH-12, SH-17 to SH-20). The question is only what is added on top. The flag set in Decision 4 is the same for all of them apart from `--runtime`.

#### Option A: plain Docker (`runc`) with every checklist control

* **What it adds:** nothing beyond the checklist. The container runs as a non root user, with every capability dropped, `no-new-privileges`, Docker's default seccomp and AppArmor profiles, a read only root filesystem, no network during tests, and cgroup caps.
* **Install:** nothing; it is what is on the machine.
* **JVM and Maven compatibility:** native. This is the configuration the JDKs, Maven, iDFlakies and NonDex are normally run under.
* **Performance:** native.
* **What it leaves open:** the container's system calls go straight to the host kernel, filtered only by the seccomp profile. A kernel exploit reachable through the allowed system calls escapes the container (TM-02 residual risk as published). Docker's own security page notes that "By default Docker drops all capabilities except those needed", but this does not reduce the kernel's attack surface for system calls that remain allowed.

#### Option B: gVisor (`runsc`) as a Docker runtime, on top of every checklist control

* **What it adds:** gVisor's application kernel (the Sentry) handles the container's system calls itself instead of passing them to the host kernel. gVisor's security guide states its "primary design goal is to minimize the System API attack vector through multiple layers of defense", that "the Sentry is not permitted to open new files, create new sockets or do many other interesting things on the host", and that file access goes through a separate Gofer process. It directly targets the TM-02 residual that Option A leaves open.
* **What it does not add:** gVisor's security guide also states that it "does not provide protection against hardware side channels", relies on host cgroups for resource limits, and that "A sandbox is not a substitute for a secure architecture." Every checklist control therefore stays.
* **Install (needs root):** gVisor documents an apt repository and a manual tarball. Both use `sudo` for every system step: adding the signing key and source list, `sudo apt-get install -y runsc` (or extracting into `/usr/local/bin`), then `sudo runsc install` to register the runtime and a Docker restart. The Docker quick start says `runsc install` "will install a docker runtime named "runsc" by default" by adding a runtime entry to the Docker configuration (for example `/etc/docker/daemon.json`), and "You must restart the Docker daemon after installing the runtime." Requirements: x86_64 or ARM64 and Linux 5.6 or newer; this machine qualifies. The default runtime stays `runc`; Rootflake selects gVisor per container with `--runtime=runsc`.
* **Platform:** gVisor's platform guide says "By default, the `systrap` platform is selected." KVM is an alternative recommended on bare metal; this machine has `/dev/kvm`, but `systrap` needs no extra setup and is the starting point.
* **JVM and Maven compatibility:** gVisor's compatibility page says its releases "go through the regression tests of popular language runtimes (Python, Java, Node.js, PHP, Go)" and "The only real way to know if it will work is to try." Its repository keeps an exclusion list for that suite, `test/runtimes/exclude/java21.csv` (122 OpenJDK tests at commit `10e1efc`). By directory, the largest groups are `java/net/MulticastSocket` (12), jpackage (21 across several directories), `sun/tools/jhsdb` (5), `com/sun/jdi` (4), and `sun/management/jmxremote/bootstrap` (4). Pull requests excluded failing `ConnectTimeout` tests (#12406, which GitHub shows as closed rather than merged, though both `ConnectTimeout` tests are on the list at that commit) and disabled tests that need `IPV6_MULTICAST_IF` support (#11597 and #11602, both merged). Only a Java 21 list exists, so **JDK 8, 11 and 17 under gVisor are not covered by gVisor's own regression suite and are unverified.** Searching the issue tracker for "maven" found no compatibility report. Implications for Rootflake:
  * Tests that use multicast, debugger attach (JDI), JMX remote, or other network features under test may behave differently under gVisor than under `runc`. A flaky test whose behavior changes with the runtime would make a gVisor run measure something different from what upstream CI sees.
  * A closed 2026 issue (#13301) reports Docker containers started with `--runtime io.containerd.runsc.v1` (the containerd shim, not the `runsc` runtime that `runsc install` registers) failing after each reboot on Ubuntu 24.04 with a cgroup setup error under `system.slice`; in the report, the `runsc install` path with `--runtime runsc` succeeded. It was closed by pull requests #13384 and #13390, merged 2026-06-08, and the GitHub compare API shows #13384's merge commit as an ancestor of `release-20261005.0`.
* **Performance:** gVisor's performance guide says system call interception "will principally impact applications that are system call bound", that raw disk I/O has no significant fundamental overhead but "The high costs of VFS operations can manifest in benchmarks" that do many small file operations, and that networking "is mostly bound by implementation costs". It names no build workload. A Maven build opens many jar and class files, so it is plausibly in the expensive group, but **no number for Maven or JVM test runs is documented; the overhead is unmeasured.**
* **Filesystem detail:** gVisor's filesystem guide says a rootfs overlay (`--overlay2=root:self`) "is typically enabled by default in runsc for performance". How this interacts with Docker's `--read-only` must be checked by the SH-06 test under `runsc`, not assumed.

#### Option C: rootless Docker, or user namespace remapping, on top of every checklist control

These two change who container processes are on the host, not what kernel they talk to.

* **Rootless mode.** Docker's documentation says rootless mode runs "the Docker daemon and containers" inside a user namespace, and installation "does not require root privileges" **if the prerequisites are met**. On this machine they are not: `newuidmap` and `newgidmap` ("provided by the `uidmap` package on most distributions") are not installed, and Ubuntu's `docker.io` package does not ship `dockerd-rootless-setuptool.sh`; Docker says to install `docker-ce-rootless-extras` in that case, which is not in Ubuntu's archive. Rootless Docker's troubleshooting page also says "Ubuntu 24.04 and later enables restricted unprivileged user namespaces by default", and this machine has that restriction on, so an AppArmor profile would be needed. Installing `uidmap` and adding that profile both need root. Limitations documented by Docker: AppArmor is unsupported in rootless mode, cgroup flags work only "when running with cgroup v2 and systemd" (this machine delegates `cpu memory pids` to the user, so `--memory`, `--cpus` and `--pids-limit` would be honored), and rootless mode runs a second daemon with its own image store.
* **User namespace remapping (`userns-remap`).** Set in `/etc/docker/daemon.json`, needs root, and the daemon keeps running as root. Container root maps to an unprivileged subordinate UID range on the host. Docker advises "it is best to enable this feature on a new Docker installation" because it masks existing images and containers, and it applies to the whole daemon, not just Rootflake.
* **JVM and Maven compatibility:** no documented incompatibility was found. Rootless networking for the download step uses a user mode network stack; its throughput cost is not documented on the pages checked and is **unverified**.
* **What they add for Rootflake:** little. The sandbox already runs as a non root user with no capabilities and `no-new-privileges`, so remapping container root protects a user that Rootflake never runs as. Neither option changes the kernel attack surface, so a kernel exploit (the actual TM-02 residual) is not stopped. Rootless mode would also remove the root equivalent Docker socket, which matters for the pipeline process (see Consequences), but a container escape under rootless lands as the laptop's own user, who owns the home directory with SSH keys and cloud configuration (asset A2). `userns-remap` lands as an unrelated subordinate UID instead, which is better for A2 but changes the daemon for every other project on the machine.

#### Option D (noted, not evaluated in depth): a dedicated virtual machine

Running the Docker daemon for sandbox work inside a local VM puts a hypervisor boundary between target code and the laptop, which is the boundary Cloud Run instances already have. This machine has KVM. It costs a VM image to maintain, memory set aside for the guest, and a second Docker daemon to drive. It stays available as the fallback if gVisor fails measurement.

### Comparison

| | A: `runc` | B: gVisor | C: rootless or `userns-remap` |
|---|---|---|---|
| Root needed to set up | no | yes (once) | yes on this machine (`uidmap`, AppArmor profile; or `daemon.json`) |
| Kernel attack surface (TM-02) | host kernel, seccomp filtered | Sentry in front of the host kernel | host kernel, seccomp filtered |
| Escape lands as | host user mapped to the container user | must first escape the Sentry | rootless: the laptop user; remap: a subordinate UID |
| Strengthens checklist controls | none added | SH-02, SH-06, SH-11, SH-19 (kernel boundary); relies on host cgroups for SH-07 to SH-09 | SH-03, SH-05 (host identity) |
| JVM and Maven compatibility | native | Java 21 regression tested by gVisor; JDK 8, 11, 17 unverified; known gaps in multicast, JDI, JMX remote | no documented issue found |
| Documented overhead | none | syscall and small file heavy work is the documented weak spot; Maven unmeasured | not documented for this workload |
| Other effects | none | one daemon restart at install | second daemon or daemon wide change |

### Decision

**Option B, gVisor as the runtime for every container that executes target project code** (the dependency download container and every test, iDFlakies and NonDex container), with every checklist control kept, **conditional on a measurement that has not happened yet.** Option A is not enough on its own because the upstream track runs projects nobody has vetted on a machine that holds personal data. Option C is not chosen because it does not address kernel exploits, which is the residual this decision is about.

Conditions and rules:

1. **Pinned release, installed from the release bucket.** `runsc` is pinned to point release `20261005.0` (tag `release-20261005.0`, published 2026-10-08). gVisor's install page says "apt installation of a specific point release is not supported", and that point releases are available from `https://storage.googleapis.com/gvisor/releases/release/${yyyymmdd}.${rc}/${ARCH}` for use with the manual install steps. So it is installed from `https://storage.googleapis.com/gvisor/releases/release/20261005.0/x86_64/gvisor.tar.zstd`, checked with `sha512sum -c` against `gvisor.tar.zstd.sha512` from the same directory, and extracted into `/usr/local/bin` as the install page describes. The published SHA-512 on 2026-10-12 was `27fed731b432d6a39d908956623fd273979fda66b3ccf4b9c12413ea417c516e18a9dc3cced403fb28efe495d64dce4251f51cf8c4d9f4a72f9833bd96b6bb35`. The `runsc` version is recorded with every run, alongside the sandbox image digest (SH-18).
2. **No silent fallback (SH-26).** The runner refuses to start a target container if the `runsc` runtime is not registered, and it checks after start that the container's `HostConfig.Runtime` (from `docker inspect`) is `runsc`. A project that cannot run under gVisor is recorded with an outcome that says so; it is never quietly rerun under `runc`.
3. **Compatibility and overhead measurement before acceptance.** Before this ADR moves to Accepted, every fixture Maven project and a sample of real corpus projects covering each supported JDK are built and run under both `runc` and `runsc`. The bar: identical outcomes to `runc` for every non flaky fixture test across the supported JDKs, and a median wall time for 50 reruns under `runsc` no more than twice the `runc` figure. If the bar fails, the fallback is Option D (a dedicated VM) with Option A inside it, decided in a follow up ADR, not Option A on the laptop.
4. **Runtime parity in reported results.** Every reported number states the runtime it was measured under. If a test's flakiness reproduces under one runtime and not the other, that is reported as a finding, not hidden.

## Decision 2 (OD-3): which files a patch may touch

### Options

* **Option 1: reject build files by default, with a recorded override list.** The patch validator (SH-15) rejects any change to `pom.xml` (at any depth), anything under `.mvn/` (which holds `maven.config`, `jvm.config` and `extensions.xml`), `mvnw` and `mvnw.cmd`, and any wrapper file. Overrides are entries in a committed file naming the project, the test, the exact path and the reason, added by hand after review, never by a runtime agent. In the offline eval, a test whose only correct fix needs a build file change ends as fix failed, and that count is reported.
* **Option 2: allow build files, with extra shortcut checks.** The shortcut checker parses the POM and `.mvn/` changes and rejects any change that can hide a failure instead of fixing it. From the Surefire 3.6.0 and Failsafe 3.6.0 parameter references, that includes at least: `rerunFailingTestsCount` (user properties `surefire.rerunFailingTestsCount`, `failsafe.rerunFailingTestsCount`), `skipAfterFailureCount`, `testFailureIgnore` (`maven.test.failure.ignore`), `skip` (`maven.test.skip`), `skipTests`, `skipITs`, `excludes`, `excludesFile`, `groups`, `excludedGroups`, `test`, `includes`, and `runOrder` (`surefire.runOrder`). It must also reject changes to `forkCount` and `reuseForks`, because running each test class in a fresh JVM can make an order dependent test pass without fixing it; `argLine` changes; `<properties>` entries that feed any of the above; new `-D` options in `.mvn/maven.config` or `.mvn/jvm.config`; and any new plugin, extension (`.mvn/extensions.xml`), repository, or dependency, since each one means new code and a new network download driven by an LLM written patch (TM-05, TM-08).

Option 2 is a parser for a moving target: every new plugin parameter or property indirection is a new hole, and the false negative cost is a fake fix in a reported number.

### Decision

**Option 1.** Build and wrapper files are rejected by default, and the override list starts empty. Option 2 is reconsidered only if the measurement below shows that a meaningful share of real fixes need build file changes; any override or Option 2 checker needs its own review before use.

### Measurement still owed

The fraction of IDoFT fixes that touch build files is **not yet measured.** The corpus builder can measure it from the merged fix diffs it already fetches:

1. For every `pr-data.csv` row with Status Accepted and a GitHub PR link, list the PR's files with `GET /repos/{owner}/{repo}/pulls/{pull_number}/files` (GitHub documents "a maximum of 3000 files" per response and `per_page` up to 100).
2. Classify each PR as touching test sources only, main sources, test resources, or build files (the path patterns in Option 1), using the `patch` text to separate build changes that only add a test dependency from changes to Surefire or Failsafe configuration.
3. Report the count and fraction for each class over all Accepted rows and over the sampled corpus, the fraction with a 95% Wilson interval, and the list of PRs in the build file class. Rows whose PR cannot be fetched are counted as such, not dropped.

## Decision 3 (OD-4): egress during the dependency download step

The download step needs the network, and resolving Maven plugins and build extensions can run their code while it is on (TM-05). The checklist already gives this container no credentials and nothing worth stealing; the question is where it may connect.

### Options

| Option | Local enforcement | Failure mode |
|---|---|---|
| **i. No restriction** | Default bridge network | Fails open. Code running during download can reach any internet host, the laptop's LAN, and host services reachable from the bridge. Builds never break for network reasons. |
| **ii. Allowlist proxy: Maven Central plus repositories declared in the project's own POM files** | Download container on an `--internal` Docker network whose only other member is a forwarding proxy (for example Squid, allowlisting by CONNECT target or TLS server name); the proxy is also on a normal bridge. Maven is pointed at the proxy through `settings.xml` `<proxies>`. Before download, the pipeline reads `<repositories>` and `<pluginRepositories>` from the POM files at the pinned commit, on the host, without executing anything, and builds the per project allowlist, which is recorded in the run log. | Fails closed: a connection to anything else, or a Maven misconfiguration that bypasses the proxy, has no route and errors. Projects whose dependencies come from a repository that is declared only somewhere the static read does not see (a parent POM fetched from a repository, a profile, a settings file) fail to download and are recorded as build failures. **Limit:** the allowlist is derived from untrusted input, so a project written to attack Rootflake can declare its own host. This stops generic payloads and poisoned dependencies phoning home, not a targeted repository. |
| **iii. Maven Central only** | Same as ii with a fixed allowlist of Maven Central's hosts; or a `settings.xml` mirror with `<mirrorOf>*</mirrorOf>` ("`*` matches all repo ids") pointing at Central, behind the same proxy. | Fails closed and is the narrowest. Any project that needs another repository fails to build, and the number of corpus projects lost this way is unknown. |

Facts the enforcement relies on, from Docker's `docker network create` reference: an internal network has "no default route" and "firewall rules are set up to drop all traffic to or from other networks", but "Communication with the gateway IP address (and thus appropriately configured host services) is possible." So host services listening on that gateway are reachable from the download container in options ii and iii, and every host service reachable from the default bridge is reachable in option i.

**Unverified, and decided by tests rather than assumed:** whether Docker's embedded DNS on an internal network resolves external names (a possible DNS exfiltration channel); whether one Maven `<proxy>` entry with `<protocol>http</protocol>` also proxies HTTPS requests (Maven's proxy guide does not say; a mistake here fails closed); and whether `runsc` networking behaves the same on an internal network.

### Decision

**Option ii**, with Maven Central always on the allowlist, HTTPS only, and the per project extras taken from the static POM read and recorded. Its main value is against TM-05 (poisoned dependencies and plugins reaching arbitrary hosts) and against accidental reach into the LAN. The download container also runs under gVisor (Decision 1) and with every other checklist control.

Two numbers decide whether this stays: how many corpus projects need any non Central repository, and how many fail under the proxy that build without it. Both are recorded by the corpus builder's build filter. If the second number is near zero, the extras list may be dropped (option iii) for a smaller attack surface.

## Decision 4: the `docker run` flag set

### Test, iDFlakies and NonDex containers

The runner builds this argument list in one function, and a separate checker accepts only this exact shape before any container starts: every flag as its own token, each in its expected count, with bounded values, mount sources under directories the runner manages, and only the environment keys listed here (SH-11, SH-12, SH-19). Values in angle brackets are filled per run; the numbers are the proposed starting caps from the next section, and memory is passed in MiB.

```
docker run --rm
  --runtime runsc
  --network none
  --user 10001:10001
  --cap-drop ALL
  --security-opt no-new-privileges=true
  --read-only
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=1g
  --tmpfs /home/sandbox:rw,nosuid,nodev,size=128m
  --mount type=bind,src=<workspace copy>,dst=/workspace
  --mount type=bind,src=<per project and commit Maven repository>,dst=/m2/tail,readonly
  --workdir /workspace
  --memory 4096m --memory-swap 4096m
  --cpus 2
  --pids-limit 1024
  --ulimit nofile=4096:4096
  --init
  --stop-timeout 10
  --log-opt max-size=<N>m --log-opt max-file=1
  --env HOME=/home/sandbox
  --env ROOTFLAKE_JDK=<8, 11 or 17>
  --env ROOTFLAKE_MAX_PROCS=<same value as --pids-limit>
  --env MAVEN_OPTS=-Xmx768m
  --env "MAVEN_ARGS=--batch-mode --offline -Dmaven.repo.local=/tmp/m2 -Dmaven.repo.local.tail=/m2/tail,/opt/m2-tail"
  --env "JAVA_TOOL_OPTIONS=-XX:MaxRAM=<container memory>m [-Duser.timezone=<zone>]"
  [--env TZ=<zone>]
  <sandbox image>@sha256:<digest>
  <command>
```

| Flag | Why | Source |
|---|---|---|
| `--rm` | "Automatically remove the container and its associated anonymous volumes when it exits", so nothing persists between runs (TM-06) | `docker run` reference |
| `--runtime runsc` | gVisor, Decision 1; "Runtime to use for this container" | `docker run` reference, gVisor Docker quick start |
| `--network none` | SH-01; with the `none` driver "only the loopback device is created" and "No IPv6 loopback address is configured" | none network driver page |
| `--user 10001:10001` | SH-03; format `<name|uid>[:<group|gid>]`. The UID matches the image's `USER`; the value is a proposal | `docker run` reference |
| `--cap-drop ALL` | SH-04 | `docker run` reference |
| `--security-opt no-new-privileges=true` | SH-05; "Disable container processes from gaining new privileges" | `docker run` reference |
| `--read-only` | SH-06; "Mount the container's root filesystem as read only" | `docker run` reference |
| `--tmpfs` | Scratch space under SH-06. tmpfs usage "counts against the container's memory cgroup limit". For the `tmpfs-size` option of `--mount type=tmpfs`, Docker documents that "If unset, the default maximum size of a tmpfs volume is 50% of the host's total RAM"; the page states no default for `--tmpfs`, so the size is always set explicitly. `suid` is a tmpfs default, hence `nosuid` and `nodev`. `/tmp` carries `exec` explicitly: a `--tmpfs` mount without options was observed to be `noexec`, which stopped Maven's own native library from loading (see Checks run so far), and Maven already runs compiled code from the writable workspace, so `noexec` on `/tmp` would add little. The home tmpfs keeps Docker's default | `docker run` reference, tmpfs mounts page |
| `--mount type=bind,...` | SH-11; exactly two host paths. `--mount` "does not automatically create a directory" if the source is missing, so a wrong path fails instead of creating one. `readonly` makes the repository read only; "Read-only mounts are made recursively read-only if kernel is v5.12 or later" (this kernel is 6.17) | bind mounts page |
| `--workdir /workspace` | Commands run from the workspace | `docker run` reference |
| `--memory` with `--memory-swap` equal | SH-07; equal values mean "the container doesn't have access to swap" | resource constraints page |
| `--cpus 2` | SH-08; "how much of the available CPU resources a container can use" | resource constraints page |
| `--pids-limit 1024` | SH-09. The cgroup v2 pids controller counts TIDs, so JVM threads count; fork and clone "return -EAGAIN" at the limit | `docker run` reference, Linux cgroup v2 documentation |
| `--ulimit nofile=4096:4096` | Bounds open files per process; syntax `<type>=<soft limit>[:<hard limit>]`. Value is a proposal | `docker run` reference |
| `--init` | SH-09; "Run an init inside the container that forwards signals and reaps processes" | `docker run` reference |
| `--stop-timeout 10` | SH-10; seconds `docker stop` waits before killing | `docker run` reference |
| `--log-opt max-size=<N>m, max-file=1` | The json-file driver's `max-size` "Defaults to -1 (unlimited)", so a noisy test could fill the disk (TM-03). N is bounded (1 to 100 MiB, 10 by default). The runner captures output itself and caps it (SH-17) | json-file logging driver page |
| `--env` (explicit values only) | SH-12; the runner passes `NAME=value` pairs and never a bare `NAME`, which would copy the host's value. The allowed keys are exactly `HOME`, `ROOTFLAKE_JDK` (the entrypoint sets `JAVA_HOME` and `PATH` from it and refuses any value but 8, 11 or 17), `ROOTFLAKE_MAX_PROCS` (used by the entrypoint only where no cgroup pids limit exists, SH-09), `MAVEN_OPTS` (Maven's own heap), `MAVEN_ARGS` (offline mode and the chained repository, "arguments passed to Maven before CLI arguments" since Maven 3.9.0), `JAVA_TOOL_OPTIONS` (the memory size every JVM uses, and the time zone during a sweep), and optionally `TZ`. Keys the runner computes cannot be overridden by a caller, and a duplicate key is rejected | `docker run` reference, Maven configuration page |
| image by digest | SH-18 | checklist SH-18 |

Never passed, and rejected by the unit test: `--privileged` (it "disables the default seccomp profile", the AppArmor profile and the SELinux label), `--cap-add`, `--device`, `--security-opt seccomp=unconfined` or any AppArmor override, `--network host` or any network other than `none`, `--pid host`, `--ipc host`, `--userns host`, `--volumes-from`, `-v` or `--mount` of anything except the two paths above, and any mount of `/var/run/docker.sock`.

### Maven repository during test runs

The downloaded repository is mounted read only (SH-06, SH-20). Maven writes into its local repository during normal operation, so a read only repository alone may break offline runs. Maven 3.9.0 added a chained local repository: `maven.repo.local.tail` is a "list of "tail" local repository paths" (Maven source, `DefaultRepositorySystemSessionFactory`, `@since 3.9.0`). The runner points `maven.repo.local` at an empty, writable directory on the `/tmp` tmpfs and `maven.repo.local.tail` at `/m2/tail`, with Maven in offline mode. **Whether Maven never writes to a tail repository is unverified**; the SH-06 test runs a full offline build with the tail mounted read only and must pass before this layout is used.

### Dependency download container

Same flags as above, with these differences: `--network <per run internal network>` instead of `none` (Decision 3); the Maven repository mounted writable at its own path; and its own disposable copy of the workspace, so nothing it writes reaches the copy that tests run in. The repository it fills is keyed by project and commit (SH-20) and mounted read only afterwards. Which Maven invocation fills the repository completely enough for offline test runs is a runner question settled by the SH-02 test.

### Workspace state between runs

Disk state written by one run persists in the workspace for the next run in the same workspace: files a test creates, build output under `target/`, and the iDFlakies and NonDex output directories. For order dependent (OD) and non idempotent outcome (NIO) tests this changes the outcome, so a series of reruns is only independent if that state is reset. The runner therefore keeps two copies:

1. **A clean snapshot.** After the patch is applied (SH-16) and the offline build succeeds, the runner saves the workspace as a read only snapshot on the host, keyed by project, commit, and patch.
2. **A run copy.** Each run gets a run copy made from that snapshot.

Two isolation modes, chosen per command by the caller:

* **Fresh state per run.** Every rerun starts a new container (`--rm` discards its `/tmp` and home tmpfs) on a new run copy from the clean snapshot, so nothing written by run k is visible to run k+1. This is the mode for categories where state between runs changes the outcome; which categories those are is a gate decision recorded with the gate, not here.
* **Shared state.** A series of reruns runs in one container and one run copy, which is cheaper and matches how a developer reruns a test locally. Its results are labelled as shared state runs.

Every reported rerun count records which mode it used. Copy cost per run is part of the throughput measurement.

### Running one test twice in one JVM (NIO)

A non idempotent outcome test passes on its first execution and fails when it runs again in the same JVM, so separate rerun processes never reveal it. The runner supports a command that executes one test twice in a single test JVM and reports both results. It runs in the same container under the same flags as every other command; how it invokes the test inside one JVM is a runner implementation detail and is tested on a fixture with a planted NIO test.

### Time zone sweep (TZD)

Time zone dependent tests are run under several time zones. For each zone the runner sets `TZ` in the container environment (added to the SH-12 allowlist) and adds `-Duser.timezone=<zone>` to `JAVA_TOOL_OPTIONS`, which the JVM tool interface specification describes as options that `JNI_CreateJavaVM` "will prepend" to the JVM's own options, so it reaches Maven's JVM and every forked test JVM without editing the project's `argLine`. Because these options come first, a project that sets `user.timezone` itself may override them; the specification does not say how conflicting options resolve, so the runner records the time zone each test JVM actually reports rather than assuming the sweep took effect. The list of zones is a gate decision.

### Wall time

The runner wraps every command in its own timeout. On expiry it runs `docker stop`, which waits `--stop-timeout` and then kills the container, and then confirms the container is gone. A timeout is retried once and then recorded as an infrastructure outcome, never as a failed fix (SH-10).

## JVM heap against container memory

A Maven test run puts at least two JVMs in the container: Maven's own, and the forked test JVM (Surefire's `forkCount` defaults to `1`; a project may raise it, and a value ending in "C" is "multiplied with the number of CPU cores"). Their heaps, metaspace (which by default "isn't limited"), thread stacks (1024 KB each by default on Linux x64), code cache, and the tmpfs contents all count against `--memory`.

The goal is that a test that leaks memory ends with a Java `OutOfMemoryError` inside the test JVM (a test failure the gate sees) rather than the kernel killing the container (an infrastructure outcome). The rule:

* **Every JVM in the container is told the container size.** JVM container support is on by default on Linux x64 (`UseContainerSupport`), and `MaxRAMPercentage` "The default value is 25 percent" of the maximum the JVM determines "as described in the `-XX:MaxRAM` option". cgroup v2 detection arrived in JDK 15 (JDK-8230305) and was backported to OpenJDK 11.0.16 and 8u372, so **the sandbox image ships JDK 8 at 8u372 or later and JDK 11 at 11.0.16 or later.** Those floors are necessary but were not sufficient on this machine: Temurin 8u504 still sized its heap from the host (see Checks run so far). The actual control is therefore `-XX:MaxRAM=<container memory>` in `JAVA_TOOL_OPTIONS`, which reaches Maven's JVM and every forked test JVM, so heap ergonomics never depend on cgroup detection. This also covers `runsc`, where cgroup detection is unverified.
* **Maven's own JVM** gets an explicit cap through `MAVEN_OPTS` ("parameters used to start up the JVM running Maven"), proposed `-Xmx768m`.
* **The forked test JVM** keeps the 25 percent default, about 1 GiB of a 4 GiB container, unless the project's own `argLine` or `.mvn/jvm.config` sets something else; the runner does not override a project's settings, since that would change the test's behavior. The container limit is the backstop.
* With two JVMs at about 0.75 GiB and 1 GiB of heap, about 2.25 GiB is left for non heap memory, a third JVM if `forkCount` is 2, and up to 1 GiB of `/tmp`. Whether that headroom is enough is measured, not assumed.

Every run records the limits each JVM detected (`-XshowSettings:system` output) and whether the container was OOM killed (`docker inspect` `State.OOMKilled`).

## Proposed starting caps (to be measured)

**These are starting values chosen to fit this laptop, not measurements.** They are measured on the fixture projects and a first sample of corpus projects before any corpus run, and changed by amending this ADR.

| Cap | Proposed value | Reasoning |
|---|---|---|
| Memory (`--memory`, `--memory-swap`) | 4 GiB | Two concurrent sandboxes use 8 of 14.9 GiB, leaving the rest for the host and the pipeline process |
| CPUs (`--cpus`) | 2 | Two concurrent sandboxes use 4 of 8 CPUs |
| Concurrent sandboxes | 2 | Follows from the two rows above |
| Process and thread count (`--pids-limit`) | 1024 | Counts threads; two JVMs plus a test's own thread pools |
| Open files (`--ulimit nofile`) | 4096 | Maven opens many jars at once |
| `/tmp` tmpfs | 1 GiB | Counts against memory |
| Home tmpfs | 128 MiB | Tests that write under `user.home` |
| Dependency download | 30 minutes | Once per project and commit |
| Build (compile and test compile, offline) | 15 minutes | |
| One test run | 5 minutes | Includes Maven startup |
| N reruns | 5 minutes per rerun, 6 hours per command | 500 reruns at even 30 seconds each is over 4 hours |
| iDFlakies, NonDex | 60 minutes each | |
| `--stop-timeout` | 10 seconds | |

## Tests required inside the sandbox

These run under the exact flag set above and the `runsc` runtime, on every change to the sandbox image or the runner, and every one must pass before any target project runs. They implement the **Verification** lines of the checklist.

| Control | Test |
|---|---|
| SH-26 | The runner refuses to start when `runsc` is missing; `docker inspect` of a started container reports `HostConfig.Runtime` as `runsc` |
| SH-01 | DNS resolution, a TCP connection to a public address, and a TCP connection to a private address all fail |
| SH-02 | A fixture whose build needs an artifact that was not fetched fails offline instead of downloading it |
| OD-4 proxy | From the download container: a host on the allowlist is reachable through the proxy; a host off it is refused; a direct connection that bypasses the proxy fails; external DNS resolution fails; no port on the network gateway accepts a connection |
| SH-03 | `id -u` is not 0 and equals the configured UID |
| SH-04 | `CapEff` in `/proc/self/status` is all zeros |
| SH-05 | `NoNewPrivs` is 1; the image contains no setuid or setgid files |
| SH-06 | Writes to `/usr`, `/etc`, the JDK and Maven directories fail; writes to `/workspace`, `/tmp` and the home tmpfs succeed; a write to `/m2/tail` fails; a full offline build with the chained repository passes |
| SH-07 | On each supported JDK, every JVM's maximum heap is derived from the passed `-XX:MaxRAM`, not from host memory; a fixture that allocates without bound ends with `OutOfMemoryError` or an OOM kill recorded as infrastructure, and the host stays responsive |
| SH-08 | A busy loop fixture stays within 2 CPUs, measured from the host |
| SH-09 | A fork bomb fixture and a thread bomb fixture are contained by `--pids-limit` under `runsc`, and the runner still returns a result (whether the host pids cgroup bounds guest threads under gVisor is unverified until this passes) |
| SH-10 | A test that sleeps forever ends at its timeout, is retried once, and is recorded as infrastructure |
| SH-11 | The mount list inside the container shows only the workspace, the repository, and the two tmpfs mounts; the argument builder's unit test passes |
| SH-12 | The container environment contains only allowlisted variables; a layer scan of the image finds no key files or tokens |
| SH-17 | A test that writes far more than the output cap is truncated by the runner, and the Docker log file stays under its size cap |
| SH-19 | The argument builder's unit test rejects every forbidden flag |
| SH-20 | Two fixture projects run in sequence; the second cannot see artifacts the first downloaded |
| Workspace reset | In fresh state mode, a fixture test that writes a marker file and fails if the marker exists passes on every rerun; in shared state mode it fails from the second run on |
| NIO command | A fixture with a planted NIO test passes the first execution and fails the second in the same JVM |
| TZD sweep | A fixture test that prints `TimeZone.getDefault()` reports each zone in the sweep |

## Checks run so far (2026-10-12, under `runc`, before gVisor was installed)

These were run on the laptop described above with the first build of the sandbox image and a one test Maven project. They settle some of the unverified points above for `runc` only; every one is repeated under `runsc` as part of the measurement.

* **JDK 8 does not see the cgroup v2 limit on this machine.** Temurin 8u504 logs "One or more required controllers disabled at kernel level" and sizes its default heap from the host (about 3.7 GiB of 14.9 GiB) inside a 4 GiB container. JDK 11.0.32.1 and 17.0.20.1 size it from the container (1 GiB). The runner therefore passes `-XX:MaxRAM=<container memory>` to every JVM through `JAVA_TOOL_OPTIONS` instead of relying on ergonomics.
* **`--tmpfs` without options is `noexec`.** Maven's own native library (jansi) failed to load from `/tmp`. The runner mounts `/tmp` with `rw,exec,nosuid,nodev`, so libraries that unpack native code to the temp directory work.
* **A read only tail repository works offline.** With `maven.repo.local` on `/tmp` and `maven.repo.local.tail` on the read only repository mount plus the image's plugin repository, an offline test run passed and the tail repository's file count did not change.
* **`dependency:go-offline` is not enough for test runs.** It does not fetch the Surefire provider (for example `surefire-junit4`), which Surefire resolves only when it runs tests. The download step also resolves every Surefire provider artifact for each Surefire version present in the repository.
* **The time zone sweep reaches the forked test JVM.** With `TZ` and `-Duser.timezone` in `JAVA_TOOL_OPTIONS`, the Surefire report recorded `user.timezone` as the requested zone.
* **Hardening flags hold under `runc`.** Effective capabilities were empty, `NoNewPrivs` was 1, writes to `/usr` failed, DNS failed with `--network none`, the process limit read 1024, and the image contains no setuid or setgid files.
* **The download network behaves as designed under `runc`.** On a Docker `--internal` network whose only other member is the allowlist proxy: an external name (`example.com`) did not resolve, so the embedded DNS gave no exfiltration channel in this check; a direct TCP connection to a public address failed; the proxy's name resolved. A `settings.xml` with an `https` and an `http` `<proxy>` entry made Maven fetch every artifact for `dependency:go-offline test-compile` through the proxy, and the proxy logged only allowed CONNECTs to `repo.maven.apache.org`. One extra check ran an unpinned client image by tag, `curlimages/curl:8.10.1` (locally `sha256:d9b4541e214bcd85196d6e92e2753ac6d0ea699f0af5741f8c6cccbfcf00ef4b`), once, on the internal network only, with no mounts and no host network; these checks are repeated with digest pinned images during the gVisor measurement.
* **The runner end to end under `runc`.** On a one test Maven project: the download (two fresh IPv4 only networks, the proxy with Central as its only allowlist, `rootflake-fetch` with Surefire provider prefetch) succeeded with no denied requests and removed the proxy and both networks; an offline `test-compile` on a separate copy succeeded against the read only repository; and the single test run passed with the exit status and the Surefire report agreeing.
* **Workspace ownership.** Files the sandbox user writes into a bind mounted workspace are owned by UID 10001 on the host, so the laptop user cannot delete them directly. Run copies are cleaned up through a container, or created under a directory the runner manages for that UID.

## Consequences

* **One time root step.** Installing gVisor from the pinned tarball and registering it with Docker (`sudo runsc install`) needs `sudo` and a Docker restart, which stops any running containers. It is a manual step on each machine that runs sandboxes, and the installed `runsc` version must match the pinned one. Upgrading gVisor means a new pin and a rerun of the measurement.
* **The Docker socket is root equivalent.** The pipeline process runs as a user in the `docker` group, so anything that compromises it (for example a path traversal through the repo MCP server, TM-09) can get root on the host through Docker. gVisor does not change this. The controls that protect the pipeline process (SH-14, SH-15, SH-22) carry more weight as a result. This is accepted as a residual risk: the mitigation is path confinement in the repo MCP server (SH-14) and patch validation (SH-15), both covered by contract tests. Rootless Docker would remove the exposure at the costs listed under Option C and is not adopted. The question is revisited if the dedicated VM fallback is used, since the socket the pipeline talks to would then belong to the VM's daemon rather than the laptop's.
* **A runtime difference is a measurement difference.** Results are labelled with the runtime. Tests in categories that depend on networking or timing may flake differently under gVisor; any such case is reported.
* **Unmeasured overhead.** If gVisor makes 500 rerun gates too slow on a laptop, the fallback is a dedicated VM, not dropping the runtime.
* **Build file fixes are out by default.** Some real flaky test fixes may be unreachable for Rootflake until the build file measurement says otherwise; they count as fix failed, visibly.
* **The egress allowlist is only as good as its input.** A targeted repository can widen its own allowlist. This is stated, not hidden.
* **JVM memory sizing does not rely on cgroup detection.** The JDK 8 and JDK 11 floors stay, but every JVM gets `-XX:MaxRAM` through `JAVA_TOOL_OPTIONS`. A project that sets its own `-Xmx` or `MaxRAM` in `argLine` or `.mvn/jvm.config` can still exceed the plan; the container limit is the backstop and an OOM kill is recorded as such.
* **The image's own supply chain.** The image installs no apt packages. The JDKs and Maven are downloaded with `ADD --checksum` pinned to SHA-256 values, the base image is pinned by digest, and the iDFlakies and NonDex plugin jars are checked against recorded SHA-256 values after Maven fetches them with `--strict-checksums` (with `maven-dependency-plugin` pinned to 3.11.0). Checksums that come from the same origin as the file prove integrity, not authenticity: a compromised origin could serve a matching pair. Verifying publisher GPG signatures is a possible later step. The transitive dependencies of the two plugins are protected only by Maven Central's checksums.

## Sources

All checked on 2026-10-12.

* Docker, `docker run` reference: https://docs.docker.com/reference/cli/docker/container/run/
* Docker, resource constraints: https://docs.docker.com/engine/containers/resource_constraints/
* Docker, none network driver: https://docs.docker.com/engine/network/drivers/none/
* Docker, `docker network create` (internal mode): https://docs.docker.com/reference/cli/docker/network/create/
* Docker, tmpfs mounts: https://docs.docker.com/engine/storage/tmpfs/
* Docker, bind mounts: https://docs.docker.com/engine/storage/bind-mounts/
* Docker, json-file logging driver: https://docs.docker.com/engine/logging/drivers/json-file/
* Docker, Engine security: https://docs.docker.com/engine/security/
* Docker, post installation steps (docker group): https://docs.docker.com/engine/install/linux-postinstall/
* Docker, rootless mode: https://docs.docker.com/engine/security/rootless/
* Docker, rootless troubleshooting: https://docs.docker.com/engine/security/rootless/troubleshoot/
* Docker, rootless tips (resource limits): https://docs.docker.com/engine/security/rootless/tips/
* Docker, user namespace remapping: https://docs.docker.com/engine/security/userns-remap/
* gVisor, installation: https://gvisor.dev/docs/user_guide/install/
* gVisor, pinned release tarball and checksum: https://storage.googleapis.com/gvisor/releases/release/20261005.0/x86_64/gvisor.tar.zstd and https://storage.googleapis.com/gvisor/releases/release/20261005.0/x86_64/gvisor.tar.zstd.sha512
* gVisor, Docker quick start: https://gvisor.dev/docs/user_guide/quick_start/docker/
* gVisor, platforms: https://gvisor.dev/docs/user_guide/platforms/
* gVisor, compatibility: https://gvisor.dev/docs/user_guide/compatibility/
* gVisor, performance guide: https://gvisor.dev/docs/architecture_guide/performance/
* gVisor, security model: https://gvisor.dev/docs/architecture_guide/security/
* gVisor, filesystem: https://gvisor.dev/docs/user_guide/filesystem/
* gVisor, rootless: https://gvisor.dev/docs/user_guide/rootless/
* gVisor, Java 21 runtime test exclusions at commit `10e1efc8a60add7991ee1b587248ae1086662a47`: https://github.com/google/gvisor/blob/10e1efc8a60add7991ee1b587248ae1086662a47/test/runtimes/exclude/java21.csv
* gVisor issue and pull requests: https://github.com/google/gvisor/issues/13301, https://github.com/google/gvisor/pull/13384, https://github.com/google/gvisor/pull/13390, https://github.com/google/gvisor/pull/12406, https://github.com/google/gvisor/pull/11597, https://github.com/google/gvisor/pull/11602
* gVisor releases: https://github.com/google/gvisor/releases
* OpenJDK, JDK-8230305 "Cgroups v2: Container awareness" and its backports: https://bugs.openjdk.org/browse/JDK-8230305
* Java 17 JVM tool interface specification (`JAVA_TOOL_OPTIONS`): https://docs.oracle.com/en/java/javase/17/docs/specs/jvmti.html
* Java 17 `java` command reference (`UseContainerSupport`, `MaxRAMPercentage`, `MaxMetaspaceSize`, `Xss`): https://docs.oracle.com/en/java/javase/17/docs/specs/man/java.html
* Maven Surefire 3.6.0 `test` goal: https://maven.apache.org/surefire/maven-surefire-plugin/test-mojo.html
* Maven Failsafe 3.6.0 `integration-test` goal: https://maven.apache.org/surefire/maven-failsafe-plugin/integration-test-mojo.html
* Maven, configuring Maven (`MAVEN_OPTS`, `.mvn/`): https://maven.apache.org/configure.html
* Maven, settings reference: https://maven.apache.org/settings.html
* Maven, mirror settings: https://maven.apache.org/guides/mini/guide-mirror-settings.html
* Maven, proxies: https://maven.apache.org/guides/mini/guide-proxies.html
* Maven 3.9.0 release notes (chained local repository): https://maven.apache.org/docs/3.9.0/release-notes.html
* Maven source, `maven.repo.local.tail`: https://github.com/apache/maven/blob/maven-3.9.11/maven-core/src/main/java/org/apache/maven/internal/aether/DefaultRepositorySystemSessionFactory.java
* Linux kernel, cgroup v2 PID controller: https://docs.kernel.org/admin-guide/cgroup-v2.html
* Squid, `acl` directive (`dstdomain`, `ssl::server_name`): https://www.squid-cache.org/Doc/config/acl/
* GitHub REST API, list pull request files: https://docs.github.com/en/rest/pulls/pulls#list-pull-requests-files
