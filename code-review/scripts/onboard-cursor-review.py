#!/usr/bin/env python3
"""Install a shared Cursor review entrypoint or rotate repository-local secrets."""

import argparse
import base64
import getpass
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import quote, urlencode

SHARED_REPO = 'sonic-molex/ci'
WORKFLOW = '.github/workflows/code-review-cursor.yml'
ACTION = 'code-review'
MARKER = '# Managed by onboard-cursor-review.py\n'


class SetupError(Exception):
    pass


def gh(args, payload=None, missing_ok=False, secret=False):
    result = subprocess.run(
        ['gh', *args], input=payload, text=True, capture_output=True, check=False)
    if result.returncode:
        if missing_ok and 'HTTP 404' in result.stderr:
            return None
        # Never echo subprocess output for secret operations.
        detail = 'secret upload failed; check repository Secrets write permission' if secret else result.stderr.strip()
        raise SetupError(detail or 'GitHub CLI failed')
    return result.stdout


def api(path, data=None, method='GET', missing_ok=False):
    args = ['api', '--hostname', 'github.com', '--method', method, path]
    if data is not None:
        args += ['--input', '-']
    raw = gh(args, json.dumps(data) if data is not None else None, missing_ok)
    return json.loads(raw) if raw else None


def contents(repo, path, ref):
    return api(f'repos/{repo}/contents/{path}?{urlencode({"ref": ref})}', missing_ok=True)


def decode(item):
    if item is None:
        return None
    if item.get('type') != 'file' or item.get('encoding') != 'base64':
        raise SetupError('Expected a regular, base64-encoded workflow file')
    return base64.b64decode(item['content']).decode('utf-8')


def render(base, shared_repo, shared_ref):
    action = json.dumps(f'{shared_repo}/{ACTION}@{shared_ref}')
    return MARKER + '''name: Cursor AI Code Review

on:
  pull_request:
    branches: [__BASE__]
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read
  pull-requests: write

jobs:
  code-review:
    if: >-
      vars.DISABLE_CURSOR_REVIEW != 'true' &&
      github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-24.04
    continue-on-error: true
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 0
          persist-credentials: false
      - uses: __ACTION__
        with:
          cursor-api-key: ${{ secrets.CURSOR_API_KEY }}
          github-token: ${{ github.token }}
          max-review-files: ${{ vars.MAX_REVIEW_FILES || '15' }}
'''.replace('__BASE__', json.dumps(base)).replace('__ACTION__', action)


def plan(repo, args):
    info = api(f'repos/{repo}')
    if info.get('archived') or not info.get('permissions', {}).get('push'):
        raise SetupError('Repository is archived or token lacks repository write access')
    if args.rotate_secrets:
        return {'repo': repo}
    base = args.base or info['default_branch']
    if base == args.branch:
        raise SetupError('The integration branch must differ from the target branch')
    base_ref = api(f'repos/{repo}/git/ref/heads/{quote(base, safe="")}')
    desired = render(base, args.shared_repo, args.shared_ref)
    base_file = contents(repo, WORKFLOW, base)
    base_text = decode(base_file)
    if base_text == desired:
        return {'repo': repo, 'active': True}
    if base_text is not None and not base_text.startswith(MARKER):
        raise SetupError(f'{WORKFLOW} already exists and is not managed by this script; migrate it manually')
    branch_ref = api(f'repos/{repo}/git/ref/heads/{quote(args.branch, safe="")}', missing_ok=True)
    query = urlencode({'state': 'open', 'head': repo.split('/')[0] + ':' + args.branch})
    prs = api(f'repos/{repo}/pulls?{query}')
    pr = next((p for p in prs if p['head']['repo']['full_name'].lower() == repo.lower()), None)
    if pr and pr['base']['ref'] != base:
        raise SetupError('Existing integration PR targets a different base; choose another --branch')
    current = contents(repo, WORKFLOW, args.branch) if branch_ref else base_file
    current_text = decode(current)
    if branch_ref and not pr:
        # Permit retries after branch creation or workflow commit but before PR creation.
        if branch_ref['object']['sha'] != base_ref['object']['sha'] and current_text != desired:
            raise SetupError('Integration branch already exists without a matching workflow/PR; choose another --branch')
    if current_text is not None and not current_text.startswith(MARKER):
        raise SetupError('Integration branch contains an unmanaged workflow; choose another --branch')
    return dict(repo=repo, base=base, base_sha=base_ref['object']['sha'],
                branch_exists=bool(branch_ref), current=current, desired=desired, pr=pr)


