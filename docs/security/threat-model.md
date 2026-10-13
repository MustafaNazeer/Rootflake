# Threat model

Rootflake clones open source Java projects, builds them, runs their tests hundreds of times, and applies patches written by an LLM. Every one of those steps executes or handles code that nobody on this project wrote or reviewed. This document lists what can go wrong, who could cause it, and which controls stop it. The controls themselves, and how each one is enforced in local Docker and on Cloud Run Jobs, live in [sandbox-hardening-checklist.md](sandbox-hardening-checklist.md).

Every threat has a stable ID (`TM-NN`) and every control has a stable ID (`SH-NN`). IDs are never renumbered or reused; a retired entry stays in place marked as retired, so reviews, commits and issues can cite them safely.

Cloud Run statements were checked against Google's documentation on 2026-10-12. Cloud Run changes often, so each claim links the page it came from.

## 1. System and trust boundaries

The pieces that matter for security:

* **Pipeline process.** Python, running on the host (locally) or in its own Cloud Run job. Runs the LangGraph pipeline and the single agent baseline, holds every credential (Vertex AI, BigQuery, Supabase), hosts the three MCP servers (repo, runner, history), and runs the deterministic gate.
* **Sandbox.** A container built from the Rootflake sandbox image (JDKs, Maven, iDFlakies, NonDex) plus the target project at a pinned commit. Executes the target's build and tests. Holds no credentials.
* **Workspace.** The sandbox's copy of the target repository. The only place a patch may land.
* **Maven repository cache.** Dependencies downloaded for one target project at one commit.
* **Results path.** JSON Lines run logs, loaded into BigQuery, summarized into a committed metrics JSON, displayed on the static dashboard.
* **Upstream path.** PR packages placed in a review queue. A person reads every package and submits any PR by hand; Rootflake never opens one.

Trust boundaries, from least to most trusted:

| Boundary | Untrusted side | Trusted side |
|---|---|---|
| B1 | Target repository content (source, tests, build files, README, comments, commit metadata) | Everything else |
| B2 | Code executing inside the sandbox (tests, build plugins, dependencies) | Host or Cloud Run instance, pipeline process |
| B3 | LLM output (diagnoses, patches, tool call arguments) | MCP servers, gate, workspace |
| B4 | Sandbox output (exit codes, Surefire reports, logs) | Gate, run logs, BigQuery, dashboard |
| B5 | Third party artifacts (Maven Central and other repositories, base images, plugins, Python packages) | Sandbox image, pipeline process |

LLM output sits on the untrusted side of B3 even though Rootflake asked for it: the model reads untrusted repository content (B1), so anything it emits may be steered by that content.

## 2. Assets

| ID | Asset | Why it matters |
|---|---|---|
| A1 | Cloud credentials: Vertex AI access, BigQuery, Supabase connection string, GitHub token if one is ever used | Theft means spending, data loss, or impersonation |
| A2 | Host machine (developer laptop or PC) and its files | Personal data, SSH keys, cloud config under the home directory |
| A3 | GCP project and its billing | Hard monthly budget of 25 USD |
| A4 | Integrity of results: gate verdicts, run logs, metrics JSON, the frozen corpus | Every published number depends on them |
| A5 | Upstream reputation | A bad or malicious PR submitted to a real project harms that project and Rootflake's credibility |
| A6 | Undisclosed vulnerabilities found in a target project | Public disclosure before a fix harms the project's users |

## 3. Adversaries

