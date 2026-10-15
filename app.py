"""Visualase GIF Extractor - local web app.

Run:  python app.py            (then open http://127.0.0.1:8790)
Everything stays on this computer: files are processed locally and the server only
listens on 127.0.0.1.
"""
import argparse
import io
import os
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

from core.analysis import DEFAULT_PIXEL_MM, FiberStore, Job
from core import export as exporter
from core.recalc import recalc, soffice_path
from core.render import annotated_gif

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
UPLOADS = os.path.join(DATA, 'uploads')
OUTPUTS = os.path.join(DATA, 'outputs')
for d in (UPLOADS, OUTPUTS):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__, static_folder=os.path.join(HERE, 'static'), static_url_path='/static')
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024   # 1 GB per upload request

store = FiberStore(os.path.join(DATA, 'saved_fibers.json'))
pool = ThreadPoolExecutor(max_workers=2)
jobs = {}
jobs_lock = threading.Lock()
tasks = {}     # long-running render tasks: id -> dict(status, progress, url, error)


def get_job(job_id, ready=True):
    job = jobs.get(job_id)
    if job is None:
        abort(404, 'Unknown file - it may have been removed or the server restarted')
    if ready and job.status != 'ready':
        abort(409, 'This file is still being analysed')
    return job


def slice_or_404(job, idx):
    if not 0 <= idx < len(job.slices):
        abort(404, 'Unknown slice')
    return idx


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(413)
def json_error(e):
    return jsonify(error=getattr(e, 'description', str(e))), e.code


