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


def drive_playlists(drive):
    """{name: [playlist lines]} for the playlist files already on a drive."""
    root = os.path.join(drive, PLAYLIST_DIR)
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


def export(drive, playlists, remove_others=False, prune=False, log=print, progress=None):
    """Copies the songs and writes the playlist files for `playlists` onto `drive`.
    remove_others: delete playlist files on the drive not in `playlists`.
    prune: delete songs under Music/ that no playlist file left on the drive uses.
    progress(done, total) is called after each song. Returns a summary string."""
    music_root = os.path.join(drive, MUSIC_DIR)
    playlist_root = os.path.join(drive, PLAYLIST_DIR)
    os.makedirs(playlist_root, exist_ok=True)

    need = bytes_to_copy(drive, playlists)
    free = shutil.disk_usage(drive).free
    if need > free:
        raise OSError(f'not enough space on {drive}: needs {need >> 20} MB, {free >> 20} MB free')

    total = sum(len(pl.songs) for pl in playlists)
    done = copied = 0
    written = set()
    for pl in playlists:
        lines = ['#EXTM3U']
        for s in pl.songs:
            dst = os.path.join(music_root, s.rel)
            if not (os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(s.src)):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                log(f'copy  {s.rel}')
                shutil.copyfile(s.src, dst)
                copied += 1
            lines += [s.extinf, s.line]
            done += 1
            if progress:
                progress(done, total)
        path = os.path.join(playlist_root, pl.name + '.m3u8')
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(lines) + '\n')
        written.add(os.path.normcase(path))
        log(f'playlist  {pl.name}  ({len(pl.songs)} songs)')

    removed = 0
    if remove_others:
        for f in os.listdir(playlist_root):
            path = os.path.join(playlist_root, f)
            if f.lower().endswith(PLAYLIST_EXT) and os.path.normcase(path) not in written:
                log(f'remove playlist  {f}')
                os.remove(path)
                removed += 1

    pruned = 0
    if prune and os.path.isdir(music_root):
        used = {os.path.normcase(line_to_file(drive, l))
                for lines in drive_playlists(drive).values() for l in lines}
        for dirpath, _, files in os.walk(music_root, topdown=False):
            for f in files:
                path = os.path.join(dirpath, f)
                if os.path.normcase(os.path.normpath(path)) not in used:
                    os.remove(path)
                    pruned += 1
            if dirpath != music_root and not os.listdir(dirpath):
                os.rmdir(dirpath)

    summary = f'{len(playlists)} playlists, {copied} songs copied'
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

    print(export(args.drive, playlists, remove_others=not args.playlist, prune=args.prune))
    for p in playlists:
        for m in p.missing:
            print(f'WARNING {p.name}: missing on PC: {m}')


if __name__ == '__main__':
    main()
