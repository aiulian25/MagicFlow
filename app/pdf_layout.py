"""Physical PDF page layout. Pixels round at 144 dpi (at most 0.09 mm)."""
from dataclasses import dataclass
import math

DPI = 144
PAGE_SIZES = {'a4': (210.0, 297.0), 'letter': (215.9, 279.4)}


@dataclass(frozen=True)
class PDFLayout:
    width: int
    height: int
    margin: int
    mode: str

    @property
    def content_size(self):
        return self.width - 2 * self.margin, self.height - 2 * self.margin

    def facts(self):
        return {'page_width_pt': self.width * 72 / DPI,
                'page_height_pt': self.height * 72 / DPI,
                'margin_pt': self.margin * 72 / DPI, 'raster_dpi': DPI,
                'placement': self.mode, 'background': 'white', 'images_per_page': 1,
                'compression': 'lossless ZIP'}


def resolve_layout(size='original', orientation='portrait', width_mm=210.0,
                   height_mm=297.0, margin_mm=10.0, mode='fit'):
    if size not in ('original', 'a4', 'letter', 'custom'):
        raise ValueError('PDF page size must be original, a4, letter or custom.')
    if size == 'original':
        return None  # old callers retain independent image-sized pages
    if orientation not in ('portrait', 'landscape') or mode not in ('fit', 'crop'):
        raise ValueError('PDF orientation must be portrait/landscape and placement fit/crop.')
    if not all(math.isfinite(v) for v in (width_mm, height_mm, margin_mm)):
        raise ValueError('PDF dimensions and margins must be finite numbers.')
    if size == 'custom' and not (50 <= width_mm <= 600 and 50 <= height_mm <= 600):
        raise ValueError('Custom PDF sides must be 50–600 mm.')
    w, h = PAGE_SIZES.get(size, (width_mm, height_mm))
    w, h = sorted((w, h), reverse=orientation == 'landscape')
    if not 0 <= margin_mm <= 100 or min(w, h) - 2 * margin_mm < 1:
        raise ValueError('PDF margin must be 0–100 mm and leave at least 1 mm of content.')
    px = lambda mm: round(mm * DPI / 25.4)
    return PDFLayout(px(w), px(h), px(margin_mm), mode)


def build_page_command(source, output, layout):
    w, h = layout.content_size
    return ['convert', str(source), '+repage', '-background', 'white',
            '-alpha', 'remove', '-alpha', 'off', '-resize', f'{w}x{h}' + ('^' if layout.mode == 'crop' else ''),
            '-gravity', 'center', '-extent', f'{w}x{h}',
            '-extent', f'{layout.width}x{layout.height}', '+repage', str(output)]


def build_pdf_command(pages, output):
    return ['convert', *map(str, pages), '-units', 'PixelsPerInch', '-density', str(DPI),
            '-compress', 'Zip', str(output)]
