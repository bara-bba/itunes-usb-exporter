#!/usr/bin/env bash
# USB playlist exporter - Proxmox VE helper script.
# Run on the Proxmox host shell (as root):
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/bara-bba/itunes-usb-exporter/main/install/usb-exporter.sh)"
#
# Install: creates an unprivileged Debian LXC with the web UI, mounts the NAS
# music share (NFS, read-only) on the host and passes it in, and sets up the
# host to mount a USB drive plugged into ONE chosen USB port and share it with
# the container (the container can't mount disks itself - it's unprivileged).
# Run it again to update the app and refresh the host side.
set -euo pipefail

REPO="${REPO:-bara-bba/itunes-usb-exporter}"
BRANCH="${BRANCH:-main}"
RAW="https://raw.githubusercontent.com/$REPO/$BRANCH"
TAG=usb-exporter
CONF=/etc/usb-exporter/host.conf
BASE=/mnt/usb-exporter            # host: music/ (NFS) and usb/ (the drive)
CTL=/var/lib/usb-exporter/ctl     # host: status + eject request, shared with the CT
CT_ROOT_UID=100000                # container root on the host (unprivileged default)

c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_err=$'\e[31m'; c_dim=$'\e[2m'; c_off=$'\e[0m'
msg()  { echo "${c_ok}==>${c_off} $*"; }
warn() { echo "${c_warn}!! ${c_off} $*"; }
die()  { echo "${c_err}xx ${c_off} $*" >&2; exit 1; }
ask()  { local q="$1" def="${2:-}" a; read -r -p "$q${def:+ [$def]}: " a </dev/tty; echo "${a:-$def}"; }
yes_no() { local a; read -r -p "$1 [y/N]: " a </dev/tty; [[ "$a" =~ ^[YyJjSs] ]]; }

[ "$(id -u)" -eq 0 ] || die "run as root on the Proxmox host"
command -v pct >/dev/null || die "pct not found - run this on a Proxmox VE host"

# ----------------------------------------------------------- host side ----
write_host_files() {
    local port_match="$1"
    mkdir -p "$BASE/music" "$BASE/usb/drive" "$CTL" /usr/local/sbin
    chown "$CT_ROOT_UID:$CT_ROOT_UID" "$CTL"
    chmod 775 "$CTL"

    cat > /usr/local/sbin/usb-exporter-status <<EOF
#!/bin/sh
# usb-exporter-status <state> [label] [fstype] [device]
printf 'state=%s\nlabel=%s\nfstype=%s\ndevice=%s\n' "\$1" "\${2:-}" "\${3:-}" "\${4:-}" > $CTL/status.tmp
chown $CT_ROOT_UID:$CT_ROOT_UID $CTL/status.tmp
mv $CTL/status.tmp $CTL/status
EOF

    cat > /usr/local/sbin/usb-exporter-mount <<EOF
#!/bin/sh
# udev -> usb-exporter-mount@<dev>.service: mount the drive for the container.
DEV=/dev/\$1
MP=$BASE/usb/drive
[ -e /run/usb-exporter-formatting ] && exit 0                      # the format script mounts it itself
findmnt -rn -M "\$MP" -o SOURCE | grep -q '^/dev/sd' && exit 0   # one drive at a time
FSTYPE=\$(blkid -o value -s TYPE "\$DEV") || exit 0
LABEL=\$(blkid -o value -s LABEL "\$DEV" || true)
IDS="uid=$CT_ROOT_UID,gid=$CT_ROOT_UID,umask=022"
case "\$FSTYPE" in
    vfat)       OPTS="\$IDS,utf8,flush" ;;
    exfat)      OPTS="\$IDS,iocharset=utf8" ;;
    ntfs|ntfs3) FSTYPE=ntfs3; OPTS="\$IDS" ;;
    *)          OPTS=defaults ;;
esac
if mount -t "\$FSTYPE" -o "\$OPTS" "\$DEV" "\$MP"; then
    [ "\$OPTS" = defaults ] && chown $CT_ROOT_UID:$CT_ROOT_UID "\$MP"
    /usr/local/sbin/usb-exporter-status mounted "\$LABEL" "\$FSTYPE" "\$DEV"
else
    /usr/local/sbin/usb-exporter-status error "\$LABEL" "\$FSTYPE" "\$DEV"
fi
EOF

    cat > /usr/local/sbin/usb-exporter-eject <<EOF
