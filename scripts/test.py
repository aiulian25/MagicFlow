#!/usr/bin/env python3
"""Dependency-free test entry point. Application tests run only in disposable containers."""
import argparse
import ast
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
SUITES = json.loads((ROOT / 'tests/suites.json').read_text())
LABEL = 'magicflow.test_run'


def container_command(image, name, run_id, code):
    return ['docker', 'run', '--rm', '--name', name,
            '--label', f'{LABEL}={run_id}', '--label', 'magicflow.task=fix026',
            '--network', 'none', '--read-only', '--security-opt', 'no-new-privileges:true',
            '--memory', '3g', '--cpus', '2', '--pids-limit', '512',
            '--tmpfs', '/data:rw,size=3g', '--tmpfs', '/tmp:rw,size=512m',
            '--mount', f'type=bind,source={ROOT / "tests"},target=/tests,readonly',
            '--entrypoint', 'python3', image, '-I', '-c', code]


def capture(command):
    return subprocess.run(command, text=True, capture_output=True, check=True).stdout.strip()


def cleanup(run_id):
    """Touch only this invocation/job's label; never prune global Docker state."""
    names = capture(['docker', 'ps', '-a', '--filter', f'label={LABEL}={run_id}', '--format', '{{.Names}}']).splitlines()
    for name in names:
        labels = json.loads(capture(['docker', 'inspect', name, '--format', '{{json .Config.Labels}}']))
        if labels.get(LABEL) != run_id:
            raise RuntimeError('Refusing cleanup of a container with another owner: ' + name)
        subprocess.run(['docker', 'stop', '--time', '10', name], check=True, capture_output=True)
    # Recovery checks may have created labelled, disposable named volumes.
    volumes = capture(['docker', 'volume', 'ls', '--filter', f'label={LABEL}={run_id}', '--format', '{{.Name}}']).splitlines()
    for volume in volumes:
        labels = json.loads(capture(['docker', 'volume', 'inspect', volume, '--format', '{{json .Labels}}']))
        if labels.get(LABEL) != run_id:
            raise RuntimeError('Refusing cleanup of a volume with another owner: ' + volume)
        subprocess.run(['docker', 'volume', 'rm', volume], check=True, capture_output=True)
    remaining = capture(['docker', 'ps', '-a', '--filter', f'label={LABEL}={run_id}', '--format', '{{.Names}}'])
    if remaining:
        raise RuntimeError('Test containers remain: ' + remaining)


def logged(command, destination, timeout=600):
    with destination.open('w') as log:
        log.write(json.dumps({'command': command}) + '\n');log.flush()
        return subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=timeout).returncode


def run_container(suite, image, logs, run_id):
    name = 'magicflow-test-' + suite + '-' + uuid.uuid4().hex[:10]
    code = "import subprocess;subprocess.run(['/entrypoint.sh','true'],check=True);"
    if suite == 'isolation':
        code += ("from pathlib import Path;import os;"
                 "assert os.stat('/data').st_dev != os.stat('/app').st_dev;"
                 "Path('/data/only-test-data').write_text('disposable');"
                 "print('MAGICFLOW_EXPECTED_TEST_FAILURE',flush=True);"
                 "raise AssertionError('deliberate cleanup/log-retention probe')")
    else:
        code += ("import sys,unittest;sys.path.insert(0,'/tests');"
                 f"suite=unittest.defaultTestLoader.loadTestsFromNames({SUITES[suite]!r});"
                 "result=unittest.TextTestRunner(verbosity=2).run(suite);"
                 "sys.exit(not result.wasSuccessful())")
    command = container_command(image, name, run_id, code)
    (logs / f'{suite}-plan.json').write_text(json.dumps(command, indent=2) + '\n')
    try:
        result = logged(command, logs / f'{suite}.log')
    finally:
        cleanup(run_id)
    if suite == 'isolation':
        if result != 1 or 'MAGICFLOW_EXPECTED_TEST_FAILURE' not in (logs / 'isolation.log').read_text().splitlines():
            raise RuntimeError('The deliberate failing probe did not run as expected.')
        print('Deliberate failure verified: log retained and container removed.')
        return 0
    return result


def syntax(logs):
    # AST parsing does not import tests or open their hard-coded data paths.
    for directory in ('app', 'tests', 'scripts'):
        for path in (ROOT / directory).glob('*.py'):
            ast.parse(path.read_text(), filename=str(path))
    html = (ROOT / 'app/static/index.html').read_text()
    script = logs / 'inline-ui.js'
    script.write_text(html[html.index('<script>') + 8:html.rindex('</script>')])
    commands = [['node', '--check', str(script)],
                ['sh', '-n', str(ROOT / 'entrypoint.sh'), str(ROOT / 'scripts/build-release.sh')]]
    commands += [['node', '--check', str(path)] for path in sorted((ROOT / 'tests').glob('*.test.mjs'))]
    commands += [['node', '--check', str(path)] for path in sorted((ROOT / 'app/static').glob('*.js'))]
    commands.append([sys.executable, '-m', 'unittest', 'discover', '-s', str(ROOT / 'tests'), '-p', 'test_test_runner.py', '-v'])
    for index, command in enumerate(commands):
        if logged(command, logs / f'syntax-{index}.log'):
            return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('suite', choices=['syntax', 'ui', *SUITES, 'isolation', 'cleanup'])
    parser.add_argument('--image', help='a FIX-025 final image; resolved to its immutable local ID')
    parser.add_argument('--logs', type=Path, default=ROOT / 'test-results')
    parser.add_argument('--run-id', default=os.environ.get('MF_TEST_RUN_ID'))
    args = parser.parse_args(argv)
    if args.suite == 'cleanup' and not args.run_id:
        parser.error('cleanup needs --run-id (or MF_TEST_RUN_ID) to identify owned resources')
    run_id = args.run_id or uuid.uuid4().hex
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', run_id):
        parser.error('run-id must contain only letters, digits, underscores and hyphens')
    if args.suite in [*SUITES, 'isolation'] and not args.image:
        parser.error('container suites need --image; application tests never run on host /data')
    if args.suite == 'cleanup':
        cleanup(run_id);return 0
    logs = args.logs.resolve();logs.mkdir(parents=True, exist_ok=True)
    summary = {'suite': args.suite, 'run_id': run_id, 'result': 'incomplete'}
    try:
        if args.suite == 'syntax':
            result = syntax(logs)
        elif args.suite == 'ui':
            result = logged(['node', '--test', *map(str, sorted((ROOT / 'tests').glob('*.test.mjs')))], logs / 'ui.log')
        else:
            image = capture(['docker', 'image', 'inspect', args.image, '--format', '{{.Id}}'])
            if not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
                raise RuntimeError('Docker did not resolve an immutable image ID')
            summary['image_id'] = image
            result = run_container(args.suite, image, logs, run_id)
        summary['result'] = 'passed' if result == 0 else 'failed'
        return result
    finally:
        (logs / f'{args.suite}-summary.json').write_text(json.dumps(summary, indent=2) + '\n')


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise KeyboardInterrupt('test run interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    sys.exit(main())
