# Shared Cursor PR review

The implementation lives in this repository's
[composite action](.github/actions/cursor-review/action.yml): CLI installation,
review script, prompt, PR comments and artifacts are maintained together.
Consumer repositories contain only `.github/workflows/code-review-cursor.yml`.
The action runs in the checked-out **consumer** repository, while its script and
prompt are loaded from `github.action_path` at the selected shared action ref.
This repository contains no model credential. Its CI tests use a fake model CLI;
only consumer workflows receive repository-local credentials.

Each consumer keeps its own `CURSOR_API_KEY` repository Actions secret. The
shared repository supplies code, not credentials. PR comments use the consumer's
built-in `GITHUB_TOKEN`; no additional PAT is stored in Actions.

## One-command onboarding

After the initial shared-action PR is reviewed and manually merged into `main`,
run from a checkout of `sonic-molex/code-review`:

```sh
python3 scripts/onboard-cursor-review.py --repo sonic-molex/TARGET_REPOSITORY
```

The script prompts once for the Cursor key without displaying it, configures the
repository Actions secret, and creates a PR containing only the entry workflow.
It detects the target's default branch automatically. Have a human review and manually merge that PR to finish
installation. Existing open integration PRs are reused on rerun; unrelated
workflows are never overwritten. If installation fails after a secret or branch
was written, that partial state remains and rerunning resumes the operation.
The script never merges a PR or force-pushes a branch.

Requirements: Python 3.9+, GitHub CLI (`gh`), and a GitHub.com login/token with
repository Contents and Pull requests write access, Actions Secrets write access,
and permission to modify workflow files. For a classic PAT, this generally means
`repo` and `workflow` scopes. The organization must allow the shared action and
`actions/checkout`, `actions/upload-artifact` in Actions policy. Consumers must be
able to access the shared repository (private shared repositories require GitHub
Actions sharing configuration). No organization-level secrets are required.

```sh
# Preview the exact entrypoint and check repository/ref access without writing.
python3 scripts/onboard-cursor-review.py --repo sonic-molex/TARGET_REPOSITORY --dry-run

# Override a non-default PR target; supply an existing credential file.
python3 scripts/onboard-cursor-review.py --repo sonic-molex/TARGET_REPOSITORY \
  --base otn-dev --key-file /secure/path/cursor-key

# Multiple repositories, one prompt. Keep this inventory for later rotation.
python3 scripts/onboard-cursor-review.py --repos-file /path/to/review-repos.txt

# Explicit release/commit selection when needed.
python3 scripts/onboard-cursor-review.py --repo sonic-molex/TARGET_REPOSITORY \
  --shared-ref COMMIT_SHA
```

`--repo` may be repeated. Inventory files contain one `OWNER/REPO` per line;
blank lines and `#` comments are allowed. `--key-file -` reads the key from stdin,
so an existing secret-manager command can pipe it directly without storing a
file. Keys are sent to `gh secret set` on stdin, never in command arguments,
workflow files, PR bodies, logs, or a generated credential file. GitHub does not
allow reading back Actions secret values; obtain the key from its original
credential source. The script configures it but does not make a billable model
request to validate it. Repository/ref preflight cannot prove Secrets write
permission; failed uploads are reported per repository.

The default shared reference is `sonic-molex/code-review@main`.
Central action updates therefore reach all consumers on their next run, without
copying scripts or opening an update PR in every repository. `--shared-ref` can
instead select a tag or full commit SHA; pinned consumers must update their
entrypoint explicitly to receive new code. Run this script again with the new
ref to create/reuse that update PR. Use `--branch NEW_BRANCH` if a previously
merged/closed integration branch is still present and cannot be reused.

## Batch secret rotation

Repository-local secrets must be updated in **every** onboarded consumer
repository. Keep the same inventory for onboarding and rotation. Do not include
this shared code repository: it does not need a Cursor key.

```sh
python3 scripts/onboard-cursor-review.py --rotate-secrets \
  --repos-file /path/to/review-repos.txt
```

This prompts once for the replacement key and updates only `CURSOR_API_KEY` in
each repository. It does not create branches or PRs, change workflows, or contact
the shared action repository. `--key-file` and `--dry-run` work here too. Each
repository gets an OK/FAIL result; any failure produces a nonzero exit status.
After preflight, writes continue for other repositories if one fails. Retry
failed repositories, verify their next review runs, and only then revoke the old
key at its source. Rotation is not atomic across repositories and does not
replace the key in jobs that are already running. Keep the inventory current.

## Review behavior

Review policy remains the port from Shasta
`c45741109495652c809a1da326eeeccf334d0310` introduced in
[sonic-optical-control PR #18](https://github.com/sonic-molex/sonic-optical-control/pull/18):

- Model `cursor-grok-4.6-high` and the original prompt.
- C/C++ files only; up to 15 files per PR, 1,000 diff lines per file.
- Commit patch IDs restored from the first 100 PR conversation comments.
- New commit diffs reviewed per file; exact `✅ No bugs found` handling.
- Markdown PR conversation comment and original JSON/report artifacts.
- Same-repository PRs only, with a non-blocking job; no normal push trigger.

Existing patch-ID tracking, truncation and failure-handling limitations remain.
The GitLab Code Quality JSON is downloadable; GitHub does not render its widget.
Model usage is charged to the account associated with each repository's key.

Consumer configuration:

- Repository Actions secret `CURSOR_API_KEY`: Cursor service credential.
- Built-in `GITHUB_TOKEN`: `contents: read`, `pull-requests: write`.
- Optional repository variable `DISABLE_CURSOR_REVIEW=true`: disable the job.
- Optional repository variable `MAX_REVIEW_FILES`: file limit override.

## Validation

```sh
python3 -m unittest discover -s scripts/tests -v
bash -n .github/actions/cursor-review/review.sh
```

## Repository boundaries and rollout

- `sonic-molex/code-review`: shared action, review script/prompt, onboarding and
  rotation script, tests and documentation. No Cursor key is configured here.
- Each consumer (including `sonic-optical-control`): one generated workflow plus
  its repository-local `CURSOR_API_KEY`. No copied implementation or setup tools.
- The initial shared-library PR must be manually merged into `main` before a
  consumer PR referencing `@main` can run. Then rerun the consumer workflow and
  have a human review and manually merge its PR. Agents never merge or enable
  auto-merge.

Changes to the shared action are tested in this repository's `Validate shared
review` workflow. Consumer repositories following `@main` receive changes only
after human review and merge into the shared default branch.
