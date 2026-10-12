---
type: snapshot
status: active
area: dj
tags: [dj, mixxx, itunes, usb, playlists]
created: 2026-10-10
updated: 2026-10-10
---

# iTunes → USB Exporter

Copies the songs of your iTunes playlists onto a USB drive, keeping the NAS
`Artist/Album/file` layout, and writes one `.m3u8` per playlist. The drive is
then plugged into the headless Pi from [[push-mixxx]], which mirrors those
playlists into Mixxx.

## GUI (Windows)

```bash
pythonw gui.py
```

- Lists your iTunes playlists with a tick box each. Against the drive picked under
  **Compare with**, each one shows **in sync** (green), **differs** (orange, with
  `+N in iTunes` / `-N only on drive` / `order changed` / `N songs missing on drive`),
  **not on drive**, or, greyed out, **only on drive** (not in iTunes any more).
  Playlists already on that drive start ticked.
- **Export to** lists every removable drive (label, free space); tick several to
  export to all of them. **Add folder…** for any other target.
- Options (off by default, confirmed before running): remove playlists on the drive
  that aren't ticked; delete songs no playlist on the drive uses.
- Export runs in the background with a progress bar and log, and stops before
  copying if a drive doesn't have enough free space.

## Command line

```bash
python export_playlists.py --list                       # your iTunes playlists
python export_playlists.py E:\ --list                   # ...and their state on drive E:
python export_playlists.py E:\                          # export all of them
python export_playlists.py E:\ --playlist "Friday set"  # only some (repeatable)
python export_playlists.py E:\ --prune                  # also delete songs no playlist uses
```

Python 3 standard library only. Re-running is incremental: songs already on
the drive with the same size are skipped.

- Reads `%USERPROFILE%\Music\iTunes\iTunes Music Library.xml` (`--library` to override).
  iTunes built-ins (Library, Music, Films, Podcasts...) and playlist folders are ignored.
- Songs under `--source-root` (default `\\truenas\media\music\data`) keep their
  relative path; others go to `Album Artist (or Artist)/Album/file` from their iTunes tags.
- Characters FAT32/exFAT reject (`<>:"/\|?*`) become `_`.

## Drive layout (shared contract with push-mixxx)

```
<drive>/
  Music/Artist/Album/song.m4a     songs, copied by this tool
  Playlists/<name>.m3u8           UTF-8, #EXTM3U/#EXTINF, paths relative to the
                                  playlist file: ../Music/Artist/Album/song.m4a
  .mixxx/                         the drive's Mixxx library, made/managed by the Pi
```

A full export (no `--playlist`) makes `Playlists/` match iTunes exactly:
`.m3u8`/`.m3u` files for playlists that no longer exist are deleted. On the Pi,
`push-mixxx/usb/import_playlists.py` mirrors that folder into locked Mixxx
playlists on every Mixxx start (playlist names are cut to 48 characters).
Other folders on the drive (e.g. a Rekordbox `Contents/`) are never touched.

## Open items

- iTunes needs the NAS songs added to its library before they can be in playlists.
- Not yet run against a real playlist export (no user playlists in iTunes as of 2026-10-10).

## Related

- [[push-mixxx]] — Pi side: USB auto-mount, per-drive library, playlist import
