"""Exports iTunes playlists to a USB music drive for the Pi.

Copies the songs of your iTunes playlists to <drive>/Music/Artist/Album/file
(the same layout as on the NAS; songs outside --source-root are placed by
their iTunes artist/album) and writes one <drive>/Playlists/<name>.m3u8 per
playlist, with paths relative to the playlist file. On the Pi,
push-mixxx/usb/import_playlists.py mirrors those files into Mixxx playlists.

Re-running is incremental: songs already on the drive with the same size are
skipped. A full export (no --playlist) also deletes playlist files for
playlists no longer in iTunes; --prune deletes songs under Music/ that no
playlist on the drive uses any more. gui.py is the windowed front end.

    python export_playlists.py E:\\
    python export_playlists.py E:\\ --playlist "Friday set" --playlist Warmup
    python export_playlists.py E:\\ --list
"""
import argparse
import os
import plistlib
import re
import shutil
import sys
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

DEFAULT_LIBRARY = os.path.join(os.path.expanduser('~'), 'Music', 'iTunes', 'iTunes Music Library.xml')
DEFAULT_SOURCE_ROOT = r'\\truenas\media\music\data'
MUSIC_DIR = 'Music'
PLAYLIST_DIR = 'Playlists'
PLAYLIST_EXT = ('.m3u8', '.m3u')
# FAT32/exFAT reject these; iTunes names can contain them.
BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class Song:
    src: str      # file on the PC / NAS
    rel: str      # path under <drive>/Music
    extinf: str   # '#EXTINF:secs,Artist - Title'

    @property
    def line(self):
        """The playlist line: relative to <drive>/Playlists."""
        return '../' + MUSIC_DIR + '/' + self.rel.replace(os.sep, '/')


@dataclass
class Playlist:
    name: str     # file-safe name = <name>.m3u8 on the drive
    songs: list   # [Song]
    missing: list  # sources not found on the PC


def clean(name, fallback):
    name = BAD_CHARS.sub('_', (name or '').strip()).rstrip('. ')
    return name or fallback


def location_to_path(url):
    """iTunes 'Location' (file://localhost/C:/... or file://localhost//server/share/...) -> path."""
    path = unquote(urlparse(url).path)
    if path.startswith('//'):                  # UNC share
        return '\\\\' + path.lstrip('/').replace('/', '\\')
    if re.match(r'^/[A-Za-z]:', path):         # drive letter
        path = path[1:]
    return os.path.normpath(path)


def drive_relpath(track, src, source_root):
    """Where a song goes under Music/ on the drive."""
    root = os.path.normcase(os.path.normpath(source_root)) + os.sep
    if os.path.normcase(src).startswith(root):
        parts = os.path.normpath(src)[len(root):].split(os.sep)
    else:
        parts = [track.get('Album Artist') or track.get('Artist') or 'Unknown Artist',
                 track.get('Album') or 'Unknown Album', os.path.basename(src)]
    return os.path.join(*[clean(p, '_') for p in parts])


def load_playlists(library_path=DEFAULT_LIBRARY, source_root=DEFAULT_SOURCE_ROOT, check_files=True):
    """The playlists you made in iTunes (no built-ins like Library/Music/Films, no folders)."""
    with open(library_path, 'rb') as f:
        library = plistlib.load(f)
    tracks = library.get('Tracks', {})
    out = []
    for pl in library.get('Playlists', []):
        if pl.get('Master') or 'Distinguished Kind' in pl or pl.get('Folder') or not pl.get('Playlist Items'):
            continue
        songs, missing = [], []
        for item in pl['Playlist Items']:
            track = tracks.get(str(item.get('Track ID')))
            if not track or 'Location' not in track:
                continue
            src = location_to_path(track['Location'])
            if check_files and not os.path.isfile(src):
                missing.append(src)
                continue
            secs = int(track.get('Total Time', 0)) // 1000
            artist, title = track.get('Artist', ''), track.get('Name', os.path.basename(src))
            extinf = f'#EXTINF:{secs},{artist} - {title}' if artist else f'#EXTINF:{secs},{title}'
            songs.append(Song(src, drive_relpath(track, src, source_root), extinf))
        out.append(Playlist(clean(pl.get('Name'), 'Playlist'), songs, missing))
    return out


def read_text(path):
    with open(path, 'rb') as fh:
        raw = fh.read()
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return raw.decode('latin-1')


