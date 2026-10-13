# iFixFlakies

Checked on 2026-10-12.

## Primary sources

* Paper: August Shi, Wing Lam, Reed Oei, Tao Xie, Darko Marinov. "iFixFlakies: A Framework for Automatically Fixing Order-Dependent Flaky Tests." ESEC/FSE 2019, Tallinn, Estonia, pages 545 to 555. DOI https://doi.org/10.1145/3338906.3338925 (title and pages confirmed through Crossref). PDF linked from IDoFT: http://mir.cs.illinois.edu/winglam/publications/2019/ShiETAL19iFixFlakies.pdf
* Project website: https://sites.google.com/view/ifixflakies
* Implementation: bundled in iDFlakies, https://github.com/UT-SE-Research/iDFlakies (README at commit `d9474c664fb3f1215763dc9ac7336c81520f20dd`, section "iFixFlakies").
* IDoFT README, which cites this paper as the definition of OD-Brit and OD-Vic: https://github.com/TestingResearchIllinois/idoft/blob/4903dc9233eead4e4191c66f9ed0b14eea091052/readme.md

## Where OD-Vic and OD-Brit come from

IDoFT's category table defines OD-Brit as "Order-Dependent Brittle tests as defined in iFixFlakies" and OD-Vic as "Order-Dependent Victim tests as defined in iFixFlakies", each linking to the paper above.

The paper's definitions (Section 2), where `run([t])` means running test `t` alone:

* **Order dependent test (Definition 2):** a test with a passing test order and a different failing test order.
* **Victim (Definition 3):** an order dependent test `v` with `run([v]) = PASS`. It passes in isolation and fails when some other tests run before it.
* **Polluter (Definition 4):** a test order `P` with `run(P + [v]) = FAIL` for a victim `v`.
* **Cleaner (Definition 5):** a test order `C` with `run(P + C + [v]) = PASS` for a polluter `P` and victim `v`.
* **Brittle (Definition 6):** an order dependent test `b` with `run([b]) = FAIL`. It fails in isolation and passes only when some other test runs first.
* **State setter (Definition 7):** a test order `S` with `run(S + [b]) = PASS` for a brittle `b`.

Cleaners and state setters are together called **helpers**.

## What it does

From the abstract: iFixFlakies "searches a test suite for helpers that make the order-dependent tests pass and then recommends patches for the order-dependent tests using code from these helpers." It is a search and patch tool built on test execution; it does not use a learned model. It only applies to order dependent tests that have a helper somewhere in the suite.

Reported evaluation (abstract and Section 1): on 110 order dependent tests from a public dataset, 100 were victims and 10 brittles; 58 had helpers, and iFixFlakies produced patches for all 58 (48 victims, 10 brittles). The authors opened pull requests for 56 of them (2 were already fixed); at writing, 21 had been accepted and the rest were pending. These are the paper's 2019 figures, not a current measurement.

## How to run it today

The iDFlakies README states "iDFlakies now includes iFixFlakies" and documents two goals, run after `idflakies:detect`:

```shell
mvn idflakies:minimize
mvn idflakies:fix
```

* `minimize` finds the minimal set of tests that makes an order dependent test pass or fail, and searches for cleaners. It writes one JSON file per test under `.dtfixingtools/minimized/`, listing polluters, state setters, and cleaners.
* `fix` writes, under `.dtfixingtools/fixer/`, a JSON file per test describing the fix plus candidate `.patch` files.

These goals are **not** in the released `edu.illinois.cs:idflakies-maven-plugin:2.0.0`. The plugin descriptor (`META-INF/maven/plugin.xml`) inside the 2.0.0 jar on Maven Central declares only `bulk`, `detect`, `help`, `replay`, and `testTime`. Using iFixFlakies today means building iDFlakies from source at `2.0.1-SNAPSHOT`.

## Relevance to Rootflake

iFixFlakies is the established non LLM approach to repairing order dependent tests (OD-Vic and OD-Brit). It is the natural point of comparison for those categories, and its victim, polluter, cleaner, brittle, and state setter vocabulary gives the Diagnoser precise terms for order dependent root causes.
