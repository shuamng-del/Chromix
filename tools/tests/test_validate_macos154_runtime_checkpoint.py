import copy
import json
import os
import subprocess
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import validate_macos154_runtime_checkpoint as runtime
from tools.tests import test_validate_macos_helper_checkpoint as legacy

ROOT = Path(__file__).resolve().parents[2]


def client_for(profile):
    selected = runtime.PROFILES[profile]
    client = legacy.Client()
    client.run.update(id=selected['run'], path=f".github/workflows/build-macos-{selected['arch']}.yml")
    client.job.update(id=selected['job'], run_id=selected['run'],
                      name=f"macos-{selected['arch']} stage {selected['stage']} (resume compile)")
    client.job['steps'][0]['name'] = f"Run stage {selected['stage']}"
    client.artifacts = [dict(id=identifier,
        name=f"chromix-mac-{selected['arch']}-tree-s{selected['stage']}-attempt-1-part{index}",
        size_in_bytes=size, expired=False, digest='sha256:' + digest,
        workflow_run={'id': selected['run'], 'head_sha': runtime.helper.SHA})
        for index, (identifier, size, digest) in enumerate(selected['artifacts'], 1)]
    client.get = lambda path: {
        f"/actions/runs/{selected['run']}": client.run,
        f"/actions/jobs/{selected['job']}": client.job,
    }[path]
    return client


class MetadataTest(unittest.TestCase):
    def validate(self, profile, client=None, ids=None):
        return runtime.validate_metadata(client or client_for(profile), runtime.helper.REPOSITORY,
            ids if ids is not None else [item[0] for item in runtime.PROFILES[profile]['artifacts']], profile)

    def test_both_exact_profiles(self):
        for profile in runtime.PROFILES:
            with self.subTest(profile=profile):
                result = self.validate(profile)
                self.assertEqual(result['profile'], profile)
                self.assertEqual(result['head_sha'], runtime.helper.SHA)
                self.assertEqual(len(result['artifacts']), 2)

    def test_size_checksum_part_and_incomplete_sets_fail_closed(self):
        for profile in runtime.PROFILES:
            for field, value in {'size_in_bytes': 1, 'digest': 'sha256:' + 'f' * 64,
                                 'expired': True, 'name': 'wrong-part', 'id': 1}.items():
                client = client_for(profile)
                client.artifacts[0][field] = value
                with self.subTest(profile=profile, field=field), self.assertRaises(ValueError):
                    self.validate(profile, client)
            for ids in ([], [runtime.PROFILES[profile]['artifacts'][0][0]], [1, 2]):
                with self.subTest(profile=profile, ids=ids), self.assertRaises(ValueError):
                    self.validate(profile, ids=ids)

    def test_producer_identity_and_runtime_preservation_are_mandatory(self):
        for profile in runtime.PROFILES:
            for subject, field, value in (
                    ('run', 'head_branch', 'main'), ('run', 'run_attempt', 2),
                    ('run', 'head_sha', 'a' * 40), ('run', 'conclusion', 'success'),
                    ('job', 'id', 1), ('job', 'run_attempt', 2), ('job', 'conclusion', 'success')):
                client = client_for(profile)
                getattr(client, subject)[field] = value
                with self.subTest(profile=profile, field=field), self.assertRaises(ValueError):
                    self.validate(profile, client)
            for index in range(len(client_for(profile).job['steps'])):
                client = client_for(profile)
                client.job['steps'][index]['conclusion'] = 'skipped'
                with self.subTest(profile=profile, step=index), self.assertRaises(ValueError):
                    self.validate(profile, client)