def parse_path_map(spec):
    """'Z:\\=/mnt/music;\\\\truenas\\media\\music=/mnt/music' -> [(prefix, target)], longest first."""
    pairs = []
    for item in (spec or '').split(';'):
        if '=' in item:
            prefix, target = item.split('=', 1)
            if prefix.strip():
                pairs.append((prefix.strip(), target.strip()))
    return sorted(pairs, key=lambda p: -len(p[0]))


def map_playlist_line(line, playlist_dir, path_map):
    """A path from an .m3u line -> local path, or None if it's a Windows path no prefix maps."""
    if line.startswith('file://'):
        line = unquote(urlparse(line).path)
    for prefix, target in path_map:
        if line.lower().startswith(prefix.lower()):
            rest = line[len(prefix):].replace('\\', '/').lstrip('/')
            return os.path.normpath(os.path.join(target, rest))
    if re.match(r'^[A-Za-z]:[\\/]', line) or line.startswith('\\\\'):
        return None
    line = line.replace('\\', '/')
    return os.path.normpath(line if os.path.isabs(line) else os.path.join(playlist_dir, line))


_dir_cache = {}


def resolve_case(path):
    """The real path when only letter case differs (the playlists were made on
    case-insensitive Windows/SMB; the server's filesystem is case-sensitive)."""
    if os.path.exists(path):
        return path
    parts = os.path.normpath(path).split(os.sep)
    cur = os.sep if path.startswith(os.sep) else ''
    for part in parts:
        if not part:
            continue
        nxt = os.path.join(cur, part)
        if not os.path.exists(nxt):
            if cur not in _dir_cache:
                try:
                    _dir_cache[cur] = {n.lower(): n for n in os.listdir(cur or '.')}
                except OSError:
                    return None
            real = _dir_cache[cur].get(part.lower())
            if real is None:
                return None
            nxt = os.path.join(cur, real)
        cur = nxt
    return cur


def load_m3u_playlists(playlist_dir, source_root, path_map=(), check_files=True):
    """Playlists from a folder of .m3u/.m3u8 files (e.g. exported from iTunes onto the NAS)."""
    _dir_cache.clear()
    out = []
    if not os.path.isdir(playlist_dir):
        return out
    seen = set()
    for f in sorted(os.listdir(playlist_dir)):
        stem, ext = os.path.splitext(f)
        name = clean(stem, 'Playlist')
        if ext.lower() not in PLAYLIST_EXT or name in seen:
            continue
        seen.add(name)
        songs, missing, extinf = [], [], None
        for raw in read_text(os.path.join(playlist_dir, f)).splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith('#EXTINF'):
                extinf = line
                continue
            if line.startswith('#'):
                continue
            src = map_playlist_line(line, playlist_dir, path_map)
            if src is not None and check_files:
                src = resolve_case(src)
            if src is None or (check_files and not os.path.isfile(src)):
                missing.append(line)
            else:
                songs.append(Song(src, drive_relpath({}, src, source_root),
                                  extinf or f'#EXTINF:-1,{os.path.splitext(os.path.basename(src))[0]}'))
            extinf = None
        out.append(Playlist(name, songs, missing))
    return out


def drive_playlists(drive, folder=PLAYLIST_DIR):
    """{name: [playlist lines]} for the playlist files already on a drive."""
    root = os.path.join(drive, folder)
    out = {}
    if not os.path.isdir(root):
        return out
    for f in sorted(os.listdir(root)):
        stem, ext = os.path.splitext(f)
        if ext.lower() not in PLAYLIST_EXT or stem in out:
            continue
        text = read_text(os.path.join(root, f))
        out[stem] = [l.strip().replace('\\', '/') for l in text.splitlines()
                     if l.strip() and not l.startswith('#')]
    return out


def line_to_file(drive, line):
    return os.path.normpath(os.path.join(drive, PLAYLIST_DIR, line))


