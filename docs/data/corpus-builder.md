# Corpus builder

The corpus builder turns IDoFT's `pr-data.csv` into the versioned corpus described in [ADR 0006](../adr/0006-corpus-format-and-split.md). It lives in `src/rootflake/corpus/` and runs as `python -m rootflake.corpus`.

## What it does today

1. **`fetch-idoft`** downloads `pr-data.csv` at the pinned IDoFT commit `4903dc9233eead4e4191c66f9ed0b14eea091052`, checks its SHA256 (`4d9a8b9a0cbb597792efe328f4bf29300de585681b77d97fdaa98c90d96f93fb`) before writing anything, and stores it in the local cache under `~/.local/share/rootflake/idoft/`. The file is never committed, because IDoFT declares no license.
2. **`screen`** applies funnel rules 1 to 5 (status, duplicates, multi label rows, the inclusion list, the minimum stratum size) and prints the counts and the allocation plan as JSON.
3. **`draw`** refuses to run, for the reason below.

```
uv run python -m rootflake.corpus fetch-idoft
uv run python -m rootflake.corpus screen
```

At the pinned commit, `screen` reports:

| Rule | Rows removed |
|---|---|
| 1, status not Accepted | 5,772 |
| 2, duplicate test | 18 |
| 3, multiple labels | 15 |
| 4, category excluded | 3 |
| 5, stratum too thin (TZD) | 4 |

| Stratum | Eligible rows | Eligible PRs | Planned dev | Planned test |
|---|---|---|---|---|
| ID | 1,754 | 527 | 35 | 70 |
| OD (OD, OD-Vic, OD-Brit) | 338 | 70 | 8 | 15 |
| NIO | 143 | 30 | 5 | 10 |
| NOD | 28 | 7 | 2 | 5 |

These match the counts and the illustrative allocation in ADR 0006, and `tests/data/test_idoft_counts.py` checks them whenever the cached file is present. The plan is a plan, not the corpus: the build and runnable filters will lower what each stratum can supply.

## Why the draw waits

The draw walks each stratum's fix PRs in seeded rank order and checks rules 6 to 12 lazily. Rules 6 to 10 are implemented: one test per PR, at most two per project per stratum, no class in both splits, a merged PR whose canonical fix diff can be computed, and a declared Java level from 8 to 17 (an undeclared level passes and the build decides). Rules 11 and 12 need the target project to build and the test to pass in the sandbox, and the run count `R` for rule 12 has to be measured on real targets first. Until both exist, the draw stops with an error instead of producing a partial corpus.

## How the checks treat failures

* A definitive answer about a PR (not found, not merged, its head not fetchable, no merge base) is recorded as `fix_diff_unavailable`.
* A rate limit, a server error, a network failure, or a malformed response aborts the walk. Recording it as a rule outcome would silently change which tests are drawn.
* The GitHub token comes from `gh auth token` at run time and is sent only in the Authorization header. It is never logged or written.

## Handling target repositories on the host

Rule 9 needs the PR's head commit and its merge base, so the builder keeps a bare clone of each target project under the managed root's `mirrors/` directory. A bare clone has no working tree, so nothing from the target is checked out on the host:

* Files are read as blobs by commit and path. Symlinks and submodule entries are never followed.
* Submodules are never fetched.
* Every git call runs with a fixed configuration and a minimal environment: no system or global config, hooks disabled, only the https transport, credential helpers off.
* Diffs use `--no-ext-diff --no-textconv`.

The tests build a hostile repository with a submodule, an outside symlink, a planted hook, and a diff driver. Each threat is first shown to fire under plain git, then shown to stay inert under the builder's git calls.

POM files are parsed with the same DTD refusing parser as the rest of the sandbox code. Property references are expanded with a length cap, so a POM cannot grow a value exponentially.