class RuntimeRecipeTest(legacy.RecipeTest):
    def target_selection(self, root, *args, **kwargs):
        old = ['patch'] * 224
        if root == self.donor:
            return self.identity, old
        return {'selection': 'normal-quit'}, old + [(runtime.PATCH, (ROOT / runtime.PATCH).read_bytes())]

    def test_runtime_recipe_checks_prepare_identity_without_executing_donor(self):
        self.put(self.src, '.chromix-toolchain-ready', '')
        profile = 'macos154-x64-runtime-v1'
        with patch.object(runtime.migration, '_donor_git', side_effect=lambda d, r, *args:
                          runtime.helper.SHA.encode() if args[0] == 'rev-parse' else b''), \
                patch.object(runtime.migration, '_same_inputs') as policy, \
                patch.object(runtime.migration, 'source_ready_key', return_value=self.key), \
                patch.object(runtime.patch_selection, 'select', side_effect=self.target_selection):
            result = runtime.recipe(self.repo, self.donor, self.work, profile)
            policy.assert_called_once_with(self.donor, self.repo, 'macos', 'x64')
            self.assertTrue(result['migration_required'])
            self.assertEqual(set(result['state']), set(runtime.STATE_FILES))
            policy.side_effect = ValueError('preparation tooling changed')
            with self.assertRaisesRegex(ValueError, 'preparation tooling'):
                runtime.recipe(self.repo, self.donor, self.work, profile)

    def test_runtime_prefix_and_arm64_args_are_strict(self):
        self.put(self.src, '.chromix-toolchain-ready', '')
        profile = 'macos154-arm64-runtime-v1'
        with patch.object(runtime.migration, '_donor_git', side_effect=lambda d, r, *args:
                          runtime.helper.SHA.encode() if args[0] == 'rev-parse' else b''), \
                patch.object(runtime.migration, '_same_inputs'), \
                patch.object(runtime.migration, 'source_ready_key', return_value=self.key), \
                patch.object(runtime.patch_selection, 'select', side_effect=self.target_selection):
            with self.assertRaisesRegex(ValueError, 'GN identity'):
                runtime.recipe(self.repo, self.donor, self.work, profile)
            path = self.src / 'out/Default/args.gn'
            path.write_text(path.read_text().replace('"x64"', '"arm64"'))
            self.assertTrue(runtime.recipe(self.repo, self.donor, self.work, profile)['migration_required'])
            for patches in (['changed'] + ['patch'] * 224, ['patch'] * 227, ['patch'] * 223):
                with patch.object(runtime.patch_selection, 'select', side_effect=[
                        (self.identity, ['patch'] * 224), ({'selection': 'new'}, patches)]):
                    with self.assertRaisesRegex(ValueError, '224-patch donor prefix'):
                        runtime.recipe(self.repo, self.donor, self.work, profile)


    def test_wrong_official_patch_content_is_rejected(self):
        self.put(self.src, '.chromix-toolchain-ready', '')
        with patch.object(runtime.migration, '_donor_git', side_effect=lambda d, r, *args:
                          runtime.helper.SHA.encode() if args[0] == 'rev-parse' else b''), \
                patch.object(runtime.migration, '_same_inputs'), \
                patch.object(runtime.patch_selection, 'select', side_effect=[
                    (self.identity, ['patch'] * 224),
                    ({'selection': 'normal-quit'}, ['patch'] * 224 + [(runtime.PATCH, b'changed')])]):
            with self.assertRaisesRegex(ValueError, 'official normal-quit'):
                runtime.recipe(self.repo, self.donor, self.work, 'macos154-x64-runtime-v1')


class SourceBoundaryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / 'work'
        self.producer = self.work / 'fingerprint-diagnostics/source-final.json'
        self.producer.parent.mkdir(parents=True)
        self.receipt = dict(status='verified', patch_count=224, identity={'selection': 'old'},
                            outputs={'chrome/file.cc': 'a' * 64})
        self.producer.write_text(json.dumps(self.receipt))
        self.recipe = dict(profile='macos154-arm64-runtime-v1', source_sha=runtime.helper.SHA,
                           donor_identity=self.receipt['identity'], identity=self.receipt['identity'],
                           patch_count=224, previous_key='old-key', key='old-key',
                           migration_required=False, state={name: 'unchanged' for name in runtime.STATE_FILES})
        for name, kwargs in (
                ('recipe', {'side_effect': lambda *args: copy.deepcopy(self.recipe)}),
                ('live_receipt', {'side_effect': lambda *args: copy.deepcopy(self.receipt)}),
                ('migration._ready', {})):
            mocker = patch('tools.validate_macos154_runtime_checkpoint.' + name, **kwargs)
            mocker.start()
            self.addCleanup(mocker.stop)

    def verify(self, baseline=None, prepared=False, build_phase="migration"):
        return runtime.verify_source(self.root / 'target', self.root / 'donor', self.work,
            self.recipe['profile'], self.root / 'proof.json', baseline=baseline, prepared=prepared,
            build_phase=build_phase)

    def baseline(self):
        path = self.root / 'baseline.json'
        path.write_text(json.dumps(self.verify()))
        return path

    def test_producer_receipt_and_live_donor_must_agree(self):
        result = self.verify()
        self.assertEqual(result['phase'], 'source-before')
        bad = dict(self.receipt, outputs={'chrome/file.cc': 'b' * 64})
        self.producer.write_text(json.dumps(bad))
        with self.assertRaisesRegex(ValueError, 'producer/donor'):
            self.verify()

    def test_after_migration_preserves_producer_and_ninja_state(self):
        baseline = self.baseline()
        self.assertEqual(self.verify(baseline)['phase'], 'source-after')
        self.recipe['state']['out/Default/args.gn'] = 'rewritten'
        with self.assertRaisesRegex(ValueError, 'state changed'):
            self.verify(baseline)

    def test_target_migration_receipt_is_not_compared_to_old_source(self):
        self.recipe.update(identity={'selection': 'new'}, patch_count=225,
                           key='new-key', migration_required=True)
        baseline = self.baseline()
        self.receipt.update(identity={'selection': 'new'}, patch_count=225,
                            outputs={'chrome/file.cc': 'b' * 64})
        result = self.verify(baseline)
        self.assertEqual(result['patch_count'], 225)
        self.assertEqual(json.loads(self.producer.read_text())['patch_count'], 224)

    def test_prepared_requires_zero_invalidation_and_new_source_identity(self):
        baseline = self.baseline()
        counters = dict(first_finish=0, tool_swap_invalidations=0, toolchain_invalidated_outputs=0)
        path = self.work / 'upstream-cache-preparation.json'
        path.write_text(json.dumps(dict(ready_for_gn=True, counters=counters)))
        self.verify(baseline, prepared=True)
        for field in counters:
            path.write_text(json.dumps(dict(ready_for_gn=True, counters=dict(counters, **{field: 1}))))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'invalidation'):
                self.verify(baseline, prepared=True)


class BuildPhaseTest(SourceBoundaryTest):
    def prepare(self):
        (self.work / 'upstream-cache-preparation.json').write_text(json.dumps(dict(
            ready_for_gn=True, counters=dict(first_finish=0, tool_swap_invalidations=0,
                                             toolchain_invalidated_outputs=0))))

    def test_graph_change_is_recorded_not_claimed_unchanged(self):
        baseline = self.baseline()
        self.prepare()
        self.recipe['state']['out/Default/build.ninja'] = 'regenerated'
        with self.assertRaisesRegex(ValueError, 'state changed'):
            self.verify(baseline, prepared=True)
        result = self.verify(baseline, prepared=True, build_phase='graph')
        self.assertEqual(result['state_transition']['changed_fields'], ['out/Default/build.ninja'])
        self.recipe['state']['out/Default/.ninja_log'] = 'compiled'
        with self.assertRaisesRegex(ValueError, 'state changed'):
            self.verify(baseline, prepared=True, build_phase='graph')
        result = self.verify(baseline, prepared=True, build_phase='compiled')
        self.assertIn('out/Default/.ninja_log', result['state_transition']['changed_fields'])

    def test_args_are_immutable_even_after_compilation(self):
        baseline = self.baseline()
        self.prepare()
        self.recipe['state']['out/Default/args.gn'] = 'rewritten'
        with self.assertRaisesRegex(ValueError, 'state changed'):
            self.verify(baseline, prepared=True, build_phase='compiled')

    def test_graph_phase_requires_prepared_and_baseline(self):
        with self.assertRaisesRegex(ValueError, 'require prepared'):
            self.verify(build_phase='graph')
        with self.assertRaisesRegex(ValueError, 'require prepared'):
            self.verify(self.baseline(), build_phase='compiled')


