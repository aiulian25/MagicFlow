#!/usr/bin/env python3
"""Test a final local image with isolated data; every container uses --rm.
Usage: python3 scripts/verify-deployment.py IMAGE LOG_DIRECTORY
No publish, production volumes, source overlay or exposed host port.
"""
import argparse
import hashlib
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('image')
parser.add_argument('log_directory')
parser.add_argument('--rollback-image', help='retained previous image to check against restored fixtures')
parser.add_argument('--lifecycle-only', action='store_true', help='run recovery checks after regressions have already passed')
args = parser.parse_args()
image, log_directory = args.image, args.log_directory
def interrupted(signum, frame):
    raise KeyboardInterrupt('release verification interrupted')
signal.signal(signal.SIGTERM, interrupted)
logs = Path(log_directory).resolve();logs.mkdir(parents=True, exist_ok=True)
image_id = subprocess.check_output(['docker','image','inspect',image,'--format','{{.Id}}'], text=True).strip()
rollback_id = subprocess.check_output(['docker','image','inspect',args.rollback_image,'--format','{{.Id}}'],text=True).strip() if args.rollback_image else None
label = ('magicflow.test_run=' + os.environ['MF_TEST_RUN_ID']) if os.environ.get('MF_TEST_RUN_ID') else 'magicflow.task=fix025'
tests = Path(__file__).resolve().parents[1] / 'tests'
suite_config = json.loads((tests / 'suites.json').read_text())
modules = suite_config['media'] + suite_config['fidelity']

runner = "import sys,unittest;sys.path.insert(0,'/tests');suite=unittest.defaultTestLoader.loadTestsFromNames("+repr(modules)+");result=unittest.TextTestRunner(verbosity=2).run(suite);sys.exit(not result.wasSuccessful())"
if not args.lifecycle_only:
    test_name = 'magicflow-fix025-tests-' + uuid.uuid4().hex[:8]
    with (logs/'regressions.log').open('w') as log:
        try:
            subprocess.run(['docker','run','--rm','--name',test_name,'--label',label,
            '--network','none','--memory','3g','--cpus','2','--tmpfs','/data:rw,size=3g',
            '-v',f'{tests}:/tests:ro','--entrypoint','sh',image_id,'-c',
            '/entrypoint.sh true && exec python3 -I -c "$1"','sh',runner], stdout=log, stderr=subprocess.STDOUT, check=True)
        finally:
            subprocess.run(['docker','stop','--time','15',test_name],capture_output=True)
    print('Final-image regression checks passed.', flush=True)

probe = r'''
import base64,hashlib,http.client,json,struct,urllib.request,zlib
from pathlib import Path
base='http://127.0.0.1:3876'
def get(path):return urllib.request.urlopen(base+path).read()
mode=__import__('sys').argv[1]
if mode=='seed':
    def chunk(t,d):return struct.pack('>I',len(d))+t+d+struct.pack('>I',zlib.crc32(t+d)&0xffffffff)
    png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',8,8,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+b'\xff\0\0'*8)*8))+chunk(b'IEND',b'')
    boundary='DEPLOYMENT_FIXTURE'
    fields=b''.join(b'--'+boundary.encode()+b'\r\nContent-Disposition: form-data; name="'+k+b'"\r\n\r\n'+v+b'\r\n' for k,v in [(b'out_format',b'png'),(b'keep_output',b'true')])
    body=fields+b'--'+boundary.encode()+b'\r\nContent-Disposition: form-data; name="file"; filename="fixture.png"\r\nContent-Type: image/png\r\n\r\n'+png+b'\r\n--'+boundary.encode()+b'--\r\n'
    req=urllib.request.Request(base+'/convert-image',data=body,headers={'Content-Type':'multipart/form-data; boundary='+boundary})
    result=json.load(urllib.request.urlopen(req));name=result['output_filename']
    settings=json.loads(get('/settings'))
    payload={'setting-quality':73,'user_presets':json.dumps({'Saved':{'setting-quality':73}})}
    req=urllib.request.Request(base+'/settings',data=json.dumps(payload).encode(),method='PUT',headers={'Content-Type':'application/json','If-Match':settings['revision']})
    urllib.request.urlopen(req).read()
    Path('/data/config/deployment-proof.json').write_text(json.dumps({'name':name,'sha':hashlib.sha256(get('/download/'+name)).hexdigest(),'recipe_sha':hashlib.sha256((Path('/data/output/.recipes')/(name+'.json')).read_bytes()).hexdigest()}))
proof=json.loads(Path('/data/config/deployment-proof.json').read_text())
saved=json.loads(get('/settings'))['settings']
assert saved['setting-quality']==73
assert json.loads(saved['user_presets'])['Saved']['setting-quality']==73
files=json.loads(get('/files'))['files'];entry=next(f for f in files if f['filename']==proof['name'])
assert entry['recipe'] and hashlib.sha256(get('/download/'+proof['name'])).hexdigest()==proof['sha']
assert hashlib.sha256((Path('/data/output/.recipes')/(proof['name']+'.json')).read_bytes()).hexdigest()==proof['recipe_sha']
manifest=json.loads(Path('/app/release-manifest.json').read_text());build=json.loads(get('/config'))['build']
assert all(build[k]==manifest[k] for k in build)
assert Path('/proc/1/status').read_text().split('Uid:')[1].splitlines()[0].split()==['1000']*4
print(json.dumps({'build':build,'output':proof,'preset':'Saved','network':'none'}))
'''

