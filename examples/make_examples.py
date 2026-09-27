#!/usr/bin/env python3
"""
Regenerate the README example images in this directory.

    python examples/make_examples.py --small SMALL.adi --big BIG.adi

SMALL is a short log (a few dozen QSOs), BIG a large one (1,000+ QSOs).
Needs hamap on PATH (pipx install -e .), Pillow, and Google Chrome for the
HTML screenshots.  Full renders go to a temp dir; only the downscaled
overviews and 1:1-ish crops land here.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

Image.MAX_IMAGE_PIXELS = None
HERE = os.path.dirname(os.path.abspath(__file__))
CHROME = shutil.which('google-chrome') or shutil.which('chromium')


def render(log, out, *opts):
    """Run hamap; return the map extent (lon0, lon1, lat0, lat1) it used."""
    res = subprocess.run(['hamap', log, '-o', out, '--verbose', *opts],
                         capture_output=True, text=True)
    if res.returncode:
        sys.exit(f"hamap failed: {' '.join(opts)}\n{res.stderr}")
    m = re.search(r'Extent \(\w+\): lon ([-\d.]+)\.\.([-\d.]+), lat ([-\d.]+)\.\.([-\d.]+)',
                  res.stdout + res.stderr)
    return tuple(float(v) for v in m.groups()) if m else None


def crop(src, extent, lon0, lon1, lat0, lat1, width):
    """Crop a lat/lon window from a full render, at most *width* px wide (never upscaled)."""
    im = Image.open(src).convert('RGB')
    W = im.size[0]
    e_lon0, e_lon1, e_lat0, e_lat1 = extent
    ax_h = W * (e_lat1 - e_lat0) / (e_lon1 - e_lon0)
    # the saved image is trimmed to the axes, which start at its top edge
    box = (int((lon0 - e_lon0) / (e_lon1 - e_lon0) * W),
           int((e_lat1 - lat1) / (e_lat1 - e_lat0) * ax_h),
           int((lon1 - e_lon0) / (e_lon1 - e_lon0) * W),
           int((e_lat1 - lat0) / (e_lat1 - e_lat0) * ax_h))
    t = im.crop(box)
    if t.size[0] <= width:
        return t                    # native pixels: a true 1:1 crop of the render
    return t.resize((width, round(width * t.size[1] / t.size[0])), Image.LANCZOS)


def overview(src, width):
    im = Image.open(src).convert('RGB')
    return im.resize((width, round(width * im.size[1] / im.size[0])), Image.LANCZOS)


def _small(img):
    """256-colour PNG: no visible loss on these maps, about a third of the size."""
    return img.quantize(256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)


def save(img, name):
    path = os.path.join(HERE, name)
    _small(img).save(path, optimize=True)
    print(f"  {name}  {img.size[0]}×{img.size[1]}  {os.path.getsize(path) // 1024} KB")


def screenshot(html, frag, name, size=(1600, 1000)):
    if not CHROME:
        print(f"  (skipped {name}: no Chrome)")
        return
    out = os.path.join(HERE, name)
    subprocess.run([CHROME, '--headless=new', '--disable-gpu', '--hide-scrollbars',
                    f'--window-size={size[0]},{size[1]}', '--virtual-time-budget=15000',
                    f'--screenshot={out}', f'file://{html}#{frag}'],
                   capture_output=True)
    im = Image.open(out).convert('RGB')
    _small(im).save(out, optimize=True)
    print(f"  {name}  {im.size[0]}×{im.size[1]}  {os.path.getsize(out) // 1024} KB")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--small', required=True, help='short ADIF log')
    ap.add_argument('--big', required=True, help='large ADIF log')
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix='hamap-examples-')
    t = lambda n: os.path.join(tmp, n)          # noqa: E731
    GL = (-90, -76, 37, 46)                      # Great Lakes / Ohio Valley window
    EU = (-11, 28, 42, 60)                       # western / central Europe

    print("Rendering (this takes a few minutes)...")
    e = render(a.big, t('big.png'), '--profile', 'big')
    save(overview(t('big.png'), 1600), 'big-profile-overview.png')
    save(crop(t('big.png'), e, *GL, 1400), 'big-profile-detail.png')
    save(crop(t('big.png'), e, *EU, 1400), 'big-profile-europe.png')

    e = render(a.big, t('sum.png'), '--profile', 'big', '--box-calls', '0')
    save(crop(t('sum.png'), e, *GL, 1400), 'summary-boxes.png')

    e = render(a.big, t('rg4.png'), '--profile', 'big', '--fill', 'grid4')
    save(crop(t('rg4.png'), e, *GL, 1400), 'region-boxes-grid4-fill.png')

    e = render(a.big, t('grids.png'), '--profile', 'grids')
    save(crop(t('grids.png'), e, -92, -72, 36, 47, 1400), 'grids-profile.png')

    e = render(a.big, t('dxcc.png'), '--profile', 'dxcc')
    save(overview(t('dxcc.png'), 1600), 'dxcc-profile-overview.png')

    e = render(a.small, t('small.png'))
    save(crop(t('small.png'), e, -126, -66, 24, 50, 1400), 'small-log-usa.png')

    print("HTML screenshots...")
    render(a.big, t('big.html'), '--html')
    screenshot(t('big.html'), 'fill=region&dots=region&view=38,-92,3.2&pin=Georgia,Ohio',
               'html-panel-popup.png')
    screenshot(t('big.html'), 'fill=grid4&dots=grid4&bands=40m&gfields=1&panel=0&view=45,-40,2.2',
               'html-grid4-40m.png')

    shutil.rmtree(tmp)
    print("Done.")


if __name__ == '__main__':
    main()