def compare(playlist, drive_lines, drive):
    """How a drive's copy of a playlist differs from iTunes.
    Returns (state, detail): state is 'not on drive' | 'in sync' | 'differs'."""
    if drive_lines is None:
        return 'not on drive', ''
    want = [s.line for s in playlist.songs]
    norm = lambda lines: [os.path.normcase(l) for l in lines]
    w, d = norm(want), norm(drive_lines)
    added = len(set(w) - set(d))
    removed = len(set(d) - set(w))
    missing_files = sum(1 for l in drive_lines if not os.path.isfile(line_to_file(drive, l)))
    parts = []
    if added:
        parts.append(f'+{added} in iTunes')
    if removed:
        parts.append(f'-{removed} only on drive')
    if not added and not removed and w != d:
        parts.append('order changed')
    if missing_files:
        parts.append(f'{missing_files} songs missing on drive')
    return ('differs', ', '.join(parts)) if parts else ('in sync', '')


def _write_m3u_dir(drive, playlists, folder, ext, remove_others, log):
    """Playlists as .m3u8 (Mixxx) or .m3u (generic), paths relative to the playlist file."""
    root = os.path.join(drive, folder)
    os.makedirs(root, exist_ok=True)
    written = set()
    for pl in playlists:
        lines = ['#EXTM3U']
        for s in pl.songs:
            lines += [s.extinf, s.line]
        path = os.path.join(root, pl.name + ext)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(lines) + '\n')
        written.add(os.path.normcase(path))
    removed = 0
    if remove_others:
        for f in os.listdir(root):
            path = os.path.join(root, f)
            if f.lower().endswith(PLAYLIST_EXT) and os.path.normcase(path) not in written:
                log(f'remove playlist  {folder}/{f}')
                os.remove(path)
                removed += 1
    return removed


LAYOUTS = ('mixxx', 'traktor')
META_DIR = '.exporter'              # on the drive: manifest + staging area
MANIFEST = 'manifest.json'
UNSORTED_DIR = 'Unsorted'           # songs no ticked playlist uses any more
# Never used as a Traktor playlist folder name (they'd clash with other things on the drive).
RESERVED = {MUSIC_DIR, PLAYLIST_DIR, UNSORTED_DIR, META_DIR, '.mixxx', 'PIONEER', 'Contents',
            'System Volume Information', 'Traktor', 'M3U'}


def _norm(p):
    return os.path.normcase(p.replace('\\', '/'))


def load_manifest(drive):
    import json
    try:
        with open(os.path.join(drive, META_DIR, MANIFEST), encoding='utf-8') as f:
            m = json.load(f)
    except (OSError, ValueError):
        m = {}
    m.setdefault('files', {})       # drive path -> {"rel": path under the NAS songs folder, "size": bytes}
    m.setdefault('playlists', {})   # Traktor layout: playlist name -> [drive paths]
    return m


