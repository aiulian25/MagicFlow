"""FIX-023: harmless mount/link/decoder corpus, fresh /data only."""
import asyncio
import io
import json
import os
import threading
import unittest
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch
import test_download_lifecycle as harness
main=harness.main

class TrustBoundaryTest(unittest.TestCase):
    setUpClass=classmethod(harness.DownloadLifecycleTest.setUpClass.__func__)
    tearDownClass=classmethod(harness.DownloadLifecycleTest.tearDownClass.__func__)
    def setUp(self):
        self.outside=main.TMP_DIR/'outside-sentinel.png'
        self.private=harness.tiny_png((250,10,10))+b'OUTSIDE_ONLY_TEST_SENTINEL'
        self.outside.write_bytes(self.private)
        self.safe=main.OUTPUT_DIR/'boundary.png';self.original=harness.tiny_png((10,250,10));self.safe.write_bytes(self.original)
    def tearDown(self):
        for name in ('boundary.png','linked.png','directory.png','pipe.png','-leading.png'):
            p=main.OUTPUT_DIR/name
            if p.is_dir() and not p.is_symlink():p.rmdir()
            else:p.unlink(missing_ok=True)
        self.outside.unlink(missing_ok=True)
        main.TEMP_REGISTRY_FILE.unlink(missing_ok=True)
    def post(self,path,data):
        return harness.request(path,method='POST',body=json.dumps(data).encode(),content_type='application/json')
    def test_seeded_link_is_rejected_by_every_consumer_and_preserved(self):
        link=main.OUTPUT_DIR/'linked.png';link.symlink_to(self.outside)
        for path in ('/download/linked.png','/thumb/linked.png'):
            status,body=harness.request(path);self.assertEqual(status,404,path);self.assertNotIn(b'OUTSIDE_ONLY',body)
        self.assertEqual(self.post('/files/archive',{'filenames':['linked.png']})[0],404)
        self.assertEqual(harness.request('/reconvert/linked.png',method='POST',body=b'format=gif',content_type='application/x-www-form-urlencoded')[0],404)
        self.assertNotIn('linked.png',[f['filename'] for f in json.loads(harness.request('/files')[1])['files']])
        self.assertEqual(harness.request('/files/linked.png',method='DELETE')[0],404)
        status, body = self.post('/files/delete', {'filenames': ['linked.png']})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'deleted': [], 'missing': ['linked.png']})
        main._register_temp_output('linked.png')
        self.assertEqual(harness.request('/files/temp/linked.png',method='DELETE')[0],404)
        self.assertTrue(link.is_symlink());self.assertEqual(self.outside.read_bytes(),self.private)
    def test_directory_fifo_and_unsafe_names_cannot_be_read(self):
        (main.OUTPUT_DIR/'directory.png').mkdir();os.mkfifo(main.OUTPUT_DIR/'pipe.png')
        for name in ('directory.png','pipe.png'):
            self.assertEqual(harness.request('/download/'+name)[0],404)
        for path in ('/download/.hidden.png','/download/..%2Foutside-sentinel.png','/download/quoted%22.png'):
            self.assertIn(harness.request(path)[0],(400,404))
        p=main.OUTPUT_DIR/'-leading.png';p.write_bytes(self.original)
        self.assertEqual(harness.request('/download/-leading.png')[1],self.original)
    def test_pinned_download_survives_name_replacement_and_supports_ranges(self):
        response=asyncio.run(main.download('boundary.png'))
        self.safe.unlink();self.safe.symlink_to(self.outside)
        messages=[]
        async def send(message):messages.append(message)
        async def receive():await asyncio.Event().wait()
        asyncio.run(response({'type':'http','method':'GET','headers':[],'asgi':{'spec_version':'2.4'}},receive,send))
        self.assertEqual(b''.join(m.get('body',b'') for m in messages),self.original)
        self.assertTrue(response._handle.closed)
        self.safe.unlink();self.safe.write_bytes(self.original)
        req=urllib.request.Request(harness.BASE+'/download/boundary.png',headers={'Range':'bytes=0-7'})
        with urllib.request.urlopen(req) as result:
            self.assertEqual(result.status,206);self.assertEqual(result.read(),self.original[:8])
    def test_archive_uses_pinned_input_after_replacement(self):
        original_open=zipfile.ZipFile.open
        swapped=False
        def opening(archive,name,mode='r',*args,**kwargs):
            nonlocal swapped
            if mode=='w' and not swapped:
                self.safe.unlink();self.safe.symlink_to(self.outside);swapped=True
            return original_open(archive,name,mode,*args,**kwargs)
        with patch.object(zipfile.ZipFile,'open',opening):
            status,data=self.post('/files/archive',{'filenames':['boundary.png']})
        self.assertEqual(status,200,data)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:self.assertEqual(archive.read('boundary.png'),self.original)
    def test_recipe_directory_link_does_not_expose_json(self):
        main.RECIPE_DIR.mkdir(exist_ok=True)
        backup=main.RECIPE_DIR.with_name('.recipes-boundary-backup')
        outside_dir=main.TMP_DIR/'outside-recipes';outside_dir.mkdir(exist_ok=True)
        marker=outside_dir/'boundary.png.json';marker.write_text('{"private":"OUTSIDE_ONLY_TEST_SENTINEL"}')
        main.RECIPE_DIR.rename(backup);main.RECIPE_DIR.symlink_to(outside_dir,target_is_directory=True)
        try:
            status,body=harness.request('/files');self.assertEqual(status,200);self.assertNotIn(b'OUTSIDE_ONLY',body)
        finally:
            main.RECIPE_DIR.unlink();backup.rename(main.RECIPE_DIR);marker.unlink();outside_dir.rmdir()
    def test_registry_cannot_supply_traversal_to_cleanup(self):
        main.TEMP_REGISTRY_FILE.write_text(json.dumps({'../tmp/outside-sentinel.png':{'expires':0}}))
        main._sweep_tmp_outputs();self.assertEqual(self.outside.read_bytes(),self.private)
    def test_running_encoder_stays_in_pinned_staging_directory(self):
        stage, descriptor = main._pinned_staging_path('boundary-stage.png')
        backup = main.STAGING_DIR.with_name('.staging-boundary-backup')
        outside = main.TMP_DIR / 'outside-staging'
        outside.mkdir()
        main.STAGING_DIR.rename(backup)
        main.STAGING_DIR.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaises(main.HTTPException) as error:
                main._require_library_layout()
            self.assertEqual(error.exception.status_code, 503)
            result = main._run_subprocess(['convert', '-size', '8x8', 'xc:red', str(stage)], 10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((backup / stage.name).is_file())
            self.assertEqual(list(outside.iterdir()), [])
            main._clear_stale_staging()
            self.assertTrue(main.STAGING_DIR.is_symlink())
        finally:
            stage.unlink(missing_ok=True)
            os.close(descriptor)
            main.STAGING_DIR.unlink()
            backup.rename(main.STAGING_DIR)
            outside.rmdir()

    def test_snapshot_does_not_follow_a_replaced_library_name(self):
        work=main.TMP_DIR/'boundary-snapshot';work.mkdir(exist_ok=True)
        try:
            with main._open_output('boundary.png') as handle:
                self.safe.unlink();self.safe.symlink_to(self.outside)
                copied=main._snapshot_output('boundary.png',work,handle)
            self.assertEqual(copied.read_bytes(),self.original)
        finally:
            import shutil
            shutil.rmtree(work)

class DecoderBoundaryTest(unittest.TestCase):
    def test_spoofed_script_and_nested_network_inputs_are_denied_without_requests(self):
        main.TMP_DIR.mkdir(parents=True,exist_ok=True)
        hits=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path);self.send_response(200);self.end_headers();self.wfile.write(harness.tiny_png())
            def log_message(self,*args):pass
        server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        import tempfile
        with tempfile.TemporaryDirectory(dir=main.TMP_DIR) as directory:
            work=Path(directory);url=f'http://127.0.0.1:{server.server_port}/pixel.png'
            try:
                self.assertTrue(urllib.request.urlopen(url).read());hits.clear()
                direct=main._run_subprocess(['ffprobe','-v','error',url],5)
                self.assertNotEqual(direct.returncode,0);self.assertEqual(hits,[])
                hls=work/'spoof.mp4';hls.write_text('#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n'+url+'\n#EXT-X-ENDLIST\n')
                result=main._run_subprocess(['ffprobe','-v','error',str(hls)],5)
                self.assertNotEqual(result.returncode,0);self.assertEqual(hits,[])
                svg=work/'spoof.png';svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><image href="'+url+'" width="8" height="8"/></svg>')
                result=main._run_subprocess(['convert',str(svg),str(work/'out.png')],5)
                self.assertNotEqual(result.returncode,0);self.assertEqual(hits,[])
                outside=work/'outside.png';outside.write_bytes(harness.tiny_png((250,10,10)))
                local_svg=work/'local.png';local_svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><image href="'+outside.as_uri()+'" width="8" height="8"/></svg>')
                result=main._run_subprocess(['convert',str(local_svg),str(work/'local-out.png')],5)
                self.assertNotEqual(result.returncode,0)
                concat=work/'spoof-concat.mp4';concat.write_text("ffconcat version 1.0\nfile '"+str(outside)+"'\n")
                result=main._run_subprocess(['ffprobe','-v','error',str(concat)],5)
                self.assertNotEqual(result.returncode,0)
                safe=work/'safe.png';safe.write_bytes(harness.tiny_png())
                result=main._run_subprocess(['convert',str(safe),str(work/'safe.pdf')],10)
                self.assertEqual(result.returncode,0,result.stderr)
                result=main._run_subprocess(['convert',str(work/'safe.pdf'),str(work/'back.png')],10)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(hits,[])
            finally:server.shutdown();server.server_close();thread.join()