#!/bin/sh
# usb-exporter-eject.path -> asked by the container (Eject button).
MP=$BASE/usb/drive
rm -f $CTL/eject
sync
if findmnt -rn -M "\$MP" -o SOURCE | grep -q '^/dev/sd'; then
    umount "\$MP" && /usr/local/sbin/usb-exporter-status ejected || /usr/local/sbin/usb-exporter-status mounted
else
    /usr/local/sbin/usb-exporter-status none
fi
EOF

    cat > /usr/local/sbin/usb-exporter-format <<EOF
#!/bin/sh
# usb-exporter-format.path -> asked by the container (Format button).
# Request file: fstype=vfat|exfat, label=NAME. Only ever touches the disk of
# the drive currently mounted from the chosen USB port.
MP=$BASE/usb/drive
REQ=$CTL/format
PORT_MATCH='$port_match'
FS=\$(sed -n 's/^fstype=//p' "\$REQ"); LABEL=\$(sed -n 's/^label=//p' "\$REQ")
rm -f "\$REQ"
fail() { echo "format: \$*" >&2; /usr/local/sbin/usb-exporter-status error "\$LABEL" "\$FS" "\$*"; exit 1; }
PART=\$(sed -n 's/^device=//p' $CTL/status)
[ -b "\$PART" ] || fail "no USB drive to format"
DISK=/dev/\$(lsblk -no PKNAME "\$PART" | head -1)
[ "\$DISK" != /dev/ ] || DISK=\$PART                          # partitionless drive
[ "\$(lsblk -dno TRAN "\$DISK")" = usb ] || fail "\$DISK is not a USB disk"
if [ -n "\$PORT_MATCH" ]; then
    DPATH=\$(udevadm info -q property -n "\$DISK" | sed -n 's/^ID_PATH=//p')
    ok=
    for p in \$PORT_MATCH; do case "\$DPATH" in "\$p"*) ok=1 ;; esac; done
    [ -n "\$ok" ] || fail "\$DISK is not on the chosen USB port(s)"
fi
case "\$FS" in
    vfat)  TYPE=c; LABEL=\$(printf '%s' "\$LABEL" | tr -cd 'A-Za-z0-9_-' | cut -c1-11 | tr a-z A-Z) ;;
    exfat) TYPE=7; LABEL=\$(printf '%s' "\$LABEL" | cut -c1-15) ;;
    *)     fail "unsupported filesystem '\$FS'" ;;
esac
[ -n "\$LABEL" ] || LABEL=MUSIC
/usr/local/sbin/usb-exporter-status formatting "\$LABEL" "\$FS" "\$PART"
touch /run/usb-exporter-formatting                    # keeps usb-exporter-mount away meanwhile
trap 'rm -f /run/usb-exporter-formatting' EXIT
sync
unmount_all() {  # every mount of this disk, also stacked ones
    for dev in \$(lsblk -lnpo NAME "\$DISK"); do
        while findmnt -rn -S "\$dev" >/dev/null; do umount "\$dev" 2>/dev/null || umount -l "\$dev"; done
    done
}
unmount_all
for dev in \$(lsblk -lnpo NAME,TYPE "\$DISK" | awk '\$2=="part"{print \$1}'); do wipefs -aq "\$dev"; done
wipefs -aq "\$DISK" || fail "wipefs failed"
printf 'label: dos\n,,%s\n' "\$TYPE" | sfdisk -q --wipe always --wipe-partitions always "\$DISK" || fail "partitioning failed"
udevadm settle; partprobe "\$DISK" 2>/dev/null || true; udevadm settle
NEW=\$(lsblk -lnpo NAME,TYPE "\$DISK" | awk '\$2=="part"{print \$1; exit}')
[ -b "\$NEW" ] || fail "new partition not found"
unmount_all
if [ "\$FS" = vfat ]; then mkfs.vfat -F 32 -n "\$LABEL" "\$NEW" >/dev/null || fail "mkfs.vfat failed"
else mkfs.exfat -L "\$LABEL" "\$NEW" >/dev/null || fail "mkfs.exfat failed"; fi
udevadm settle
rm -f /run/usb-exporter-formatting
/usr/local/sbin/usb-exporter-mount "\$(basename "\$NEW")"
EOF

    cat > /usr/local/sbin/usb-exporter-unplugged <<EOF