@app.get('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


@app.get('/api/info')
def info():
    return jsonify(default_pixel_mm=DEFAULT_PIXEL_MM, libreoffice=bool(soffice_path()))


# ---------------------------------------------------------------- upload / jobs
@app.post('/api/upload')
def upload():
    files = request.files.getlist('files')
    if not files:
        abort(400, 'No files received')
    created = []
    for fs in files:
        name = secure_filename(fs.filename or '') or 'upload.gif'
        if not name.lower().endswith('.gif'):
            created.append({'filename': fs.filename, 'status': 'error', 'error': 'Only .gif files are supported'})
            continue
        job = Job(None, fs.filename)
        folder = os.path.join(UPLOADS, job.id)
        os.makedirs(folder, exist_ok=True)
        job.path = os.path.join(folder, name)
        fs.save(job.path)
        with jobs_lock:
            jobs[job.id] = job
        pool.submit(job.run, store)
        created.append(job.summary())
    return jsonify(jobs=created)


@app.get('/api/jobs')
def list_jobs():
    return jsonify(jobs=[j.summary() for j in sorted(jobs.values(), key=lambda j: j.created)])


@app.get('/api/jobs/<job_id>')
def job_info(job_id):
    return jsonify(get_job(job_id, ready=False).summary())


@app.delete('/api/jobs/<job_id>')
def delete_job(job_id):
    with jobs_lock:
        jobs.pop(job_id, None)
    return jsonify(ok=True)


@app.get('/api/jobs/<job_id>/frames')
def job_frames(job_id):
    job = get_job(job_id)
    return jsonify(frames=job.frames, durations=job.durations, width=job.width, height=job.height,
                   rows=job.rows, anat_col=job.anat_col, point_keys=job.point_keys)


@app.get('/api/jobs/<job_id>/frame/<int:i>.png')
def frame_png(job_id, i):
    job = get_job(job_id)
    if not 0 <= i < len(job.frame_png):
        abort(404, 'Unknown frame')
    resp = send_file(io.BytesIO(job.frame_png[i]), mimetype='image/png')
    resp.headers['Cache-Control'] = 'private, max-age=3600'
    return resp


@app.get('/api/jobs/<job_id>/slice/<int:idx>/anatomy.png')
def anatomy(job_id, idx):
    job = get_job(job_id)
    slice_or_404(job, idx)
    return send_file(io.BytesIO(job.anatomy_png(idx)), mimetype='image/png')


# ---------------------------------------------------------------- fiber review
def _xy(v):
    try:
        x, y = float(v[0]), float(v[1])
    except (TypeError, ValueError, IndexError):
        abort(400, 'Coordinates must be [x, y]')
    return [min(max(x, 0), 255.99), min(max(y, 0), 255.99)]


@app.post('/api/jobs/<job_id>/slice/<int:idx>/snap')
def snap(job_id, idx):
    job = get_job(job_id)
    slice_or_404(job, idx)
    body = request.get_json(force=True)
    return jsonify(job.snap_fiber(idx, _xy(body.get('entry')), _xy(body.get('tip')), bool(body.get('trace_tip'))))


@app.post('/api/jobs/<job_id>/slice/<int:idx>/auto')
def auto(job_id, idx):
    job = get_job(job_id)
    slice_or_404(job, idx)
    res = job.auto_fiber(idx)
    if not res:
        abort(409, 'Could not find a fiber automatically - please place it by clicking')
    return jsonify(res)


@app.put('/api/jobs/<job_id>/slice/<int:idx>/fiber')
def set_fiber(job_id, idx):
    job = get_job(job_id)
    slice_or_404(job, idx)
    body = request.get_json(force=True)
    label = (body.get('label') or '').strip()[:60] or None
    job.set_fiber(idx, _xy(body.get('entry')), _xy(body.get('tip')), 'manual',
                  store if body.get('remember', True) else None, label)
    return jsonify(job.slice_view(job.slices[idx]))


# ---------------------------------------------------------------- outputs
@app.post('/api/jobs/<job_id>/export')
def do_export(job_id):
    job = get_job(job_id)
    o = request.get_json(force=True)
    try:
        pixel_mm = float(o.get('pixel_mm') or DEFAULT_PIXEL_MM)
        if not 0.01 < pixel_mm < 20:
            raise ValueError
    except ValueError:
        abort(400, 'Pixel size must be a number of millimetres, e.g. 1.015625')
    fmt = 'csv' if o.get('format') == 'csv' else 'xlsx'
    out_dir = os.path.join(OUTPUTS, job.id)
    path, rows = exporter.export(job, out_dir, fields=o.get('fields'), points=o.get('points'),
                                 frame_mode=o.get('frames', 'all'), start=o.get('start'), end=o.get('end'),
                                 fmt=fmt, pixel_mm=pixel_mm)
    recalculated = False
    if fmt == 'xlsx' and o.get('recalc', True):
        recalculated = recalc(path)
    name = os.path.basename(path)
    job.outputs[fmt] = {'name': name, 'url': f'/api/download/{job.id}/{name}', 'time': time.time()}
    return jsonify(file=name, url=job.outputs[fmt]['url'], rows=rows, recalculated=recalculated,
                   n_frames=len(exporter.select_frames(job, o.get('frames', 'all'), o.get('start'), o.get('end'))))


@app.post('/api/jobs/<job_id>/annotated')
def do_annotated(job_id):
    job = get_job(job_id)
    o = request.get_json(force=True)
    scale = 2 if int(o.get('scale', 2)) >= 2 else 1
    step = max(1, min(int(o.get('step', 1)), 50))
    show = o.get('show') or None
    pixel_mm = float(o.get('pixel_mm') or DEFAULT_PIXEL_MM)
    task_id = f'{job.id}-gif-{int(time.time() * 1000)}'
    tasks[task_id] = {'status': 'running', 'progress': 0.0}
    out_dir = os.path.join(OUTPUTS, job.id)
    os.makedirs(out_dir, exist_ok=True)
    name = os.path.splitext(job.filename)[0] + '_annotated.gif'

    def work():
        try:
            annotated_gif(job, os.path.join(out_dir, name), scale, step, show, pixel_mm,
                          progress=lambda p: tasks[task_id].update(progress=p))
            url = f'/api/download/{job.id}/{name}'
            job.outputs['annotated'] = {'name': name, 'url': url, 'time': time.time()}
            tasks[task_id].update(status='done', progress=1.0, url=url, name=name)
        except Exception as e:  # reported to the UI
            tasks[task_id].update(status='error', error=f'{type(e).__name__}: {e}')
    pool.submit(work)
    return jsonify(task=task_id)


@app.get('/api/tasks/<task_id>')
def task_status(task_id):
    t = tasks.get(task_id)
    if t is None:
        abort(404, 'Unknown task')
    return jsonify(t)


@app.get('/api/download/<job_id>/<path:name>')
def download(job_id, name):
    folder = os.path.join(OUTPUTS, secure_filename(job_id))
    return send_from_directory(folder, name, as_attachment=True)


@app.post('/api/download_all')
def download_all():
    ids = (request.get_json(force=True) or {}).get('jobs') or list(jobs)
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for jid in ids:
            job = jobs.get(jid)
            if not job:
                continue
            for kind in ('xlsx', 'csv'):
                o = job.outputs.get(kind)
                if o:
                    z.write(os.path.join(OUTPUTS, job.id, o['name']), o['name'])
                    n += 1
    if not n:
        abort(409, 'Nothing to download yet - run the export first')
    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True, download_name='visualase_results.zip')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Visualase GIF Extractor (local web app)')
    ap.add_argument('--port', type=int, default=int(os.environ.get('PORT', 8790)))
    ap.add_argument('--no-browser', action='store_true', help='do not open a browser tab')
    args = ap.parse_args()
    url = f'http://127.0.0.1:{args.port}'
    print(f'\n  Visualase GIF Extractor running at {url}\n  (press Ctrl+C to stop)\n')
    if not args.no_browser:
        threading.Timer(1.0, lambda: __import__('webbrowser').open(url)).start()
    app.run(host='127.0.0.1', port=args.port, debug=False, threaded=True)
