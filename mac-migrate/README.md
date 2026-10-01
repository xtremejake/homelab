# mac-migrate

Copies an old Mac home folder to Ubuntu over SSH, verifies the copy byte-for-byte with SHA-256 checksums, and ports the iTunes library (playlists, ratings, format checks) to Linux-compatible formats.

The Mac is **read-only throughout** — files are pulled with rsync and checksums are computed on the Mac by a small script streamed over SSH. Nothing on the Mac is changed or deleted.

## Requirements

**Ubuntu** (the machine running the script):
```bash
sudo apt install rsync ffmpeg gstreamer1.0-libav
```
Python 3, no extra packages. Works under conda.

**Mac**: nothing to install. El Capitan ships with rsync 2.6.9 and Python 2.7; the script handles both.

## Before you start (Mac)

1. **System Preferences → Sharing → Remote Login**: on.
2. **Export iTunes library**: File → Library → Export Library… → save as `Music/iTunes/Library.xml`.
3. **Quit all apps** including menu-bar apps (Dropbox, Chrome, etc.) — open files cause checksum mismatches.
4. Keep the Mac awake: open Terminal and run `caffeinate -dims`, leave it running.

## Connect the two computers

**Both on the router** (simplest): get the Mac's IP with `ipconfig getifaddr en0`, use it with `--host`.

**Direct Ethernet cable** (faster):
```bash
# Mac — set a static address (persists across reboots)
sudo networksetup -setmanual Ethernet 10.10.10.1 255.255.255.0

# Ubuntu — create a static connection (leviathan's port is enp0s31f6; check with `ip -br link`)
sudo nmcli con add type ethernet ifname enp0s31f6 con-name mac-direct \
  ipv4.method manual ipv4.addresses 10.10.10.2/24 ipv4.never-default yes ipv6.method ignore
sudo nmcli con up mac-direct
ping -c 3 10.10.10.1
```

## Run it

Settings are saved after the first run, so `--host` and `--user` are only needed once.

```bash
# Dry run: test connection, count files, check free space
python3 mac_migrate.py preflight --host 10.10.10.1 --user jakemarold

# Full run (copy → checksum → verify → repair → music), blocking sleep
systemd-inhibit --what=sleep:idle --why="Mac transfer" python3 mac_migrate.py all
```

Keep Ubuntu awake for the duration — a full run takes hours.

| Command | What it does | Writes? |
|---------|-------------|---------|
| `preflight` | SSH test, file count, size breakdown, free-space check | No |
| `transfer` | rsync copy; safe to re-run, retries on drop | Ubuntu only |
| `manifest` | SHA-256 every file on the Mac (read-only) | No |
| `verify` | Compare copies against Mac checksums | No |
| `repair` | Re-copy failures, re-verify | Ubuntu only |
| `music` | Port iTunes library: playlists (.m3u8), ratings CSV, DRM/format report | Ubuntu only |
| `flac` | Optional: lossless ALAC → FLAC, bit-exact check | Ubuntu only |
| `nfc` | Optional: fix Mac-style accented filenames (dry-run unless `--apply`) | Ubuntu only |
| `all` | preflight → transfer → manifest → verify (+repair) → music | |

Useful options: `--exclude "Movies/Old"`, `--dest PATH`, `--legacy-ssh`, `--jobs 2`, `--deep`.

Interrupted? Re-run the same command — rsync picks up where it stopped.

## Reports

All in `~/mac-transfer/.migrate/`:

| File | Contents |
|------|----------|
| `verify_problems.tsv` | Every failed file: status, path, detail |
| `inventory.txt` | Sizes by folder and file type, largest files |
| `manifest.jsonl` | Mac's checksum of every file |
| `rsync-*.log` | Transfer logs |

Music reports in `~/mac-transfer/music-port/`:

| File | Contents |
|------|----------|
| `music_report.txt` | Track status, formats, warnings, PASS/FAIL |
| `library.csv` | Every track: rating, play count, codec, path |
| `protected_media.csv` | All DRM files and whether they're playable |
| `playlists/` | One .m3u8 per iTunes playlist |

## Verification results

`RESULT: PASS` = every readable Mac file is on Ubuntu, byte-for-byte identical.

Files under `Library/` that keep failing (e.g. `*.ldb`, `*.sqlite-wal`) belong to apps still running on the Mac. Quit them, re-run `manifest` then `verify`.

