#!/usr/bin/env python3
"""Web front end for the exporter, for the Proxmox LXC (see install/).

Reads the M3U playlists from the NAS (mounted read-only), compares them with
the USB drive plugged into the server, and exports the selected ones. Python
standard library only. Configured by environment variables
(/etc/usb-exporter.env in the container):

  MUSIC_ROOT    NAS music share inside the container       (/mnt/music)
  SOURCE_ROOT   folder whose Artist/Album layout is kept   (/mnt/music/data)
  PLAYLIST_DIR  folder with the .m3u/.m3u8 playlists       (/mnt/music/playlists)
  PATH_MAP      how Windows paths in the playlists map     (Z:\\=/mnt/music)
  USB_ROOT      where the host mounts the USB drive        (/mnt/usb)
  CTL_DIR       status/eject files shared with the host    (/mnt/usb-ctl)
  PORT          HTTP port                                  (8080)
"""
import json
import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import export_playlists as ex  # noqa: E402

CFG = {
    'MUSIC_ROOT': '/mnt/music',
    'SOURCE_ROOT': '/mnt/music/data',
    'PLAYLIST_DIR': '/mnt/music/playlists',
    'PATH_MAP': 'Z:\\=/mnt/music',
    'USB_ROOT': '/mnt/usb',
    'CTL_DIR': '/mnt/usb-ctl',
    'PORT': '8080',
}
CFG.update({k: os.environ[k] for k in CFG if os.environ.get(k)})
PATH_MAP = ex.parse_path_map(CFG['PATH_MAP'])
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

job_lock = threading.Lock()
job = {'running': False, 'done': 0, 'total': 0, 'log': [], 'summary': '', 'error': ''}


def drive_info():
    """State written by the host's usb-exporter-mount/eject scripts."""
    info = {'state': 'none', 'label': '', 'fstype': ''}
    try:
        for line in ex.read_text(os.path.join(CFG['CTL_DIR'], 'status')).splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                info[k.strip()] = v.strip()
    except OSError:
        pass
    if info['state'] == 'mounted' and os.path.isdir(CFG['USB_ROOT']):
        try:
            du = shutil.disk_usage(CFG['USB_ROOT'])
            info.update(free=du.free, total=du.total)
        except OSError:
            info['state'] = 'error'
    elif info['state'] == 'mounted':
        info['state'] = 'error'
    return info


def load():
    return ex.load_m3u_playlists(CFG['PLAYLIST_DIR'], CFG['SOURCE_ROOT'], PATH_MAP)


def state():
    drive = drive_info()
    mounted = drive['state'] == 'mounted'
    usb = CFG['USB_ROOT']
    on_drive = ex.drive_playlists(usb) if mounted else {}
    rows, names = [], set()
    for p in load():
        names.add(p.name)
        st, detail = ex.compare(p, on_drive.get(p.name), usb) if mounted else ('', '')
        rows.append({'name': p.name, 'songs': len(p.songs), 'missing': p.missing[:20],
                     'missing_count': len(p.missing), 'state': st, 'detail': detail})
    extra = [{'name': n, 'songs': len(lines)} for n, lines in on_drive.items() if n not in names]
    with job_lock:
        j = dict(job, log=job['log'][-200:])
    return {'drive': drive, 'playlists': rows, 'only_on_drive': extra, 'job': j,
            'source': CFG['PLAYLIST_DIR']}


def run_export(names, remove_others, prune):
    def log(msg):
        with job_lock:
            job['log'].append(msg)

    def progress(done, total):
        with job_lock:
            job['done'], job['total'] = done, total

    try:
        chosen = [p for p in load() if p.name in names]
        summary = ex.export(CFG['USB_ROOT'], chosen, remove_others=remove_others, prune=prune,
                            log=log, progress=progress)
        getattr(os, "sync", lambda: None)()
        with job_lock:
            job['summary'] = summary
        log(summary)
    except Exception as e:  # report anything to the page instead of dying silently
        with job_lock:
            job['error'] = str(e)
        log(f'FAILED: {e}')
    finally:
        with job_lock:
            job['running'] = False


def eject():
    with job_lock:
        if job['running']:
            return 409, {'error': 'an export is running'}
    if drive_info()['state'] != 'mounted':
        return 200, {'state': drive_info()['state']}
    getattr(os, "sync", lambda: None)()
    with open(os.path.join(CFG['CTL_DIR'], 'eject'), 'w') as f:
        f.write(str(time.time()))
    for _ in range(60):  # the host's usb-exporter-eject.path reacts within a second or two
        time.sleep(0.5)
        if drive_info()['state'] != 'mounted':
            return 200, {'state': drive_info()['state']}
    return 504, {'error': 'the host did not unmount the drive - is usb-exporter-eject.path running on Proxmox?'}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            with open(os.path.join(STATIC, 'index.html'), 'rb') as f:
                return self._send(200, f.read(), 'text/html; charset=utf-8')
        if self.path == '/api/state':
            return self._send(200, state())
        if self.path == '/api/job':
            with job_lock:
                return self._send(200, dict(job, log=job['log'][-200:]))
        self._send(404, {'error': 'not found'})

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            return self._send(400, {'error': 'bad JSON'})
        if self.path == '/api/export':
            if drive_info()['state'] != 'mounted':
                return self._send(409, {'error': 'no USB drive mounted'})
            with job_lock:
                if job['running']:
                    return self._send(409, {'error': 'an export is already running'})
                job.update(running=True, done=0, total=0, log=[], summary='', error='')
            threading.Thread(target=run_export, daemon=True,
                             args=(set(body.get('names', [])), bool(body.get('remove_others')),
                                   bool(body.get('prune')))).start()
            return self._send(202, {'started': True})
        if self.path == '/api/eject':
            code, res = eject()
            return self._send(code, res)
        self._send(404, {'error': 'not found'})


def main():
    port = int(CFG['PORT'])
    print(f'usb-exporter on :{port}  playlists={CFG["PLAYLIST_DIR"]}  usb={CFG["USB_ROOT"]}', flush=True)
    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()


if __name__ == '__main__':
    main()
