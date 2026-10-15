"""Laser-fiber ("stick") localisation on a clean 256x256 anatomy image.

* snap()    - refine a user/seed line (entry -> tip) onto the actual thin dark line.
* suggest() - automatic first guess: start from where heating concentrates (the fiber
              tip heats the tissue), then find the darkest straight ray towards the skull.
Coordinates are panel-local pixels (x right, y down).
"""
import numpy as np
from scipy import ndimage


def head_mask(img):
    m = ndimage.gaussian_filter(img, 2) > 40
    lab, n = ndimage.label(m)
    if n == 0:
        return m
    sizes = ndimage.sum(m, lab, range(1, n + 1))
    m = lab == (1 + int(np.argmax(sizes)))
    return ndimage.binary_fill_holes(ndimage.binary_closing(m, iterations=6))


def thin_dark_response(img, valid=None):
    """How much darker each pixel is than both neighbours 2 px away (best of 16 directions)."""
    if valid is not None:
        img = np.where(valid, img, ndimage.median_filter(img, 5))
    best = np.zeros_like(img, dtype=float)
    for ang in np.deg2rad(np.arange(0, 180, 11.25)):
        dx, dy = np.cos(ang) * 2, np.sin(ang) * 2
        a = ndimage.shift(img, (dy, dx), order=1, mode='nearest')
        b = ndimage.shift(img, (-dy, -dx), order=1, mode='nearest')
        best = np.maximum(best, np.minimum(a, b) - img)
    return ndimage.gaussian_filter(best, 0.5)


def _sample(resp, p, q, step=0.5):
    L = float(np.hypot(*(q - p)))
    t = np.arange(0, L + 1e-9, step) / max(L, 1e-9)
    pts = p[None] + t[:, None] * (q - p)[None]
    return pts, ndimage.map_coordinates(resp, [pts[:, 1], pts[:, 0]], order=1, mode='nearest')


def snap(resp, entry, tip, trace_tip=True):
    """Fit the dark line near the entry->tip segment; optionally trace the tip to where the line ends."""
    E0, T0 = np.asarray(entry, float), np.asarray(tip, float)
    L0 = float(np.hypot(*(T0 - E0)))
    if L0 < 4:
        return {'entry': E0, 'tip': T0, 'score': 0.0}
    d0 = (T0 - E0) / L0
    n0 = np.array([-d0[1], d0[0]])
    mid, half = (E0 + T0) / 2, L0 / 2
    best = None
    for ang in np.deg2rad(np.arange(-8, 8.01, 0.5)):
        c, s = np.cos(ang), np.sin(ang)
        d = np.array([c * d0[0] - s * d0[1], s * d0[0] + c * d0[1]])
        for off in np.arange(-4, 4.01, 0.25):
            m = mid + off * n0
            _, v = _sample(resp, m - 0.8 * half * d, m + 0.8 * half * d)
            sc = float(v.mean())
            if best is None or sc > best[0]:
                best = (sc, m, d)
    sc, m, d = best
    E = m + np.dot(E0 - m, d) * d
    T = m + np.dot(T0 - m, d) * d
    if trace_tip:
        _, v_core = _sample(resp, m - 0.6 * half * d, m + 0.6 * half * d)
        thr = 0.4 * float(np.median(v_core))
        pts, v = _sample(resp, m, T + 8 * d)
        last, gap = 0, 0
        for i, val in enumerate(v):
            if val > thr:
                last, gap = i, 0
            else:
                gap += 1
                if gap > 6:
                    break
        T = pts[last]
    return {'entry': E, 'tip': T, 'score': sc}


