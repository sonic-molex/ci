# Shared CI pipelines

`sonic-molex/ci` is the public home for shared CI pipeline implementations across
SONiC Molex repositories. Business repositories keep thin entry workflows and
their own secrets; implementation, setup tools, tests and documentation live here.

## Modules

| Module | Purpose | Consumer action |
| --- | --- | --- |
| [code-review](code-review/README.md) | Cursor C/C++ PR review, one-command onboarding and batch secret rotation | `sonic-molex/ci/code-review@main` |

Build, compilation and packaging pipelines will be added as independent modules
when implemented.

## Layout

Keep everything specific to a capability together in its top-level directory:

```text
code-review/
  action.yml
  review.sh
  cursor-review-prompt.txt
  README.md
  scripts/
    onboard-cursor-review.py
    tests/
.github/workflows/
  code-review.yml
```

GitHub requires workflow entrypoints in `.github/workflows/`. Keep those thin;
put each pipeline's implementation, supporting scripts, tests and documentation
inside its module. Follow this layout for future build and packaging modules.

## Integration and changes

See each module's README for its inputs, required permissions, repository-local
secrets, onboarding and validation commands. Consumers follow shared `main` by
default; a tag or commit SHA can be selected when explicit pinning is needed.

All implementation changes go through PRs. Humans review and manually merge
PRs; agents never merge or enable auto-merge. For the initial rollout, manually
merge the shared module PR first, then rerun and review the consumer PR before
manually merging it.