def apply(plan_data, args, key):
    repo = plan_data['repo']
    gh(['secret', 'set', 'CURSOR_API_KEY', '--repo', 'https://github.com/' + repo,
        '--app', 'actions'], payload=key, secret=True)
    if args.rotate_secrets:
        return 'CURSOR_API_KEY updated'
    if plan_data.get('active'):
        return 'CURSOR_API_KEY updated; entrypoint already installed'
    if not plan_data['branch_exists']:
        api(f'repos/{repo}/git/refs', {'ref': 'refs/heads/' + args.branch,
                                     'sha': plan_data['base_sha']}, 'POST')
    current = plan_data['current']
    if decode(current) != plan_data['desired']:
        body = {'message': 'ci: enable shared Cursor PR review', 'branch': args.branch,
                'content': base64.b64encode(plan_data['desired'].encode()).decode()}
        if current:
            body['sha'] = current['sha']
        api(f'repos/{repo}/contents/{WORKFLOW}', body, 'PUT')
    pr = plan_data['pr']
    if not pr:
        pr = api(f'repos/{repo}/pulls', {
            'title': 'ci: enable shared Cursor PR review',
            'head': args.branch, 'base': plan_data['base'],
            'body': (
                f'Enable source, interface, configuration and build-file PR review using `{args.shared_repo}/{ACTION}@{args.shared_ref}`. '
                f'Only `{WORKFLOW}` is added or updated; review code stays in the shared action.\n\n'
                'The repository-local `CURSOR_API_KEY` Actions secret has been configured. '
                'PR comments use the built-in workflow token. Same-repository PRs only; '
                'the review job remains non-blocking.\n\n'
                'Validation: the setup script checked the shared action and target branch before '
                'writing. Inspect this PR\'s Actions run for runtime validation.\n'
            )}, 'POST')
    return 'CURSOR_API_KEY updated; ' + pr['html_url']


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', action='append', default=[], help='OWNER/REPO; repeat for multiple repositories')
    parser.add_argument('--repos-file', type=Path, help='One OWNER/REPO per line; # comments allowed')
    parser.add_argument('--key-file', help='Read the Cursor key from a file, or - for stdin; default: hidden prompt')
    parser.add_argument('--rotate-secrets', action='store_true', help='Only update repository secrets; do not change workflows or create PRs')
    parser.add_argument('--dry-run', action='store_true', help='Read-only preflight and workflow preview; no key required')
    parser.add_argument('--base', help='PR target branch (default: each repository default branch)')
    parser.add_argument('--branch', default='ci/enable-cursor-review', help='Integration branch; reruns reuse its open PR')
    parser.add_argument('--shared-repo', default=SHARED_REPO)
    parser.add_argument('--shared-ref', default='main', help='Shared action branch/tag/SHA (default: main, receives central updates)')
    args = parser.parse_args(argv)
    if args.repos_file:
        args.repo += [line.split('#', 1)[0].strip() for line in args.repos_file.read_text().splitlines()]
    args.repo = list(dict.fromkeys(repo for repo in args.repo if repo))
    if not args.repo:
        parser.error('provide --repo or --repos-file')
    for repo in [*args.repo, args.shared_repo]:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            parser.error('repository must be OWNER/REPO')
    if not re.fullmatch(r'[A-Za-z0-9_./-]+', args.shared_ref):
        parser.error('--shared-ref must be a branch, tag or SHA')
    return args


def read_key(args):
    if args.key_file == '-':
        key = sys.stdin.read().strip()
    elif args.key_file:
        key = Path(args.key_file).read_text().strip()
    else:
        if not sys.stdin.isatty():
            raise SetupError('Use --key-file PATH or --key-file - in non-interactive sessions')
        key = getpass.getpass('Cursor API key (stored separately in each repository): ').strip()
    if not key or '\n' in key or '\r' in key:
        raise SetupError('Cursor key must be a non-empty single line')
    return key


def main(argv=None):
    args = parse_args(argv)
    if not shutil.which('gh'):
        raise SetupError('Install GitHub CLI and authenticate with gh auth login first')
    if not args.rotate_secrets:
        shared = contents(args.shared_repo, ACTION + '/action.yml', args.shared_ref)
        if shared is None:
            raise SetupError('Shared action does not exist at the selected ref; merge the shared-action PR first or use --shared-ref')
    # Preflight every target before writing any secret or branch.
    plans = []
    failed = False
    for repo in args.repo:
        try:
            plans.append(plan(repo, args))
        except SetupError as error:
            print(f'FAIL {repo}: {error}', file=sys.stderr)
            failed = True
    if failed:
        return 1
    if args.dry_run:
        for item in plans:
            print(f'PLAN {item["repo"]}: update repository Actions secret CURSOR_API_KEY')
            if not args.rotate_secrets:
                print('Entrypoint already installed' if item.get('active') else item['desired'])
        return 0
    key = read_key(args)
    for item in plans:
        try:
            print(f'OK {item["repo"]}: {apply(item, args, key)}')
        except SetupError as error:
            print(f'FAIL {item["repo"]}: {error}', file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (SetupError, OSError, ValueError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