#!/bin/sh
# Drive pulled without ejecting: drop the stale mount.
MP=$BASE/usb/drive
[ "\$(findmnt -rn -M "\$MP" -o SOURCE | grep '^/dev/sd' | head -1)" = "/dev/\$1" ] || exit 0
while findmnt -rn -M "\$MP" -o SOURCE | grep -q '^/dev/sd'; do umount -l "\$MP"; done
/usr/local/sbin/usb-exporter-status none
EOF
    chmod 755 /usr/local/sbin/usb-exporter-*

    cat > /etc/systemd/system/usb-exporter-mount@.service <<'EOF'
[Unit]
Description=Mount USB drive %I for the usb-exporter container
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/usb-exporter-mount %I
EOF
    cat > /etc/systemd/system/usb-exporter-unplugged@.service <<'EOF'
[Unit]
Description=USB drive %I unplugged (usb-exporter)
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/usb-exporter-unplugged %I
EOF
    cat > /etc/systemd/system/usb-exporter-eject.service <<'EOF'
[Unit]
Description=Eject the usb-exporter USB drive
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/usb-exporter-eject
EOF
    cat > /etc/systemd/system/usb-exporter-eject.path <<EOF
[Unit]
Description=Watch for eject requests from the usb-exporter container
[Path]
PathExists=$CTL/eject
Unit=usb-exporter-eject.service
[Install]
WantedBy=multi-user.target
EOF
    cat > /etc/systemd/system/usb-exporter-format.service <<'EOF'
[Unit]
Description=Format the usb-exporter USB drive
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/usb-exporter-format
EOF
    cat > /etc/systemd/system/usb-exporter-format.path <<EOF
[Unit]
Description=Watch for format requests from the usb-exporter container
[Path]
PathExists=$CTL/format
Unit=usb-exporter-format.service
[Install]
WantedBy=multi-user.target
EOF
    # The drive is mounted ON this bind mount; "shared" makes that mount
    # propagate into the container (its lxc.mount.entry is rslave).
    cat > /etc/systemd/system/usb-exporter-propagation.service <<EOF
[Unit]
Description=Shared mount point for the usb-exporter USB drive
Before=pve-guests.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'mkdir -p $BASE/usb; findmnt -rn -M $BASE/usb >/dev/null || mount --bind $BASE/usb $BASE/usb; mount --make-shared $BASE/usb'
[Install]
WantedBy=multi-user.target
EOF

    # port_match: space-separated ID_PATH prefixes (one per allowed USB port); empty = any port
    local add='ACTION=="add", SUBSYSTEM=="block", ENV{ID_BUS}=="usb", ENV{ID_FS_USAGE}=="filesystem"'
    local mount='TAG+="systemd", ENV{SYSTEMD_WANTS}+="usb-exporter-mount@%k.service"' p
    {
        echo "# usb-exporter: mount USB drives plugged into the chosen port(s) for the container"
        if [ -n "$port_match" ]; then
            for p in $port_match; do echo "$add, ENV{ID_PATH}==\"$p*\", $mount"; done
        else
            echo "$add, $mount"
        fi
        echo 'ACTION=="remove", SUBSYSTEM=="block", ENV{ID_BUS}=="usb", RUN+="/bin/systemctl --no-block start usb-exporter-unplugged@%k.service"'
    } > /etc/udev/rules.d/90-usb-exporter.rules

    systemctl daemon-reload
    command -v mkfs.vfat >/dev/null && command -v mkfs.exfat >/dev/null \
        || apt-get install -y -qq dosfstools exfatprogs >/dev/null
    systemctl enable -q --now usb-exporter-propagation.service usb-exporter-eject.path usb-exporter-format.path
    udevadm control --reload-rules
    [ -f "$CTL/status" ] || /usr/local/sbin/usb-exporter-status none
}

setup_nfs() {
    local server="$1" export_path="$2"
    command -v mount.nfs >/dev/null || { msg "Installing nfs-common on the host"; apt-get install -y -qq nfs-common >/dev/null; }
    sed -i "\#[[:space:]]$BASE/music[[:space:]]#d" /etc/fstab
    echo "$server:$export_path $BASE/music nfs ro,_netdev,nofail,soft,timeo=100,x-systemd.before=pve-guests.service 0 0" >> /etc/fstab
    systemctl daemon-reload
    mountpoint -q "$BASE/music" && umount "$BASE/music" || true
    mount "$BASE/music" || die "could not mount $server:$export_path - check the NFS share on the NAS allows this host ($(hostname -I | awk '{print $1}'))"
}

