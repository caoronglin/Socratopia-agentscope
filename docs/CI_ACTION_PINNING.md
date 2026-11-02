# CI dependency action pinning

## Failure and cause (2026-10-26)

Dependabot PR [#40](https://github.com/caoronglin/Socratopia-agentscope/pull/40)
rotates `actions/setup-python` from the old immutable commit to a newer
40-character commit SHA. Its Python 3.11 and 3.13 strict CI jobs fail in
`tests/test_dependency_ci.py::DependencyAndStandaloneContracts::test_updated_official_action_families_present`,
because the test requires the *old literal SHA* even when the workflow uses
a correctly pinned new SHA. The platform-v5 and Web bundle runs also fail
on that same branch. This is a false-positive policy check, not evidence
that the upgraded action is unsafe.

## Contract

- Validate **every** `uses:` reference in the four shipping workflows
  (`ci.yml`, `release.yml`, `platform-v5.yml`, `web-v5-bundle.yml`)
  as `owner/repo@<40 lowercase hexadecimal characters>`. Do not accept
  mutable tags, branch refs, abbreviated SHAs, or unpinned actions.
- Verify the required official **action families** are still present in
  each workflow; do not embed an expected commit digest in the tests.
- Keep the existing prohibition on `pull_request_target` in these workflows.
  A syntactically valid SHA does **not** prove the commit is authentic or safe;
  reviewers must inspect upstream release provenance and changes.
- Keep Dependabot updates as independently reviewed PRs. Do not auto-merge
  dependency upgrades, trigger a stable release, or alter course data.

## Reproduce and verify

Run `python -m pytest -q tests/test_dependency_ci.py` and
`python scripts/check.py --strict`. Then inspect the full PR checks,
including Python 3.11/3.13, Python 3.15, browser E2E and native bundle
smoke jobs. After this fix is merged following review, refresh PR #40
against main and re-run all its checks before considering its merge.

The regression test accepts two different synthetic 40-character SHA
values and rejects tags, missing pins, abbreviated and nonhex refs.
No real-model classroom behavior or production filesystem is exercised
by this CI-policy change. The `v4.3.2` tag and Release remain immutable.
