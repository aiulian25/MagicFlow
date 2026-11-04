"""Host-only tests for test infrastructure; no app imports or media/data access."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('test_entrypoint', ROOT / 'scripts/test.py')
runner = importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)


class TestIsolationContract(unittest.TestCase):
    def test_container_plan_can_mount_only_readonly_tests_and_disposable_data(self):
        command = runner.container_command('sha256:fixture', 'fixture', 'owned', 'pass')
        self.assertIn('--rm', command)
        self.assertEqual(command[command.index('--network')+1], 'none')
        self.assertIn('--read-only', command)
        mounts = [command[i+1] for i, arg in enumerate(command) if arg == '--mount']
        self.assertEqual(mounts, [f'type=bind,source={ROOT / "tests"},target=/tests,readonly'])
        temporary = [command[i+1] for i, arg in enumerate(command) if arg == '--tmpfs']
        self.assertIn('/data:rw,size=3g', temporary)
        self.assertNotIn('-v', command)

    def test_every_application_test_module_is_assigned_to_media_or_fidelity(self):
        actual = {p.stem for p in (ROOT / 'tests').glob('test_*.py')} - {'test_test_runner'}
        assigned = runner.SUITES['media'] + runner.SUITES['fidelity']
        self.assertEqual(set(assigned), actual)
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertLessEqual(set(runner.SUITES['api']), set(assigned))

    def test_cleanup_rejects_a_resource_whose_owner_changed(self):
        with patch.object(runner, 'capture', side_effect=['other', json.dumps({runner.LABEL:'someone-else'})]), patch.object(subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):runner.cleanup('owned')
            run.assert_not_called()

    def test_cleanup_never_prunes_or_stops_unrelated_resources(self):
        with patch.object(runner, 'capture', side_effect=['own', json.dumps({runner.LABEL:'owned'}), '', '']), patch.object(subprocess, 'run') as run:
            runner.cleanup('owned')
            self.assertEqual(run.call_args.args[0], ['docker','stop','--time','10','own'])

    def test_failed_job_still_cleans_up_and_retains_external_log(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory)
            def fail(command, destination, **kwargs):
                destination.write_text('meaningful failing test output');return 1
            with patch.object(runner, 'logged', side_effect=fail), patch.object(runner, 'cleanup') as cleanup:
                self.assertEqual(runner.run_container('api','sha256:fixture',logs,'owned'),1)
                cleanup.assert_called_once_with('owned')
            self.assertIn('meaningful', (logs/'api.log').read_text())

    def test_isolation_requires_actual_probe_output_not_the_command_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory)
            def failed_setup(command, destination, **kwargs):
                destination.write_text(json.dumps({'command': command}) + '\nsetup failed\n')
                return 1
            with patch.object(runner, 'logged', side_effect=failed_setup), patch.object(runner, 'cleanup'):
                with self.assertRaises(RuntimeError):
                    runner.run_container('isolation', 'fixture', logs, 'owned')

    def test_isolation_recognizes_the_expected_failure_after_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory)
            def expected_failure(command, destination, **kwargs):
                destination.write_text('MAGICFLOW_EXPECTED_TEST_FAILURE\nAssertionError: deliberate failure\n')
                return 1
            with patch.object(runner, 'logged', side_effect=expected_failure), patch.object(runner, 'cleanup') as cleanup:
                self.assertEqual(runner.run_container('isolation', 'fixture', logs, 'owned'), 0)
                cleanup.assert_called_once_with('owned')

    def test_timeout_still_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(runner, 'logged', side_effect=subprocess.TimeoutExpired('fixture',1)), patch.object(runner, 'cleanup') as cleanup:
            with self.assertRaises(subprocess.TimeoutExpired):runner.run_container('api','fixture',Path(directory),'owned')
            cleanup.assert_called_once_with('owned')

    def test_invalid_image_resolution_cannot_pass_without_running_tests(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(runner, 'capture', return_value='not-an-image-id'), patch.object(runner, 'run_container') as run:
            with self.assertRaises(RuntimeError):
                runner.main(['api', '--image', 'fixture', '--logs', directory])
            run.assert_not_called()

    def test_missing_image_is_rejected_before_any_test_can_touch_host_data(self):
        with patch.object(runner, 'capture') as capture:
            with self.assertRaises(SystemExit) as error:runner.main(['api'])
            self.assertEqual(error.exception.code,2);capture.assert_not_called()
