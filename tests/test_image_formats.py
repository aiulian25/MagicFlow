"""FEAT-007 first tranche: AVIF still input and one ICO image only."""
import json
import struct
import subprocess
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch
import test_download_lifecycle as harness
import capabilities
import utils
main=harness.main


def box(kind,data):
    return struct.pack('>I',8+len(data))+kind+data


class ImageFormatTest(unittest.TestCase):
    setUpClass=classmethod(harness.DownloadLifecycleTest.setUpClass.__func__)
    tearDownClass=classmethod(harness.DownloadLifecycleTest.tearDownClass.__func__)

    def setUp(self):
        self.source=main.TMP_DIR/'format-source.png'
        self.avif=main.TMP_DIR/'format-source.avif'
        subprocess.run(['convert','-size','48x24','xc:none','-fill','red','-draw','rectangle 12,6 36,18',str(self.source)],check=True,capture_output=True)
        subprocess.run(['avifenc','-j','2','--min','0','--max','0',str(self.source),str(self.avif)],check=True,capture_output=True)
        self.created=[]

    def tearDown(self):
        for name in self.created:
            (main.OUTPUT_DIR/name).unlink(missing_ok=True)
            main._remove_output_artifacts(name)
        self.source.unlink(missing_ok=True);self.avif.unlink(missing_ok=True)
        main._write_temp_registry({})

    def post(self,path,fields,content=None,name='source.avif',mime='image/avif'):
        content=self.avif.read_bytes() if content is None else content
        body=b''.join(b'--IMAGEBOUND\r\nContent-Disposition: form-data; name="'+key.encode()+b'"\r\n\r\n'+str(value).encode()+b'\r\n' for key,value in fields.items())
        body+=b'--IMAGEBOUND\r\nContent-Disposition: form-data; name="file"; filename="'+name.encode()+b'"\r\nContent-Type: '+mime.encode()+b'\r\n\r\n'+content+b'\r\n--IMAGEBOUND--\r\n'
        request=urllib.request.Request(harness.BASE+path,data=body,headers={'Content-Type':'multipart/form-data; boundary=IMAGEBOUND'})
        try:
            with urllib.request.urlopen(request,timeout=45) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as error:return error.code,json.load(error)

    def convert(self,fields,**kwargs):
        status,payload=self.post('/convert-image',fields,**kwargs)
        if status==200:self.created.append(payload['output_filename'])
        return status,payload

    def test_capabilities_advertise_only_verified_image_additions(self):
        status,raw=harness.request('/config');self.assertEqual(status,200)
        config=json.loads(raw)
        self.assertIn('.avif',config['allowed_input_exts']);self.assertIn('image/avif',config['allowed_mime_types'])
        self.assertIn('ico',config['still_output_formats']);self.assertEqual(config['ico_sizes'],list(utils.ICO_SIZES))
        self.assertNotIn('.ico',config['allowed_input_exts'])
        self.assertNotIn('.avif',config['explode_input_exts'])
        self.assertNotIn('ico',config['animation_output_formats'])
        self.assertNotIn('.avifs',config['allowed_input_exts'])

    def test_real_avif_decodes_to_png_and_combines_as_one_image(self):
        status,payload=self.convert({'out_format':'png','keep_output':'true'})
        self.assertEqual(status,200,payload)
        path=main.OUTPUT_DIR/payload['output_filename']
        self.assertEqual(main._read_output_dims(path),(48,24))
        alpha=subprocess.check_output(['convert',str(path),'-alpha','extract','-format','%[fx:minima] %[fx:maxima]','info:'],text=True)
        self.assertEqual(alpha.split(),['0','1'])
        # Combine's multipart field is files; use its existing generated fixture helper.
        import test_capabilities
        with patch.object(test_capabilities, 'BASE', harness.BASE):
            status,result=test_capabilities.post('/convert',{'format':'gif','keep_output':'true'},[('files','still.avif',self.avif.read_bytes(),'image/avif')])
        self.assertEqual(status,200,result);self.created.append(result['output_filename'])
        self.assertEqual(main._count_frames(str(main.OUTPUT_DIR/result['output_filename'])),1)

    def test_independent_avif_alpha_and_orientation(self):
        fixture=Path(__file__).parent/'fixtures'/'avif-alpha.avif'
        import hashlib
        self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(),'b371cc88244a873131e4d10ff9363d71ce4f41cf333bd4a491b38d970d9abd3b')
        status,payload=self.convert({'out_format':'png','keep_output':'true'},content=fixture.read_bytes())
        self.assertEqual(status,200,payload)
        path=main.OUTPUT_DIR/payload['output_filename']
        self.assertEqual(main._read_output_dims(path),(256,512))
        alpha=subprocess.check_output(['convert',str(path),'-alpha','extract','-format','%[fx:minima] %[fx:maxima]','info:'],text=True).split()
        self.assertAlmostEqual(float(alpha[0]),128/255,places=5);self.assertEqual(float(alpha[1]),1)

    def test_ico_sizes_header_alpha_and_recipe(self):
        for size in utils.ICO_SIZES:
            status,payload=self.convert({'out_format':'ico','icon_size':size,'keep_output':'true'},content=self.source.read_bytes(),name='source.png',mime='image/png')
            self.assertEqual(status,200,(size,payload))
            path=main.OUTPUT_DIR/payload['output_filename']
            data=path.read_bytes();self.assertEqual(struct.unpack('<HHH',data[:6]),(0,1,1))
            self.assertEqual((data[6] or 256,data[7] or 256),(size,size))
            self.assertEqual(main._read_output_dims(path),(size,size))
            alpha=subprocess.check_output(['convert',str(path),'-alpha','extract','-format','%[fx:minima] %[fx:maxima]','info:'],text=True)
            self.assertEqual(alpha.split(),['0','1'])
            recipe=json.loads((main.RECIPE_DIR/(path.name+'.json')).read_text())
            self.assertEqual(recipe['requested']['icon_size'],size);self.assertEqual(recipe['effective']['format'],'ico')
            self.assertIsNone(recipe['effective']['quality']);self.assertEqual(recipe['effective']['frames'],1)

    def test_ico_download_preview_and_temporary_release(self):
        status,payload=self.convert({'out_format':'ico','icon_size':32,'keep_output':'false'},content=self.source.read_bytes(),name='source.png',mime='image/png')
        self.assertEqual(status,200,payload);name=payload['output_filename']
        with urllib.request.urlopen(harness.BASE+'/download/'+name) as response:
            self.assertEqual(response.headers.get_content_type(),'image/vnd.microsoft.icon');self.assertTrue(response.read())
        for route in ('/thumb/','/compare-preview/'):
            status,body=harness.request(route+name);self.assertEqual(status,200,(route,body))
        self.assertTrue((main.OUTPUT_DIR/name).exists())
        self.assertEqual(harness.request('/files/temp/'+name,method='DELETE')[0],200)
        self.assertFalse((main.OUTPUT_DIR/name).exists())

    def test_invalid_icon_size_or_generic_geometry_does_not_publish(self):
        before=set(main.OUTPUT_DIR.iterdir())
        for fields in ({'out_format':'ico','icon_size':17},{'out_format':'ico','icon_size':512},{'out_format':'ico','width':32}):
            status,payload=self.convert(fields);self.assertEqual(status,400,payload)
        self.assertEqual(set(main.OUTPUT_DIR.iterdir()),before)

    def test_corrupt_and_sequence_avif_are_rejected_clearly(self):
        for content in (b'not avif',box(b'ftyp',b'avis'+b'\0'*4+b'avif'),box(b'ftyp',b'avif'+b'\0'*4)+box(b'moov',b'')):
            status,payload=self.convert({'out_format':'png'},content=content)
            self.assertEqual(status,400,payload)
        # Declared HDR metadata is refused before a native decoder can reinterpret it.
        hdr=box(b'ftyp',b'avif'+b'\0'*4)+box(b'meta',b'\0'*4+box(b'iprp',box(b'ipco',box(b'colr',b'nclx'+struct.pack('>HHHB',9,16,9,128)))))
        status,payload=self.convert({'out_format':'png'},content=hdr)
        self.assertEqual(status,400);self.assertIn('HDR AVIF',payload['detail'])
        wide=hdr.replace(struct.pack('>HHHB',9,16,9,128),struct.pack('>HHHB',12,13,6,128))
        status,payload=self.convert({'out_format':'png'},content=wide)
        self.assertEqual(status,400);self.assertIn('color space is unsupported',payload['detail'])

    def test_unavailable_capabilities_refuse_requests_without_losing_presets(self):
        original=dict(main.CAPABILITIES)
        try:
            main.CAPABILITIES['still_outputs']=[f for f in original['still_outputs'] if f!='ico']
            main.CAPABILITIES['unavailable']={**original['unavailable'],'still:ico':'icon coder denied by policy'}
            status,payload=self.convert({'out_format':'ico'})
            self.assertEqual(status,400);self.assertIn('icon coder denied',payload['detail'])
            main.CAPABILITIES['input_extensions']=[e for e in original['input_extensions'] if e!='.avif']
            main.CAPABILITIES['unavailable']['input:.avif']='AVIF decoder unavailable'
            status,payload=self.convert({'out_format':'png'})
            self.assertEqual(status,400);self.assertIn('AVIF decoder unavailable',payload['detail'])
        finally:main.CAPABILITIES.clear();main.CAPABILITIES.update(original)

    def test_capability_probe_withholds_ico_when_alpha_verification_fails(self):
        with patch.object(capabilities,'_run',return_value=(False,'denied')):
            ok,reason=capabilities._probe_still('ico',main.TMP_DIR)
        self.assertFalse(ok);self.assertIn('denied',reason)

    def test_avif_brand_parser_handles_truncation_and_metadata_limits(self):
        for data in (b'',box(b'ftyp',b'heic'+b'\0'*4),struct.pack('>I',1000)+b'ftyp'+b'avif',box(b'free',b'')*4097):
            self.avif.write_bytes(data)
            with self.assertRaises(ValueError):utils.validate_avif_still(self.avif)
