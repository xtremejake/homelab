#!/usr/bin/env python3
"""
itc_art.py - put iTunes' hidden album artwork back INTO the song files.

iTunes kept downloaded cover art in its own .itc files (Music/iTunes/Album Artwork/...)
instead of inside the songs, so Apple Music for Windows, iCloud and other players
can't see it. This script:

  1. reads every .itc / .itc2 file and pulls out the image (JPEG/PNG/raw),
  2. matches it to tracks through the Persistent IDs in your iTunes Library.xml,
  3. copies each track that is missing artwork to an OUTPUT folder and embeds the
     cover there (originals are never modified),
  4. optionally fills other tracks of the same album with that album's cover,
  5. writes art_report.csv + a summary, and saves the extracted images so you
     can look at them.

Then copy the output folder over the Windows copy (robocopy) - only changed files.

Needs: Python 3, `pip install mutagen` (Pillow optional, for rare raw-pixel .itc
files). Put this file next to mac_migrate.py (it reuses its path matching).

  python3 itc_art.py --dry-run     # see what would happen
  python3 itc_art.py               # do it
"""
import argparse, csv, json, os, re, shutil, struct, sys, unicodedata
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from mac_migrate import Resolver, load_plist, location_to_path, norm
except ImportError:
    sys.exit("Put itc_art.py in the same folder as mac_migrate.py")
try:
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.id3 import ID3, APIC, ID3NoHeaderError
    from mutagen.aiff import AIFF
    from mutagen.flac import FLAC, Picture
except ImportError:
    sys.exit("Install mutagen first:  python3 -m pip install mutagen")

AUDIO_EXT = {".m4a", ".m4b", ".mp3", ".aif", ".aiff", ".flac"}
JPEG, PNG = "image/jpeg", "image/png"


# ---------------------------------------------------------------- .itc parsing
def _rgba_to_png(raw, w, h, argb):
    try:
        from PIL import Image
    except ImportError:
        return None
    if len(raw) < w * h * 4:
        return None
    img = Image.frombuffer("RGBA", (w, h), raw[: w * h * 4], "raw", "ARGB" if argb else "RGBA", 0, 1)
    import io
    buf = io.BytesIO(); img.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def _sniff(blob):
    """Find a JPEG/PNG inside a blob; return (mime, bytes) or None."""
    p = blob.find(b"\x89PNG\r\n\x1a\n")
    j = blob.find(b"\xff\xd8\xff")
    if p >= 0 and (j < 0 or p < j):
        end = blob.find(b"IEND", p)
        return PNG, blob[p: end + 8 if end > 0 else len(blob)]
    if j >= 0:
        end = blob.rfind(b"\xff\xd9")
        return JPEG, blob[j: end + 2 if end > j else len(blob)]
    return None


def parse_itc(path):
    """Return (images, ids). images: list of (mime, data, w, h); ids: hex persistent IDs seen."""
    data = open(path, "rb").read()
    images, ids = [], set()
    pos = 0x11C                                     # first 'item' block (iTunes 9+ layout)
    while pos + 0x40 <= len(data):
        size, tag, hdr = struct.unpack(">I4sI", data[pos:pos + 12])
        if tag != b"item" or size < 0x40 or pos + size > len(data):
            break
        lib_id, trk_id = data[pos + 0x10:pos + 0x18], data[pos + 0x18:pos + 0x20]
        ids.update({lib_id.hex().upper(), trk_id.hex().upper()})
        ftype = data[pos + 0x30:pos + 0x34]
        w, h = struct.unpack(">II", data[pos + 0x38:pos + 0x40])
        img = data[pos + hdr:pos + size] if hdr < size else b""
        if ftype in (b"ARGb", b"RGBA"):
            png = _rgba_to_png(img, w, h, ftype == b"ARGb")
            if png: images.append((PNG, png, w, h))
        else:
            s = _sniff(img)
            if s: images.append((s[0], s[1], w, h))
        pos += size
    if not images:                                  # unknown/older layout: just look for an image
        s = _sniff(data)
        if s: images.append((s[0], s[1], 0, 0))
    return images, ids


