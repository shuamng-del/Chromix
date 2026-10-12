import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import validate_macos_helper_checkpoint as repair

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / '.github/workflows/repair-macos-x64-helper.yml'


class Client:
    def __init__(self):
        self.calls = []
        self.run = dict(id=repair.RUN, path='.github/workflows/build-macos-x64.yml',
                        head_branch=repair.BRANCH, head_sha=repair.SHA, event='workflow_dispatch',
                        repository={'full_name': repair.REPOSITORY},
                        head_repository={'full_name': repair.REPOSITORY},
                        status='completed', conclusion='failure', run_attempt=1)
        self.job = dict(id=repair.JOB, run_id=repair.RUN, head_sha=repair.SHA, run_attempt=1,
                        name='macOS Intel / macos-x64 stage 2 (resume compile)',
                        status='completed', conclusion='failure', steps=[
                            {'name': name, 'conclusion': conclusion} for name, conclusion in {
                                'Run stage 2': 'failure', 'Upload failed macOS runtime bundle': 'success',
                                'Preserve failed macOS runtime checkpoint': 'success',
                                'Verify handoff snapshot': 'success',
                                **{f'Upload tree part {n}': 'success' for n in range(1, 5)},
                            }.items()])
        self.artifacts = [dict(id=identifier,
                               name=f'chromix-mac-x64-tree-s2-attempt-1-part{index}',
                               size_in_bytes=1024, expired=False, digest='sha256:' + 'b' * 64,
                               workflow_run={'id': repair.RUN, 'head_sha': repair.SHA})
                          for index, identifier in enumerate(repair.ARTIFACT_IDS, 1)]
        self.failed = dict(self.artifacts[0], id=repair.FAILED_ARTIFACT,
                           name='chromix-mac-x64-failed-runtime-s2-attempt-1')

    def get(self, path):
        self.calls.append(path)
        return {f'/actions/runs/{repair.RUN}': self.run,
                f'/actions/jobs/{repair.JOB}': self.job,
                f'/actions/artifacts/{repair.FAILED_ARTIFACT}': self.failed}[path]

    def items(self, path, key):
        self.calls.append(path)
        return [self.job] if key == 'jobs' else self.artifacts


