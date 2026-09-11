"""Verify multi-language selection, consumer execution and scope migration."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('select_files', ROOT / 'select_files.py')
SELECTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SELECTOR)


class SelectionTest(unittest.TestCase):
    def test_representative_file_types(self):
        names = [
            'source.cpp', 'header.h', 'source.go', 'source.py', 'source.rs', 'start.sh',
            'model.yang', 'api.thrift', 'api.proto', '.github/workflows/review.yml',
            'settings.yaml', 'settings.json', 'settings.xml', 'settings.toml',
            'settings.ini', 'settings.conf', 'settings.cfg', 'Dockerfile',
            'Dockerfile.j2', 'Dockerfile.build', 'Makefile', 'Makefile.work', 'rules.mk',
            'CMakeLists.txt', 'build.cmake', 'configure.ac', 'Makefile.am',
            'template.j2', 'service.service', 'debian/control', 'debian/postinst',
            'sonic.install', 'go.mod', 'requirements-dev.txt',
        ]
        with tempfile.TemporaryDirectory() as directory:
            for name in names:
                with self.subTest(name=name):
                    path = Path(directory) / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('example\n')
                    self.assertIsNone(SELECTOR.exclusion(path))
            script = Path(directory) / 'extensionless'
            script.write_text('#!/bin/sh\nexit 0\n')
            self.assertIsNone(SELECTOR.exclusion(script))

    def test_exclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content, reason in [
                ('README.md', b'document', 'documentation'),
                ('package-lock.json', b'{}', 'generated dependency lockfile'),
                ('go.sum', b'module checksum', 'generated dependency lockfile'),
                ('binary.py', b'\0binary', 'binary'),
                ('data.bin', b'text', 'unsupported file type'),
            ]:
                with self.subTest(name=name):
                    path = root / name
                    path.write_bytes(content)
                    self.assertEqual(SELECTOR.exclusion(path), reason)
            link = root / 'link.yml'
            link.symlink_to(root / 'package-lock.json')
            self.assertEqual(SELECTOR.exclusion(link), 'symlink')
            self.assertIn('non-file', SELECTOR.exclusion(root / 'deleted.py'))


class ConsumerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.consumer = self.root / 'consumer'
        self.consumer.mkdir()
        self.git('init')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('commit', '--allow-empty', '-m', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        binary = self.root / 'bin'
        binary.mkdir()
        agent = binary / 'cursor-agent'
        agent.write_text('''#!/usr/bin/env python3
import json, sys
if '--version' in sys.argv:
    print('test-agent')
else:
    with open('prompts.jsonl', 'a') as output:
        output.write(json.dumps(sys.stdin.read()) + '\\n')
    print('\u2705 No bugs found')
''')
        agent.chmod(0o755)
        curl = binary / 'curl'
        curl.write_text('''#!/usr/bin/env python3
import os, sys
print('201' if 'POST' in sys.argv else os.environ.get('HISTORY_JSON', '[]'))
''')
        curl.chmod(0o755)
        self.env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'],
                        CURSOR_API_KEY='test-only', CURSOR_MODEL='test-model',
                        GITHUB_TOKEN='test-only', GITHUB_API_URL='https://example.invalid',
                        GITHUB_REPOSITORY='test/consumer', PR_NUMBER='1', PR_DIFF_BASE_SHA=self.base)

    def git(self, *args, input=None):
        return subprocess.check_output(['git', *args], cwd=self.consumer, text=True,
                                       input=input, stderr=subprocess.DEVNULL).strip()

    def commit(self, files):
        for name, content in files.items():
            path = self.consumer / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content if isinstance(content, bytes) else content.encode())
        self.git('add', '.')
        self.git('commit', '-m', 'fixture')
        self.env['PR_HEAD_SHA'] = self.git('rev-parse', 'HEAD')

    def run_review(self):
        result = subprocess.run(['bash', str(ROOT / 'review.sh')], cwd=self.consumer,
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_mixed_files_and_escaped_paths_reach_model(self):
        eligible = {'worker.py': 'print(42)\n', 'worker.go': 'package main\n',
                    'schema.yang': 'module example {}\n', 'api.thrift': 'struct Item {}\n',
                    'api.proto': 'syntax = "proto3";\n', 'script.sh': '#!/bin/sh\nexit 0\n',
                    'Makefile': 'all:\n\ttrue\n', 'Dockerfile': 'FROM scratch\n',
                    'config/quoted"name.yml': 'enabled: true\n',
                    'config/settings.yaml': 'enabled: true\n', 'config.json': '{}\n'}
        self.commit({**eligible, 'README.md': 'docs\n', 'binary.py': b'\0binary'})
        self.run_review()
        artifacts = self.consumer / 'cursor_review_results'
        selected = json.loads((artifacts / 'file_selection.json').read_text())
        self.assertEqual(set(selected['eligible']), set(eligible))
        self.assertEqual({entry['path'] for entry in selected['skipped']}, {'README.md', 'binary.py'})
        prompts = [json.loads(line) for line in (self.consumer / 'prompts.jsonl').read_text().splitlines()]
        self.assertEqual(len(prompts), len(eligible))
        for name in eligible:
            self.assertTrue(any(f'File: {name}\n' in prompt for prompt in prompts), name)
        quality = json.loads((artifacts / 'code-quality-report.json').read_text())
        self.assertEqual({entry['location']['path'] for entry in quality}, set(eligible))

    def test_old_marker_does_not_suppress_new_scope_but_v2_deduplicates(self):
        self.commit({'worker.go': 'package main\n', 'settings.yml': 'enabled: true\n'})
        patch = self.git('patch-id', '--stable', input=self.git('show', '--pretty=format:', 'HEAD')).split()[0]
        self.env['HISTORY_JSON'] = json.dumps([{'body': f'<!-- cursor-reviewed-patchids:{patch} -->'}])
        self.run_review()
        report = (self.consumer / 'cursor_review_results/review_report.md').read_text()
        self.assertIn(f'<!-- cursor-reviewed-patchids-v2:{patch} -->', report)
        before = (self.consumer / 'prompts.jsonl').read_text()
        self.env['HISTORY_JSON'] = json.dumps([{'body': report}])
        result = self.run_review()
        self.assertIn('New patch-ids to review: 0', result.stdout)
        self.assertEqual((self.consumer / 'prompts.jsonl').read_text(), before)
        self.assertNotIn('Posted review comment', result.stdout)

    def test_document_and_binary_only_skips_model(self):
        self.commit({'README.md': 'docs\n', 'package.deb': b'\0binary'})
        result = self.run_review()
        self.assertIn('No reviewable source/configuration/build files changed', result.stdout)
        self.assertFalse((self.consumer / 'prompts.jsonl').exists())
        self.assertEqual(json.loads((self.consumer / 'cursor_review_results/code-quality-report.json').read_text()), [])

    def test_existing_file_budget_is_retained(self):
        self.commit({f'worker{i:02}.py': 'print(42)\n' for i in range(17)})
        self.env['MAX_REVIEW_FILES'] = '15'
        result = self.run_review()
        self.assertIn('Code files exceed budget (17 > 15)', result.stdout)
        self.assertEqual(len((self.consumer / 'prompts.jsonl').read_text().splitlines()), 15)


if __name__ == '__main__':
    unittest.main()