def save_manifest(drive, m):
    import json
    os.makedirs(os.path.join(drive, META_DIR), exist_ok=True)
    tmp = os.path.join(drive, META_DIR, MANIFEST + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(m, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(drive, META_DIR, MANIFEST))


def drive_layout(drive):
    """'traktor', 'mixxx' or None (empty / foreign drive)."""
    m = load_manifest(drive)
    if m.get('layout') in LAYOUTS:
        return m['layout']
    if os.path.isdir(os.path.join(drive, PLAYLIST_DIR)) or os.path.isdir(os.path.join(drive, MUSIC_DIR)):
        return 'mixxx'
    return None


def folder_name(playlist_name):
    name = clean(playlist_name, 'Playlist')
    return name + ' (playlist)' if name in RESERVED or name.startswith('.') else name


def drive_state(drive):
    """{playlist name: [song rels]} as the drive has them now, whatever its layout."""
    layout = drive_layout(drive)
    if layout == 'traktor':
        m = load_manifest(drive)
        out = {}
        for name, paths in m['playlists'].items():
            out[name] = [m['files'][p]['rel'] if p in m['files'] and os.path.isfile(os.path.join(drive, p)) else None
                         for p in paths]
        return out
    prefix = '../' + MUSIC_DIR + '/'
    out = {}
    for name, lines in drive_playlists(drive).items():
        out[name] = [l[len(prefix):] if l.startswith(prefix) and os.path.isfile(line_to_file(drive, l)) else None
                     for l in lines]
    return out


def compare_rels(playlist, have):
    """Like compare(), on song rels: have = list of rels (None = file missing) or None."""
    if have is None:
        return 'not on drive', ''
    want = [_norm(s.rel) for s in playlist.songs]
    got = [_norm(r) for r in have if r]
    added, removed = len(set(want) - set(got)), len(set(got) - set(want))
    missing = sum(1 for r in have if r is None)
    parts = []
    if added:
        parts.append(f'+{added} on the NAS')
    if removed:
        parts.append(f'-{removed} only on drive')
    if missing:
        parts.append(f'{missing} songs missing on drive')
    return ('differs', ', '.join(parts)) if parts else ('in sync', '')


def _scan_pool(drive, m):
    """Files on the drive whose source song we know: drive path -> rel."""
    pool = {}
    # A file at its recorded path is that song even if its size changed: DJ software
    # (Traktor) writes its analysis into the files' tags. Never replace those.
    for p, info in m['files'].items():
        if os.path.isfile(os.path.join(drive, p)):
            pool[p] = info['rel']
    music = os.path.join(drive, MUSIC_DIR)
    for dirpath, _, files in os.walk(music):
        for f in files:
            rel = os.path.relpath(os.path.join(dirpath, f), music).replace('\\', '/')
            pool.setdefault(MUSIC_DIR + '/' + rel, rel)
    return pool


def plan(drive, playlists, layout, remove_others=False, prune=False):
    """What arranging `playlists` onto `drive` in `layout` takes, without touching anything.
    Returns a dict: keep / move [(from, to)] / copy_drive [(from, to)] / copy_nas [(Song, to)] /
    leftovers [(from, to or None=delete)], desired {path: rel}, playlists {name: [paths]}, bytes."""
    m = load_manifest(drive)
    pool = _scan_pool(drive, m)
    desired, pl_paths = {}, {}
    if layout == 'mixxx':
        for pl in playlists:
            for s in pl.songs:
                desired[MUSIC_DIR + '/' + s.rel.replace('\\', '/')] = s
    else:
        for pl in playlists:
            folder, used, paths = folder_name(pl.name), set(), []
            for s in pl.songs:
                base, ext = os.path.splitext(os.path.basename(s.rel))
                name, n = base + ext, 2
                while _norm(name) in used:  # two different songs with the same file name
                    name, n = f'{base} ({n}){ext}', n + 1
                used.add(_norm(name))
                paths.append(f'{folder}/{name}')
                desired[f'{folder}/{name}'] = s
            pl_paths[pl.name] = paths

    # Traktor folders of playlists not exported now stay as they are (unless remove_others)
    protected = {}
    if layout == 'traktor' and m.get('layout') == 'traktor' and not remove_others:
        chosen = {pl.name for pl in playlists}
        for name, paths in m['playlists'].items():
            if name not in chosen:
                kept = [p for p in paths if p in pool]
                if kept:
                    pl_paths[name] = kept
                    protected.update({p: pool[p] for p in kept})

    by_rel = {}
    for p, rel in pool.items():
        by_rel.setdefault(_norm(rel), []).append(p)
    used = set(protected)
    keep, move, copy_drive, copy_nas, placed = [], [], [], [], {}
    for dst, s in desired.items():          # already in place
        if pool.get(dst) is not None and _norm(pool[dst]) == _norm(s.rel):
            keep.append(dst)
            used.add(dst)
            placed.setdefault(_norm(s.rel), dst)
    for dst, s in desired.items():
        if dst in used and dst in keep:
            continue
        r = _norm(s.rel)
        free = [p for p in by_rel.get(r, []) if p not in used and p not in desired]
        if free:
            move.append((free[0], dst))
            used.add(free[0])
            placed.setdefault(r, dst)
        elif r in placed:
            copy_drive.append((placed[r], dst))
        else:
            copy_nas.append((s, dst))
            placed[r] = dst

    leftovers, unsorted_names = [], set()
    targets = set(desired) | set(protected)
    for p, rel in pool.items():
        if p in used or p in targets:
            continue
        if prune:
            leftovers.append((p, None))
        elif layout == 'mixxx':
            dst = MUSIC_DIR + '/' + rel
            if dst in targets or _norm(dst) in unsorted_names:  # an extra copy of a song already in place
                leftovers.append((p, None))
            else:
                leftovers.append((p, dst))
                unsorted_names.add(_norm(dst))
        else:
            if p.startswith(UNSORTED_DIR + '/'):
                leftovers.append((p, p))
                unsorted_names.add(_norm(p))
                continue
            base, ext = os.path.splitext(os.path.basename(p))
            name, n = base + ext, 2
            while _norm(f'{UNSORTED_DIR}/{name}') in unsorted_names:
                name, n = f'{base} ({n}){ext}', n + 1
            unsorted_names.add(_norm(f'{UNSORTED_DIR}/{name}'))
            leftovers.append((p, f'{UNSORTED_DIR}/{name}'))

    size = lambda p: os.path.getsize(os.path.join(drive, p))
    nbytes = sum(os.path.getsize(s.src) for s, _ in copy_nas) + sum(size(a) for a, _ in copy_drive if a in pool)
    nbytes += sum(os.path.getsize(desired[a].src) for a, _ in copy_drive if a not in pool)
    return {'keep': keep, 'move': move, 'copy_drive': copy_drive, 'copy_nas': copy_nas,
            'leftovers': leftovers, 'desired': {p: s.rel for p, s in desired.items()},
            'protected': protected, 'playlists': pl_paths, 'bytes': nbytes, 'pool': pool}


def bytes_to_copy(drive, playlists, layout=None):
    return plan(drive, playlists, layout or drive_layout(drive) or 'mixxx')['bytes']


def _remove_empty_dirs(root, stop):
    for dirpath, _, _ in sorted(os.walk(root, topdown=False), key=lambda t: -len(t[0])):
        if os.path.abspath(dirpath) != os.path.abspath(stop) and not os.listdir(dirpath):
            os.rmdir(dirpath)


def export(drive, playlists, remove_others=False, prune=False, log=print, progress=None, layout=None):
    """Arranges `playlists` on `drive` in a layout:
      mixxx   = Music/<Artist/Album/file> + Playlists/<name>.m3u8 (the Pi imports them)
      traktor = one folder per playlist with the songs inside, no playlist files.
    Files already on the drive are moved (renamed), not copied again; only songs the
    drive doesn't have, or extra copies of a song in several playlists, are copied.
    Songs no exported playlist uses go to Unsorted/ (Traktor) or stay in Music/ (Mixxx),
    or are deleted with prune. remove_others: also dissolve playlists not exported now.
    progress(done, total) is called per file. Returns a summary string."""
    layout = layout or drive_layout(drive) or 'mixxx'
    p = plan(drive, playlists, layout, remove_others, prune)
    free = shutil.disk_usage(drive).free
    if p['bytes'] > free:
        raise OSError(f'not enough space on {drive}: needs {p["bytes"] >> 20} MB, {free >> 20} MB free')
    j = lambda rel: os.path.join(drive, rel)
    total = (len(p['move']) + len(p['copy_drive']) + len(p['copy_nas'])
             + sum(1 for a, b in p['leftovers'] if a != b))
    done = 0

    def step():
        nonlocal done
        done += 1
        if progress:
            progress(done, total)

    # 1. everything that changes place goes to a staging area first, so a file can
    #    land where another one is about to leave
    stage = os.path.join(drive, META_DIR, 'staging')
    os.makedirs(stage, exist_ok=True)
    staged = []
    for i, (src, dst) in enumerate(p['move'] + [(a, b) for a, b in p['leftovers'] if b and a != b]):
        tmp = os.path.join(stage, str(i))
        os.replace(j(src), tmp)
        staged.append((tmp, dst))
    for src, dst in p['leftovers']:
        if dst is None:
            log(f'delete  {src}')
            os.remove(j(src))
            step()
    # anything else sitting where a song must go (not a song we know) is moved aside
    for dst in list(p['desired']):
        if os.path.exists(j(dst)) and dst not in p['keep']:
            aside = f'{UNSORTED_DIR}/{os.path.basename(dst)}'
            os.makedirs(os.path.dirname(j(aside)), exist_ok=True)
            os.replace(j(dst), j(aside))
    # 2. put the staged files in place
    for tmp, dst in staged:
        os.makedirs(os.path.dirname(j(dst)), exist_ok=True)
        os.replace(tmp, j(dst))
        log(f'move  {dst}')
        step()
    # 3. copies: from the NAS, then extra copies from the drive itself
    for s, dst in p['copy_nas']:
        os.makedirs(os.path.dirname(j(dst)), exist_ok=True)
        log(f'copy  {dst}')
        shutil.copyfile(s.src, j(dst))
        step()
    for src, dst in p['copy_drive']:
        os.makedirs(os.path.dirname(j(dst)), exist_ok=True)
        log(f'copy  {dst}  (from the drive)')
        shutil.copyfile(j(src), j(dst))
        step()

    # 4. playlist files and bookkeeping
    m = load_manifest(drive)
    files = {}
    for path, rel in list(p['desired'].items()) + list(p['protected'].items()):
        files[path] = {'rel': rel.replace('\\', '/'), 'size': os.path.getsize(j(path))}
    for src, dst in p['leftovers']:
        if dst:
            files[dst] = {'rel': p['pool'][src], 'size': os.path.getsize(j(dst))}
    removed = 0
    if layout == 'mixxx':
        removed = _write_m3u_dir(drive, playlists, PLAYLIST_DIR, '.m3u8', remove_others, log)
        m['playlists'] = {}
    else:
        pl_dir = os.path.join(drive, PLAYLIST_DIR)
        if os.path.isdir(pl_dir):  # the Traktor layout has no playlist files
            for f in os.listdir(pl_dir):
                if f.lower().endswith(PLAYLIST_EXT):
                    os.remove(os.path.join(pl_dir, f))
        removed = len([n for n in m['playlists'] if n not in p['playlists']])
        m['playlists'] = p['playlists']
    for old in ('Traktor/Exporter USB.nml',):  # written by v0.5.0
        if os.path.isfile(j(old)):
            os.remove(j(old))
    m.update(layout=layout, files=files)
    save_manifest(drive, m)
    for d in [MUSIC_DIR, PLAYLIST_DIR, 'Traktor', 'M3U', UNSORTED_DIR] + [folder_name(n) for n in p['playlists']] \
            + sorted({x.split('/')[0] for x, _ in p['move'] + p['leftovers'] if '/' in x}):
        if os.path.isdir(j(d)):
            _remove_empty_dirs(j(d), drive)
    _remove_empty_dirs(stage, os.path.join(drive, META_DIR))

    summary = (f'{len(playlists)} playlists ({layout}): {len(p["copy_nas"])} copied from the NAS, '
               f'{len(p["move"])} moved, {len(p["copy_drive"])} duplicated')
    left = [d for _, d in p['leftovers'] if d and d.startswith(UNSORTED_DIR + '/')]
    if left:
        summary += f', {len(left)} in {UNSORTED_DIR}/'
    if prune:
        summary += f', {sum(1 for _, d in p["leftovers"] if d is None)} deleted'
    if remove_others:
        summary += f', {removed} playlists removed'
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('drive', nargs='?', help='root of the USB drive, e.g. E:\\')
    ap.add_argument('--library', default=DEFAULT_LIBRARY, help='iTunes Music Library.xml')
    ap.add_argument('--source-root', default=DEFAULT_SOURCE_ROOT,
                    help='music root whose Artist/Album layout is kept (default: the NAS share)')
    ap.add_argument('--playlist', action='append', help='export only this playlist (repeatable)')
    ap.add_argument('--list', action='store_true', help='list your playlists and exit')
    ap.add_argument('--prune', action='store_true', help='delete songs under Music/ no playlist uses')
    ap.add_argument('--layout', choices=LAYOUTS,
                    help="mixxx: Music/ + Playlists/*.m3u8; traktor: a folder per playlist "
                         "(default: the drive's current layout, else mixxx)")
    args = ap.parse_args()

    playlists = load_playlists(args.library, args.source_root)
    if args.playlist:
        wanted = {clean(n, 'Playlist') for n in args.playlist}
        playlists = [p for p in playlists if p.name in wanted]
        unknown = wanted - {p.name for p in playlists}
        if unknown:
            sys.exit(f'no such playlist(s): {", ".join(sorted(unknown))}')

    if args.list or not args.drive:
        on_drive = drive_playlists(args.drive) if args.drive else {}
        for p in playlists:
            state = ''
            if args.drive:
                st, detail = compare(p, on_drive.get(p.name), args.drive)
                state = f'  [{st}{": " + detail if detail else ""}]'
            print(f'{p.name}  ({len(p.songs)} songs){state}')
        if not args.drive and not args.list:
            print('\npass the USB drive root to export, e.g.  python export_playlists.py E:\\')
        return

    print(export(args.drive, playlists, remove_others=not args.playlist, prune=args.prune, layout=args.layout))
    for p in playlists:
        for m in p.missing:
            print(f'WARNING {p.name}: missing on PC: {m}')


if __name__ == '__main__':
    main()