def find_entry(img, tip, d_out):
    """Walk outward from the tip along the fiber, reading tissue just beside it (+/-3 px, so the dark
    fiber itself is ignored), until it leaves brain tissue = approximate inner skull surface.
    Bright rims within ~22 px of the tip are treated as lesion enhancement, not skull."""
    n = np.array([-d_out[1], d_out[0]])
    ts = np.arange(0, 160, 0.5)
    pts = tip[None] + ts[:, None] * d_out[None]
    ok = (pts[:, 0] > 3) & (pts[:, 0] < 252) & (pts[:, 1] > 3) & (pts[:, 1] < 252)
    ts, pts = ts[ok], pts[ok]
    if len(ts) < 30:
        return tip + 25 * d_out
    a = ndimage.map_coordinates(img, [pts[:, 1] + 3 * n[1], pts[:, 0] + 3 * n[0]], order=1)
    b = ndimage.map_coordinates(img, [pts[:, 1] - 3 * n[1], pts[:, 0] - 3 * n[0]], order=1)
    side = ndimage.gaussian_filter1d(np.maximum(a, b), 2)
    ref = float(np.median(side[(ts >= 10) & (ts <= 40)]))
    bad = (side < 0.55 * ref) | ((side > ref + max(40, 0.5 * ref)) & (ts > 22))
    for k in range(len(ts)):
        if ts[k] > 12 and bad[k:k + 4].all():
            return pts[k]
    return pts[-1]


def suggest(img, valid, prior_map=None, point_xy=None):
    """Automatic fiber guess. Returns dict(entry, tip, score) or None.

    prior_map: where the tip probably is (e.g. tissue that warms when the laser is on).
    point_xy:  positions of the high-limit (<90) points, which are placed beside the tip.
    """
    img = np.where(valid, img, ndimage.median_filter(img, 5)) if valid is not None else img
    resp = thin_dark_response(img)
    # 1) tip prior
    prior = None
    near = None
    if point_xy:
        c = np.mean(np.asarray(point_xy, float), axis=0)
        yy, xx = np.mgrid[0:256, 0:256]
        near = np.hypot(xx - c[0], yy - c[1]) < 30
        prior = c
    if prior_map is not None:
        head = ndimage.binary_erosion(head_mask(img), iterations=4)
        h = ndimage.gaussian_filter(np.where(head, np.clip(prior_map, 0, None), 0), 3)
        if near is not None:
            h = np.where(near, h, 0)
        if h.max() > 0:
            prior = np.array(np.unravel_index(np.argmax(h), h.shape)[::-1], float)
    if prior is None:
        ys, xs = np.nonzero(img > np.percentile(img, 60))
        prior = np.array([xs.mean(), ys.mean()])
    # 2) direction: the darkest straight ray leaving the prior region (fiber is dark along its length)
    best = None
    angs = np.deg2rad(np.arange(0, 360, 2.0))
    D = np.stack([np.cos(angs), np.sin(angs)], axis=1)            # (A, 2)
    ts = np.arange(4, 41, 1.0)
    for oy in range(-10, 11, 2):
        for ox in range(-10, 11, 2):
            T0 = prior + (ox, oy)
            if not (8 <= T0[0] < 248 and 8 <= T0[1] < 248):
                continue
            pts = T0[None, None, :] + ts[None, :, None] * D[:, None, :]   # (A, N, 2)
            px, py = np.clip(pts[..., 0], 0, 255), np.clip(pts[..., 1], 0, 255)
            v = ndimage.map_coordinates(resp, [py.ravel(), px.ravel()], order=1).reshape(px.shape)
            sc = np.quantile(v, 0.3, axis=1)
            ai = int(np.argmax(sc))
            if best is None or sc[ai] > best[0]:
                best = (float(sc[ai]), T0.copy(), D[ai])
    if best is None:
        return None
    sc, T0, d = best
    # 3) fit the line and trace the tip, then find the skull entry from the tissue profile
    first = snap(resp, T0 + 40 * d, T0, trace_tip=True)
    T = first['tip']
    d_out = first['entry'] - T
    d_out /= max(np.hypot(*d_out), 1e-6)
    E0 = find_entry(img, T, d_out)
    res = snap(resp, E0, T, trace_tip=False)
    res['confidence'] = sc
    return res
