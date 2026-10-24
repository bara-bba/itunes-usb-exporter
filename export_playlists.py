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


def bytes_to_copy(drive, playlists):
    total, seen = 0, set()
    for pl in playlists:
        for s in pl.songs:
            key = os.path.normcase(s.rel)
            if key in seen:
                continue
            seen.add(key)
            dst = os.path.join(drive, MUSIC_DIR, s.rel)
            size = os.path.getsize(s.src)
            if not (os.path.isfile(dst) and os.path.getsize(dst) == size):
                total += size
    return total


TARGETS = ('mixxx', 'traktor', 'm3u')
GENERIC_DIR = 'M3U'                 # generic .m3u playlists (any player)
TRAKTOR_DIR = 'Traktor'             # one .nml with every exported playlist
TRAKTOR_FILE = 'Exporter USB.nml'
TRAKTOR_FOLDER = 'Exporter USB'     # playlist folder Traktor shows after the import


def _copy_songs(drive, playlists, log, progress):
    music_root = os.path.join(drive, MUSIC_DIR)
    total = sum(len(pl.songs) for pl in playlists)
    done = copied = 0
    for pl in playlists:
        for s in pl.songs:
            dst = os.path.join(music_root, s.rel)
            if not (os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(s.src)):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                log(f'copy  {s.rel}')
                shutil.copyfile(s.src, dst)
                copied += 1
            done += 1
            if progress:
                progress(done, total)
    return copied


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


def _xml(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;'))


def traktor_volume(platform, label, letter=''):
    """How Traktor names the drive in its paths: the drive letter on Windows
    ('E:'), the volume name on macOS."""
    if platform == 'windows':
        letter = (letter or 'E').strip().rstrip(':\\/').upper()[:1] or 'E'
        return letter + ':'
    return label or 'USB'


def write_traktor_nml(drive, playlists, volume):
    """One Traktor .nml holding every playlist in a folder: in Traktor,
    Playlists > Import Playlist > this file brings them all in at once."""
    import uuid
    entries, keys = {}, []
    for pl in playlists:
        pk = []
        for s in pl.songs:
            parts = [MUSIC_DIR] + s.rel.replace('\\', '/').split('/')
            d = '/:' + '/:'.join(parts[:-1]) + '/:'
            key = volume + d + parts[-1]
            if key not in entries:
                info = s.extinf.split(',', 1)[1] if ',' in s.extinf else parts[-1]
                artist, _, title = info.partition(' - ') if ' - ' in info else ('', '', info)
                entries[key] = (f'<ENTRY TITLE="{_xml(title)}" ARTIST="{_xml(artist)}">'
                                f'<LOCATION DIR="{_xml(d)}" FILE="{_xml(parts[-1])}" VOLUME="{_xml(volume)}" '
                                f'VOLUMEID="{_xml(volume)}"></LOCATION></ENTRY>')
            pk.append(key)
        keys.append((pl.name, pk))
    nodes = ''.join(
        f'<NODE TYPE="PLAYLIST" NAME="{_xml(name)}"><PLAYLIST ENTRIES="{len(pk)}" TYPE="LIST" UUID="{uuid.uuid4().hex}">'
        + ''.join(f'<ENTRY><PRIMARYKEY TYPE="TRACK" KEY="{_xml(k)}"></PRIMARYKEY></ENTRY>' for k in pk)
        + '</PLAYLIST></NODE>' for name, pk in keys)
    nml = ('<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n'
           '<NML VERSION="19"><HEAD COMPANY="www.native-instruments.com" PROGRAM="Traktor"></HEAD>'
           f'<COLLECTION ENTRIES="{len(entries)}">' + ''.join(entries.values()) + '</COLLECTION>'
           '<PLAYLISTS><NODE TYPE="FOLDER" NAME="$ROOT"><SUBNODES COUNT="1">'
           f'<NODE TYPE="FOLDER" NAME="{_xml(TRAKTOR_FOLDER)}"><SUBNODES COUNT="{len(keys)}">{nodes}</SUBNODES></NODE>'
           '</SUBNODES></NODE></PLAYLISTS></NML>\n')
    root = os.path.join(drive, TRAKTOR_DIR)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, TRAKTOR_FILE), 'w', encoding='utf-8', newline='\n') as f:
        f.write(nml)


