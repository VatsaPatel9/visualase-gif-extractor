"""Render the extracted geometry on top of the original GIF frames (annotated GIF export)."""
import io
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .ocr import PANEL

COL_FIBER = (255, 221, 0)
COL_CENTER = (0, 229, 255)
COL_TIP = (255, 64, 64)
COL_POINT = (255, 80, 255)
COL_LINE = (255, 255, 255)


def _font(size):
    for p in ('/System/Library/Fonts/Supplemental/Arial.ttf', '/Library/Fonts/Arial.ttf',
              'C:/Windows/Fonts/arial.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _draw_labels(d, labels, font, gap):
    """Draw labels beside their points, nudging them vertically so they never overlap."""
    placed = []
    for y, x, txt in sorted(labels):
        tb = d.textbbox((0, 0), txt, font=font)
        w, h = tb[2] + 4, tb[3] + 3
        x0, y_home = x + gap + 2, y - h / 2
        y0 = y_home
        for k in (0, 1, -1, 2, -2, 3, -3, 4, -4, 5):
            cand = y_home + k * (h + 1)
            if not any(x0 < px + pw and px < x0 + w and cand < py + h and py < cand + h for px, py, pw in placed):
                y0 = cand
                break
        placed.append((x0, y0, w))
        if abs(y0 - y_home) > 1:
            d.line([(x + gap, y), (x0, y0 + h / 2)], fill=COL_POINT, width=1)
        d.rectangle([x0 - 1, y0, x0 + w, y0 + h], fill=(0, 0, 0))
        d.text((x0 + 1, y0 + 1), txt, fill=COL_LINE, font=font)


def draw_overlay(img, job, frame, scale=2, show=None, pixel_mm=1.015625):
    """Return an RGB PIL image (scaled) with fiber, center, tip, points and distance labels drawn."""
    show = show or {'fiber': True, 'center': True, 'tip': True, 'points': True, 'lines': True, 'labels': True}
    im = img.convert('RGB').resize((img.width * scale, img.height * scale), Image.NEAREST)
    d = ImageDraw.Draw(im)
    font = _font(7 * scale)
    cols = [0] + ([1] if job.anat_col == 1 else [])
    for row in range(job.rows):
        sidx = frame['slice'].get(str(row))
        if sidx is None:
            continue
        s = job.slices[sidx]
        if not s.get('fiber'):
            continue
        E = np.asarray(s['fiber']['entry'], float)
        T = np.asarray(s['fiber']['tip'], float)
        C = (E + T) / 2
        for col in cols:
            ox, oy = col * PANEL, row * PANEL

            def P(q):
                return ((q[0] + ox + 0.5) * scale, (q[1] + oy + 0.5) * scale)
            if show.get('fiber'):
                d.line([P(E), P(T)], fill=COL_FIBER, width=max(1, scale // 2))
            if col == 0 and show.get('points'):
                labels = []
                for key, v in frame['points'].items():
                    if int(key.split(':')[0]) != row or v[2] is None:
                        continue
                    q = (v[2], v[3])
                    if show.get('lines'):
                        d.line([P(C), P(q)], fill=COL_LINE, width=1)
                    x, y = P(q)
                    rr = 4 * scale
                    d.ellipse([x - rr, y - rr, x + rr, y + rr], outline=COL_POINT, width=max(1, scale // 2))
                    if show.get('labels'):
                        dist = float(np.hypot(q[0] - C[0], q[1] - C[1]) * pixel_mm)
                        labels.append((y, x, f'{key.split(":")[1]}: {dist:.1f} mm'))
                _draw_labels(d, labels, font, 4 * scale)
            for q, colr, on in ((T, COL_TIP, show.get('tip')), (C, COL_CENTER, show.get('center'))):
                if on:
                    x, y = P(q)
                    rr = 3 * scale
                    d.ellipse([x - rr, y - rr, x + rr, y + rr], outline=colr, width=max(1, scale // 2 + 1))
    return im


def annotated_gif(job, path, scale=2, step=1, show=None, pixel_mm=1.015625, progress=None):
    idx = list(range(0, job.n_frames, max(1, int(step))))

    def frames():
        for n, i in enumerate(idx):
            base = Image.open(io.BytesIO(job.frame_png[i]))
            im = draw_overlay(base, job, job.frames[i], scale, show, pixel_mm)
            if progress:
                progress((n + 1) / len(idx))
            yield im.quantize(colors=255, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)

    it = frames()
    first = next(it)
    durs = [job.durations[i] * max(1, int(step)) if step > 1 else job.durations[i] for i in idx]
    first.save(path, save_all=True, append_images=it, duration=durs, loop=0, optimize=False, disposal=1)
    return path
