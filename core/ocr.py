"""Read the Visualase overlay of one GIF frame: status line, point legend and point-label positions."""
import re
import itertools
import numpy as np
from scipy import ndimage

from .glyphs import TEMPLATES, DIGITS, STATUS_CHARS, find_glyphs, find_relaxed, read_line

PANEL = 256
LEGEND_RE = re.compile(r'^(\d+)\s+(-?\d+\.\d|--\.-)\s+<(\d+)$')
STATUS_RE = re.compile(r'^(\d\d):(\d\d) Laser (off\.|Power = +(\d+)%(?: +(\d+\.\d) Watts)?)$')


def color_masks(a):
    """Magenta (point text) and white (status text) masks; tolerant to per-frame palette shifts."""
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    magenta = (r >= 200) & (g <= 80) & (b >= 200)
    white = (r >= 200) & (g >= 200) & (b >= 200)
    return magenta, white


def read_status(white):
    """Best-fit read of the bottom status line (6-px grid from x=0).

    Returns dict(raw, time, secs, laser, power, watts) - fields are None when unreadable.
    """
    H, W = white.shape
    y0 = H - 11
    chars = []
    for c in range(min(W, 2 * PANEL) // 6):
        cell = white[y0:y0 + 10, 6 * c:6 * c + 5]
        if not cell.any():
            chars.append(' ')
            continue
        best = None
        for ch in STATUS_CHARS:
            t = TEMPLATES[ch]
            miss = int((t & ~cell).sum())
            extra = int((~t & cell).sum())
            score = miss * 3 + extra
            if best is None or score < best[0]:
                best = (score, ch, miss, extra)
        score, ch, miss, extra = best
        # exact, or all glyph pixels present with a few stray bright pixels from the image behind
        chars.append(ch if (score == 0 or (miss == 0 and extra <= 4)) else '?')
    text = ''.join(chars)
    m = re.match(r'^(\d\d:\d\d Laser (?:off\.|Power = +\d+%(?: +\d+\.\d Watts)?))', text)
    raw = m.group(1) if m else text.strip()
    out = {'raw': raw, 'time': None, 'secs': None, 'laser': None, 'power': None, 'watts': None}
    m = STATUS_RE.match(raw)
    if m:
        off = m.group(3).startswith('off')
        out.update(time=f'{m.group(1)}:{m.group(2)}', secs=int(m.group(1)) * 60 + int(m.group(2)),
                   laser='Off' if off else 'On',
                   power=0 if off else int(m.group(4)),
                   watts=0.0 if off else (float(m.group(5)) if m.group(5) else None))
    return out


def group_lines(glyphs):
    """Group glyph hits into text runs: same top row, 6-px pitch, gaps of up to 2 blank cells."""
    rows = {}
    for y, x, ch in glyphs:
        rows.setdefault(y, []).append((x, ch))
    runs = []
    for y, items in rows.items():
        items.sort()
        cur = [items[0]]
        for x, ch in items[1:]:
            px = cur[-1][0]
            if (x - px) % 6 == 0 and x - px <= 18:
                cur.append((x, ch))
            else:
                runs.append((y, cur))
                cur = [(x, ch)]
        runs.append((y, cur))
    out = []
    for y, items in runs:
        x0 = items[0][0]
        s = ''
        for x, ch in items:
            s += ' ' * ((x - x0) // 6 - len(s)) + ch
        out.append((y, x0, s, items))
    return out


def _glyph_mask(shape, hits):
    m = np.zeros(shape, bool)
    for y, x, ch in hits:
        t = TEMPLATES[ch]
        h, w = m[y:y + 10, x:x + 5].shape
        m[y:y + h, x:x + w] |= t[:h, :w]
    return m


def _resolve_cluster(mag, comp, expected):
    """Explain an irregular magenta blob (overlapping labels) as 1-3 digit placements."""
    ys, xs = np.nonzero(comp)
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    if (y1 - y0) > 30 or (x1 - x0) > 30:
        return None
    cands = []
    for ch in DIGITS[1:]:
        t = TEMPLATES[ch]
        for y in range(y0 - 9, y1 + 1):
            for x in range(x0 - 4, x1 + 1):
                if y < 0 or x < 0 or y + 10 > mag.shape[0] or x + 5 > mag.shape[1]:
                    continue
                if not (mag[y:y + 10, x:x + 5] | ~t).all():
                    continue
                cover = np.zeros_like(comp)
                cover[y:y + 10, x:x + 5] = t
                if (cover & comp).sum() < t.sum() * 0.5:
                    continue
                cands.append((y, x, ch, cover))
    pools = ([[c for c in cands if c[2] in expected]] if expected else []) + [cands]
    for pool in pools:
        for k in range(1, 4):
            if k == 3 and len(pool) > 40:
                break
            best = None
            for combo in itertools.combinations(pool, k):
                if len({c[2] for c in combo}) != k:
                    continue
                cov = np.zeros_like(comp)
                for c in combo:
                    cov |= c[3]
                if (cov & comp).sum() != comp.sum():
                    continue
                extra = int((cov & ~comp).sum())
                if best is None or extra < best[0]:
                    best = (extra, [(c[0], c[1], c[2]) for c in combo])
            if best is not None:
                return best[1]
    return None


LEGEND_LOOSE_RE = re.compile(r'^(\d+)\s+(-?\d+\.\d|--\.-)\s+<(\d{2,3})(?![\d])')


def _legend_lines(mask, lines):
    """Legend lines ("<label>  <temp> <<limit>") among grouped glyph runs of one colour.

    A run that starts like a legend line but fails the strict parse (e.g. its "<90" runs over bright
    image pixels) is re-read cell by cell with a tolerant matcher. Returns [(y, x0, label, temp, limit, cells)].
    """
    out = []
    for y, x0, s, items in lines:
        m = LEGEND_RE.match(s)
        cells = [(y, xx, ch) for xx, ch in items]
        if not m and re.match(r'^\d+\s+[-\d]', s):
            txt = read_line(mask, y, x0)
            m = LEGEND_LOOSE_RE.match(txt)
            if m:
                cells = [(y, x0 + 6 * k, ch) for k, ch in enumerate(txt[:m.end()]) if ch in TEMPLATES]
        if m:
            temp = m.group(2)
            out.append((y, x0, int(m.group(1)), None if '-' in temp else float(temp), int(m.group(3)), cells))
    return out


def _pick(cands, hint, max_dist=25):
    """Choose one label placement: nearest to where the point was last seen, else the only candidate."""
    if not cands:
        return None
    if hint is not None:
        d = [((y - hint[0]) ** 2 + (x - hint[1]) ** 2) ** 0.5 for y, x in cands]
        k = min(range(len(cands)), key=d.__getitem__)
        return cands[k] if d[k] <= max_dist else None
    return cands[0] if len(cands) == 1 else None


def analyze_frame(a, hints=None):
    """OCR one RGB frame (H x W x 3 int array).

    hints: optional {panel_row: {label: (y, x)}} - where each point label was seen in earlier frames;
           used to place labels that cannot be told apart from the image on their own.

    Returns (status_dict, panels) where panels maps panel row -> {
        'legend': {label: (temp or None, limit)},
        'markers': {label: [(y, x, how), ...]}   # glyph cell top-left, image coords
        'alarm': {label, ...}                    # points drawn in white (reading over its limit)
        'notes': [...]
    } for the left (temperature) column of panels.

    Visualase draws point numbers and legend lines in magenta, and switches a point to white while its
    reading is over its limit, so both colours are read.
    """
    hints = hints or {}
    mag, white = color_masks(a)
    status = read_status(white)
    H, W = mag.shape
    # white text of interest lives in the left column, above the status line
    wl = white.copy()
    wl[max(0, H - 14):, :] = False
    wl[:, PANEL:] = False
    panels = {}

    def panel(y, x):
        if x >= PANEL:
            return None
        return panels.setdefault(y // PANEL, {'legend': {}, 'markers': {}, 'alarm': set(), 'notes': []})

    lines = {'magenta': group_lines(find_glyphs(mag)), 'white': group_lines(find_glyphs(wl))}
    used = {'magenta': np.zeros_like(mag), 'white': np.zeros_like(mag)}
    legend_boxes = []
    for color, mask in (('magenta', mag), ('white', wl)):
        for y, x0, lab, temp, lim, cells in _legend_lines(mask, lines[color]):
            p = panel(y, x0)
            if p is None:
                continue
            p['legend'][lab] = (temp, lim)
            if color == 'white':
                p['alarm'].add(lab)
            used[color] |= _glyph_mask(mag.shape, cells)
            legend_boxes.append((y - 1, y + 11, x0 - 1, cells[-1][1] + 6))

    def in_legend(y, x):
        return any(y0 <= y + 9 and y <= y1 and x0 <= x + 4 and x <= x1 for y0, y1, x0, x1 in legend_boxes)

    # magenta single-digit point labels
    for y, x0, s, items in lines['magenta']:
        if len(s.replace(' ', '')) >= 3 or in_legend(y, x0):
            used['magenta'] |= _glyph_mask(mag.shape, [(y, xx, ch) for xx, ch in items])   # other text, e.g. study ID
            continue
        for xx, ch in items:
            if ch not in DIGITS or ch == '0':
                continue
            p = panel(y, xx)
            if p is not None:
                p['markers'].setdefault(int(ch), []).append((y, xx, 'exact'))
            used['magenta'] |= _glyph_mask(mag.shape, [(y, xx, ch)])
    # overlapping magenta labels
    rest = mag & ~used['magenta']
    lab_img, _ = ndimage.label(rest, structure=np.ones((3, 3)))
    for i, sl in enumerate(ndimage.find_objects(lab_img), start=1):
        comp = lab_img == i
        if comp.sum() < 6:
            continue
        p = panel(sl[0].start, sl[1].start)
        if p is None:
            continue
        expected = {str(k) for k in p['legend'] if k not in p['markers'] and k not in p['alarm']}
        res = _resolve_cluster(mag, comp, expected)
        if res:
            for y, x, ch in res:
                p['markers'].setdefault(int(ch), []).append((y, x, 'overlap'))
        else:
            p['notes'].append(f'unresolved label pixels near x={sl[1].start}, y={sl[0].start % PANEL}')
    # white (over-limit) point labels: exact shape with at most 1 stray white pixel in the cell
    free = wl & ~used['white']
    for row, p in panels.items():
        for lab in sorted(p['alarm']):
            if lab in p['markers'] or not 1 <= lab <= 9:
                continue
            hint = hints.get(row, {}).get(lab)
            cands = [(y, x) for y, x, extra in find_relaxed(free, str(lab), max_extra=1)
                     if y // PANEL == row and not in_legend(y, x)]
            got = _pick(cands, hint)
            if got is None and hint is not None:
                got = _match_near(free, str(lab), hint, in_legend)
            if got:
                p['markers'][lab] = [(got[0], got[1], 'white')]
    return status, panels


def _match_near(mask, ch, hint, in_legend, radius=20, max_miss=2, max_extra=4):
    """Tolerant search for a label close to where it was last seen: a few glyph pixels may be covered
    by thermal-map colour and a few bright image pixels may touch it. Best (fewest misses, then fewest
    extras, then nearest) placement wins."""
    t = TEMPLATES[ch]
    H, W = mask.shape
    best = None
    for y in range(max(0, hint[0] - radius), min(H - 10, hint[0] + radius) + 1):
        for x in range(max(0, hint[1] - radius), min(W - 5, hint[1] + radius) + 1):
            cell = mask[y:y + 10, x:x + 5]
            miss = int((t & ~cell).sum())
            if miss > max_miss:
                continue
            extra = int((cell & ~t).sum())
            if extra > max_extra or in_legend(y, x):
                continue
            score = (miss * 2 + extra, (y - hint[0]) ** 2 + (x - hint[1]) ** 2)
            if best is None or score < best[0]:
                best = (score, (y, x))
    return best[1] if best else None