## Transferring to Windows / iCloud

No iCloud app exists for Linux, so the route is **Ubuntu → Windows → iCloud for Windows**.

### Ubuntu: share the folder read-only via Samba

```bash
sudo apt install samba
sudo tee -a /etc/samba/smb.conf >/dev/null <<'EOF'

[mac-transfer]
   path = /home/xtremejake/mac-transfer
   read only = yes
   browseable = yes
   valid users = xtremejake
   follow symlinks = no
EOF
sudo smbpasswd -a xtremejake
sudo systemctl restart smbd
sudo ufw allow samba   # if ufw is active
```

### Windows: copy with robocopy

Keep Windows awake (Administrator PowerShell):
```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

Connect via router (use `ip -br addr` for Ubuntu's IP) or direct cable:
```powershell
New-NetIPAddress -InterfaceAlias "Ethernet" -IPAddress 10.10.10.3 -PrefixLength 24
Test-NetConnection 10.10.10.2 -Port 445   # should show TcpTestSucceeded : True
```

Copy (change `D:\MacArchive` to wherever you have space):
```powershell
robocopy \\10.10.10.2\mac-transfer\home D:\MacArchive /E /Z /MT:8 /R:3 /W:10 /COPY:DT /DCOPY:T /XJ `
  /LOG+:D:\robocopy-mac.log /TEE /NP
```

Re-running copies only what's missing or changed. Check the summary at the end: `FAILED 0`.

### Verify the Windows copy

On Ubuntu, generate checksums:
```bash
cd ~/mac-transfer/home && find . -type f ! -path './.rsync-partial/*' -print0 \
  | xargs -0 sha256sum > ~/mac-transfer/windows-checksums.sha256
```

On Windows (PowerShell — use PowerShell 7 for long paths):
```powershell
$root = "D:\MacArchive"
$list = "\\10.10.10.2\mac-transfer\windows-checksums.sha256"
$n = 0; $bad = 0
Get-Content -LiteralPath $list -Encoding UTF8 | ForEach-Object {
    $hash, $rel = $_ -split '  ', 2
    $rel  = ($rel -replace '^\./', '') -replace '/', '\'
    $path = Join-Path $root $rel
    $n++
    if (-not (Test-Path -LiteralPath $path)) { "MISSING   $rel"; $bad++ }
    elseif ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $hash) { "MISMATCH  $rel"; $bad++ }
} | Tee-Object -FilePath D:\verify-windows.log
"$n files checked, $bad problems"
```

Enable long paths if needed (Administrator PowerShell, then restart):
```powershell
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force
```

### Check for Windows-incompatible filenames first

```bash
cd ~/mac-transfer/home
find . -name '*[\\:*?"<>|]*'          # forbidden characters
find . -mindepth 1 \( -name '* ' -o -name '*.' \)   # trailing space/dot
find . | awk 'length($0) > 230'       # paths approaching 260-char limit
```

Rename any hits on Ubuntu before copying.

### Into iCloud

1. Install **iCloud for Windows** from the Microsoft Store, sign in, enable iCloud Drive and Photos.
2. Check your storage plan — free tier is 5 GB; files over 50 GB can't be uploaded.
3. **Documents**: copy folders from `D:\MacArchive` into the iCloud Drive folder in Explorer; wait for green ticks.
4. **Photos**: add via iCloud for Windows Photos settings using originals from `iPhoto Library/Masters/`.
5. **Music**: iCloud Drive works as a backup. For iTunes import on Windows, patch the XML paths first:
   ```bash
   sed 's#file://localhost/Users/jakemarold/#file://localhost/D:/MacArchive/#g' \
     ~/mac-transfer/home/Music/iTunes/Library.xml > ~/mac-transfer/Library-windows.xml
   ```
   Then in iTunes for Windows: File → Library → Import Playlist → `\\10.10.10.2\mac-transfer\Library-windows.xml`.
   Use **iTunes**, not the Apple Music app — the latter can't import a library.

## Cleanup

Only after both `verify` and `music` say PASS (and the Windows copy is verified):

```bash
# Ubuntu: remove the direct-cable connection
sudo nmcli con delete mac-direct
sudo nmcli con up "Wired connection 1"
```

```bash
# Mac: restore DHCP, then turn off Remote Login in Sharing
sudo networksetup -setdhcp Ethernet
```