* **ADV1, malicious or compromised target repository.** Test code, build plugins, or build extensions written to attack whoever runs them. IDoFT projects are real, mostly well known projects, so this is unlikely for the offline corpus, but the upstream track scans projects nobody has vetted, and any project can be compromised at a given commit.
* **ADV2, poisoned dependency.** A malicious artifact in the target's dependency tree or plugin tree, independent of the project's own authors.
* **ADV3, prompt injection author.** Anyone who can put text in a repository (a contributor, a dependency's README, a crafted test name or failure message) and wants to steer the runtime agents.
* **ADV4, the LLM itself, behaving badly without an attacker.** Hallucinated paths, destructive edits, shortcut fixes, runaway tool loops.
* **ADV5, accidental insider.** A mistake in Rootflake's own code or configuration, such as a credential passed into the sandbox or an over privileged service account.

## 4. Threats

Each entry lists the entry point, the impact, a likelihood and impact rating (low, medium, high), the controls that address it, and the residual risk after those controls.

### TM-01. Network exfiltration from test code

* **Entry point:** test or build code running inside the sandbox (B2), ADV1 or ADV2.
* **Description:** code opens outbound connections to send out anything it can read, download a second stage payload, or join a botnet. During the dependency download step the network is necessarily on.
* **Impact:** high if any secret is reachable (TM-04); otherwise abuse of Rootflake's IP address and bandwidth. **Likelihood:** low for the offline corpus, medium for the upstream track.
* **Controls:** SH-01, SH-02, SH-11, SH-12, SH-13.
* **Residual risk:** code that runs during the dependency download step (TM-05) still has network access. Nothing sensitive is reachable from it, so the residual impact is limited to outbound abuse during that window.

### TM-02. Sandbox escape

* **Entry point:** B2.
* **Description:** code inside the container exploits the container runtime, the kernel, or a misconfiguration (privileged mode, the Docker socket mounted, a sensitive host path mounted, extra capabilities) to act on the host.
* **Impact:** high (A1, A2). **Likelihood:** low; requires a kernel or runtime exploit if the configuration controls hold.
* **Controls:** SH-03, SH-04, SH-05, SH-06, SH-12, SH-19.
* **Residual risk:** local Docker containers share the host kernel, so a kernel exploit is not stopped by any control in the checklist. On Cloud Run, each instance sits behind a virtual machine monitor boundary ([security design](https://docs.cloud.google.com/run/docs/securing/security)), which is a stronger boundary than a container. Running bulk sandbox work on a dedicated machine or virtual machine, or under a stronger local runtime, would reduce this further; that is an open decision (see section 6).

### TM-03. Resource exhaustion

* **Entry point:** B2, or ADV4 through an expensive rerun request.
* **Description:** fork bombs, memory leaks, disk filling, infinite loops, or tests that hang forever.
* **Impact:** medium: a frozen host, a stalled eval run, or a Cloud Run bill. **Likelihood:** medium; hangs and leaks happen in ordinary flaky tests without any attacker.
* **Controls:** SH-07, SH-08, SH-09, SH-10, SH-23.
* **Residual risk:** low once every limit is in place and tested.

### TM-04. Credential exposure to the sandbox

* **Entry point:** B2, enabled by an ADV5 mistake.
* **Description:** a credential reaches code inside the sandbox through an environment variable, a mounted file (for example a gcloud config directory or a service account key), the Docker socket, build arguments baked into an image layer, or, on Cloud Run, the metadata server. Cloud Run's metadata server can generate OAuth2 access tokens for the service identity of the job ([container runtime contract](https://docs.cloud.google.com/run/docs/container-contract)), so any code in a Cloud Run job container can obtain a token for whatever service account that job runs as.
* **Impact:** high (A1, A3). **Likelihood:** medium without explicit controls, because the Cloud Run path makes it the default behavior.
* **Controls:** SH-11, SH-12, SH-13, SH-22.
* **Residual risk:** low if the sandbox job runs as a service account that holds no IAM roles at all and SH-13 is tested.

### TM-05. Poisoned dependencies and build time code execution

* **Entry point:** B5, ADV2 and ADV1.
* **Description:** Maven plugins and build extensions are code, and resolving them can execute that code while the network is still on. A malicious artifact can also plant files in the Maven repository cache that later runs reuse.
* **Impact:** medium to high: network abuse during the download step, cache poisoning across projects (TM-06). **Likelihood:** low.
* **Controls:** SH-02, SH-12, SH-18, SH-20.
* **Residual risk:** Rootflake cannot vet the target's dependency tree; it can only make sure the download step holds nothing worth stealing and that its output is isolated per project and commit.

### TM-06. Cross run contamination and persistence

* **Entry point:** B2 and B5.
* **Description:** one target project writes into a shared cache (Maven repository, image layer, workspace leftovers) and influences a different project's run, or the same test's later reruns. This is both a security problem and an eval integrity problem: a contaminated rerun is not an independent rerun.
* **Impact:** medium (A4). **Likelihood:** low to medium.
* **Controls:** SH-06, SH-20.
* **Residual risk:** low once caches are keyed per project and commit and mounted read only during test runs.

### TM-07. Prompt injection through repository content

* **Entry point:** B1 into the runtime agents' context. Maps to OWASP [LLM01:2025 Prompt Injection](https://genai.owasp.org/llm-top-10/).
* **Description:** text in the repository (code comments, test names, string literals, failure messages printed by a test, README files, commit messages) contains instructions that the Diagnoser, Fixer, or baseline follows: to write a malicious or shortcut patch, to call tools in a harmful way, to misreport the diagnosis, or to burn the budget.
* **Impact:** medium. The worst outcomes are a malicious patch that reaches the upstream review queue (A5) or a skewed eval (A4). **Likelihood:** low for the offline corpus, medium for the upstream track.
* **Controls:** SH-14, SH-15, SH-16, SH-21, SH-23, and the deterministic gate, which no runtime agent can override.
* **Residual risk:** prompt injection cannot be fully prevented. The design limits what a fully hijacked agent can do: it has no network tool, no credential, no path outside the workspace, and no way to change a verdict, and a person reads every patch before it leaves the machine.

### TM-08. Malicious or harmful LLM patches

* **Entry point:** B3, ADV3 or ADV4.
* **Description:** a patch that writes outside the workspace (absolute paths, `..` segments, symlinks, renames), creates executable or binary files, edits build files to run extra code during the gate or to add retries, or deletes and weakens the test (a shortcut fix).
* **Impact:** high for writes outside the workspace (A2); medium for gate integrity (A4, A5). **Likelihood:** medium; models produce malformed patches without any attacker.
* **Controls:** SH-14, SH-15, the shortcut checker in the gate, SH-16.
* **Residual risk:** low for file system escape once SH-14 is tested; shortcut detection is only as good as the shortcut checker's fixture set.

### TM-09. Path traversal through the repo MCP server

* **Entry point:** B3 (tool call arguments) and B1 (symlinks inside the target repository).
* **Description:** the repo server's read file, search code, view diff, and apply patch tools accept paths. A path such as `../../.config/gcloud/credentials.db`, an absolute path, or a path that passes through a symlink committed to the target repository could make the server, which runs in the credential holding pipeline process, read or write host files. Search results could carry host file contents back into the model's context and from there into logs.
* **Impact:** high (A1, A2). **Likelihood:** medium if paths are only checked as strings.
* **Controls:** SH-14, SH-15.
* **Residual risk:** low once path resolution follows the rules in SH-14 and the contract tests include traversal and symlink fixtures.

### TM-10. Malicious sandbox output

* **Entry point:** B4.
* **Description:** test output is attacker controlled. Huge logs exhaust memory or the token budget; terminal escape sequences corrupt logs; crafted text injects into prompts (TM-07); stored logs are later rendered on the dashboard's per test page, where unescaped HTML would be cross site scripting.
* **Impact:** medium. **Likelihood:** medium for oversized output, low for deliberate attacks.
* **Controls:** SH-17, SH-21.
* **Residual risk:** low.

### TM-11. Forged test results

* **Entry point:** B4.
* **Description:** the gate reads results produced inside the sandbox. Code inside the sandbox could write fake Surefire reports, detect the rerun harness and behave differently under it, or otherwise make a failing test look like a pass.
* **Impact:** medium (A4, A5). **Likelihood:** low; it needs a target project written to fool Rootflake specifically.
* **Controls:** SH-17 (the runner checks the process exit status and the reports together and treats any disagreement as an infrastructure outcome), SH-06, plus human review of every upstream package and the target project's own CI on any submitted PR.
* **Residual risk:** accepted. A sandbox cannot fully attest to what happened inside it. This is stated here rather than hidden, and no published number claims more than the gate measured.

### TM-12. Secret and sensitive data leakage through logs, prompts, or the dashboard

* **Entry point:** ADV5.
* **Description:** a credential or connection string ends up in a prompt sent to the model, a JSON Lines log, a BigQuery table, the committed metrics JSON, or the public dashboard. Error messages from client libraries can include connection strings.
* **Impact:** high (A1). **Likelihood:** low to medium. Maps to [LLM02:2025 Sensitive Information Disclosure](https://genai.owasp.org/llm-top-10/).
* **Controls:** SH-12, SH-22.
* **Residual risk:** low.

### TM-13. Premature public disclosure of a target project vulnerability

* **Entry point:** any run that surfaces something that looks like a real security bug in a target project.
* **Description:** the finding lands in a public PR description, issue, run log, or the dashboard before the maintainers can fix it.
* **Impact:** high for the target project's users (A6). **Likelihood:** low.
* **Controls:** SH-24.
* **Residual risk:** low.

### TM-14. Runaway cost

* **Entry point:** ADV3 or ADV4 (tool loops, oversized contexts), ADV5 (a misconfigured job). Maps to [LLM10:2025 Unbounded Consumption](https://genai.owasp.org/llm-top-10/).
* **Description:** spending past the monthly budget through model calls, Cloud Run execution time, or retries.
* **Impact:** medium (A3). **Likelihood:** medium.
* **Controls:** SH-10, SH-23.
* **Residual risk:** low; the budget alert is an alert, not a hard stop, so the per test and per eval run caps carry the actual enforcement.

### TM-15. Supply chain of Rootflake itself

* **Entry point:** B5.
* **Description:** a compromised base image, JDK distribution, iDFlakies or NonDex release, Python package, or CI action ends up in the sandbox image or the pipeline process. Anything in the pipeline process can reach every credential. Maps to [LLM03:2025 Supply Chain](https://genai.owasp.org/llm-top-10/).
* **Impact:** high. **Likelihood:** low.
* **Controls:** SH-18.
* **Residual risk:** low to medium; pinning narrows the window but does not vet upstream code.

### TM-16. Over privileged cloud identities

* **Entry point:** ADV5, then any of the threats above.
* **Description:** a Cloud Run job runs as the Compute Engine default service account, which Google warns "might automatically be granted the Editor role on your project" ([service identity](https://docs.cloud.google.com/run/docs/securing/service-identity)), or the pipeline and the sandbox share one service account. Combined with TM-04 this turns any sandbox compromise into project wide access.
* **Impact:** high (A1, A3). **Likelihood:** medium if nothing is configured, because the default is unsafe.
* **Controls:** SH-13, SH-25.
* **Residual risk:** low once Terraform defines dedicated service accounts and IAM is reviewed.

## 5. Threat to control matrix

| Threat | Controls |
|---|---|
| TM-01 Network exfiltration | SH-01, SH-02, SH-11, SH-12, SH-13 |
| TM-02 Sandbox escape | SH-03, SH-04, SH-05, SH-06, SH-12, SH-19 |
| TM-03 Resource exhaustion | SH-07, SH-08, SH-09, SH-10, SH-23 |
| TM-04 Credential exposure | SH-11, SH-12, SH-13, SH-22 |
| TM-05 Poisoned dependencies | SH-02, SH-12, SH-18, SH-20 |
| TM-06 Cross run contamination | SH-06, SH-20 |
| TM-07 Prompt injection | SH-14, SH-15, SH-16, SH-21, SH-23 |
| TM-08 Harmful patches | SH-14, SH-15, SH-16 |
| TM-09 Repo server path traversal | SH-14, SH-15 |
| TM-10 Malicious sandbox output | SH-17, SH-21 |
| TM-11 Forged results | SH-06, SH-17 (residual risk accepted) |
| TM-12 Secret leakage | SH-12, SH-22 |
| TM-13 Vulnerability disclosure | SH-24 |
| TM-14 Runaway cost | SH-10, SH-23 |
| TM-15 Rootflake supply chain | SH-18 |
| TM-16 Over privileged identities | SH-13, SH-25 |

## 6. Open decisions

These are not settled yet and will be recorded as ADRs before the code they affect lands.

* **OD-1. How the sandbox gets "no network" on Cloud Run Jobs.** Cloud Run has no equivalent of Docker's `--network none`. The candidates are listed under SH-01 in the checklist. Until one is chosen and tested, the deployed pipeline does not execute target project tests on Cloud Run.
* **OD-2. Local runtime strength.** Plain Docker shares the host kernel (TM-02). Options include running bulk work on a dedicated machine or virtual machine, or a stronger container runtime. Not yet evaluated.
* **OD-3. Which files a patch may touch.** The checklist proposes rejecting changes to build files by default (SH-15). Some legitimate flaky test fixes may need a build file change; how often that happens in the corpus is not yet measured.
* **OD-4. Egress allowlist during dependency download.** Restricting the download step to known Maven repositories would narrow TM-01 and TM-05 further, at the cost of breaking projects that use other repositories. Not yet decided.
