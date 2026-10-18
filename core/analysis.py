"""One pass over a Visualase GIF: OCR every frame, rebuild the clean anatomy per slice,
map persistent heating, and propose the laser-fiber line for each slice."""
import hashlib
import io
import json
import os
import threading
import time
import uuid

import numpy as np
from PIL import Image

from .ocr import analyze_frame, PANEL
from . import fiber as fib

DEFAULT_PIXEL_MM = 1.015625   # Visualase 256x256 panels, 260 mm FOV (DICOM PixelSpacing of the planning T1 SE)


def gray_mask(p):
    r, g, b = p[..., 0], p[..., 1], p[..., 2]
    return (np.abs(r - g) <= 6) & (np.abs(g - b) <= 6)


def warmth(p):
    """Approximate temperature (degC) from the thermal colour map (blue 37 ... red 91); 0 where no colour."""
    p = p.astype(np.float32)
    r, g, b = p[..., 0], p[..., 1], p[..., 2]
    mx, mn = p.max(-1), p.min(-1)
    c = mx - mn
    sat = c / np.maximum(mx, 1)
    cz = np.maximum(c, 1e-6)
    hue = np.where(mx == r, (60 * ((g - b) / cz)) % 360,
                   np.where(mx == g, 60 * ((b - r) / cz + 2), 60 * ((r - g) / cz + 4)))
    coloured = (sat > 0.5) & (mx > 100) & (hue <= 250)
    return np.where(coloured, 91 - (np.clip(hue, 0, 240) / 240) * 54, 0).astype(np.uint8)


def heating_prior(warm_frames, laser_on):
    """Map of tissue that warms when the laser is on (artifacts and noise do not follow the laser).

    Frames where most of the image is coloured (phase-wrap artifacts) are ignored."""
    if not warm_frames:
        return None
    frac = np.array([(w > 0).mean() for w in warm_frames])
    good = frac < max(3 * np.median(frac), 0.03)
    on = [w for w, o, g in zip(warm_frames, laser_on, good) if g and o]
    off = [w for w, o, g in zip(warm_frames, laser_on, good) if g and not o]
    if on and off:
        return np.mean(on, axis=0) - np.mean(off, axis=0)
    keep = [w for w, g in zip(warm_frames, good) if g] or warm_frames
    return np.mean(keep, axis=0)


def _ncc(a, b, m):
    if m.sum() < 500:
        return -1.0
    a = a[m] - a[m].mean()
    b = b[m] - b[m].mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / d) if d else -1.0


def file_sha1(path):
    h = hashlib.sha1()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


