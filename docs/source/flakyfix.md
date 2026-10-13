# FlakyFix

Checked on 2026-10-12 against arXiv version 4.

## Primary sources

* arXiv: https://arxiv.org/abs/2307.00012 (v1 submitted 2023-06-21; v4 is the version read, PDF https://arxiv.org/pdf/2307.00012v4)
* Published version: Sakina Fatima, Hadi Hemmati, Lionel Briand. "FlakyFix: Using Large Language Models for Predicting Flaky Test Fix Categories and Test Code Repair." IEEE Transactions on Software Engineering, 2024, pages 3146 to 3171. DOI https://doi.org/10.1109/TSE.2024.3472476 (confirmed through Crossref and the arXiv journal reference).
* Replication package named in the paper: https://figshare.com/s/47f0fb6207ac3f9e2351

## What it actually does

FlakyFix targets flaky tests whose root cause is in the test code, not the production code, and works from the test code alone. It has three steps:

1. **Label a dataset with fix categories.** A keyword heuristic, run as a Python script over the diff between a flaky test and its developer fix, assigns each test one or more of 13 fix categories (for example Change Assertion, Change Condition, Reset Variable, Reorder Data, Change Data Structure, Handle Exception, Change Data Format). Inter rater agreement on the manual check is reported with Cohen's Kappa.
2. **Predict the fix category.** Fine tuned code models, **CodeBERT and UniXcoder** (about 125 million parameters each), with and without few shot learning (a Siamese network with triplet loss), one binary classifier per category, stratified 4 fold cross validation. The reported precision ranges from 83% to 98% and recall from 67% to 97%; UniXcoder outperforms CodeBERT; few shot learning gives no significant improvement.
3. **Repair the test with an LLM.** **GPT-3.5 Turbo** is prompted with the flaky test alone, with the test plus the predicted fix category, and (for three categories) with in context examples. Repairs are scored mainly by **CodeBLEU** against the developer's fix: a mean of 81% with the category label against 75% without.

So the fix category prediction uses small fine tuned code models, and GPT-3.5 Turbo is used only for repair.

## Data

* Source: IDoFT Java data collected up to 2023-10-23: 5,500 tests, 5,372 after removing duplicates, 788 with Accepted fixes, 562 after dropping 226 whose fix changed only the code under test. These 562 come from 123 projects.
* Training categories: those with at least 30 tests.
* Repair evaluation: to limit leakage, only tests fixed or added after September 2021 (stated as GPT-3.5's training cutoff), giving 181 tests from 61 projects.

## How repairs were verified by execution

Only a small sample was run. Of the 181 tests, the authors chose projects with at least five flaky tests (nine projects); four could not be built or executed, leaving **35 tests from 5 projects**. Developer fixed and GPT repaired versions were each run **10 times**. 24 of the 35 GPT repairs passed and 11 failed. A bootstrap 95% interval for the pass rate on this sample is **51% to 83%**.

For the tests that were not executed, pass rates were **projected** with a logistic regression from CodeBLEU score to pass probability, trained on the 35 executed tests. Projected pass rates on the 181 test set: 44% to 50% without the label, 51% to 57% with it. On the 131 test in context subset: 53% to 60% with in context learning. For the 11 failed repairs, on average 16% of tokens had to change to reach the developer fix.

Inconsistency within v4: the abstract gives "roughly between 51% and 83%" for repairs expected to pass, while the introduction's findings list says "roughly between 46% and 80%". The 51% to 83% figure matches the bootstrap result in the RQ3 section, so that is the one I cite.

## How Rootflake differs

What FlakyFix measures and what Rootflake measures are different things, and should not be compared as if they were the same number.

* FlakyFix's headline repair metric is textual similarity (CodeBLEU) to the developer's fix, with execution on 35 tests run 10 times each and model based projection for the rest. Rootflake counts a fix only after it passes hundreds of sandboxed reruns, with every outcome counted and a Wilson interval on the fix rate.
* FlakyFix restricts itself to fixes that touch test code only. Rootflake does not impose that restriction in its scope.
* FlakyFix is a classifier plus a single LLM prompt. Rootflake is a multi agent pipeline that also reproduces the flakiness, diagnoses it, checks the diff for shortcut fixes, and supports upstream submission.
