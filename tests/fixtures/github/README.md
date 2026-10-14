# Recorded GitHub API responses

`apache-struts-pull-263.json` is the response of `GET /repos/apache/struts/pulls/263`, fetched on 2026-10-14 with `gh api` and reduced to the fields the corpus builder reads (`number`, `merged`, `merged_at`, `merge_commit_sha`, `head.sha`, `base.sha`). The pull request is the fix PR named by several Accepted rows of IDoFT `pr-data.csv` at commit `4903dc9233eead4e4191c66f9ed0b14eea091052`. Apache Struts is licensed under the Apache License 2.0; these values are repository metadata, not code.

Other cases in the tests (unmerged, malformed) are built in the test code from this response and are labelled there.
