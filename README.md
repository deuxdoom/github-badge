# GitHub Badge Audit

A Python 3.13+ standard-library CLI for `deuxdoom` that audits visible GitHub achievements through an existing `gh` login. Public profile absence stays `UNKNOWN`; no official Achievements REST API is assumed. [한국어 사용법](README.ko.md)

```powershell
python run.py --audit
python run.py --dry-run
python -m unittest discover -v
python run.py --execute
python run.py --resume
python run.py --cleanup
python run.py --report
```

Audit and dry run cannot mutate GitHub. Quickdraw and Starstruck are always user-confirmed skips. The only automated publication workflow is one meaningful PR containing this requested implementation in the initially empty `deuxdoom/github-badge` repository, after approval of its exact file contents and cleanup plan. The initial README commit establishes a base for that PR. It does not generate repeated badge-farming changes.

Execution checks the account before every mutation, refuses branch protections and active rulesets, checks reviews and CI, pins the head SHA when merging, and only removes its own unchanged, merged branch. No repository deletion, settings changes, collaborator management, payment, starring, fabricated coauthors, or artificial accepted answers are supported. Real collaborators, reviewed answers and payment remain external prerequisites.

Checkpoints recover observable remote resources after interruptions. Ambiguous writes are never blindly retried. Identical immutable Git objects can be retried with fixed metadata. A pending write that is still not observable requires investigation before proceeding. A completed checkpoint cannot generate a second PR. Source hashes, atomic checkpoints, an OS process lock, a fixed publication file list and endpoint allowlist constrain the workflow.

See `.state/preview.json` and the source files before approving. `.state/plan.json` contains the exact publishable bundle; runtime state, evidence, local configuration and reports are excluded from publication. No third-party automation scripts are executed. Existing broad `gh` scopes are reused without widening them; the workflow needs repository contents and PR write access on the specified repository, not repository-delete or billing permissions.

GitHub may delay or hide achievements. A successful merge is reported separately from a verified badge. Branch cleanup retains PRs, commits, events, server audit records and external caches. Reports and source remain local.
