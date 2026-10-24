"""Renders the PNG/ICO app icons from the same shapes as static/icon.svg.
Needs Pillow (dev machine only; the server itself doesn't).  python server/make_icons.py"""
import os

from PIL import Image, ImageDraw

INK, LIME = (11, 11, 11, 255), (200, 245, 60, 255)
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
SS = 8  # supersampling for smooth edges


def icon(size, rounded=True):
    n = size * SS
    s = n / 64.0
    img = Image.new('RGBA', (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if rounded:
        d.rounded_rectangle([0, 0, n - 1, n - 1], radius=12 * s, fill=INK)
    else:
        d.rectangle([0, 0, n, n], fill=INK)

    def line(points, w):
        pts = [(x * s, y * s) for x, y in points]
        d.line(pts, fill=LIME, width=round(w * s), joint='curve')
        r = w * s / 2
        for x, y in pts:  # round caps
            d.ellipse([x - r, y - r, x + r, y + r], fill=LIME)

    line([(32, 38), (32, 13)], 3.5)
    line([(32, 33), (22, 27), (22, 22)], 3.5)
    line([(32, 29), (42, 23), (42, 20)], 3.5)
    d.polygon([(32 * s, 6 * s), (37 * s, 14 * s), (27 * s, 14 * s)], fill=LIME)
    for cx, cy, r in ((22, 19.5, 3.5), (32, 40, 4)):
        d.ellipse([(cx - r) * s, (cy - r) * s, (cx + r) * s, (cy + r) * s], fill=LIME)
    d.rectangle([38.5 * s, 13.5 * s, 45.5 * s, 20.5 * s], fill=LIME)
    line([(12, 52), (52, 52)], 4)
    line([(18, 46), (12, 52), (18, 58)], 4)
    line([(46, 46), (52, 52), (46, 58)], 4)
    return img.resize((size, size), Image.LANCZOS)


def main():
    icon(192).save(os.path.join(STATIC, 'icon-192.png'))
    icon(512).save(os.path.join(STATIC, 'icon-512.png'))
    icon(180, rounded=False).save(os.path.join(STATIC, 'apple-touch-icon.png'))  # iOS rounds it itself
    icon(48).save(os.path.join(STATIC, 'favicon.ico'), sizes=[(16, 16), (32, 32), (48, 48)])
    print('icons written to', STATIC)


if __name__ == '__main__':
    main()
