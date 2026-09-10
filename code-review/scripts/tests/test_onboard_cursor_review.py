import base64
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('onboard', Path(__file__).parents[1] / 'onboard-cursor-review.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def file_item(text):
    return dict(type='file', encoding='base64', sha='blob-sha', content=base64.b64encode(text.encode()).decode())


class OnboardingTest(unittest.TestCase):
    def args(self, *extra):
        return m.parse_args(['--repo', 'team/consumer', *extra])

    def test_cross_repo_entrypoint_preserves_consumer_context(self):
        text = m.render('release/v1', 'team/shared', 'abc123')
        self.assertIn('"team/shared/code-review@abc123"', text)
        self.assertIn('branches: ["release/v1"]', text)
        self.assertIn('${{ secrets.CURSOR_API_KEY }}', text)
        self.assertIn('head.repo.full_name == github.repository', text)
        self.assertNotIn('pull_request_target', text)
        self.assertNotIn('ci/code-review-cursor.sh', text)

    def test_default_branch_and_unmanaged_workflow_protection(self):
        def api(path, *args, **kwargs):
            if path == 'repos/team/consumer':
                return dict(default_branch='develop', permissions={'push': True})
            if '/git/ref/heads/' in path:
                return {'object': {'sha': 'base'}} if path.endswith('develop') else None
            if '/pulls?' in path:
                return []
            raise AssertionError(path)
        with patch.object(m, 'api', side_effect=api), patch.object(m, 'contents', return_value=None):
            plan = m.plan('team/consumer', self.args())
            self.assertEqual(plan['base'], 'develop')
            self.assertIn('branches: ["develop"]', plan['desired'])
        with patch.object(m, 'api', side_effect=api), patch.object(m, 'contents', return_value=file_item('existing workflow')):
            with self.assertRaisesRegex(m.SetupError, 'not managed'):
                m.plan('team/consumer', self.args())

    def test_secret_before_branch_then_single_file_pr(self):
        args = self.args()
        plan = dict(repo='team/consumer', base='main', base_sha='base',
                    branch_exists=False, current=None, desired='workflow', pr=None)
        events = []
        def gh(*a, **kw):
            events.append(('secret', a, kw))
        def api(path, data, method):
            events.append((method, path, data))
            return {'html_url': 'https://github.com/team/consumer/pull/1'}
        with patch.object(m, 'gh', side_effect=gh), patch.object(m, 'api', side_effect=api):
            result = m.apply(plan, args, 'test-key')
        self.assertEqual([e[0] for e in events], ['secret', 'POST', 'PUT', 'POST'])
        self.assertNotIn('test-key', repr(events[0][1]))
        self.assertEqual(events[0][2]['payload'], 'test-key')
        self.assertEqual(events[2][1], 'repos/team/consumer/contents/' + m.WORKFLOW)
        self.assertNotIn('test-key', repr(events[1:]) + result)

    def test_rerun_reuses_pr_and_unchanged_file(self):
        plan = dict(repo='team/consumer', branch_exists=True, current=file_item('same'),
                    desired='same', pr={'html_url': 'existing-pr'})
        with patch.object(m, 'gh'), patch.object(m, 'api') as api:
            self.assertIn('existing-pr', m.apply(plan, self.args(), 'key'))
            api.assert_not_called()

    def test_secret_failure_stops_branch_and_pr_writes(self):
        with patch.object(m, 'gh', side_effect=m.SetupError('upload failed')), patch.object(m, 'api') as api:
            with self.assertRaises(m.SetupError):
                m.apply({'repo': 'team/consumer'}, self.args(), 'key')
            api.assert_not_called()

    def test_secret_errors_do_not_echo_cli_output(self):
        result = subprocess.CompletedProcess([], 1, 'key-output', 'key-error')
        with patch.object(m.subprocess, 'run', return_value=result):
            with self.assertRaises(m.SetupError) as caught:
                m.gh(['secret', 'set', 'CURSOR_API_KEY'], payload='key', secret=True)
        self.assertNotIn('key-output', str(caught.exception))
        self.assertNotIn('key-error', str(caught.exception))

    def test_rotation_partial_failure_continues_without_code_writes(self):
        repos = ['team/consumer', 'team/second']
        out = io.StringIO()
        with patch.object(m.shutil, 'which', return_value='/bin/gh'), \
             patch.object(m, 'plan', side_effect=lambda repo, args: {'repo': repo}), \
             patch.object(m, 'read_key', return_value='test-key'), \
             patch.object(m, 'gh', side_effect=[m.SetupError('upload failed'), '']) as gh, \
             patch.object(m, 'api') as api, redirect_stdout(out), redirect_stderr(out):
            code = m.main(['--repo', repos[0], '--repo', repos[1], '--rotate-secrets'])
        self.assertEqual(code, 1)
        self.assertEqual(gh.call_count, 2)
        api.assert_not_called()
        self.assertIn('FAIL team/consumer', out.getvalue())
        self.assertIn('OK team/second', out.getvalue())
        self.assertNotIn('test-key', out.getvalue())

    def test_dry_run_never_reads_key_or_writes(self):
        with patch.object(m.shutil, 'which', return_value='/bin/gh'), \
             patch.object(m, 'plan', return_value={'repo': 'team/consumer'}), \
             patch.object(m, 'read_key') as key, patch.object(m, 'gh') as gh, redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(['--repo', 'team/consumer', '--rotate-secrets', '--dry-run']), 0)
            key.assert_not_called()
            gh.assert_not_called()

    def test_preflight_failure_prevents_all_writes(self):
        with patch.object(m.shutil, 'which', return_value='/bin/gh'), \
             patch.object(m, 'plan', side_effect=[{'repo': 'team/consumer'}, m.SetupError('no access')]), \
             patch.object(m, 'read_key') as key, patch.object(m, 'apply') as apply, redirect_stderr(io.StringIO()):
            self.assertEqual(m.main(['--repo', 'team/consumer', '--repo', 'team/second', '--rotate-secrets']), 1)
            key.assert_not_called()
            apply.assert_not_called()


if __name__ == '__main__':
    unittest.main()