def export(drive, playlists, remove_others=False, prune=False, log=print, progress=None,
           targets=('mixxx',), traktor_platform='mac', traktor_letter='', label=''):
    """Copies the songs of `playlists` to <drive>/Music once, then writes them for
    each target: mixxx = Playlists/*.m3u8 (the Pi imports them), traktor = one
    Traktor/Exporter USB.nml, m3u = M3U/*.m3u for any other player.
    remove_others: delete .m3u8/.m3u playlist files not in `playlists`.
    prune: delete songs under Music/ that no playlist left on the drive uses.
    progress(done, total) is called after each song. Returns a summary string."""
    targets = [t for t in TARGETS if t in set(targets)] or ['mixxx']
    need = bytes_to_copy(drive, playlists)
    free = shutil.disk_usage(drive).free
    if need > free:
        raise OSError(f'not enough space on {drive}: needs {need >> 20} MB, {free >> 20} MB free')

    copied = _copy_songs(drive, playlists, log, progress)
    removed = 0
    if 'mixxx' in targets:
        removed += _write_m3u_dir(drive, playlists, PLAYLIST_DIR, '.m3u8', remove_others, log)
        log(f'Mixxx: {len(playlists)} playlists in {PLAYLIST_DIR}/')
    if 'm3u' in targets:
        removed += _write_m3u_dir(drive, playlists, GENERIC_DIR, '.m3u', remove_others, log)
        log(f'M3U: {len(playlists)} playlists in {GENERIC_DIR}/')
    if 'traktor' in targets:
        vol = traktor_volume(traktor_platform, label, traktor_letter)
        write_traktor_nml(drive, playlists, vol)
        log(f'Traktor: {TRAKTOR_DIR}/{TRAKTOR_FILE} ({len(playlists)} playlists, drive "{vol}")')

    pruned = 0
    music_root = os.path.join(drive, MUSIC_DIR)
    if prune and os.path.isdir(music_root):
        used = {os.path.normcase(os.path.join(music_root, s.rel)) for pl in playlists for s in pl.songs}
        for folder in (PLAYLIST_DIR, GENERIC_DIR):
            for lines in drive_playlists(drive, folder).values():
                used |= {os.path.normcase(os.path.normpath(os.path.join(drive, folder, l))) for l in lines}
        for dirpath, _, files in os.walk(music_root, topdown=False):
            for f in files:
                path = os.path.join(dirpath, f)
                if os.path.normcase(os.path.normpath(path)) not in used:
                    os.remove(path)
                    pruned += 1
            if dirpath != music_root and not os.listdir(dirpath):
                os.rmdir(dirpath)

    names = {'mixxx': 'Mixxx', 'traktor': 'Traktor', 'm3u': 'M3U'}
    summary = f'{len(playlists)} playlists for {" + ".join(names[t] for t in targets)}, {copied} songs copied'
    if remove_others:
        summary += f', {removed} playlists removed'
    if prune:
        summary += f', {pruned} unused songs deleted'
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
    ap.add_argument('--for', dest='targets', default='mixxx',
                    help='comma-separated software to export for: mixxx (default), traktor, m3u')
    ap.add_argument('--traktor-os', choices=('mac', 'windows'), default='mac',
                    help='Traktor runs on mac (drive found by name) or windows (by drive letter)')
    ap.add_argument('--traktor-drive', default='', help='Traktor on Windows: the drive letter, e.g. E')
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

    label = os.path.basename(os.path.normpath(args.drive)) or ''
    print(export(args.drive, playlists, remove_others=not args.playlist, prune=args.prune,
                 targets=[t.strip() for t in args.targets.split(',')], traktor_platform=args.traktor_os,
                 traktor_letter=args.traktor_drive or args.drive[:1], label=label))
    for p in playlists:
        for m in p.missing:
            print(f'WARNING {p.name}: missing on PC: {m}')


if __name__ == '__main__':
    main()
