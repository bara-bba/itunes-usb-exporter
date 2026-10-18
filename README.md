---
type: snapshot
status: active
area: dj
tags: [dj, mixxx, itunes, usb, playlists]
created: 2026-10-10
updated: 2026-10-18
---

# iTunes → USB Exporter

Copies the songs of your playlists onto a USB drive, keeping the NAS
`Artist/Album/file` layout, and writes one `.m3u8` per playlist. The drive is
then plugged into the headless Pi from [[push-mixxx]], which mirrors those
playlists into Mixxx.

Two ways to run it:

- **Proxmox LXC (main way):** a web page served from a container on the server
  that also hosts the NAS. It reads the M3U playlists straight from the NAS and
  writes to a USB drive plugged into the server, so nothing goes through a PC.
- **Windows:** desktop GUI / command line reading iTunes' library XML.

## Proxmox LXC

On the Proxmox host shell, as root:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/bara-bba/itunes-usb-exporter/main/install/usb-exporter.sh)"
```

The script asks for the container settings, the NAS NFS share (it lists the
exports) and then which USB port to use: plug a drive into it (e.g. the front
port) when asked and that physical port is remembered. It then:

- creates an **unprivileged** Debian 12 LXC (tag `usb-exporter`) running the web UI on port 8080;
- mounts the NFS share read-only on the host (`/mnt/usb-exporter/music`) and passes it in as `/mnt/music`;
- installs a udev rule + services on the host that mount any drive plugged into
  that port at `/mnt/usb-exporter/usb` (vfat/exFAT/NTFS, owned by the container's
  root) and propagate it into the container as `/mnt/usb`;
- wires the page's **Eject** button to the host (the container writes a request
  file, `usb-exporter-eject.path` unmounts the drive).

Run the same command again to **update** (it finds the container by its tag);
inside the container, `update` does the app part only.

Why the host mounts the drive: an unprivileged container may use a block device
but the kernel won't let it mount one. Passing the device in and mounting it with
FUSE works for exFAT/NTFS but not reliably for FAT32, the usual format.

The page (open `http://<container-ip>:8080`, also from a phone) shows each NAS
playlist with its state on the plugged-in drive (in sync / differs / not on
drive / only on drive), songs the NAS doesn't have, Export with progress, and
Eject. Playlist paths like `Z:\data\Artist\Album\song.m4a` are mapped with
`PATH_MAP` (`Z:\=/mnt/music`), case differences are tolerated. Settings live in
`/etc/usb-exporter.env` in the container; host settings in `/etc/usb-exporter/host.conf`.

Requirements on the NAS: an NFS export of the music share that allows the
Proxmox host's IP, readable by everyone (or mapped to a user that can read it).

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

- The Proxmox installer and the host-side USB mount/propagation are untested on
  the real server (written 2026-10-18 without host access). The web app and the
  M3U reader were tested against the real NAS playlists (10 playlists, all songs found).
- Windows GUI: iTunes needs the NAS songs in its library before they can be in playlists.

## Related

- [[push-mixxx]] — Pi side: USB auto-mount, per-drive library, playlist import