detect_port() {
    # Prints only the port prefix on stdout; everything for the user goes to stderr.
    local before after dev path a
    before=$(lsblk -dnro NAME,TRAN | awk '$2=="usb"{print $1}' | sort)
    {
        echo
        echo "Plug a USB drive into the port you want to use (e.g. the front one)."
        echo "${c_dim}If a drive is already in that port, unplug it first, press Enter, then plug it in.${c_off}"
    } >&2
    read -r -p "Press Enter, then plug the drive in (or type 'skip' to accept any USB port): " a </dev/tty
    [ "${a:-}" = skip ] && { echo ""; return; }
    echo "Waiting for a new USB drive (60 s)..." >&2
    for _ in $(seq 60); do
        sleep 1
        after=$(lsblk -dnro NAME,TRAN | awk '$2=="usb"{print $1}' | sort)
        dev=$(comm -13 <(echo "$before") <(echo "$after") | head -1)
        [ -n "$dev" ] && break
    done
    [ -n "${dev:-}" ] || die "no new USB drive seen - run the script again"
    udevadm settle
    path=$(udevadm info -q property -n "/dev/$dev" | sed -n 's/^ID_PATH=//p')
    [ -n "$path" ] || die "could not read the USB port of /dev/$dev"
    # pci-0000:00:14.0-usb-0:2:1.0-scsi-0:0:0:0 -> pci-0000:00:14.0-usb-0:2:  (the physical port)
    path="${path%%-scsi-*}"
    path="${path%:*}:"
    echo "Found /dev/$dev ($(lsblk -dnro MODEL "/dev/$dev" | head -1)) on port ${path}" >&2
    echo "$path"
}

# ------------------------------------------------------------ container ----
container_env() {
    local data_dir="$1" playlist_dir="$2" prefix="$3"
    cat <<EOF
MUSIC_ROOT=/mnt/music
SOURCE_ROOT=/mnt/music/$data_dir
PLAYLIST_DIR=/mnt/music/$playlist_dir
PATH_MAP=$prefix=/mnt/music
USB_ROOT=/mnt/usb/drive
CTL_DIR=/mnt/usb-ctl
PORT=8080
EOF
}

run_in_ct() {
    # Keeps the container's own git source (e.g. a Gitea remote set for auto-deploy).
    local ctid="$1" src
    src=$(pct exec "$ctid" -- git -C /opt/usb-exporter remote get-url origin 2>/dev/null) || src="${GIT_URL:-$REPO}"
    curl -fsSL "$RAW/install/lxc-install.sh" -o /tmp/usb-exporter-lxc-install.sh
    pct push "$ctid" /tmp/usb-exporter-lxc-install.sh /root/lxc-install.sh
    pct exec "$ctid" -- bash /root/lxc-install.sh "$src" "$BRANCH"
}

ct_ip() {
    local ip=''
    for _ in $(seq 30); do
        ip=$(pct exec "$1" -- hostname -I 2>/dev/null | awk '{print $1}')
        [ -n "$ip" ] && break
        sleep 1
    done
    echo "$ip"
}

do_update() {
    local ctid="$1"
    # shellcheck disable=SC1090
    [ -f "$CONF" ] && . "$CONF"
    msg "Refreshing the host side"
    write_host_files "${PORT_MATCH:-}"
    msg "Updating the app in container $ctid"
    pct status "$ctid" | grep -q running || pct start "$ctid"
    run_in_ct "$ctid"
    msg "Done: http://$(ct_ip "$ctid"):8080"
}