class WorkflowTest(unittest.TestCase):
    def test_incremental_native_pipeline_and_exact_interfaces(self):
        text = (ROOT / '.github/workflows/repair-macos154-runtime.yml').read_text()
        self.assertLess(len(text.splitlines()), 500)
        for required in ('runner: macos-15\n', 'runner: macos-15-intel', 'fail-fast: false',
                         'timeout-minutes: 355', runtime.helper.SHA, '--previous-repo "$DONOR"',
                         '--repo "$GITHUB_WORKSPACE" --workdir "$WORK"',
                         'source-before', 'source-after', '--prepared', 'if: always()',
                         "steps.admission.outcome == 'success'", 'build/posix/ci-parts.sh',
                         'unaccepted-attempt-', 'snapshot_volumes.py', 'BUDGET=14400',
                         'RC=${PIPESTATUS[0]}', 'TARGETS=(chrome)',
                         'TARGETS=(chrome_helper_app_default chrome)', '--build-phase graph',
                         '--build-phase compiled', 'source-final-target.json',
                         'tools/verify_macos_bundle.py --zip', 'tools/macos_browser_smoke.py',
                         'tools/fingerprint_acceptance.py', 'tools/browser_feature_check.py'):
            self.assertIn(required, text)
        for forbidden in ('workflow_call:', '  push:', '  pull_request:', 'ci-stage.sh', '--resume',
                          'fetch-upstream-cache', 'build/prepare-ungoogled.sh', 'release_browser',
                          'merge_gn_args.py', '--no-sandbox', 'continue-on-error:', 'git reset',
                          'git clean', '--use-mock-keychain', '--disable-features='):
            self.assertNotIn(forbidden, text)
        ordered = ('source-before', 'migrate_restored_snapshot.py', 'source-migrated.json',
                   'prepare-restored-tools.sh', 'bootstrap_gn.py', 'repair_macos_helper_outputs.py',
                   'TARGETS=(chrome)', 'source-final.json', 'Package and verify',
                   'tools/macos_browser_smoke.py', 'tools/fingerprint_acceptance.py',
                   'tools/browser_feature_check.py', 'Upload candidate ZIP', 'id: checkpoint')
        for before, after in zip(ordered, ordered[1:]):
            self.assertLess(text.index(before), text.index(after))

    def test_helper_architecture_and_unaccepted_failure_artifacts(self):
        import yaml
        workflow = yaml.safe_load((ROOT / '.github/workflows/repair-macos154-runtime.yml').read_text())
        steps = workflow['jobs']['repair']['steps']
        repair = next(s for s in steps if 'repair_macos_helper_outputs.py' in s.get('run', ''))
        self.assertEqual(repair['if'], "matrix.arch == 'x64'")
        arm = next(s for s in steps if s.get('name', '').startswith('Require intact ARM64'))
        self.assertEqual(arm['if'], "matrix.arch == 'arm64'")
        self.assertIn('exit 1', arm['run'])
        for step in steps:
            if step.get('id') in ('smoke', 'acceptance', 'features'):
                self.assertNotIn('continue-on-error', step)
            if step.get('uses') == 'actions/upload-artifact@v4':
                self.assertTrue('unaccepted' in step['with']['name'] or 'evidence' in step['with']['name'])
        candidate = next(s for s in steps if s.get('name', '').startswith('Upload candidate ZIP'))
        self.assertEqual(candidate['if'], 'always()')


    def test_compile_timeout_and_budget_failure_return_one_without_fallback(self):
        import yaml
        workflow = yaml.safe_load((ROOT / '.github/workflows/repair-macos154-runtime.yml').read_text())
        script = next(s['run'] for s in workflow['jobs']['repair']['steps'] if s.get('id') == 'compile')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, content in {
                    'python3': '#!/bin/bash\nprintf "/unused/ninja\\n"\n',
                    'gtimeout': '#!/bin/bash\nprintf "%s\\n" "$@" > "$EVIDENCE/invocation"\nexit "${FAKE_RC:-0}"\n',
            }.items():
                path = root / name
                path.write_text(content)
                path.chmod(0o755)
            environment = dict(os.environ, PATH=str(root) + ':' + os.environ['PATH'],
                               WORK=str(root / 'work'), EVIDENCE=str(root), CHROMIX_JOBS='4',
                               CHROMIX_JOB_DEADLINE=str(int(time.time()) + 350 * 60))
            for arch in ('arm64', 'x64'):
                for code in ('0', '124', '137', '2'):
                    result = subprocess.run(['/bin/bash', '-c', script], env=dict(environment, ARCH=arch, FAKE_RC=code),
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0 if code == '0' else 1)
                    invocation = (root / 'invocation').read_text().splitlines()
                    self.assertEqual(invocation[-1], 'chrome')
                    self.assertEqual('chrome_helper_app_default' in invocation, arch == 'x64')
            (root / 'invocation').unlink()
            result = subprocess.run(['/bin/bash', '-c', script], env=dict(environment, ARCH='arm64',
                                    CHROMIX_JOB_DEADLINE=str(int(time.time()) + 60)), capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertFalse((root / 'invocation').exists())


if __name__ == '__main__':
    unittest.main()
