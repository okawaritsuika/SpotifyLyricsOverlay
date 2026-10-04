"""Official brand marks for catalog navigation, loaded once per Tk root."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageTk

from explorer_library import SERIES_COLORS


ASSET_DIR = Path(__file__).resolve().parent / 'resources' / 'series_symbols'
BRAND_KEYS = frozenset(('765', 'million', 'cinderella', 'sidem', 'shiny', 'gakuen'))


def _render_symbol(key, size):
    if key in BRAND_KEYS:
        with Image.open(ASSET_DIR / (key + '.png')) as original:
            return original.convert('RGBA').resize((size, size), Image.Resampling.LANCZOS)
    # All / other are navigation categories, not brands.
    canvas = Image.new('RGBA', (128, 128))
    draw = ImageDraw.Draw(canvas)
    color = SERIES_COLORS.get(key, SERIES_COLORS['other'])
    if key == '':
        draw.polygon([(48, 28), (104, 16), (104, 40), (48, 52)], fill=color)
        draw.line([(48, 36), (48, 92)], fill=color, width=12)
        draw.line([(104, 28), (104, 80)], fill=color, width=12)
        draw.ellipse((16, 80, 52, 108), fill=color)
        draw.ellipse((72, 68, 108, 96), fill=color)
    else:
        draw.ellipse((10, 32, 82, 96), outline=color, width=14)
        draw.ellipse((46, 32, 118, 96), outline=color, width=14)
        draw.line([(48, 64), (80, 64)], fill='#f2f8ff', width=10)
    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def series_symbol(master, key, size=28):
    """Keep Tk images alive for their interpreter without a global root reference."""
    root = master._root()
    cache = getattr(root, '_explorer_series_symbols', None)
    if cache is None:
        cache = root._explorer_series_symbols = {}
    identity = (key, size)
    if identity not in cache:
        cache[identity] = ImageTk.PhotoImage(_render_symbol(key, size), master=root)
    return cache[identity]