do_install() {
    local ctid hn storage tstore bridge mem disk nfs_server nfs_export data_dir pl_dir prefix port tmpl
    echo; msg "USB playlist exporter - new container"
    ctid=$(ask "Container ID" "$(pvesh get /cluster/nextid)")
    hn=$(ask "Hostname" "usb-exporter")
    echo "${c_dim}Storages for the container disk: $(pvesm status -content rootdir | awk 'NR>1{print $1}' | xargs)${c_off}"
    storage=$(ask "Container storage" "$(pvesm status -content rootdir | awk 'NR==2{print $1}')")
    tstore=$(ask "Template storage" "$(pvesm status -content vztmpl | awk 'NR==2{print $1}')")
    bridge=$(ask "Network bridge (DHCP)" "vmbr0")
    mem=$(ask "Memory (MB)" "512")
    disk=$(ask "Disk (GB)" "4")

    echo; msg "NAS music share (NFS)"
    nfs_server=$(ask "NFS server (NAS IP or name)")
    [ -n "$nfs_server" ] || die "an NFS server is required"
    command -v showmount >/dev/null || apt-get install -y -qq nfs-common >/dev/null
    echo "${c_dim}Exports on $nfs_server:${c_off}"; showmount -e "$nfs_server" 2>/dev/null | tail -n +2 || warn "could not list exports"
    nfs_export=$(ask "Export path of the music share (the folder holding data/ and playlists/)")
    [ -n "$nfs_export" ] || die "an export path is required"
    data_dir=$(ask "Songs folder inside the share" "data")
    pl_dir=$(ask "Playlists folder inside the share" "playlists")
    prefix=$(ask "Path prefix used in the .m3u files for that share" 'Z:\')

    echo; msg "USB port"
    port=$(detect_port)
    [ -n "$port" ] || warn "no port chosen: drives in ANY USB port will be mounted for the container"

    echo
    echo "Container $ctid ($hn) on $storage, ${mem} MB, ${disk} GB, $bridge/DHCP, unprivileged"
    echo "NAS:  $nfs_server:$nfs_export  (songs: $data_dir, playlists: $pl_dir, prefix: $prefix)"
    echo "USB:  ${port:-any port}"
    yes_no "Create it?" || die "cancelled"

    msg "Host: NFS mount"
    mkdir -p "$BASE/music"
    setup_nfs "$nfs_server" "$nfs_export"
    [ -d "$BASE/music/$pl_dir" ] || warn "$BASE/music/$pl_dir not found on the share"

    msg "Host: USB mount, eject and udev rule"
    write_host_files "$port"
    mkdir -p "$(dirname "$CONF")"
    printf 'CTID=%s\nPORT_MATCH=%q\nNFS_SERVER=%s\nNFS_EXPORT=%q\n' "$ctid" "$port" "$nfs_server" "$nfs_export" > "$CONF"

    msg "Template"
    pveam update >/dev/null || warn "pveam update failed, using the local template list"
    tmpl=$(pveam available --section system | awk '/debian-12-standard/{print $2}' | sort -V | tail -1)
    [ -n "$tmpl" ] || die "no debian-12-standard template available"
    pveam list "$tstore" | grep -q "$tmpl" || pveam download "$tstore" "$tmpl" >/dev/null

    msg "Creating container $ctid"
    pct create "$ctid" "$tstore:vztmpl/$tmpl" --hostname "$hn" --unprivileged 1 \
        --cores 1 --memory "$mem" --swap 256 --rootfs "$storage:$disk" \
        --net0 "name=eth0,bridge=$bridge,ip=dhcp" --onboot 1 --features nesting=1 \
        --tags "$TAG" --description "USB playlist exporter - https://github.com/$REPO" >/dev/null
    pct set "$ctid" -mp0 "$BASE/music,mp=/mnt/music,ro=1" -mp1 "$CTL,mp=/mnt/usb-ctl"
    echo "lxc.mount.entry: $BASE/usb mnt/usb none rbind,create=dir,rslave 0 0" >> "/etc/pve/lxc/$ctid.conf"

    msg "Starting container"
    pct start "$ctid"
    sleep 3
    container_env "$data_dir" "$pl_dir" "$prefix" > /tmp/usb-exporter.env
    pct push "$ctid" /tmp/usb-exporter.env /etc/usb-exporter.env
    pct exec "$ctid" -- bash -c 'for i in $(seq 30); do getent hosts github.com >/dev/null && exit 0; sleep 1; done; exit 1' \
        || die "container has no network/DNS"
    msg "Installing the app"
    run_in_ct "$ctid"

    echo
    msg "Ready: http://$(ct_ip "$ctid"):8080"
    echo "  Plug a drive into ${port:+the chosen }USB port, open the page, tick playlists, Export, then Eject."
    echo "  Update later: run this script again (or 'update' inside the container)."
}

# no match = grep exits 1, which pipefail would turn into a silent exit on a first install
existing=$(grep -l "^tags:.*\b$TAG\b" /etc/pve/lxc/*.conf 2>/dev/null | head -1 | xargs -r basename | sed 's/\.conf$//') || true
if [ -n "$existing" ] && [ "${1:-}" != "install" ]; then
    echo "Found the usb-exporter container: $existing"
    if yes_no "Update it?"; then do_update "$existing"; exit 0; fi
    yes_no "Install a NEW container instead?" || exit 0
fi
do_install
