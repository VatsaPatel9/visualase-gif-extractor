"""Process Visualase GIFs without the web UI.

Examples
  .venv/bin/python batch.py path/to/case_folder/*.gif
  .venv/bin/python batch.py ..            # every GIF under the parent folder
  .venv/bin/python batch.py a.gif --format csv --frames laser_on --out results

Fibers come from earlier reviews in the app (data/saved_fibers.json) when available,
otherwise from automatic detection - those are flagged so you can review them in the app.
"""
import argparse
import glob
import os
import sys

from core.analysis import DEFAULT_PIXEL_MM, FiberStore, Job
from core import export
from core.recalc import recalc

HERE = os.path.dirname(os.path.abspath(__file__))


def expand(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            out += sorted(glob.glob(os.path.join(p, '**', '*.gif'), recursive=True))
        elif p.lower().endswith('.gif') and os.path.exists(p):
            out.append(p)
        else:
            print(f'skipping {p}: not a .gif file or folder', file=sys.stderr)
    return [p for p in out if os.sep + 'data' + os.sep + 'uploads' not in os.path.abspath(p)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paths', nargs='+', help='GIF files and/or folders (searched recursively)')
    ap.add_argument('--out', default='visualase_results', help='output folder (default: ./visualase_results)')
    ap.add_argument('--format', choices=['xlsx', 'csv'], default='xlsx')
    ap.add_argument('--frames', choices=['all', 'laser_on', 'last_per_run'], default='all')
    ap.add_argument('--fields', default=','.join(export.ALL_FIELDS),
                    help=f'comma-separated subset of: {",".join(export.ALL_FIELDS)}')
    ap.add_argument('--pixel-mm', type=float, default=DEFAULT_PIXEL_MM, help='mm per pixel (default 1.015625)')
    ap.add_argument('--no-recalc', action='store_true', help='skip LibreOffice formula pre-calculation')
    args = ap.parse_args()

    files = expand(args.paths)
    if not files:
        sys.exit('No GIF files found.')
    store = FiberStore(os.path.join(HERE, 'data', 'saved_fibers.json'))
    fields = [f.strip() for f in args.fields.split(',') if f.strip()]
    for f in files:
        job = Job(f, os.path.basename(f))
        job.run(store)
        if job.status != 'ready':
            print(f'{f}: FAILED - {job.error}')
            continue
        path, rows = export.export(job, args.out, fields=fields, frame_mode=args.frames,
                                   fmt=args.format, pixel_mm=args.pixel_mm)
        if args.format == 'xlsx' and not args.no_recalc:
            recalc(path)
        print(f'{f}\n  -> {path}')
        for s in job.slices:
            if s.get('fiber_source') != 'saved':
                print(f'  ! {s["label"]}: fiber {"found automatically" if s.get("fiber") else "NOT found"} - review it in the app')
        for r in rows:
            print(f'    {r["slice"]:<22} {r["result"]}')


if __name__ == '__main__':
    main()