class MetadataTest(unittest.TestCase):
    def validate(self, client):
        return repair.validate_metadata(client, repair.REPOSITORY, list(repair.ARTIFACT_IDS))

    def test_completed_failure_is_the_only_selected_donor(self):
        client = Client()
        result = self.validate(client)
        self.assertEqual(result['job_id'], repair.JOB)
        self.assertEqual(result['head_sha'], repair.SHA)
        self.assertEqual(result['failed_runtime_artifact']['id'], repair.FAILED_ARTIFACT)
        self.assertEqual([entry['id'] for entry in result['artifacts']], list(repair.ARTIFACT_IDS))
        self.assertIn(f'/actions/runs/{repair.RUN}/attempts/1/jobs', client.calls)

    def test_artifact_selection_cannot_follow_latest_or_other_stage(self):
        for ids in ([], [repair.FAILED_ARTIFACT], [repair.ARTIFACT_IDS[0]],
                    [*repair.ARTIFACT_IDS, repair.ARTIFACT_IDS[0]], [1, 2]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                repair.validate_metadata(Client(), repair.REPOSITORY, ids)
        with self.assertRaises(ValueError):
            repair.validate_metadata(Client(), 'foreign/Chromix', list(repair.ARTIFACT_IDS))

    def test_run_job_sha_attempt_branch_and_terminal_status_are_fixed(self):
        cases = {
            'run': {'head_sha': 'a' * 40, 'head_branch': 'main', 'event': 'push',
                    'run_attempt': 2, 'conclusion': 'success', 'status': 'in_progress'},
            'job': {'id': 1, 'run_id': 1, 'head_sha': 'a' * 40, 'run_attempt': 2,
                    'conclusion': 'success', 'status': 'in_progress'},
        }
        for subject, fields in cases.items():
            for field, value in fields.items():
                client = Client()
                getattr(client, subject)[field] = value
                with self.subTest(subject=subject, field=field), self.assertRaises(ValueError):
                    self.validate(client)

    def test_every_runtime_checkpoint_and_upload_step_is_required(self):
        for index in range(len(Client().job['steps'])):
            client = Client()
            client.job['steps'][index]['conclusion'] = 'skipped'
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.validate(client)

    def test_missing_expired_wrong_or_digestless_artifacts_rejected(self):
        for field, value in {'expired': True, 'size_in_bytes': 0, 'digest': None,
                             'name': 'chromix-mac-x64-tree-s1-attempt-1-part1',
                             'workflow_run': {'id': 1, 'head_sha': repair.SHA}}.items():
            for subject in ('tree', 'failed'):
                client = Client()
                target = client.artifacts[0] if subject == 'tree' else client.failed
                target[field] = value
                with self.subTest(subject=subject, field=field), self.assertRaises(ValueError):
                    self.validate(client)
        client = Client()
        client.artifacts.append(dict(client.artifacts[0], id=123))
        with self.assertRaises(ValueError):
            self.validate(client)


class RecipeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.donor, self.work = [self.root / name for name in ('target', 'donor', 'work')]
        self.src = self.work / 'src'
        for directory in (self.repo, self.donor, self.src):
            directory.mkdir(parents=True)
        self.pins = {'ChromiumVersion': repair.VERSION, 'UngoogledCommit': 'a' * 40,
                     'UngoogledMacOSCommit': 'b' * 40}
        self.identity = {'patches': [str(n) for n in range(224)]}
        self.key = f"macos|x64|{repair.VERSION}|{'a' * 40}|{'b' * 40}|{'c' * 64}"
        for directory in (self.repo, self.donor):
            for name in repair.RECIPE_FILES:
                self.put(directory, name, 'same recipe')
        for name in ('.chromix-upstream-restored.json', '.chromix-restored-build-prepared.json',
                     'out/Default/build.ninja', 'out/Default/.ninja_log', 'out/Default/.ninja_deps'):
            self.put(self.src, name, 'preserved')
        self.put(self.src, '.chromix-source-ready', self.key + '\n')
        self.put(self.src, 'out/Default/args.gn', 'target_cpu = "x64"\nv8_target_cpu = "x64"\n'
                 'target_os = "mac"\nthin_lto_enable_optimizations = false\n')
        for name, kwargs in (
            ('subprocess.check_output', {'side_effect': lambda command, **kw:
                                        repair.SHA + '\n' if 'rev-parse' in command else ''}),
            ('load_pins', {'return_value': self.pins}),
            ('patch_selection.select', {'return_value': (self.identity, ['patch'] * 224)}),
            ('patch_selection.preparation_key', {'return_value': 'c' * 64}),
        ):
            mocked = patch('tools.validate_macos_helper_checkpoint.' + name, **kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)

    def put(self, root, name, value):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def verify(self):
        return repair.verify_recipe(self.repo, self.donor, self.work)

    def test_matching_recipe_is_read_only(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.src.rglob('*') if p.is_file()}
        self.assertEqual(self.verify()['preparation_key'], self.key)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})

    def test_modified_prepare_or_macos_build_is_rejected(self):
        for name in repair.RECIPE_FILES:
            self.put(self.repo, name, 'modified')
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'recipe changed'):
                self.verify()
            self.put(self.repo, name, 'same recipe')

    def test_no_ready_stamp_rewrite_or_missing_ninja_state_fallback(self):
        self.put(self.src, '.chromix-source-ready', 'wrong')
        with self.assertRaisesRegex(ValueError, 'never rewrite'):
            self.verify()
        self.assertEqual((self.src / '.chromix-source-ready').read_text(), 'wrong')
        self.put(self.src, '.chromix-source-ready', self.key)
        (self.src / 'out/Default/.ninja_log').unlink()
        with self.assertRaisesRegex(ValueError, 'incremental state'):
            self.verify()

    def test_fast_x64_args_are_not_rewritten(self):
        args = self.src / 'out/Default/args.gn'
        original = args.read_text()
        for modified in (original.replace('"x64"', '"arm64"'),
                         original.replace('false', 'true')):
            args.write_text(modified)
            with self.assertRaisesRegex(ValueError, 'fast GN identity'):
                self.verify()
            self.assertEqual(args.read_text(), modified)

    def test_baseline_rejects_args_rewrite_and_full_invalidation(self):
        baseline = self.root / 'baseline.json'
        baseline.write_text(json.dumps(dict(self.verify(), status='verified')))
        prepared = {'ready_for_gn': True, 'counters': {
            'first_finish': 0, 'tool_swap_invalidations': 0, 'toolchain_invalidated_outputs': 0}}
        self.put(self.work, 'upstream-cache-preparation.json', json.dumps(prepared))
        args = self.src / 'out/Default/args.gn'
        original = args.read_text()
        args.write_text(original + '# rewritten\n')
        with self.assertRaisesRegex(ValueError, 'args_sha256'):
            repair.verify_source(self.repo, self.donor, self.work, self.root / 'proof.json', baseline=baseline)
        args.write_text(original)
        for field in prepared['counters']:
            changed = copy.deepcopy(prepared)
            changed['counters'][field] = 1
            self.put(self.work, 'upstream-cache-preparation.json', json.dumps(changed))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'full toolchain'):
                repair.verify_source(self.repo, self.donor, self.work, self.root / 'proof.json', baseline=baseline)

    def test_receipt_hash_mismatch_fails_without_executing_donor_code(self):
        receipt = {'status': 'verified', 'patch_count': 224, 'identity': self.identity,
                   'outputs': {'chrome/file.cc': 'a' * 64}}
        self.put(self.work, 'fingerprint-diagnostics/source-final.json', json.dumps(receipt))
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            output = Path(command[command.index('--output') + 1])
            output.write_text(json.dumps(receipt))

        report = self.root / 'proof.json'
        with patch.object(repair.subprocess, 'run', side_effect=run):
            result = repair.verify_source(self.repo, self.donor, self.work, report)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(len(commands), 2)
        self.assertTrue(all(c[1] == str(self.repo / 'tools/verify_patch_stack.py') for c in commands))
        bad = copy.deepcopy(receipt)
        bad['outputs']['chrome/file.cc'] = 'b' * 64
        self.put(self.work, 'fingerprint-diagnostics/source-final.json', json.dumps(bad))
        with patch.object(repair.subprocess, 'run', side_effect=run), self.assertRaisesRegex(ValueError, 'hashes differ'):
            repair.verify_source(self.repo, self.donor, self.work, report)


