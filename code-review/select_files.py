"""Select source, interface, configuration and build files from new commits."""

import argparse
import json
from pathlib import Path
import subprocess


EXTENSIONS = {
    '.c', '.cc', '.cpp', '.cxx', '.h', '.hh', '.hpp', '.hxx',
    '.go', '.py', '.pyi', '.sh', '.bash', '.ksh', '.zsh', '.pl', '.pm',
    '.rs', '.lua', '.js', '.jsx', '.ts', '.tsx', '.java', '.rb',
    '.yang', '.thrift', '.proto',
    '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf', '.xml',
    '.j2', '.jinja', '.jinja2', '.tmpl', '.template', '.in', '.yangjson',
    '.mk', '.mak', '.cmake', '.am', '.ac', '.m4', '.dockerfile', '.dep', '.profile',
    '.patch', '.diff',
    '.service', '.socket', '.timer', '.target', '.path', '.mount',
    '.install', '.links', '.dirs', '.symbols', '.conffiles',
    '.preinst', '.postinst', '.prerm', '.postrm', '.triggers',
}
NAMES = {
    'makefile', 'gnumakefile', 'dockerfile', 'containerfile', 'cmakelists.txt',
    'kconfig', 'meson.build', 'meson_options.txt', 'go.mod', 'gemfile',
    'jenkinsfile', '.gitmodules', '.gitignore', '.dockerignore',
}
DEBIAN_NAMES = {'control', 'rules', 'compat', 'format', 'options',
                'preinst', 'postinst', 'prerm', 'postrm', 'triggers'}
LOCKFILES = {'go.sum', 'cargo.lock', 'package-lock.json', 'yarn.lock',
             'pnpm-lock.yaml', 'poetry.lock', 'pipfile.lock', 'composer.lock'}


def exclusion(path):
    name = path.name.lower()
    if path.is_symlink():
        return 'symlink'
    if not path.is_file():
        return 'deleted-or-non-file (including submodules)'
    if '\n' in str(path) or '\r' in str(path):
        return 'unsupported newline in path'
    with path.open('rb') as source:
        sample = source.read(8192)
    if b'\0' in sample:
        return 'binary'
    if name in LOCKFILES or name.endswith('.lock'):
        return 'generated dependency lockfile'
    if path.suffix.lower() in {'.md', '.rst', '.adoc'} or name in {'license', 'copying', 'changelog', 'copyright'}:
        return 'documentation'
    if (path.suffix.lower() in EXTENSIONS or name in NAMES
            or name.startswith(('dockerfile.', 'containerfile.', 'makefile.'))
            or (name.startswith('requirements') and name.endswith('.txt'))
            or ('debian' in path.parts and name in DEBIAN_NAMES)
            or sample.startswith(b'#!')):
        return None
    return 'unsupported file type'


def select(commits):
    paths = set()
    for commit in commits:
        raw = subprocess.check_output([
            'git', 'diff-tree', '--no-commit-id', '--name-only', '-r', '-z', commit])
        paths.update(p.decode('utf-8') for p in raw.split(b'\0') if p)
    eligible, skipped = [], []
    for filename in sorted(paths):
        reason = exclusion(Path(filename))
        if reason:
            skipped.append({'path': filename, 'reason': reason})
        else:
            eligible.append(filename)
    return {'scope_version': 2, 'eligible': eligible, 'skipped': skipped}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commits-file', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    result = select(args.commits_file.read_text().splitlines())
    args.report.write_text(json.dumps(result, indent=2) + '\n')
    for filename in result['eligible']:
        print(filename)


if __name__ == '__main__':
    main()