def best(images):
    return max(images, key=lambda im: (im[2] * im[3], len(im[1])))


# ---------------------------------------------------------------- embedding
def has_art(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in (".m4a", ".m4b"):
            t = MP4(path).tags; return bool(t and t.get("covr"))
        if ext == ".mp3":
            try: return bool(ID3(path).getall("APIC"))
            except ID3NoHeaderError: return False
        if ext in (".aif", ".aiff"):
            t = AIFF(path).tags; return bool(t and t.getall("APIC"))
        if ext == ".flac":
            return bool(FLAC(path).pictures)
    except Exception:
        return False
    return False


def embed(path, mime, data):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".m4a", ".m4b"):
        f = MP4(path)
        if f.tags is None: f.add_tags()
        fmt = MP4Cover.FORMAT_PNG if mime == PNG else MP4Cover.FORMAT_JPEG
        f.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
        f.save()
    elif ext == ".mp3":
        try: t = ID3(path)
        except ID3NoHeaderError: t = ID3()
        t.delall("APIC")
        t.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        t.save(path, v2_version=3)          # ID3v2.3: best supported by Windows/Apple apps
    elif ext in (".aif", ".aiff"):
        f = AIFF(path)
        if f.tags is None: f.add_tags()
        f.tags.delall("APIC")
        f.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        f.save()
    elif ext == ".flac":
        f = FLAC(path); f.clear_pictures()
        p = Picture(); p.type = 3; p.mime = mime; p.data = data
        f.add_picture(p); f.save()
    else:
        raise ValueError("format can't hold artwork")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", default=os.path.expanduser("~/mac-transfer/home"),
                    help="the copied Mac home folder (default ~/mac-transfer/home)")
    ap.add_argument("--xml", help="iTunes Library.xml (default: <home>/Music/iTunes/Library.xml)")
    ap.add_argument("--out", help="where to write updated tracks (default: <home>/../art-fixed)")
    ap.add_argument("--mac-home", help="Mac home path, e.g. /Users/jakemarold (auto-detected)")
    ap.add_argument("--no-album-fill", action="store_true",
                    help="only use a track's own artwork, don't share an album's cover across its tracks")
    ap.add_argument("--force", action="store_true", help="replace artwork that is already embedded")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    a = ap.parse_args()

    home = os.path.abspath(os.path.expanduser(a.home))
    base = os.path.dirname(home)
    out = os.path.abspath(a.out or os.path.join(base, "art-fixed"))
    xml = a.xml or next((os.path.join(home, "Music", "iTunes", n) for n in
                         ("Library.xml", "iTunes Library.xml", "iTunes Music Library.xml")
                         if os.path.exists(os.path.join(home, "Music", "iTunes", n))), None)
    if not xml: sys.exit("Library.xml not found - pass --xml")
    art_dir = os.path.join(home, "Music", "iTunes", "Album Artwork")
    if not os.path.isdir(art_dir): sys.exit("No 'Album Artwork' folder at " + art_dir)

    mac_home = a.mac_home
    cfg = os.path.join(base, ".migrate", "config.json")
    if not mac_home and os.path.exists(cfg):
        mac_home = json.load(open(cfg)).get("mac_home")
    lib = load_plist(xml)
    if not mac_home:
        m = re.match(r"(/Users/[^/]+)", location_to_path(lib.get("Music Folder", "")) or "")
        mac_home = m.group(1) if m else sys.exit("pass --mac-home /Users/NAME")
    res = Resolver(home, mac_home)

    # tracks
    tracks = {}
    for t in lib.get("Tracks", {}).values():
        pid = (t.get("Persistent ID") or "").upper()
        if not pid or not t.get("Location"): continue
        path, st = res.resolve(location_to_path(t["Location"]) or "")
        tracks[pid] = {"t": t, "path": path, "status": st}
    print("Library: %d tracks with files" % len(tracks))

    # artwork
    art, unmatched, n_itc, n_bad = {}, [], 0, 0
    for dp, dns, fns in os.walk(art_dir):
        for fn in fns:
            if not fn.lower().endswith((".itc", ".itc2")): continue
            n_itc += 1
            fp = os.path.join(dp, fn)
            try: images, ids = parse_itc(fp)
            except Exception: images, ids = [], set()
            if not images:
                n_bad += 1; continue
            stem = os.path.splitext(fn)[0]
            ids |= {p.upper() for p in stem.split("-") if re.fullmatch(r"[0-9A-Fa-f]{16}", p)}
            hit = [i for i in ids if i in tracks]
            if not hit:
                unmatched.append(fp); continue
            img = best(images)
            for pid in hit:
                if pid not in art or len(img[1]) > len(art[pid][1]):
                    art[pid] = (img[0], img[1], os.path.relpath(fp, home))
    print("Artwork files: %d .itc found, %d matched to tracks, %d unmatched, %d had no readable image"
          % (n_itc, len({v[2] for v in art.values()}), len(unmatched), n_bad))

    # album fill
    def album_key(t):
        alb = norm(t.get("Album"))
        if not alb: return None
        who = norm(t.get("Album Artist")) or ("__compilation__" if t.get("Compilation") else norm(t.get("Artist")))
        return who, alb
    album_art = {}
    if not a.no_album_fill:
        for pid, img in art.items():
            k = album_key(tracks[pid]["t"])
            if k and (k not in album_art or len(img[1]) > len(album_art[k][1])):
                album_art[k] = img

    rows, counts = [], defaultdict(int)
    ext_dir = os.path.join(out, "_extracted_art")
    for pid, r in tracks.items():
        t, path = r["t"], r["path"]
        name = "%s - %s - %s" % (t.get("Artist", ""), t.get("Album", ""), t.get("Name", ""))
        def row(status, src=""):
            counts[status] += 1; rows.append([status, name, path or "", src])
        if not path: row("file-missing"); continue
        ext = os.path.splitext(path)[1].lower()
        if t.get("Protected") or ext == ".m4p": row("skipped-protected"); continue
        if ext not in AUDIO_EXT: row("skipped-format"); continue
        img, how = art.get(pid), "own artwork"
        if not img:
            k = album_key(t)
            img, how = (album_art.get(k), "album artwork") if k else (None, "")
        if not img: row("no-artwork-found"); continue
        if has_art(path) and not a.force: row("already-had-artwork"); continue
        if a.dry_run: row("would-embed (%s)" % how, img[2]); continue
        dst = os.path.join(out, os.path.relpath(path, home))
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(path, dst)
            embed(dst, img[0], img[1])
            if not has_art(dst): raise RuntimeError("artwork not found after saving")
            row("embedded (%s)" % how, img[2])
        except Exception as e:
            if os.path.exists(dst): os.remove(dst)
            row("error: %s" % str(e)[:80], img[2])

    if not a.dry_run:
        os.makedirs(ext_dir, exist_ok=True)
        for pid, (mime, data, src) in art.items():
            open(os.path.join(ext_dir, pid + (".png" if mime == PNG else ".jpg")), "wb").write(data)
        with open(os.path.join(out, "_unmatched_itc.txt"), "w") as f:
            f.write("\n".join(unmatched) + "\n")
    rep = os.path.join(out if not a.dry_run else base, "art_report.csv")
    os.makedirs(os.path.dirname(rep), exist_ok=True)
    with open(rep, "w", newline="", errors="surrogateescape") as f:
        w = csv.writer(f); w.writerow(["status", "track", "file", "artwork source"]); w.writerows(sorted(rows))

    print("\nRESULTS%s" % ("  (DRY RUN - nothing written)" if a.dry_run else ""))
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]): print("  %-34s %d" % (k, v))
    print("\nReport: " + rep)
    if not a.dry_run:
        print("Updated tracks: %s  (same folder layout as the home folder)" % out)
        print("Extracted images for spot-checking: " + ext_dir)


if __name__ == "__main__":
    main()
