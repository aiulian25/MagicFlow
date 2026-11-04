"""FEAT-004: bounded, non-destructive first-frame comparison previews."""
import json
import struct
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
import urllib.request
import urllib.error
import test_download_lifecycle as harness
main = harness.main


class ComparePreviewTest(unittest.TestCase):
    setUpClass = classmethod(harness.DownloadLifecycleTest.setUpClass.__func__)
    tearDownClass = classmethod(harness.DownloadLifecycleTest.tearDownClass.__func__)

    def setUp(self):
        self.source = main.OUTPUT_DIR / 'compare-source.png'
        subprocess.run(['convert','-size','1800x900','xc:none','-fill','red','-draw','rectangle 450,200 1350,700',str(self.source)],check=True,capture_output=True)
        self.before = {p.name for p in main.TMP_DIR.iterdir()}

    def tearDown(self):
        for path in main.OUTPUT_DIR.glob('compare-*'):
            path.unlink(missing_ok=True)
        self.assertEqual({p.name for p in main.TMP_DIR.iterdir()}, self.before)

    def preview(self, path, content=None, name='original.png', origin=None):
        headers = {}
        if content is not None:
            body=(b'--BOUNDARY\r\nContent-Disposition: form-data; name="file"; filename="'+name.encode()+b'"\r\nContent-Type: image/png\r\n\r\n'+content+b'\r\n--BOUNDARY--\r\n')
            headers['Content-Type']='multipart/form-data; boundary=BOUNDARY'
        else:body=None
        if origin:headers['Origin']=origin
        request=urllib.request.Request(harness.BASE+path,data=body,headers=headers)
        try:
            with urllib.request.urlopen(request,timeout=30) as response:
                return response.status,response.read(),response.headers
        except urllib.error.HTTPError as error:
            return error.code,error.read(),error.headers

    def test_upload_is_bounded_alpha_preserving_and_ephemeral(self):
        data=self.source.read_bytes()
        status,body,headers=self.preview('/compare-preview',data)
        self.assertEqual(status,200,body)
        self.assertEqual(struct.unpack('>II',body[16:24]),(640,320))
        self.assertEqual(headers['X-Compare-Width'],'1800')
        self.assertEqual(headers['X-Compare-Bytes'],str(len(data)))
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertEqual(body[24:26],bytes([8,6]))  # 8-bit RGBA PNG
        result=main.TMP_DIR/'compare-check.png';result.write_bytes(body)
        try:
            pixel=subprocess.check_output(['convert',str(result),'-format','%[pixel:p{0,0}]','info:'],text=True)
            self.assertIn(',0)',pixel.replace(' ',''))
        finally:result.unlink()

    def test_saved_preview_never_releases_temporary_source(self):
        original=self.source.read_bytes();main._register_temp_output(self.source.name)
        status,body,_=self.preview('/compare-preview/'+self.source.name)
        self.assertEqual(status,200,body)
        self.assertEqual(self.source.read_bytes(),original)
        self.assertIn(self.source.name,main._read_temp_registry())
        main._write_temp_registry({})

    def test_links_and_invalid_paths_do_not_expose_outside_bytes(self):
        link=main.OUTPUT_DIR/'compare-link.png';link.symlink_to(self.source)
        self.assertEqual(self.preview('/compare-preview/compare-link.png')[0],404)
        self.assertTrue(link.is_symlink())
        self.assertIn(self.preview('/compare-preview/..%2Fetc%2Fpasswd')[0],(400,404))
        self.assertEqual(self.preview('/compare-preview/compare-missing.png')[0],404)

    def test_corrupt_empty_oversized_and_cross_origin_uploads_are_rejected(self):
        self.assertEqual(self.preview('/compare-preview',b'bad')[0],400)
        self.assertEqual(self.preview('/compare-preview',b'')[0],400)
        with patch.object(main,'MAX_FILE_SIZE',8):
            self.assertEqual(self.preview('/compare-preview',self.source.read_bytes())[0],413)
        self.assertEqual(self.preview('/compare-preview',self.source.read_bytes(),origin='https://elsewhere.invalid')[0],403)

    def test_first_frame_or_page_of_gif_pdf_apng_and_video(self):
        one=main.TMP_DIR/'compare-one.png';two=main.TMP_DIR/'compare-two.png'
        one.write_bytes(harness.tiny_png((255,0,0)));two.write_bytes(harness.tiny_png((0,0,255)))
        try:
            for suffix in ('gif','pdf'):
                target=main.OUTPUT_DIR/('compare-pair.'+suffix)
                subprocess.run(['convert',str(one),str(two),str(target)],check=True,capture_output=True)
            for suffix in ('apng','mp4'):
                target=main.OUTPUT_DIR/('compare-pair.'+suffix)
                subprocess.run(['ffmpeg','-y','-v','error','-loop','1','-i',str(one),'-t','0.2',str(target)],check=True,capture_output=True)
            for suffix in ('gif','pdf','apng','mp4'):
                status,body,headers=self.preview('/compare-preview/compare-pair.'+suffix)
                self.assertEqual(status,200,(suffix,body))
                self.assertTrue(body.startswith(main.PNG_SIGNATURE))
                self.assertEqual(headers['X-Compare-Mode'],'first-frame-srgb-8bit')
                output=main.TMP_DIR/'compare-frame.png';output.write_bytes(body)
                try:
                    pixel=subprocess.check_output(['convert',str(output),'-format','%[fx:r] %[fx:b]','info:'],text=True).split()
                    self.assertGreater(float(pixel[0]),.8);self.assertLess(float(pixel[1]),.1)
                finally:output.unlink()
        finally:one.unlink();two.unlink()

    def test_budget_and_timeout_failures_clean_private_work(self):
        with patch.object(main,'MAX_DECODED_PIXELS',1):
            self.assertEqual(self.preview('/compare-preview/'+self.source.name)[0],400)
        with patch.object(main,'_run_subprocess',side_effect=subprocess.TimeoutExpired('preview',1)):
            self.assertEqual(self.preview('/compare-preview/'+self.source.name)[0],504)

    def test_orientation_metadata_matches_oriented_preview(self):
        target=main.OUTPUT_DIR/'compare-oriented.tiff'
        subprocess.run(['convert','-size','30x20','xc:red','-orient','RightTop',str(target)],check=True,capture_output=True)
        status,body,headers=self.preview('/compare-preview/'+target.name)
        self.assertEqual(status,200,body)
        self.assertEqual((headers['X-Compare-Width'],headers['X-Compare-Height']),('20','30'))
        self.assertEqual(struct.unpack('>II',body[16:24]),(20,30))
