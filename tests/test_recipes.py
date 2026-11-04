"""FIX-012: recipes describe past work; re-encoding starts from baked pixels.

Run in an isolated container, with app and tests mounted read-only and a fresh
/data: python3 -I -m unittest discover -s /tests -p 'test_recipes.py' -v
Uses the existing timing suite's stdlib HTTP/fixture harness, never live data.
"""
import json
import subprocess
import unittest
import urllib.request
from unittest.mock import patch

import test_timing as harness

main = harness.main


class RecipesTest(unittest.TestCase):
    setUpClass = classmethod(harness.ConvertTimingTest.setUpClass.__func__)
    tearDownClass = classmethod(harness.ConvertTimingTest.tearDownClass.__func__)
    setUp = harness.ConvertTimingTest.setUp
    tearDown = harness.ConvertTimingTest.tearDown
    stills = harness.ConvertTimingTest.stills
    combine = harness.ConvertTimingTest.combine
    recipe_for = harness.ConvertTimingTest.recipe_for

    def reconvert(self, source, **fields):
        status, result = harness.post('/reconvert/' + source.name,
            {'format': 'gif', 'keep_output': 'true', **fields})
        self.assertEqual(status, 200, result)
        return main.OUTPUT_DIR / result['output_filename']

    def test_rotation_negate_and_boomerang_are_not_reapplied(self):
        source, _ = self.combine(self.stills(), 'gif', rotate=90, negate='true',
            boomerang='true', delays='100,300,200', optimize_layers='false')
        result = self.reconvert(source, optimize_layers='false')
        self.assertEqual(main._read_output_dims(source), (24, 32))
        self.assertEqual(main._read_output_dims(result), (24, 32))
        self.assertEqual(main._probe_frames_and_size(result)[0], 4)
        for flag in ([], ['-coalesce']):
            def pixels(path):
                return subprocess.check_output(['convert', str(path), *flag, '-depth', '8', 'rgba:-'])
            self.assertEqual(pixels(source), pixels(result))
        self.assertEqual(harness.frame_durations_ms(result), [100, 300, 200, 300])
        recipe = self.recipe_for(result)
        self.assertEqual(recipe['requested']['rotate'], 0)
        self.assertEqual(recipe['provenance']['source_kind'], 'saved_output')
        self.assertEqual(recipe['sources'], [source.name])

    def test_requested_size_and_output_size_coexist_for_all_producers(self):
        combined, _ = self.combine(self.stills(), 'gif', width=20, height=20, rotate=90)
        self.assertEqual(main._read_output_dims(combined), (15, 20))
        status, payload = harness.post('/convert-image',
            {'out_format': 'png', 'width': 20, 'height': 20},
            [('file', 'original.png', harness.coloured_png(0), 'image/png')])
        self.assertEqual(status, 200, payload)
        still = main.OUTPUT_DIR / payload['output_filename']
        reencoded = self.reconvert(still, width=10, height=10)
        for path, requested, measured in [(combined, (20, 20), (15, 20)),
                (still, (20, 20), (20, 15)), (reencoded, (10, 10), (10, 8))]:
            with self.subTest(producer=path.name):
                recipe = self.recipe_for(path)
                self.assertEqual(recipe['schema_version'], 2)
                self.assertEqual(tuple(recipe['requested'][k] for k in ('width', 'height')), requested)
                self.assertEqual(tuple(recipe['effective'][k] for k in ('width', 'height')), measured)
                self.assertEqual(tuple(recipe[k] for k in ('width', 'height')), measured)

    def test_requested_delays_and_measured_timeline_are_separate(self):
        path, _ = self.combine(self.stills(), 'gif', delays='105,305,205', boomerang='true')
        recipe = self.recipe_for(path)
        self.assertEqual(recipe['requested']['delays'], '105,305,205')
        self.assertEqual(recipe['effective']['timing']['measured_delays_ms'],
                         harness.frame_durations_ms(path))
        self.assertTrue(recipe['warnings'])
        self.assertFalse(recipe['provenance']['original_uploads_retained'])

    def test_video_encoder_quality_and_real_size_are_recorded(self):
        path, _ = self.combine(self.stills(), 'mp4', width=15, height=11,
                               resize_mode='stretch', quality=40, delays='100,300,200')
        recipe = self.recipe_for(path)
        effective = recipe['effective']
        self.assertEqual((effective['width'], effective['height']), (16, 12))
        self.assertEqual(effective['encoder'], 'libx264')
        self.assertEqual(effective['video_crf'], harness.utils.video_crf_for_quality('mp4', 40))
        self.assertEqual(effective['timing']['basis'], 'frame_intervals')
        self.assertEqual(effective['timing']['measured_delays_ms'], [100, 300])
        self.assertTrue(any('final' in warning.lower() for warning in recipe['warnings']))

    def test_apng_reports_ignored_quality_and_timing_fallback(self):
        source, _ = self.combine(self.stills(), 'gif')
        with patch.object(main, '_read_frame_delays', return_value=None):
            result = self.reconvert(source, format='apng', quality=17, delay=155)
        recipe = self.recipe_for(result)
        self.assertIsNone(recipe['effective']['quality'])
        self.assertEqual(recipe['effective']['encoder'], 'apng')
        self.assertEqual(recipe['effective']['timing']['mode'], 'fallback_uniform')
        self.assertTrue(any('quality' in warning.lower() for warning in recipe['warnings']))
        self.assertTrue(any('source timing' in warning.lower() for warning in recipe['warnings']))

    def test_legacy_unknown_and_malformed_sidecars_are_never_rewritten(self):
        source, _ = self.combine(self.stills(), 'gif')
        sidecar = main.RECIPE_DIR / (source.name + '.json')
        for content in ['{"rotate":90,"extension":{"future":"keep me"}}',
                        '{broken', '[]', 'null', '"wrong type"']:
            with self.subTest(content=content):
                sidecar.write_text(content)
                with urllib.request.urlopen(harness.BASE + '/files') as response:
                    files = json.load(response)['files']
                recipe = next(item['recipe'] for item in files if item['filename'] == source.name)
                if content.startswith('{"'):
                    self.assertEqual(recipe['extension'], {'future': 'keep me'})
                else:
                    self.assertIsNone(recipe)
                self.reconvert(source)
                self.assertEqual(sidecar.read_text(), content)
        sidecar.unlink()
        self.reconvert(source)
        self.assertFalse(sidecar.exists())


if __name__ == '__main__':
    unittest.main()