active = []
volumes = []
def start(root, suffix, named=False, selected_image=None):
    name = 'magicflow-fix025-' + suffix + '-' + uuid.uuid4().hex[:8]
    output = (logs/(suffix+'.log')).open('w')
    command = ['docker','run','--rm','--name',name,'--label',label,'--network','none',
        '--security-opt','no-new-privileges:true','--pids-limit','512','--read-only','--tmpfs','/tmp:rw,size=256m','--memory','3g','--cpus','2']
    for directory in ('input','output','tmp','config'):
        if named:
            volume = name + '-' + directory
            subprocess.run(['docker','volume','create','--label',label,volume],check=True,capture_output=True)
            volumes.append(volume)
            command += ['--mount',f'type=volume,source={volume},target=/data/{directory}']
        else:
            command += ['--mount',f'type=bind,source={root/directory},target=/data/{directory}']
    process=subprocess.Popen([*command,selected_image or image_id],stdout=output,stderr=subprocess.STDOUT)
    active.append((name,process,output))
    for _ in range(120):
        if process.poll() is not None:raise RuntimeError('Service failed; inspect '+str(logs/(suffix+'.log')))
        result=subprocess.run(['docker','exec',name,'python3','-c',"import urllib.request;urllib.request.urlopen('http://127.0.0.1:3876/health')"],capture_output=True)
        if result.returncode==0:return name
        time.sleep(.5)
    raise RuntimeError('Service startup timed out')
def stop(name):
    subprocess.run(['docker','stop','--time','15',name],check=True,capture_output=True)
    item=next(item for item in active if item[0]==name)
    assert item[1].wait(timeout=20)==0, 'Unclean service exit'
    item[2].close();active.remove(item)
def check(name,mode):
    result=subprocess.check_output(['docker','exec',name,'python3','-c',probe,mode],text=True)
    with (logs/'lifecycle-results.jsonl').open('a') as out:out.write(result)
def as_root(root, *command):
    """Run one command over the data tree as root inside the image itself.

    The service writes as its own unprivileged account and keeps what it writes
    private: each /data/output/.recipes sidecar is 0600 under that uid, and so
    are the thumbnails. A host whose user is a different uid therefore cannot
    read or recreate the tree, and chowning it to the invoking user is not a way
    out — that hands the tree to a uid the service is not, so /files reports
    every recipe as missing and the restored container fails its own
    verification. Archiving, restoring and finally releasing the tree from
    inside a container keeps the ownership the service needs, which is also how
    an operator backs up a bind mount."""
    subprocess.run(['docker','run','--rm','--user','0','--network','none','--label',label,
                    '-v',f'{root}:/work','-v',f'{logs}:/archive','--entrypoint',command[0],
                    image_id,*command[1:]],check=True,capture_output=True)
try:
    name=start(None,'named-volumes',named=True);check(name,'seed');stop(name)
    for volume in list(volumes):
        subprocess.run(['docker','volume','rm',volume],check=True,capture_output=True);volumes.remove(volume)
    print('Clean named-volume startup passed.',flush=True)
    with tempfile.TemporaryDirectory(prefix='data-',dir=logs) as temporary:
        root=Path(temporary)
        for part in ('input','output','tmp','config'):(root/part).mkdir()
        name=start(root,'clean');check(name,'seed');stop(name)
        name=start(root,'restart');check(name,'verify');stop(name)
        backup=logs/'data-backup.tar.gz'
        as_root(root,'tar','-czf','/archive/'+backup.name,'-C','/work','input','output','tmp','config')
        restored=root/'restored';restored.mkdir()
        as_root(root,'tar','-xzf','/archive/'+backup.name,'-C','/work/'+restored.name)
        # Recreate from the retained immutable image ID, as a rollback does.
        name=start(restored,'restored-immutable');check(name,'verify');stop(name)
        if rollback_id:
            name=start(restored,'rollback',selected_image=rollback_id);check(name,'verify');stop(name)
            print('Retained previous-image rollback passed: '+rollback_id,flush=True)
        as_root(root,'chown','-R',f'{os.getuid()}:{os.getgid()}','/work')   # so TemporaryDirectory can remove it
        print('Clean start, restart, backup/restore, immutable-image recreation and graceful stops passed.',flush=True)
finally:
    for name,process,output in list(active):
        subprocess.run(['docker','stop','--time','15',name],capture_output=True)
        try:process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            subprocess.run(['docker','kill',name],capture_output=True);process.wait(timeout=10)
        output.close()
    for volume in list(volumes):
        subprocess.run(['docker','volume','rm',volume],check=True,capture_output=True)
    remaining=subprocess.check_output(['docker','ps','-a','--filter','label='+label,'--format','{{.Names}}'],text=True).strip()
    if remaining:raise RuntimeError('Task containers remain: '+remaining)
