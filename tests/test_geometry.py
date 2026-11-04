"""FIX-014: fixed-canvas outputs preserve every mixed-size frame.

Run in a disposable container with app/tests mounted read-only and fresh /data:
  python3 -I -m unittest discover -s /tests -p test_geometry.py -v
Uses the existing stdlib HTTP/media harness; no production data.
"""
import shutil
import subprocess
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import test_timing as harness

main = harness.main


class GeometryTest(unittest.TestCase):
    setUpClass = classmethod(harness.ConvertTimingTest.setUpClass.__func__)
    tearDownClass = classmethod(harness.ConvertTimingTest.tearDownClass.__func__)
    setUp = harness.ConvertTimingTest.setUp
    tearDown = harness.ConvertTimingTest.tearDown
    combine = harness.ConvertTimingTest.combine
    recipe_for = harness.ConvertTimingTest.recipe_for

    def mixed(self, sizes=((32, 16), (16, 32), (8, 8))):
        return [('files', f'f{i}.png', harness.coloured_png(i, w, h), 'image/png')
                for i, (w, h) in enumerate(sizes)]

    def decoded(self, path):
        """Decode to raw RGBA with each output frame's real size, not a resize."""
        work = main.TMP_DIR / ('geometry-' + uuid.uuid4().hex)
        work.mkdir()
        try:
            frames = main._extract_frames(path, work, {}, prefix='check')
            result = []
            for frame in frames:
                w, h = main._read_output_dims(Path(frame))
                raw = subprocess.check_output(['convert', frame, '-depth', '8', 'rgba:-'])
                result.append((w, h, raw))
            return result
        finally:
            shutil.rmtree(work)

    def test_mixed_frames_keep_order_count_and_content_in_every_animation_format(self):
        for fmt in ('apng', 'mp4', 'webm', 'gif', 'webp'):
            with self.subTest(format=fmt):
                path, _ = self.combine(self.mixed(), fmt, quality=100, optimize_layers='false')
                frames = self.decoded(path)
                self.assertEqual([(w, h) for w, h, _ in frames], [(32, 32)] * 3)
                # Each center keeps that input's distinctive color, so count
                # alone cannot hide a duplicated or replaced picture.
                for i, (w, h, raw) in enumerate(frames):
                    center = raw[((h // 2) * w + w // 2) * 4:][:3]
                    expected = (20 + i * 60, 200 - i * 50, 90)
                    self.assertTrue(all(abs(a - b) <= 10 for a, b in zip(center, expected)),
                                    (fmt, i, list(center), expected))

    def test_fit_is_shrink_only_with_centered_transparent_padding(self):
        path, _ = self.combine(self.mixed(), 'apng', width=24, height=24)
        frames = self.decoded(path)
        self.assertEqual([(w, h) for w, h, _ in frames], [(24, 24)] * 3)
        # 32x16 -> 24x12, 16x32 -> 12x24, 8x8 stays 8x8.
        for (_, _, raw), (w, h) in zip(frames, ((24, 12), (12, 24), (8, 8))):
            opaque = [(i % 24, i // 24) for i, alpha in enumerate(raw[3::4]) if alpha == 255]
            self.assertEqual(len(opaque), w * h)
            self.assertEqual((min(x for x, _ in opaque), min(y for _, y in opaque)),
                             ((24 - w) // 2, (24 - h) // 2))

    def test_explicit_pad_crop_and_stretch_remain_distinct(self):
        files = self.mixed()[:2]
        # A blue left edge survives padding/stretch, but center-crop removes it.
        striped = subprocess.check_output(['convert', 'png:-', '-fill', 'blue',
            '-draw', 'rectangle 0,0 7,15', 'PNG32:-'], input=files[0][2])
        files[0] = ('files', 'striped.png', striped, 'image/png')
        for mode in ('pad', 'crop', 'stretch'):
            with self.subTest(mode=mode):
                path, _ = self.combine(files, 'apng', width=20, height=20,
                                       resize_mode=mode)
                frames = self.decoded(path)
                self.assertEqual([(w, h) for w, h, _ in frames], [(20, 20)] * 2)
                alpha = frames[0][2][3::4]
                self.assertEqual(sum(v == 255 for v in alpha), 200 if mode == 'pad' else 400)
                left_center = frames[0][2][(10 * 20) * 4:][:3]
                self.assertEqual(left_center[2] > 240, mode != 'crop')

    def test_width_only_and_rotation_use_transformed_dimensions(self):
        for fields, expected in [({'width': 16}, (16, 32)),
                                 ({'width': 16, 'rotate': 90}, (16, 32)),
                                 ({'height': 16}, (32, 16))]:
            with self.subTest(fields=fields):
                path, _ = self.combine(self.mixed(), 'apng', **fields)
                self.assertEqual([(w, h) for w, h, _ in self.decoded(path)], [expected] * 3)

    def test_odd_mp4_dimensions_pad_right_and_bottom_and_record_actual_canvas(self):
        path, _ = self.combine(self.mixed(((31, 15), (15, 31), (7, 7))), 'mp4',
                               background='#ffffff', quality=100)
        self.assertEqual(main._probe_frames_and_size(path), (3, 32, 32))
        recipe = self.recipe_for(path)
        self.assertEqual((recipe['effective']['width'], recipe['effective']['height']), (32, 32))
        # Check pre-encode geometry precisely; lossy video borders can ring.
        work = main.TMP_DIR / ('geometry-' + uuid.uuid4().hex)
        work.mkdir()
        try:
            src = work / 'prep_00000.png'
            src.write_bytes(harness.coloured_png(0, 31, 15))
            main._normalize_animation_canvas(work, 'mp4', '#ffffff', {})
            raw = subprocess.check_output(['convert', str(src), '-depth', '8', 'rgba:-'])
            self.assertEqual(main._read_output_dims(src), (32, 16))
            self.assertEqual(raw[:4], bytes((20, 200, 90, 255)))
            self.assertEqual(raw[31*4:32*4], bytes((255, 255, 255, 255)))
            self.assertEqual(raw[-4:], bytes((255, 255, 255, 255)))
        finally:
            shutil.rmtree(work)

    def test_default_mp4_padding_is_opaque_black(self):
        path, _ = self.combine(self.mixed(), 'mp4', quality=100)
        for _, _, raw in self.decoded(path):
            self.assertTrue(all(v < 12 for v in raw[:3]))
            self.assertEqual(raw[3], 255)

    def test_mixed_palette_gray_and_alpha_inputs_keep_all_frames_and_timing(self):
        work = main.TMP_DIR / ('geometry-' + uuid.uuid4().hex)
        work.mkdir()
        try:
            specs = [('palette', 'PNG8', 'red'), ('gray', 'PNG', 'gray50'),
                     ('alpha', 'PNG32', 'rgba(0,255,0,0.5)')]
            files = []
            for name, coder, color in specs:
                src = work / (name + '.png')
                subprocess.run(['convert', '-size', '16x16', 'xc:' + color,
                                coder + ':' + str(src)], check=True)
                files.append(('files', src.name, src.read_bytes(), 'image/png'))
            for fmt in ('apng', 'webm'):
                with self.subTest(fmt=fmt):
                    path, _ = self.combine(files, fmt, delays='100,300,200', boomerang='true')
                    self.assertEqual(main._probe_frames_and_size(path)[0], 4)
                    delays = harness.frame_durations_ms(path)
                    self.assertEqual(delays[:3], [100, 300, 200])
        finally:
            shutil.rmtree(work)

    def test_documents_keep_independent_page_sizes_and_can_reconvert_to_shared_canvas(self):
        path, _ = self.combine(self.mixed(), 'tiff')
        self.assertEqual([(w, h) for w, h, _ in self.decoded(path)], [(32, 16), (16, 32), (8, 8)])
        for fmt in ('apng', 'mp4', 'gif', 'webp'):
            with self.subTest(fmt=fmt):
                status, payload = harness.post('/reconvert/' + path.name, {'format': fmt})
                self.assertEqual(status, 200, payload)
                output = main.OUTPUT_DIR / payload['output_filename']
                self.assertEqual([(w, h) for w, h, _ in self.decoded(output)], [(32, 32)] * 3)
        pdf, _ = self.combine(self.mixed(), 'pdf')
        info = subprocess.check_output(['identify', '-format', '%w %h\n', str(pdf)], text=True)
        pages = [tuple(map(int, line.split())) for line in info.splitlines()]
        self.assertEqual(len(pages), 3)
        self.assertGreater(pages[0][0], pages[0][1])
        self.assertLess(pages[1][0], pages[1][1])
        self.assertEqual(pages[2][0], pages[2][1])

    def test_canvas_expansion_checks_pixel_budget_before_padding(self):
        work = main.TMP_DIR / ('geometry-' + uuid.uuid4().hex)
        work.mkdir()
        try:
            for i, (w, h) in enumerate(((100, 1), (1, 100))):
                (work / f'prep_{i:05d}.png').write_bytes(harness.coloured_png(i, w, h))
            with patch.object(main, 'MAX_DECODED_PIXELS', 1000), patch.object(main, '_run_or_500') as run:
                with self.assertRaises(main.HTTPException) as raised:
                    main._normalize_animation_canvas(work, 'apng', '', {})
                self.assertEqual(raised.exception.status_code, 400)
                run.assert_not_called()
        finally:
            shutil.rmtree(work)

    def test_incompatible_normalized_frames_stop_before_encoding(self):
        work = main.TMP_DIR / ('geometry-' + uuid.uuid4().hex)
        work.mkdir()
        try:
            (work / 'prep_00000.png').write_bytes(harness.coloured_png(0, 32, 16))
            (work / 'prep_00001.png').write_bytes(harness.coloured_png(1, 16, 32))
            def broken_normalizer(*args):
                (work / 'canvas.png').write_bytes(harness.coloured_png(0, 1, 1))
            with patch.object(main, '_run_or_500', side_effect=broken_normalizer):
                with self.assertRaises(main.HTTPException) as raised:
                    main._normalize_animation_canvas(work, 'apng', '', {})
                self.assertIn('incompatible dimensions or pixel formats', raised.exception.detail)
        finally:
            shutil.rmtree(work)


if __name__ == '__main__':
    unittest.main()
