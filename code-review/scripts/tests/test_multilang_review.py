"""Verify multi-language filtering, consumer execution and deduplication."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


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

    def test_representative_file_types(self):
        names = [
            'source.c', 'source.cpp', 'source.cc', 'source.cxx',
            'header.h', 'header.hpp', 'header.hxx',
            'source.go', 'source.py', 'start.sh', 'script.lua', 'source.rs',
            'model.yang', 'api.thrift', 'api.proto',
            'settings.json', '.github/workflows/review.yml', 'settings.yaml',
            'settings.xml', 'settings.ini', 'settings.conf', 'settings.cfg',
            'sai.profile', 'Dockerfile.j2', 'rules.mk', 'build.cmake',
            'rules.dep', 'external-changes.patch',
        ]
        self.commit({name: 'example\n' for name in names})
        self.env['MAX_REVIEW_FILES'] = '100'
        self.run_review()
        quality = json.loads((self.consumer / 'cursor_review_results/code-quality-report.json').read_text())
        self.assertEqual({entry['location']['path'] for entry in quality}, set(names))

    def test_non_allowlisted_names_skip_model(self):
        self.commit({'README.md': 'docs\n', 'go.mod': 'module example\n',
                     'go.sum': 'module checksum\n', 'data.bin': b'\0binary',
                     'Dockerfile': 'FROM scratch\n', 'Makefile': 'all:\n\ttrue\n',
                     'CMakeLists.txt': 'project(example)\n', 'debian/control': 'Package: example\n',
                     'settings.toml': 'enabled = true\n', 'service.service': '[Unit]\n',
                     'source.js': 'const answer = 42;\n', 'source.PY': 'print(42)\n',
                     'extensionless': '#!/bin/sh\nexit 0\n'})
        self.run_review()
        self.assertFalse((self.consumer / 'prompts.jsonl').exists())

    def test_mixed_files_and_escaped_paths_reach_model(self):
        eligible = {'worker.py': 'print(42)\n', 'worker.go': 'package main\n',
                    'schema.yang': 'module example {}\n', 'api.thrift': 'struct Item {}\n',
                    'api.proto': 'syntax = "proto3";\n', 'script.sh': '#!/bin/sh\nexit 0\n',
                    'rules.mk': 'all:\n\ttrue\n', 'Dockerfile.j2': 'FROM scratch\n',
                    'config/quoted"name.yml': 'enabled: true\n',
                    'config/settings.yaml': 'enabled: true\n', 'config.json': '{}\n',
                    'package-lock.json': '{}\n'}
        self.commit({**eligible, 'README.md': 'docs\n', 'data.bin': b'\0binary',
                     'go.mod': 'module example\n', 'Dockerfile': 'FROM scratch\n'})
        self.run_review()
        artifacts = self.consumer / 'cursor_review_results'
        self.assertFalse((artifacts / 'file_selection.json').exists())
        prompts = [json.loads(line) for line in (self.consumer / 'prompts.jsonl').read_text().splitlines()]
        self.assertEqual(len(prompts), len(eligible))
        for name in eligible:
            self.assertTrue(any(f'File: {name}\n' in prompt for prompt in prompts), name)
        quality = json.loads((artifacts / 'code-quality-report.json').read_text())
        self.assertEqual({entry['location']['path'] for entry in quality}, set(eligible))

    def test_original_patch_marker_deduplicates(self):
        self.commit({'worker.go': 'package main\n', 'settings.yml': 'enabled: true\n'})
        patch = self.git('patch-id', '--stable', input=self.git('show', '--pretty=format:', 'HEAD')).split()[0]
        self.run_review()
        report = (self.consumer / 'cursor_review_results/review_report.md').read_text()
        self.assertIn(f'<!-- cursor-reviewed-patchids:{patch} -->', report)
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