class WorkflowTest(unittest.TestCase):
    def test_dispatch_only_no_reset_migration_or_cold_fallback(self):
        text = WORKFLOW.read_text()
        self.assertIn('  workflow_dispatch:', text)
        for forbidden in ('  push:', '  pull_request:', 'workflow_call:', 'ci-stage.sh',
                          'migrate_restored_snapshot.py', 'merge_gn_args.py', 'git reset',
                          'git clean', 'rm -rf', '--no-sandbox', 'codesign --sign',
                          'build/macos/build.sh', 'bash build/prepare-ungoogled.sh'):
            self.assertNotIn(forbidden, text)
        for required in ('runs-on: macos-15-intel', 'CHROMIX_BUILD_PROFILE: fast',
                         '11719847390,11719568394', repair.SHA, 'restore_posix_snapshot.py',
                         'prepare-restored-tools.sh', 'chrome_helper_app_default chrome',
                         'tools/macos_browser_smoke.py', 'tools/fingerprint_acceptance.py',
                         'tools/verify_macos_bundle.py --zip', 'if: always()'):
            self.assertIn(required, text)
        self.assertLess(text.index('source-before.json'), text.index('prepare-restored-tools.sh'))
        self.assertLess(text.index('tools/macos_browser_smoke.py'), text.index('tools/fingerprint_acceptance.py'))
        self.assertLess(text.index('tools/fingerprint_acceptance.py'), text.index('Upload accepted repaired bundle'))


if __name__ == '__main__':
    unittest.main()