class FiberStore:
    """Fibers confirmed by a user, remembered per GIF content (sha1) so re-uploads start from them."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def get(self, key):
        with self.lock:
            return self._load().get(key)

    def put(self, key, value):
        with self.lock:
            d = self._load()
            d[key] = value
            tmp = self.path + '.tmp'
            with open(tmp, 'w') as fh:
                json.dump(d, fh, indent=1)
            os.replace(tmp, self.path)


class Job:
    def __init__(self, path, filename):
        self.id = uuid.uuid4().hex[:10]
        self.path = path
        self.filename = filename
        self.created = time.time()
        self.status = 'queued'
        self.progress = 0.0
        self.message = 'Waiting to start'
        self.error = None
        self.sha1 = None
        self.width = self.height = self.n_frames = 0
        self.durations = []
        self.rows = 0
        self.anat_col = 0
        self.slices = []
        self.frames = []
        self.point_keys = []
        self.frame_png = []
        self.outputs = {}

    # ---------- public views ----------
    def summary(self):
        return {
            'id': self.id, 'filename': self.filename, 'status': self.status,
            'progress': round(self.progress, 3), 'message': self.message, 'error': self.error,
            'width': self.width, 'height': self.height, 'n_frames': self.n_frames,
            'rows': self.rows, 'has_anatomy_half': self.anat_col == 1,
            'runs': max((f['run'] for f in self.frames), default=0),
            'laser_on_frames': sum(1 for f in self.frames if f['laser'] == 'On'),
            'point_keys': self.point_keys,
            'slices': [self.slice_view(s) for s in self.slices],
            'outputs': self.outputs,
        }

    def slice_view(self, s):
        fr = s['frames']
        out = {'idx': s['idx'], 'row': s['row'], 'label': s['label'],
               'first_frame': fr[0], 'last_frame': fr[-1], 'n_frames': len(fr),
               'fiber': None, 'fiber_source': s.get('fiber_source')}
        if s.get('fiber'):
            e, t = np.asarray(s['fiber']['entry']), np.asarray(s['fiber']['tip'])
            out['fiber'] = {'entry': [round(float(v), 2) for v in e], 'tip': [round(float(v), 2) for v in t],
                            'center': [round(float(v), 2) for v in (e + t) / 2],
                            'length_px': round(float(np.hypot(*(t - e))), 2)}
        return out

    def store_key(self, s):
        k = [x for x in self.slices if x['row'] == s['row']].index(s)
        return f'{self.sha1}:{s["row"]}:{k}'

    # ---------- analysis ----------
    def run(self, store):
        try:
            self.status = 'analyzing'
            self._analyze(store)
            self.status = 'ready'
            self.progress = 1.0
            self.message = 'Ready'
        except Exception as e:  # surfaced to the UI
            import traceback
            traceback.print_exc()
            self.status = 'error'
            self.error = f'{type(e).__name__}: {e}'
            self.message = 'Analysis failed'

    def _analyze(self, store):
        self.message = 'Reading file'
        self.sha1 = file_sha1(self.path)
        im = Image.open(self.path)
        if im.format != 'GIF':
            raise ValueError('Not a GIF file')
        self.width, self.height = im.size
        if self.width % PANEL or self.height % PANEL:
            raise ValueError(f'Unexpected image size {self.width}x{self.height}: Visualase panels are 256x256')
        self.n_frames = getattr(im, 'n_frames', 1)
        self.rows = self.height // PANEL
        self.anat_col = 1 if self.width >= 2 * PANEL else 0
        groups = {r: [] for r in range(self.rows)}   # per row: list of dicts(sum, cnt, hot, n, frames)
        hints = {}                                    # per row: where each point label was last seen
        frame_group = []
        for i in range(self.n_frames):
            im.seek(i)
            self.durations.append(int(im.info.get('duration', 600) or 600))
            rgb = im.convert('RGB')
            buf = io.BytesIO()
            rgb.save(buf, 'PNG', compress_level=1)
            self.frame_png.append(buf.getvalue())
            a = np.asarray(rgb).astype(np.int32)
            status, panels = analyze_frame(a, hints)
            for r, p in panels.items():
                for lab, pos in p['markers'].items():
                    if len(pos) == 1:
                        hints.setdefault(r, {})[lab] = pos[0][:2]
            gsel = {}
            for r in range(self.rows):
                an = a[r * PANEL:(r + 1) * PANEL, self.anat_col * PANEL:(self.anat_col + 1) * PANEL].astype(np.float32)
                g, m = an.mean(-1), gray_mask(an)
                best = None
                for gi, G in enumerate(groups[r]):
                    c = _ncc(g, G['sum'] / np.maximum(G['cnt'], 1), m & (G['cnt'] > 0))
                    if c > 0.9 and (best is None or c > best[0]):
                        best = (c, gi)
                if best is None:
                    groups[r].append({'sum': np.zeros((PANEL, PANEL)), 'cnt': np.zeros((PANEL, PANEL)),
                                      'warm': [], 'n': 0, 'frames': []})
                    gi = len(groups[r]) - 1
                else:
                    gi = best[1]
                G = groups[r][gi]
                G['sum'] += np.where(m, g, 0)
                G['cnt'] += m
                G['warm'].append(warmth(a[r * PANEL:(r + 1) * PANEL, 0:PANEL]))
                G['n'] += 1
                G['frames'].append(i)
                gsel[r] = gi
            frame_group.append(gsel)
            self.frames.append(self._frame_record(i, status, panels))
            self.progress = 0.9 * (i + 1) / self.n_frames
            self.message = f'Reading frame {i + 1} of {self.n_frames}'

        # merge tiny groups (e.g. half-drawn frames) into the most similar large group
        remap = {}
        for r, gl in groups.items():
            big = [gi for gi, G in enumerate(gl) if G['n'] >= 3] or [max(range(len(gl)), key=lambda k: gl[k]['n'])]
            for gi, G in enumerate(gl):
                if gi in big:
                    remap[(r, gi)] = gi
                    continue
                mean = G['sum'] / np.maximum(G['cnt'], 1)
                tgt = max(big, key=lambda k: _ncc(mean, gl[k]['sum'] / np.maximum(gl[k]['cnt'], 1),
                                                  (G['cnt'] > 0) & (gl[k]['cnt'] > 0)))
                T = gl[tgt]
                for key in ('sum', 'cnt', 'warm'):
                    T[key] += G[key]
                T['n'] += G['n']
                T['frames'] += G['frames']
                remap[(r, gi)] = tgt
        # build slices
        sid = {}
        multi = self.rows > 1
        for r, gl in groups.items():
            kept = sorted({remap[(r, gi)] for gi in range(len(gl))}, key=lambda k: min(gl[k]['frames']))
            for k_in_row, gi in enumerate(kept):
                G = gl[gi]
                clean = (G['sum'] / np.maximum(G['cnt'], 1)).astype(np.float32)
                if multi:
                    base = 'Top panel' if r == 0 else 'Bottom panel' if r == self.rows - 1 else f'Row {r + 1} panel'
                    label = f'{base} {k_in_row + 1}' if len(kept) > 1 else base
                else:
                    label = f'Slice {k_in_row + 1}'
                order = np.argsort(G['frames'])
                s = {'idx': len(self.slices), 'row': r, 'label': label, 'frames': sorted(G['frames']),
                     'clean': clean, 'valid': G['cnt'] > 0, 'warm': [G['warm'][k] for k in order],
                     'fiber': None, 'fiber_source': None}
                self.slices.append(s)
                sid[(r, gi)] = s['idx']
        for f, gsel in zip(self.frames, frame_group):
            f['slice'] = {str(r): sid[(r, remap[(r, gi)])] for r, gi in gsel.items()}
        self._assign_runs()
        self._fill_hidden_limits()
        self.point_keys = sorted({k for f in self.frames for k in f['points']},
                                 key=lambda k: tuple(int(v) for v in k.split(':')))

        # fibers: remembered from an earlier review, else automatic suggestion
        for n, s in enumerate(self.slices):
            self.message = f'Locating the laser fiber ({n + 1}/{len(self.slices)})'
            self.progress = 0.9 + 0.1 * n / max(len(self.slices), 1)
            # cues kept for later re-detection: where tissue heats with the laser, and the <90 points
            s['tip_points'] = [(v[2], v[3]) for f in self.frames if f['slice'].get(str(s['row'])) == s['idx']
                               for k, v in f['points'].items()
                               if int(k.split(':')[0]) == s['row'] and v[2] is not None and v[1] == 90]
            on = [self.frames[i]['laser'] == 'On' for i in s['frames']]
            prior = heating_prior(s.pop('warm'), on)
            if prior is not None and s['row'] == self.rows - 1:
                prior[-14:, :] = 0          # the status bar turns red when the laser is on
            s['prior'] = None if prior is None else prior.astype(np.float32)
            saved = store.get(self.store_key(s))
            if saved:
                s['fiber'] = {'entry': saved['entry'], 'tip': saved['tip']}
                s['fiber_source'] = 'saved'
                if saved.get('label'):
                    s['label'] = saved['label']
                continue
            res = self.auto_fiber(s['idx'])
            if res:
                s['fiber'] = {'entry': res['entry'], 'tip': res['tip']}
                s['fiber_source'] = 'auto'

    def auto_fiber(self, idx):
        s = self.slices[idx]
        res = fib.suggest(s['clean'].astype(float), s['valid'], s.get('prior'), s.get('tip_points') or None)
        if not res:
            return None
        return {'entry': [float(v) for v in res['entry']], 'tip': [float(v) for v in res['tip']]}

    def snap_fiber(self, idx, entry, tip, trace_tip=False):
        s = self.slices[idx]
        resp = s.get('_resp')
        if resp is None:
            resp = s['_resp'] = fib.thin_dark_response(s['clean'].astype(float), s['valid'])
        res = fib.snap(resp, entry, tip, trace_tip=trace_tip)
        return {'entry': [float(v) for v in res['entry']], 'tip': [float(v) for v in res['tip']]}

    def anatomy_png(self, idx):
        """Clean anatomy of a slice, contrast-stretched for display."""
        s = self.slices[idx]
        img = s['clean']
        v = img[s['valid']]
        lo, hi = (np.percentile(v, 1), np.percentile(v, 99.7)) if v.size else (0, 255)
        out = np.clip((img - lo) / max(hi - lo, 1) * 255, 0, 255).astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(out).save(buf, 'PNG')
        return buf.getvalue()

    def _frame_record(self, i, status, panels):
        rec = {'frame': i, 'run': 0, 'time': status['time'], 'secs': status['secs'], 'laser': status['laser'],
               'power': status['power'], 'watts': status['watts'], 'slice': {}, 'points': {}, 'notes': []}
        if status['time'] is None:
            rec['notes'].append('Status line not readable (frame only partially drawn in the GIF)')
        elif rec['laser'] == 'On' and rec['watts'] is None:
            rec['notes'].append('Watts not shown on this frame')
        for r in range(self.rows):
            p = panels.get(r)
            legend = p['legend'] if p else {}
            markers = p['markers'] if p else {}
            if not legend:
                if markers:
                    rec['notes'].append(f'Row {r + 1}: point legend not readable (frame only partially drawn)')
                continue
            for lab, (temp, lim) in sorted(legend.items()):
                pos = markers.get(lab)
                x = y = None
                if pos and len(pos) == 1:
                    yy, xx, _ = pos[0]
                    x, y = xx + 2, yy + 4 - r * PANEL      # centre of the 5x9 digit glyph, panel-local
                elif pos:
                    rec['notes'].append(f'Point {lab}: label found at {len(pos)} places; position left blank')
                else:
                    rec['notes'].append(f'Point {lab}: label hidden/overlapped on the image; position not readable')
                if temp is None:
                    rec['notes'].append(f'Point {lab}: temperature shown as "--.-" (no reading)')
                if lab in p.get('alarm', ()):
                    rec['notes'].append(f'Point {lab}: over its limit (drawn in white)')
                rec['points'][f'{r}:{lab}'] = [temp, lim, x, y]
        return rec

    def _fill_hidden_limits(self):
        """A point's limit can be hidden when its (white) "<90" is drawn over bright white anatomy.
        Limits do not change for a point, so take it from the nearest frame where it was readable."""
        known = {}
        for f in self.frames:
            for k, v in f['points'].items():
                if v[1] is not None:
                    known.setdefault(k, []).append(f['frame'])
        for f in self.frames:
            for k, v in f['points'].items():
                if v[1] is not None:
                    continue
                lab = k.split(':')[1]
                src = known.get(k)
                if not src:
                    f['notes'].append(f'Point {lab}: limit hidden behind the image and not readable in any frame')
                    continue
                n = min(src, key=lambda i: (abs(i - f['frame']), i))
                v[1] = self.frames[n]['points'][k][1]
                f['notes'].append(f'Point {lab}: limit hidden behind the image - taken from frame {n}')

    def _assign_runs(self):
        run, prev = 1, None
        for f in self.frames:
            if f['secs'] is not None:
                if prev is not None and f['secs'] < prev:
                    run += 1
                prev = f['secs']
            f['run'] = run
        if any(f['points'] for f in self.frames):
            for f in self.frames:
                if not f['points'] and not any('not readable' in n for n in f['notes']):
                    f['notes'].append('No temperature points displayed on this frame')

    def set_fiber(self, idx, entry, tip, source, store=None, label=None):
        s = self.slices[idx]
        s['fiber'] = {'entry': [float(v) for v in entry], 'tip': [float(v) for v in tip]}
        s['fiber_source'] = source
        if label is not None:
            s['label'] = label
        if store is not None:
            store.put(self.store_key(s), {'entry': s['fiber']['entry'], 'tip': s['fiber']['tip'],
                                          'label': s['label'], 'file': self.filename})
