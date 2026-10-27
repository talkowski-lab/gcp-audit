# AGENTS.md

Instructions for coding agents working in this repo. Read [`CONTRIBUTING.md`](CONTRIBUTING.md)
first; all of its rules apply. This file adds the rules specific to agents.

To **use** gca (run audits, read its output, set up the service account) rather than change its
code, read [`skills/gca/SKILL.md`](skills/gca/SKILL.md).

## The invariant

**This toolkit never changes a live cloud resource**, whether behind a flag or because a user asked.
[`docs/SAFETY.md`](docs/SAFETY.md) describes the three layers that enforce this. Do not weaken
any of them. Do not claim, in code, docs or a report, that anything checks IAM roles or
permissions: nothing does.

## Credentials and the network

* Never authenticate to real Google APIs from this repo, whether in a test or while debugging. A
  question that needs live credentials goes in `docs/DESIGN.md` §Open questions.
* Never run `gcloud`, `gsutil`, `bq` or `curl` against the cloud, with one exception. When the
  operator asks for facts about their projects in their own words, in that session, you may run:
  * read-only `gcloud ... list` and `describe` commands for projects, billing accounts and
    projects, organizations, `container`, `sql`, `dataproc`, `artifacts`, `pubsub`, `dataflow`
    and `notebooks`;
  * `get-iam-policy` on a named project or billing account;
  * read-only `GET` requests to a named Google API;
  * the Terra workspace list.

  Label any number obtained this way with the command and the date, and note that it reflects a
  person's login, not the auditor's service account. The exception never covers anything that
  changes a resource or spends a project's compute, including a BigQuery query.
* Reading public Google documentation is fine. Record what you relied on in
  `docs/API-EVIDENCE.md`.

## Working

* Run the venv binaries by path (`.venv/bin/pytest`). On the development machine, a bare `pytest`
  runs a wrapper that reports "No tests collected" even when the suite passes.
* Judge a check by its exit status, never by its last line of output.
* Ask the maintainer rather than deciding anything on CONTRIBUTING.md's list of changes that need
  an explicit decision.
* Report the commands you ran and their actual output. Claiming a pass you did not run is worse
  than reporting a failure.
* Add no new dependencies and no new CLI commands, and make no unrelated changes along the way.
