"""FEAT-013: physical page boxes, raster placement and safe defaults."""
import re
import subprocess
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import test_timing as harness
import pdf_layout
main = harness.main


class PDFLayoutTest(unittest.TestCase):
    setUpClass = classmethod(harness.ConvertTimingTest.setUpClass.__func__)
    tearDownClass = classmethod(harness.ConvertTimingTest.tearDownClass.__func__)
    setUp = harness.ConvertTimingTest.setUp
    tearDown = harness.ConvertTimingTest.tearDown
    combine = harness.ConvertTimingTest.combine
    recipe_for = harness.ConvertTimingTest.recipe_for

    def files(self):
        return [('files', f'p{i}.png', harness.coloured_png(i, w, h), 'image/png')
                for i, (w, h) in enumerate(((80, 40), (40, 80), (40, 40)))]

    def boxes(self, path):
        return [tuple(map(float, value.split())) for value in
                re.findall(rb'/MediaBox\s*\[([^]]+)\]', path.read_bytes())]

    def test_fixed_page_boxes_orientation_and_recipe(self):
        for size, orientation, mm in [('a4', 'portrait', (210, 297)),
                                      ('letter', 'landscape', (279.4, 215.9)),
                                      ('custom', 'portrait', (100, 150))]:
            with self.subTest(size=size):
                path, _ = self.combine(self.files(), 'pdf', pdf_page_size=size,
                                      pdf_orientation=orientation, pdf_width_mm=100, pdf_height_mm=150)
                boxes = self.boxes(path);self.assertEqual(len(boxes), 3)
                for box in boxes:
                    self.assertEqual(box[:2], (0, 0))
                    for actual, side in zip(box[2:], mm):self.assertAlmostEqual(actual, side * 72 / 25.4, delta=.26)
                recipe=self.recipe_for(path)
                self.assertEqual(recipe['requested']['pdf_page_size'], size)
                self.assertEqual(recipe['effective']['pdf_layout']['raster_dpi'],144)
                self.assertEqual(recipe['effective']['pdf_layout']['images_per_page'],1)
                self.assertIsNone(recipe['effective']['quality'])

    def test_fit_crop_margin_alpha_and_order_are_in_the_file(self):
        for mode in ('fit','crop'):
            path, _ = self.combine(self.files(), 'pdf', pdf_page_size='custom',
                                  pdf_width_mm=100,pdf_height_mm=100,pdf_margin_mm=10,pdf_fit=mode)
            prefix=main.TMP_DIR/('pdf-render-'+uuid.uuid4().hex)
            # Render the actual PDF at the encoder's DPI, not its intermediate PNGs.
            subprocess.run(['convert','-density','144',str(path),str(prefix)+'-%d.png'],check=True,capture_output=True)
            try:
                for index in range(3):
                    page=Path(str(prefix)+f'-{index}.png')
                    w,h=main._read_output_dims(page)
                    raw=subprocess.check_output(['convert',str(page),'-depth','8','rgb:-'])
                    pixel=lambda x,y:tuple(raw[(y*w+x)*3:][:3])
                    self.assertEqual(pixel(5,5),(255,255,255))
                    center=pixel(w//2,h//2);expected=(20+60*index,200-50*index,90)
                    self.assertTrue(all(abs(a-b)<4 for a,b in zip(center,expected)),(mode,index,center))
                    # 10 mm border remains white in both modes; landscape fit
                    # leaves vertical whitespace but crop fills the content box.
                    self.assertEqual(pixel(w//2,50),(255,255,255))
                    if mode == 'crop' or index == 0:
                        ink=[x for x in range(w) if pixel(x,h//2)!=(255,255,255)]
                        self.assertEqual((min(ink),max(ink)),(57,w-58))
                    if mode == 'crop':
                        ink=[y for y in range(h) if pixel(w//2,y)!=(255,255,255)]
                        self.assertEqual((min(ink),max(ink)),(57,h-58))
                    if index==0:
                        self.assertEqual(pixel(w//2,65)==(255,255,255),mode=='fit')
            finally:
                for page in main.TMP_DIR.glob(prefix.name+'-*.png'):page.unlink()
        transparent=subprocess.check_output(['convert','-size','20x20','xc:none','PNG32:-'])
        path,_=self.combine([('files','alpha.png',transparent,'image/png')],'pdf',pdf_page_size='a4')
        sample=subprocess.check_output(['convert','-density','10',str(path)+'[0]','-format','%[pixel:p{10,10}]','info:'],text=True)
        self.assertIn('255,255,255',sample)

    def test_bad_dimensions_margins_and_variants_do_not_publish(self):
        before=set(main.OUTPUT_DIR.iterdir())
        for extra in ({'pdf_width_mm':601},{'pdf_height_mm':1},{'pdf_width_mm':'nan'},
                      {'pdf_margin_mm':50},{'pdf_margin_mm':-1},{'pdf_fit':'stretch'},
                      {'pdf_orientation':'auto'},{'pdf_page_size':'a3'}):
            fields={'format':'pdf','pdf_page_size':'custom','pdf_width_mm':100,'pdf_height_mm':100,**extra}
            status,result=harness.post('/convert',fields,self.files())
            self.assertEqual(status,400,result)
        self.assertEqual(set(main.OUTPUT_DIR.iterdir()),before)

    def test_original_default_keeps_independent_pages_and_non_pdf_ignores_layout(self):
        path,_=self.combine(self.files(),'pdf')
        boxes=self.boxes(path)
        self.assertEqual(len(boxes),3);self.assertGreater(boxes[0][2],boxes[0][3]);self.assertLess(boxes[1][2],boxes[1][3])
        path,_=self.combine(self.files(),'tiff',pdf_page_size='custom',pdf_width_mm=999)
        self.assertEqual(main._probe_frames_and_size(path)[0],3)

    def test_reconvert_uses_the_same_layout_without_replaying_recipe(self):
        source,_=self.combine(self.files(),'tiff')
        status,result=harness.post('/reconvert/'+source.name,{'format':'pdf','pdf_page_size':'letter','pdf_orientation':'landscape'})
        self.assertEqual(status,200,result)
        path=main.OUTPUT_DIR/result['output_filename']
        self.assertEqual(len(self.boxes(path)),3)
        self.assertAlmostEqual(self.boxes(path)[0][2],792,delta=.26)
        self.assertEqual(self.recipe_for(path)['effective']['pdf_layout']['placement'],'fit')
        self.assertTrue(source.exists())

    def test_pdf_preferences_and_user_preset_survive_storage_reload(self):
        import json,urllib.request
        settings_path=main.SETTINGS_FILE
        original=settings_path.read_bytes() if settings_path.exists() else None
        backup=settings_path.with_name('settings.backup.json')
        original_backup=backup.read_bytes() if backup.exists() else None
        values={'setting-pdf-page-size':'custom','setting-pdf-orientation':'landscape',
                'setting-pdf-width-mm':'100','setting-pdf-height-mm':'150',
                'setting-pdf-margin-mm':'0','setting-pdf-fit':'crop'}
        try:
            current=json.load(urllib.request.urlopen(harness.BASE+'/settings'))
            data={**values,'user_presets':json.dumps({'Print':values})}
            request=urllib.request.Request(harness.BASE+'/settings',data=json.dumps(data).encode(),
                                          method='PUT',headers={'Content-Type':'application/json','If-Match':current['revision']})
            with urllib.request.urlopen(request) as response:self.assertEqual(response.status,200)
            stored=json.load(urllib.request.urlopen(harness.BASE+'/settings'))['settings']
            self.assertEqual(json.loads(stored['user_presets'])['Print'],values)
            self.assertEqual(main._read_settings_document()[0],stored)
        finally:
            if original is None:settings_path.unlink(missing_ok=True)
            else:settings_path.write_bytes(original)
            if original_backup is None:backup.unlink(missing_ok=True)
            else:backup.write_bytes(original_backup)

    def test_page_expansion_is_budgeted_before_native_rendering(self):
        layout=pdf_layout.resolve_layout('a4')
        with patch.object(main,'MAX_DECODED_PIXELS',1000),patch.object(main,'_run_or_500') as run:
            with self.assertRaises(main.HTTPException):main._layout_pdf_pages(['a.png'],layout,main.TMP_DIR,{})
            run.assert_not_called()
