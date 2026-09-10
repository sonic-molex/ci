"""Exercise the shared script from a separate consumer checkout, without a model call."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class SharedActionTest(unittest.TestCase):
    def test_review_uses_consumer_git_and_shared_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            consumer = Path(temporary) / 'consumer'
            consumer.mkdir()
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=consumer, text=True, stderr=subprocess.DEVNULL).strip()
            git('init')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.invalid')
            git('commit', '--allow-empty', '-m', 'base')
            base = git('rev-parse', 'HEAD')
            (consumer / 'consumer.cpp').write_text('int answer() { return 42; }\n')
            git('add', 'consumer.cpp')
            git('commit', '-m', 'consumer change')
            head = git('rev-parse', 'HEAD')
            bin_dir = Path(temporary) / 'bin'
            bin_dir.mkdir()
            agent = bin_dir / 'cursor-agent'
            agent.write_text('#!/bin/sh\nif [ "$1" = --version ]; then echo test-agent; exit; fi\ncat > received-prompt.txt\nprintf "✅ No bugs found\\n"\n')
            agent.chmod(0o755)
            env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'],
                       CURSOR_API_KEY='test-only', CURSOR_MODEL='test-model',
                       GITHUB_TOKEN='', GITHUB_API_URL='', PR_NUMBER='1',
                       PR_DIFF_BASE_SHA=base, PR_HEAD_SHA=head)
            result = subprocess.run(['bash', str(ROOT / 'review.sh')],
                                    cwd=consumer, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            prompt = (consumer / 'received-prompt.txt').read_text()
            self.assertIn('File: consumer.cpp', prompt)
            self.assertIn('int answer()', prompt)
            self.assertNotIn('__CURSOR_', prompt)
            artifacts = consumer / 'cursor_review_results'
            report = (artifacts / 'review_report.md').read_text()
            self.assertIn('`consumer.cpp`', report)
            self.assertIn('No bugs found', report)
            quality = json.loads((artifacts / 'code-quality-report.json').read_text())
            self.assertEqual(quality[0]['location']['path'], 'consumer.cpp')


if __name__ == '__main__':
    unittest.main()
