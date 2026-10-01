#!/usr/bin/env python3
"""
mac_migrate.py - copy an old Mac's home folder to Ubuntu, prove the copy is
complete, and port the iTunes library (playlists, ratings, format checks).

Runs on the UBUNTU machine. The Mac is only ever READ: files are pulled with
rsync over SSH, and checksums are computed on the Mac by a small read-only
Python script streamed over SSH. Nothing on the Mac is modified or deleted.

Steps (each is a sub-command; run with -h for options):
  preflight   check the connection, count what will be copied, check free space
  transfer    copy the home folder (minus Applications/caches); safe to re-run
  manifest    compute a SHA-256 checksum of every file ON THE MAC
  verify      check every copied file against the Mac's checksums
  repair      re-copy only the files that failed verification, then re-verify
  music       port the iTunes library: playlists (.m3u8), ratings CSV, DRM
              report, format/playability tests
  flac        optional: make lossless FLAC copies of Apple Lossless tracks and
              prove they decode to identical audio (originals untouched)
  nfc         optional: convert Mac-style accented file names to Linux style
              (dry run unless --apply)
  all         preflight -> transfer -> manifest -> verify (+repair) -> music

First run:   python3 mac_migrate.py all --host 192.168.1.50 --user jake
Settings are saved in <dest>/.migrate/config.json, so later commands only
need the command name (e.g. `python3 mac_migrate.py verify`).
"""
import argparse, csv, datetime, hashlib, heapq, json, os, plistlib, random, re
import shlex, shutil, subprocess, sys, threading, time, unicodedata
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

DEFAULT_DEST = os.path.expanduser("~/mac-transfer")
# Paths relative to the Mac home folder that are skipped (apps + junk).
DEFAULT_EXCLUDES = ["Applications", ".Trash", "Library/Caches", "Library/Logs",
                    "Library/Saved Application State"]
EXCLUDE_NAMES = [".DS_Store"]
RETRYABLE_RSYNC = {10, 11, 12, 30, 35, 255}

# --------------------------------------------------------------------------
# Read-only walker that runs ON THE MAC (Python 2.7 on El Capitan, or 3.x).
# It only lists and reads files; it never writes anything on the Mac.
# --------------------------------------------------------------------------
WALKER = r'''
import os, sys, json, hashlib, stat, time, heapq
CFG = json.loads(__CFG__)
PY2 = sys.version_info[0] == 2
root = os.path.expanduser(CFG["root"])
if PY2 and not isinstance(root, str):
    root = root.encode("utf-8")
ANCH = CFG["anchored"]; NAMES = set(CFG["names"]); HASH = CFG["mode"] == "hash"
def u(b):
    if PY2:
        try: return b.decode("utf-8"), False
        except UnicodeDecodeError: return b.decode("latin-1"), True
    return b, False
def emit(o): sys.stdout.write(json.dumps(o) + "\n")
def excluded(rel, name):
    if name in NAMES: return True
    for a in ANCH:
        if rel == a or rel.startswith(a + "/"): return True
    return False
def sha(path):
    h = hashlib.sha256(); f = open(path, "rb")
    try:
        while True:
            b = f.read(1048576)
            if not b: break
            h.update(b)
    finally: f.close()
    return h.hexdigest()
S = {"files": 0, "bytes": 0, "errors": 0, "links": 0, "special": 0}
T0 = [time.time(), time.time()]
tops = {}; subs = {}; big = []
def rel_of(full):
    return u(os.path.relpath(full, root))[0]
def err(rel, e):
    S["errors"] += 1; emit({"t": "e", "p": rel, "err": str(e)})
def onerr(e):
    fn = getattr(e, "filename", None) or root
    try: err(rel_of(fn), e)
    except Exception: err(repr(fn), e)
def progress(final=False):
    now = time.time()
    if final or now - T0[1] >= 15:
        T0[1] = now; el = max(now - T0[0], 1e-3)
        sys.stderr.write("  [mac] %d files, %.2f GB %s (%.0f MB/s), %d unreadable\n" % (
            S["files"], S["bytes"] / 1e9, "checksummed" if HASH else "counted",
            S["bytes"] / 1e6 / el, S["errors"]))
        sys.stderr.flush()
for dp, dns, fns in os.walk(root, onerror=onerr):
    reld = "" if dp == root else rel_of(dp)
    keep = []
    for d in sorted(dns):
        full = os.path.join(dp, d); name = u(d)[0]
        rel = reld + "/" + name if reld else name
        if excluded(rel, name): continue
        if os.path.islink(full):
            S["links"] += 1; emit({"t": "l", "p": rel, "target": u(os.readlink(full))[0]}); continue
        keep.append(d)
    dns[:] = keep
    for f in sorted(fns):
        full = os.path.join(dp, f); name, bad = u(f)
        rel = reld + "/" + name if reld else name
        if excluded(rel, name): continue
        try: st = os.lstat(full)
        except OSError as e: err(rel, e); continue
        if stat.S_ISLNK(st.st_mode):
            S["links"] += 1; emit({"t": "l", "p": rel, "target": u(os.readlink(full))[0]}); continue
        if not stat.S_ISREG(st.st_mode):
            S["special"] += 1; continue
        rec = {"t": "f", "p": rel, "s": st.st_size, "m": int(st.st_mtime)}
        if bad: rec["bad"] = 1
        if HASH:
            try: rec["h"] = sha(full)
            except (IOError, OSError) as e: err(rel, e); continue
            emit(rec)
        elif not os.access(full, os.R_OK):
            err(rel, "permission denied"); continue
        S["files"] += 1; S["bytes"] += st.st_size
        parts = rel.split("/")
        top = parts[0] if len(parts) > 1 else "(loose files in home folder)"
        tops[top] = tops.get(top, 0) + st.st_size
        if len(parts) > 2:
            k = parts[0] + "/" + parts[1]; subs[k] = subs.get(k, 0) + st.st_size
        if len(big) < 25: heapq.heappush(big, (st.st_size, rel))
        elif st.st_size > big[0][0]: heapq.heapreplace(big, (st.st_size, rel))
        progress()
progress(True)
S.update({"t": "sum", "tops": tops, "elapsed": time.time() - T0[0], "py": sys.version.split()[0],
          "subs": sorted(subs.items(), key=lambda kv: -kv[1])[:40],
          "big": sorted(big, reverse=True)})
emit(S)
'''


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def gb(n): return "%.2f GB" % (n / 1e9)
def now_stamp(): return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
def die(msg): print("\nERROR: " + msg, file=sys.stderr); sys.exit(2)
def head(title): print("\n=== %s ===" % title)


class Ctx:
    """Settings (saved in <dest>/.migrate/config.json) + how to reach the Mac."""
    def __init__(self, args):
        self.dest = os.path.abspath(os.path.expanduser(getattr(args, "dest", None) or DEFAULT_DEST))
        self.state = os.path.join(self.dest, ".migrate")
        self.home = os.path.join(self.dest, "home")          # copy of the Mac home folder
        os.makedirs(self.state, exist_ok=True)
        self.cfg_path = os.path.join(self.state, "config.json")
        cfg = json.load(open(self.cfg_path)) if os.path.exists(self.cfg_path) else {}
        for key in ("host", "user", "port", "source", "mac_home", "remote_rsync",
                    "legacy_ssh", "ssh_opt", "jobs"):
            val = getattr(args, key, None)
            if val not in (None, [], False):
                cfg[key] = val
        if getattr(args, "exclude", None):
            cfg["excludes"] = sorted(set(cfg.get("excludes", DEFAULT_EXCLUDES) + args.exclude))
        cfg.setdefault("excludes", list(DEFAULT_EXCLUDES))
        self.cfg = cfg
        json.dump(cfg, open(self.cfg_path, "w"), indent=2)

    def p(self, name): return os.path.join(self.state, name)
    @property
    def local(self): return self.cfg.get("source")
    @property
    def target(self):
        h = self.cfg.get("host")
        if not h and not self.local:
            die("Tell me where the Mac is: --host <ip> --user <mac username>  "
                "(or --source /path/to/mounted/home for a mounted Mac disk)")
        return ("%s@%s" % (self.cfg["user"], h)) if self.cfg.get("user") else h

    def ssh_opts(self):
        os.makedirs(os.path.expanduser("~/.ssh"), mode=0o700, exist_ok=True)
        o = ["-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=10",
             "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/mac-migrate-%C",
             "-o", "ControlPersist=20m",
             # chacha20 is much faster than AES on a Core 2 Duo (no AES-NI)
             "-o", "Ciphers=chacha20-poly1305@openssh.com,aes128-ctr,aes128-gcm@openssh.com"]
        if self.cfg.get("port"): o += ["-p", str(self.cfg["port"])]
        if self.cfg.get("legacy_ssh"):
            o += ["-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa"]
        for extra in self.cfg.get("ssh_opt", []): o += ["-o", extra]
        return o

    def ssh(self, command, **kw):
        return subprocess.run(["ssh"] + self.ssh_opts() + [self.target, command], **kw)

    def run_walker(self, mode, out_path):
        """Run the read-only walker on the Mac; stream JSON lines to out_path."""
        wcfg = {"root": self.local or "~", "mode": mode,
                "anchored": self.cfg["excludes"], "names": EXCLUDE_NAMES}
        script = WALKER.replace("__CFG__", repr(json.dumps(wcfg)))
        if self.local:
            cmd = [sys.executable, "-"]
        else:
            cmd = ["ssh"] + self.ssh_opts() + [self.target,
                   'PY=$(command -v python || command -v python3); exec "$PY" -']
        tmp = out_path + ".part"
        with open(tmp, "w") as f:
            r = subprocess.run(cmd, input=script, stdout=f, text=True)
        if r.returncode != 0:
            die("the scan on the Mac failed (exit %d); see messages above" % r.returncode)
        summary = None
        with open(tmp) as f:
            for line in f:
                o = json.loads(line)
                if o.get("t") == "sum":
                    summary = o
        if not summary:
            die("the scan on the Mac ended early (no summary line)")
        os.replace(tmp, out_path)
        return summary

    def rsync_base(self):
        cmd = ["rsync", "-rltp", "--chmod=u+rwX", "--partial", "--partial-dir=.rsync-partial",
               "--timeout=900"]
        if not self.local:
            cmd += ["-e", "ssh " + " ".join(shlex.quote(x) for x in self.ssh_opts())]
            if self.cfg.get("remote_rsync"):
                cmd += ["--rsync-path", self.cfg["remote_rsync"]]
        for a in self.cfg["excludes"]: cmd.append("--exclude=/" + a)
        for n in EXCLUDE_NAMES: cmd.append("--exclude=" + n)
        return cmd

    def rsync_src(self):
        return self.local.rstrip("/") + "/" if self.local else self.target + ":"

    def mac_home(self):
        if self.cfg.get("mac_home"): return self.cfg["mac_home"]
        if self.local: return "/Users/" + os.path.basename(self.local.rstrip("/"))
        return None


def local_rsync_new():
    try:
        out = subprocess.run(["rsync", "--version"], capture_output=True, text=True).stdout
        m = re.search(r"version\s+(\d+)\.(\d+)", out)
        return m and (int(m.group(1)), int(m.group(2))) >= (3, 1)
    except FileNotFoundError:
        die("rsync is not installed on Ubuntu: sudo apt install rsync")


def run_rsync(cmd, retries, log_path):
    """Run rsync, retrying on network drops. Re-runs resume where they stopped."""
    for attempt in range(1, retries + 2):
        print("\n$ " + " ".join(shlex.quote(c) for c in cmd[:3]) + " ...  (log: %s)" % log_path)
        rc = subprocess.run(cmd).returncode
        if rc == 0:
            return 0
        if rc in (23, 24):
            print("\nrsync finished, but some files could not be read or vanished on the Mac "
                  "(exit %d). 'verify' will list them." % rc)
            return rc
        if rc in RETRYABLE_RSYNC and attempt <= retries:
            print("\nConnection problem (rsync exit %d); retrying in 30 s (attempt %d of %d). "
                  "Nothing already copied is lost." % (rc, attempt + 1, retries + 1))
            time.sleep(30); continue
        die("rsync stopped with exit code %d. Re-run the same command to resume." % rc)


# --------------------------------------------------------------------------
# preflight / transfer / manifest
# --------------------------------------------------------------------------
def cmd_preflight(args, ctx):
    head("Preflight")
    for tool, hint in (("rsync", "sudo apt install rsync"), ("ffprobe", "sudo apt install ffmpeg")):
        print("  %-9s %s" % (tool, "ok" if shutil.which(tool) else "MISSING -> " + hint))
    if not ctx.local:
        r = ctx.ssh('echo "HOME=$HOME"; echo "OS=$(sw_vers -productVersion 2>/dev/null)"; '
                    'echo "RSYNC=$(%s --version 2>/dev/null | head -1)"; '
                    'echo "PY=$( (python -V || python3 -V) 2>&1 | head -1)"'
                    % (ctx.cfg.get("remote_rsync") or "rsync"), capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stderr)
            die("could not SSH to %s. On the Mac: System Preferences > Sharing > Remote Login "
                "must be on. If the error mentions 'host key' or 'ssh-rsa', add --legacy-ssh."
                % ctx.target)
        info = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
        print("  Mac: macOS %s, home %s\n       %s\n       %s" % (
            info.get("OS") or "?", info.get("HOME"), info.get("RSYNC") or "rsync NOT FOUND",
            info.get("PY")))
        if not info.get("RSYNC"): die("rsync not found on the Mac")
        ctx.cfg["mac_home"] = info.get("HOME")
        json.dump(ctx.cfg, open(ctx.cfg_path, "w"), indent=2)
    print("  Excluding: " + ", ".join(ctx.cfg["excludes"]))
    print("\nCounting files on the Mac (read-only; a few minutes for a full disk)...")
    s = ctx.run_walker("count", ctx.p("preflight.jsonl"))
    print("\n  %d files, %s to copy. %d symlinks, %d unreadable items."
          % (s["files"], gb(s["bytes"]), s["links"], s["errors"]))
    print("\n  Size by folder:")
    for k, v in sorted(s["tops"].items(), key=lambda kv: -kv[1])[:20]:
        print("    %10s  %s" % (gb(v), k))
    print("\n  Biggest sub-folders:")
    for k, v in s["subs"][:12]:
        print("    %10s  %s" % (gb(v), k))
    if s["errors"]:
        print("\n  Unreadable on the Mac (will not be copied; first 10, full list in preflight.jsonl):")
        n = 0
        for line in open(ctx.p("preflight.jsonl")):
            o = json.loads(line)
            if o.get("t") == "e":
                print("    " + o["p"] + "  (" + o["err"][:80] + ")")
                n += 1
                if n >= 10: break
    probe = ctx.dest
    while not os.path.exists(probe): probe = os.path.dirname(probe)
    free = shutil.disk_usage(probe).free
    need = s["bytes"] * 1.02
    print("\n  Free space at %s: %s; needed: about %s -> %s" % (
        ctx.dest, gb(free), gb(need), "OK" if free > need else "NOT ENOUGH SPACE"))
    print("  Estimated copy time: %.1f h over gigabit Ethernet, %.1f h over Wi-Fi"
          % (s["bytes"] / 50e6 / 3600, s["bytes"] / 8e6 / 3600))
    if free <= need:
        die("free up space on Ubuntu or add --exclude for large folders you don't need")
    return s


def cmd_transfer(args, ctx):
    head("Transfer (Mac -> %s)" % ctx.home)
    os.makedirs(ctx.home, exist_ok=True)
    log = ctx.p("rsync-%s.log" % now_stamp())
    cmd = ctx.rsync_base() + ["--stats", "-h", "--log-file=" + log]
    cmd += ["--info=progress2"] if local_rsync_new() else ["--progress"]
    cmd += [ctx.rsync_src(), ctx.home + "/"]
    return run_rsync(cmd, args.retries, log)


def cmd_manifest(args, ctx):
    head("Checksum manifest (computed on the Mac, read-only)")
    print("This reads every file on the Mac once more. Please don't use the Mac meanwhile.")
    s = ctx.run_walker("hash", ctx.p("manifest.jsonl"))
    print("\n  %d files, %s checksummed in %.0f min; %d unreadable."
          % (s["files"], gb(s["bytes"]), s["elapsed"] / 60, s["errors"]))
    return s


# --------------------------------------------------------------------------
# verify / repair
# --------------------------------------------------------------------------
CATS = {
    "photos": "jpg jpeg png heic gif tif tiff bmp cr2 nef arw dng orf raf psd",
    "video": "mov mp4 m4v avi mkv mpg mpeg 3gp mts m2ts wmv dv",
    "music/audio": "mp3 m4a m4p aac aif aiff wav flac m4b aa aax caf",
    "documents": "pdf doc docx xls xlsx ppt pptx key pages numbers txt rtf tex bib md odt",
    "research data": "csv tsv mat h5 hdf5 nc dat sav dta rds rdata sqlite db npy npz json xml fits",
    "archives/disk images": "zip gz tgz bz2 xz 7z rar dmg iso tar sparseimage",
}
EXT2CAT = {e: c for c, exts in CATS.items() for e in exts.split()}


def load_manifest(ctx):
    path = ctx.p("manifest.jsonl")
    if not os.path.exists(path): die("no manifest yet - run: manifest")
    files, links, errors = {}, {}, []
    for line in open(path):
        o = json.loads(line)
        if o["t"] == "f": files[o["p"]] = o
        elif o["t"] == "l": links[o["p"]] = o["target"]
        elif o["t"] == "e": errors.append(o)
    return files, links, errors


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def cmd_verify(args, ctx):
    head("Verify copy against the Mac's checksums")
    files, links, mac_errors = load_manifest(ctx)
    cache_path = ctx.p("verify_cache.json")   # dest file -> [size, mtime_ns, sha]
    cache = {} if args.rehash or not os.path.exists(cache_path) else json.load(open(cache_path))
    problems, todo = [], []
    for p, o in files.items():
        full = os.path.join(ctx.home, p)
        try: st = os.lstat(full)
        except OSError:
            problems.append((p, "missing", "")); continue
        if not os.path.isfile(full) or os.path.islink(full):
            problems.append((p, "missing", "not a regular file")); continue
        if st.st_size != o["s"]:
            problems.append((p, "size-mismatch", "Mac %d bytes, Ubuntu %d" % (o["s"], st.st_size)))
            continue
        c = cache.get(p)
        if c and c[0] == st.st_size and c[1] == st.st_mtime_ns and not args.quick:
            if c[2] != o["h"]: problems.append((p, "checksum-mismatch", "changed since copied"))
            continue
        if not args.quick:
            todo.append((p, o, st))
    for p, tgt in links.items():
        full = os.path.join(ctx.home, p)
        if not os.path.islink(full): problems.append((p, "link-missing", tgt))
        elif os.readlink(full) != tgt: problems.append((p, "link-differs", tgt))

    total = sum(st.st_size for _, _, st in todo)
    done = [0]; lock = threading.Lock(); t0 = time.time(); last = [t0]
    def check(item):
        p, o, st = item
        try: h = sha256_file(os.path.join(ctx.home, p))
        except OSError as e: return p, "missing", str(e)
        with lock:
            cache[p] = [st.st_size, st.st_mtime_ns, h]
            done[0] += st.st_size
            if time.time() - last[0] > 15:
                last[0] = time.time()
                el = time.time() - t0
                print("  checked %s of %s (%.0f MB/s)" % (gb(done[0]), gb(total), done[0] / 1e6 / el))
        if h != o["h"]:
            return p, "checksum-mismatch", "content differs from the Mac"
        return None
    if todo:
        print("Checksumming %d files (%s) on Ubuntu..." % (len(todo), gb(total)))
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            for r in ex.map(check, todo):
                if r: problems.append(r)
        json.dump(cache, open(cache_path, "w"))

    known = set(files) | set(links)
    extras = []
    for dp, dns, fns in os.walk(ctx.home):
        dns[:] = [d for d in dns if d != ".rsync-partial"]
        for n in fns + [d for d in dns if os.path.islink(os.path.join(dp, d))]:
            rel = os.path.relpath(os.path.join(dp, n), ctx.home)
            if rel not in known: extras.append(rel)

    counts = {}
    for _, st, _ in problems: counts[st] = counts.get(st, 0) + 1
    report = {"time": now_stamp(), "files_in_manifest": len(files), "links": len(links),
              "bytes": sum(o["s"] for o in files.values()), "quick": args.quick,
              "problems": [{"p": p, "status": s, "detail": d} for p, s, d in problems],
              "counts": counts, "mac_unreadable": mac_errors, "extras": len(extras)}
    json.dump(report, open(ctx.p("verify_report.json"), "w"), indent=1)
    with open(ctx.p("verify_problems.tsv"), "w", errors="surrogateescape") as f:
        for p, s, d in problems: f.write("%s\t%s\t%s\n" % (s, p, d))
        for e in mac_errors: f.write("unreadable-on-mac\t%s\t%s\n" % (e["p"], e["err"]))
    write_inventory(ctx, files)

    print("\n  Files expected:   %d (%s)" % (len(files), gb(report["bytes"])))
    print("  Verified OK:      %d" % (len(files) + len(links) - len(problems)))
    for s, n in sorted(counts.items()): print("  %-17s %d" % (s + ":", n))
    for p, s, d in problems[:15]: print("     %s  %s  %s" % (s, p, d))
    if mac_errors:
        print("  Unreadable on the Mac (never copied): %d  -> listed in verify_problems.tsv"
              % len(mac_errors))
    if extras:
        print("  On Ubuntu but not on the Mac: %d (harmless; e.g. from an earlier run)" % len(extras))
    ok = not problems
    print("\n  RESULT: %s%s" % ("PASS - every file on the Mac is on Ubuntu, byte-for-byte"
                               if ok else "FAIL - run: repair",
                               " (size-only check)" if args.quick else ""))
    print("  Reports: %s/verify_problems.tsv, inventory.txt" % ctx.state)
    return ok


def write_inventory(ctx, files):
    tops, cats, big = {}, {}, []
    for p, o in files.items():
        top = p.split("/")[0] if "/" in p else "(loose files)"
        tops[top] = tops.get(top, 0) + o["s"]
        ext = os.path.splitext(p)[1][1:].lower()
        c = EXT2CAT.get(ext, "other")
        cats[c] = cats.get(c, 0) + o["s"]
        heapq.heappush(big, (o["s"], p))
        if len(big) > 30: heapq.heappop(big)
    with open(ctx.p("inventory.txt"), "w", errors="surrogateescape") as f:
        f.write("What was copied - to help decide what to trim later\n\nBy folder:\n")
        for k, v in sorted(tops.items(), key=lambda kv: -kv[1]): f.write("  %10s  %s\n" % (gb(v), k))
        f.write("\nBy type:\n")
        for k, v in sorted(cats.items(), key=lambda kv: -kv[1]): f.write("  %10s  %s\n" % (gb(v), k))
        f.write("\nLargest files:\n")
        for s, p in sorted(big, reverse=True): f.write("  %10s  %s\n" % (gb(s), p))


def cmd_repair(args, ctx):
    head("Repair: re-copy files that failed verification")
    rp = ctx.p("verify_report.json")
    if not os.path.exists(rp): die("run verify first")
    paths = [x["p"] for x in json.load(open(rp))["problems"]]
    if not paths:
        print("Nothing to repair."); return True
    lst = ctx.p("repair_list.bin")
    with open(lst, "wb") as f:
        for p in paths: f.write(os.fsencode(p) + b"\0")
    print("Re-copying %d files..." % len(paths))
    cmd = ctx.rsync_base() + ["-I", "--files-from=" + lst, "--from0", "--stats", "-h",
                              ctx.rsync_src(), ctx.home + "/"]
    run_rsync(cmd, args.retries, ctx.state)
    ok = cmd_verify(args, ctx)
    if not ok:
        print("\nStill failing? If a file was changed on the Mac after 'manifest' ran, "
              "re-run 'manifest' then 'verify'. Files unreadable on the Mac can't be copied.")
    return ok


# --------------------------------------------------------------------------
# music: port the iTunes library and test it
# --------------------------------------------------------------------------
MEDIA_EXT = {".m4a", ".m4p", ".mp3", ".aac", ".aif", ".aiff", ".wav", ".m4v", ".mp4", ".mov",
             ".m4b", ".aa", ".aax", ".flac", ".caf"}
DRM_TAGS = {"drms", "drmi", "enca", "encv"}
# GStreamer elements (used by Rhythmbox, Lollypop, Strawberry, ...) per codec
GST = {"aac": ["avdec_aac", "faad", "fdkaacdec"], "alac": ["avdec_alac"],
       "mp3": ["mpg123audiodec", "avdec_mp3", "mad"], "flac": ["flacdec"],
       "vorbis": ["vorbisdec"], "opus": ["opusdec"], "h264": ["avdec_h264", "openh264dec"],
       "mpeg4": ["avdec_mpeg4"], "ac3": ["avdec_ac3", "a52dec"]}
_gst_cache = {}


def gst_can_play(codec):
    if codec.startswith("pcm_"): codec = "pcm"
    if codec == "pcm": return True
    if not shutil.which("gst-inspect-1.0"): return None
    if codec not in GST: return None
    if codec not in _gst_cache:
        _gst_cache[codec] = any(subprocess.run(["gst-inspect-1.0", "--exists", e]).returncode == 0
                                for e in GST[codec])
    return _gst_cache[codec]


def probe(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                            "-show_streams", path], capture_output=True, text=True,
                           errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        return {"error": "ffprobe timed out"}
    try: j = json.loads(r.stdout or "{}")
    except ValueError: j = {}
    st, fmt = j.get("streams", []), j.get("format", {})
    audio = [s for s in st if s.get("codec_type") == "audio"]
    art = [s for s in st if s.get("disposition", {}).get("attached_pic")]
    video = [s for s in st if s.get("codec_type") == "video" and s not in art]
    a = audio[0] if audio else {}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    return {"error": r.stderr.strip()[:200] if (r.returncode or not st) else "",
            "codec": a.get("codec_name", ""), "vcodec": video[0].get("codec_name", "") if video else "",
            "duration": float(fmt.get("duration") or a.get("duration") or 0),
            "bits": int(a.get("bits_per_raw_sample") or a.get("bits_per_sample") or 0),
            "has_art": bool(art), "tags": tags,
            "drm": any(s.get("codec_tag_string") in DRM_TAGS for s in st)}


def deep_decode(path):
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", "0:a:0",
                        "-f", "null", "-"], capture_output=True, text=True, errors="replace")
    return (r.stderr.strip() or ("ffmpeg exit %d" % r.returncode if r.returncode else ""))[:200]


def norm(s): return " ".join(unicodedata.normalize("NFC", str(s or "")).casefold().split())


class Resolver:
    """Map a Mac path from the iTunes XML to the copied file on Ubuntu.
    Tolerates Mac case-insensitivity and accented-character (NFD/NFC) differences."""
    def __init__(self, home, mac_home):
        self.home, self.mac_home, self.cache = home, mac_home.rstrip("/"), {}
    def listing(self, d):
        if d not in self.cache:
            try: self.cache[d] = {norm(n): n for n in os.listdir(d)}
            except OSError: self.cache[d] = {}
        return self.cache[d]
    def resolve(self, mac_path):
        if not mac_path.startswith(self.mac_home + "/"): return None, "outside-home"
        cur = self.home
        for part in mac_path[len(self.mac_home) + 1:].split("/"):
            cand = os.path.join(cur, part)
            if os.path.lexists(cand): cur = cand; continue
            real = self.listing(cur).get(norm(part))
            if real is None: return None, "missing"
            cur = os.path.join(cur, real)
        return (cur, "ok") if os.path.isfile(cur) else (None, "missing")


def location_to_path(loc):
    u = urllib.parse.urlparse(loc)
    return urllib.parse.unquote(u.path) if u.scheme == "file" else None


def find_library_xml(ctx, given):
    if given: return given
    base = os.path.join(ctx.home, "Music", "iTunes")
    for name in ("Library.xml", "iTunes Library.xml", "iTunes Music Library.xml"):
        if os.path.exists(os.path.join(base, name)): return os.path.join(base, name)
    for dp, dns, fns in os.walk(os.path.join(ctx.home, "Music")):
        for n in fns:
            if n.endswith(".xml") and os.path.getsize(os.path.join(dp, n)) > 1000:
                with open(os.path.join(dp, n), "rb") as f:
                    if b"<key>Tracks</key>" in f.read(4096): return os.path.join(dp, n)
    return None


def load_plist(path):
    data = open(path, "rb").read()
    try: return plistlib.loads(data)
    except Exception:  # iTunes sometimes writes control characters XML parsers reject
        return plistlib.loads(re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", data))


def safe_name(s, used):
    s = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", s).strip(" .")[:120] or "playlist"
    base, i = s, 2
    while s.lower() in used: s = "%s (%d)" % (base, i); i += 1
    used.add(s.lower())
    return s


def cmd_music(args, ctx):
    head("iTunes library port + tests")
    if not shutil.which("ffprobe"): die("install ffmpeg first: sudo apt install ffmpeg")
    out = os.path.join(ctx.dest, "music-port")
    os.makedirs(out, exist_ok=True)
    xml = find_library_xml(ctx, args.xml)
    if not xml:
        die("No iTunes library XML found in the copy. On the Mac: iTunes > File > Library > "
            "Export Library..., save it as Library.xml in Music/iTunes, then run 'repair' or "
            "'transfer' again (it copies just the new file) and re-run 'music'.")
    print("Library file: " + xml)
    lib = load_plist(xml)
    mac_home = ctx.mac_home()
    if not mac_home:
        mf = location_to_path(lib.get("Music Folder", "")) or ""
        m = re.match(r"(/Users/[^/]+)", mf)
        mac_home = m.group(1) if m else die("can't tell the Mac home folder; pass --mac-home /Users/NAME")
    res = Resolver(ctx.home, mac_home)
    flac_map = {}
    if os.path.exists(os.path.join(out, "flac_map.json")):
        flac_map = json.load(open(os.path.join(out, "flac_map.json")))

    # 1. resolve every track to a file on Ubuntu
    tracks = {}
    for tid, t in lib.get("Tracks", {}).items():
        rec = {"t": t, "path": None, "status": "", "warn": []}
        loc = t.get("Location")
        if not loc or t.get("Track Type") in ("Remote", "URL"):
            rec["status"] = "cloud-only"          # iTunes Match/Apple Music/stream: no local file
        else:
            mp = location_to_path(loc)
            rec["mac_path"] = mp
            rec["path"], rec["status"] = res.resolve(mp) if mp else (None, "missing")
        tracks[str(tid)] = rec
    local = [r for r in tracks.values() if r["path"]]
    # A track that is missing on Ubuntu AND absent from the Mac's manifest was already
    # broken in iTunes (the "!" tracks) - not a transfer failure.
    if os.path.exists(ctx.p("manifest.jsonl")):
        mac_files = set()
        for line in open(ctx.p("manifest.jsonl")):
            o = json.loads(line)
            if o["t"] == "f": mac_files.add(norm(o["p"]))
        for r in tracks.values():
            mp = r.get("mac_path") or ""
            if r["status"] == "missing" and mp.startswith(mac_home + "/") \
                    and norm(mp[len(mac_home) + 1:]) not in mac_files:
                r["status"] = "missing-on-mac"

    # 2. probe each file (format, duration, tags, artwork, DRM) + optional full decode
    deep_set = set()
    if args.deep:
        pool = [r["path"] for r in local]
        deep_set = set(pool if not args.sample else random.Random(1).sample(pool, min(args.sample, len(pool))))
    print("Inspecting %d tracks with ffprobe%s..." % (len(local), " + full decode of %d" % len(deep_set) if deep_set else ""))
    def work(r):
        r["probe"] = probe(r["path"])
        if r["path"] in deep_set and not r["probe"]["drm"] and r["probe"]["codec"]:
            r["decode_err"] = deep_decode(r["path"])
        return r
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        list(ex.map(work, local))

    for r in local:
        t, pr = r["t"], r["probe"]
        if t.get("Protected") or pr["drm"] or "Protected" in t.get("Kind", ""):
            r["status"] = "protected"
        elif pr["error"] and not pr["codec"]:
            r["status"] = "unreadable"; r["warn"].append("ffprobe: " + pr["error"])
        elif r.get("decode_err"):
            r["status"] = "decode-errors"; r["warn"].append("decode: " + r["decode_err"])
        if t.get("Size") and os.path.getsize(r["path"]) != t["Size"]:
            r["warn"].append("size differs from library")
        if t.get("Total Time") and pr["duration"]:
            if abs(t["Total Time"] / 1000 - pr["duration"]) > max(2, 0.01 * t["Total Time"] / 1000):
                r["warn"].append("duration differs (%.0fs vs %.0fs)" % (t["Total Time"] / 1000, pr["duration"]))
        for key, tag in (() if r["status"] == "protected" or pr["vcodec"] else
                         (("Name", "title"), ("Artist", "artist"), ("Album", "album"))):
            if t.get(key) and norm(t[key]) != norm(pr["tags"].get(tag)):
                r["warn"].append("tag %s %s in file" % (tag, "missing" if not pr["tags"].get(tag) else "differs"))
        if not pr["has_art"] and not pr["vcodec"]:
            r["warn"].append("no embedded artwork")

    # 3. whole-home DRM scan (.m4p/.m4v/... anywhere, in the library or not)
    in_lib = {os.path.realpath(r["path"]) for r in local}
    drm_rows = []
    for dp, dns, fns in os.walk(ctx.home):
        dns[:] = [d for d in dns if d != ".rsync-partial"]
        for n in fns:
            if os.path.splitext(n)[1].lower() in (".m4p", ".m4v", ".m4b", ".aa", ".aax"):
                fp = os.path.join(dp, n)
                pr = probe(fp)
                drm_rows.append([os.path.relpath(fp, ctx.home), "PROTECTED" if pr["drm"] or n.lower().endswith((".m4p", ".aa", ".aax")) else "playable",
                                 pr["codec"] or pr["vcodec"], "yes" if os.path.realpath(fp) in in_lib else "no"])
    with open(os.path.join(out, "protected_media.csv"), "w", newline="", errors="surrogateescape") as f:
        w = csv.writer(f); w.writerow(["file", "status", "codec", "in iTunes library"])
        w.writerows(sorted(drm_rows))

    # 4. library.csv - everything iTunes knew (ratings, play counts, ...)
    cols = ["Name", "Artist", "Album Artist", "Album", "Genre", "Year", "Track Number", "Disc Number",
            "Kind", "Rating", "Loved", "Play Count", "Skip Count", "Play Date UTC", "Date Added"]
    with open(os.path.join(out, "library.csv"), "w", newline="", errors="surrogateescape") as f:
        w = csv.writer(f)
        w.writerow(["Track ID"] + cols + ["Stars", "Status", "Codec", "Ubuntu Path", "Notes"])
        for tid, r in tracks.items():
            t = r["t"]
            stars = "" if t.get("Rating Computed") or not t.get("Rating") else t["Rating"] // 20
            w.writerow([tid] + [t.get(c, "") for c in cols] +
                       [stars, r["status"], r.get("probe", {}).get("codec", ""), r["path"] or r.get("mac_path", ""),
                        "; ".join(r["warn"])])

    # 5. playlists -> .m3u8 (and a FLAC-preferring set if 'flac' was run)
    pls = lib.get("Playlists", [])
    by_pid = {p["Playlist Persistent ID"]: p for p in pls if p.get("Playlist Persistent ID")}
    def full_name(p):
        names, cur, guard = [], p, 0
        while cur is not None and guard < 20:
            names.append(cur.get("Name", ""))
            parent = cur.get("Parent Persistent ID")
            cur = by_pid.get(parent) if parent else None
            guard += 1
        return " - ".join(reversed(names))
    pl_stats = {"written": 0, "entries": 0, "unresolved": 0}
    for sub, prefer_flac in (("playlists", False), ("playlists-flac", True)):
        if prefer_flac and not flac_map: continue
        pdir = os.path.join(out, sub)
        if os.path.isdir(pdir): shutil.rmtree(pdir)
        os.makedirs(pdir)
        used = set()
        for p in pls:
            if p.get("Master") or p.get("Folder") or p.get("Visible") is False: continue
            if p.get("Distinguished Kind") and p.get("Name") != "Purchased": continue
            lines = ["#EXTM3U"]
            for it in p.get("Playlist Items", []):
                r = tracks.get(str(it.get("Track ID")))
                if not r: continue
                t = r["t"]
                label = "%s - %s" % (t.get("Artist", ""), t.get("Name", ""))
                if not r["path"] or r["status"] in ("protected",):
                    lines.append("# SKIPPED (%s): %s" % (r["status"] or "missing", label))
                    if not prefer_flac and r["status"] == "missing":
                        pl_stats["unresolved"] += 1
                    continue
                path = flac_map.get(r["path"], r["path"]) if prefer_flac else r["path"]
                lines += ["#EXTINF:%d,%s" % ((t.get("Total Time") or 0) // 1000, label), path]
                if not prefer_flac: pl_stats["entries"] += 1
            with open(os.path.join(pdir, safe_name(full_name(p), used) + ".m3u8"), "w",
                      encoding="utf-8", errors="surrogateescape") as f:
                f.write("\n".join(lines) + "\n")
            if not prefer_flac: pl_stats["written"] += 1

    # 6. tests
    broken = 0
    for sub in ("playlists", "playlists-flac"):
        pdir = os.path.join(out, sub)
        if not os.path.isdir(pdir): continue
        for fn in os.listdir(pdir):
            for line in open(os.path.join(pdir, fn), encoding="utf-8", errors="surrogateescape"):
                line = line.rstrip("\n")
                if line and not line.startswith("#") and not os.path.isfile(line): broken += 1

    st_counts, codec_counts, warn_counts = {}, {}, {}
    for r in tracks.values():
        s = r["status"] if r["status"] not in ("", "ok") else "ok"
        st_counts[s] = st_counts.get(s, 0) + 1
        for w in r["warn"]:
            k = re.sub(r"\(.*\)", "", w.split(":")[0]).strip()
            warn_counts[k] = warn_counts.get(k, 0) + 1
    for r in local:
        c = r["probe"]["codec"] or r["probe"]["vcodec"] or "?"
        if r["status"] == "protected": c = "DRM (" + c + ")" if c != "?" else "DRM"
        codec_counts[c] = codec_counts.get(c, 0) + 1
    fails = st_counts.get("missing", 0) + st_counts.get("unreadable", 0) + st_counts.get("decode-errors", 0) + broken

    lines = []
    P = lines.append
    P("iTunes library port - %s" % now_stamp())
    P("Library: %s\n  %d tracks, %d playlists exported" % (xml, len(tracks), pl_stats["written"]))
    P("\nTRACK STATUS")
    labels = {"missing-on-mac": "already missing on the Mac (broken in iTunes; not a copy problem)",
              "missing": "MISSING on Ubuntu but present on the Mac -> run repair",
              "cloud-only": "in the cloud only (no file on the Mac)",
              "outside-home": "stored outside the home folder (e.g. external drive) - not copied"}
    for k in ("ok", "protected", "cloud-only", "outside-home", "missing-on-mac", "missing", "unreadable", "decode-errors"):
        if st_counts.get(k): P("  %-14s %4d  %s" % (k, st_counts[k], labels.get(k, "")))
    P("\nFORMATS (files found)")
    for c, n in sorted(codec_counts.items(), key=lambda kv: -kv[1]):
        if c.startswith("DRM"): verdict = "copy-protected - Apple apps only (see protected_media.csv)"
        else:
            g = gst_can_play(c)
            verdict = {True: "plays on this Ubuntu (GStreamer players)",
                       False: "needs codecs: sudo apt install gstreamer1.0-libav ubuntu-restricted-extras",
                       None: "not checked (VLC/mpv play it with ffmpeg)"}[g]
        P("  %-12s %6d   %s" % (c, n, verdict))
    P("\nMETADATA / INTEGRITY WARNINGS (per track details in library.csv 'Notes')")
    for k, n in sorted(warn_counts.items(), key=lambda kv: -kv[1]): P("  %-32s %d" % (k, n))
    rated = sum(1 for r in tracks.values() if r["t"].get("Rating") and not r["t"].get("Rating Computed"))
    played = sum(1 for r in tracks.values() if r["t"].get("Play Count"))
    P("  (ratings: %d tracks, play counts: %d tracks -> kept in library.csv; these live only"
      " in the iTunes database, never in the files)" % (rated, played))
    prot = [r for r in drm_rows if r[1] == "PROTECTED"]
    P("\nDRM SCAN (.m4p/.m4v/.m4b/.aa anywhere in home): %d files, %d protected, %d play fine"
      % (len(drm_rows), len(prot), len(drm_rows) - len(prot)))
    P("\nPLAYLISTS: %d written to %s/playlists (%d entries); %d entries skipped as missing"
      % (pl_stats["written"], out, pl_stats["entries"], pl_stats["unresolved"]))
    P("  playlist-entry test: %s" % ("PASS - every entry points to an existing file" if not broken
                                    else "FAIL - %d entries point to missing files" % broken))
    if args.deep: P("  full-decode test: %d decoded, %d with errors" % (len(deep_set), st_counts.get("decode-errors", 0)))
    P("\nRESULT: %s" % ("PASS - library ported; every local, unprotected track was found and reads correctly"
                       if not fails else "FAIL - %d problems; see library.csv (Status column)" % fails))
    report = "\n".join(lines)
    open(os.path.join(out, "music_report.txt"), "w", errors="surrogateescape").write(report + "\n")
    print("\n" + report)
    print("\nOutputs in %s: music_report.txt, library.csv, protected_media.csv, playlists/" % out)
    return not fails


# --------------------------------------------------------------------------
# optional: ALAC -> FLAC copies, verified bit-exact
# --------------------------------------------------------------------------
def pcm_md5(path):
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-map", "0:a:0",
                        "-c:a", "pcm_s32le", "-f", "md5", "-"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def cmd_flac(args, ctx):
    head("Apple Lossless -> FLAC (originals untouched)")
    if not shutil.which("ffmpeg"): die("sudo apt install ffmpeg")
    out_root = os.path.join(ctx.dest, "music-flac")
    music = os.path.join(ctx.home, "Music")
    todo = []
    for dp, dns, fns in os.walk(music):
        for n in fns:
            if n.lower().endswith(".m4a"):
                fp = os.path.join(dp, n)
                if probe(fp)["codec"] == "alac": todo.append(fp)
    print("%d Apple Lossless files found." % len(todo))
    fmap_path = os.path.join(ctx.dest, "music-port", "flac_map.json")
    os.makedirs(os.path.dirname(fmap_path), exist_ok=True)
    fmap = json.load(open(fmap_path)) if os.path.exists(fmap_path) else {}
    def conv(src):
        dst = os.path.join(out_root, os.path.splitext(os.path.relpath(src, ctx.home))[0] + ".flac")
        if fmap.get(src) == dst and os.path.exists(dst): return src, dst, "done earlier"
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        base = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src, "-map", "0:a:0", "-map_metadata", "0",
                "-c:a", "flac"]
        r = subprocess.run(base[:-2] + ["-map", "0:v?", "-c:v", "copy", "-disposition:v", "attached_pic",
                                        "-c:a", "flac", dst], capture_output=True, text=True)
        if r.returncode != 0:  # retry without cover art
            r = subprocess.run(base + [dst], capture_output=True, text=True)
            if r.returncode != 0: return src, None, "conversion failed: " + r.stderr[:150]
        a, b = pcm_md5(src), pcm_md5(dst)
        if not a or a != b:
            os.remove(dst); return src, None, "AUDIO MISMATCH - FLAC deleted, original kept"
        return src, dst, "ok (identical audio)"
    bad = 0
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        for src, dst, msg in ex.map(conv, todo):
            if dst: fmap[src] = dst
            else: bad += 1; print("  FAILED %s: %s" % (src, msg))
    json.dump(fmap, open(fmap_path, "w"), indent=1)
    print("\n%d converted and verified bit-identical, %d failed. FLAC files in %s"
          % (len(todo) - bad, bad, out_root))
    print("Re-run 'music' to get playlists-flac/ pointing at the FLAC versions.")
    return bad == 0


# --------------------------------------------------------------------------
# optional: Mac (NFD) -> Linux (NFC) file names
# --------------------------------------------------------------------------
def cmd_nfc(args, ctx):
    head("Accented file names: Mac style (NFD) -> Linux style (NFC)%s"
         % ("" if args.apply else "  [DRY RUN - add --apply to rename]"))
    renames, clashes = [], []
    for dp, dns, fns in os.walk(ctx.home, topdown=False):
        for n in fns + dns:
            nn = unicodedata.normalize("NFC", n)
            if nn != n:
                (clashes if os.path.lexists(os.path.join(dp, nn)) else renames).append((dp, n, nn))
    for dp, n, nn in renames[:20]: print("  %s  ->  %s" % (os.path.join(os.path.relpath(dp, ctx.home), n), nn))
    print("%d names to change, %d skipped because the new name already exists." % (len(renames), len(clashes)))
    if not args.apply or not renames: return True
    for dp, n, nn in renames: os.rename(os.path.join(dp, n), os.path.join(dp, nn))
    # keep the manifest/cache in step so 'verify' still works
    mp = ctx.p("manifest.jsonl")
    if os.path.exists(mp):
        shutil.copy(mp, mp + ".before-nfc")
        with open(mp) as f: lines = [json.loads(l) for l in f]
        with open(mp, "w") as f:
            for o in lines:
                if "p" in o: o["p"] = unicodedata.normalize("NFC", o["p"])
                f.write(json.dumps(o) + "\n")
    if os.path.exists(ctx.p("verify_cache.json")): os.remove(ctx.p("verify_cache.json"))
    print("Renamed. Run 'verify' again (full re-check), then 'music' to rebuild playlists.")
    return True


# --------------------------------------------------------------------------
def cmd_all(args, ctx):
    cmd_preflight(args, ctx)
    cmd_transfer(args, ctx)
    cmd_manifest(args, ctx)
    ok = cmd_verify(args, ctx)
    if not ok:
        ok = cmd_repair(args, ctx)
    if not ok:
        die("verification still failing - see %s/verify_problems.tsv" % ctx.state)
    try:
        return cmd_music(args, ctx)
    except SystemExit:
        print("(music step skipped - see message above; the file copy itself is done and verified)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("where things are (saved after first use)")
    g.add_argument("--dest", help="Ubuntu folder for the copy (default ~/mac-transfer)")
    g.add_argument("--host", help="Mac IP address or hostname")
    g.add_argument("--user", help="Mac username (run `whoami` on the Mac)")
    g.add_argument("--port", type=int)
    g.add_argument("--source", help="instead of SSH: path of the Mac home folder on a mounted disk")
    g.add_argument("--mac-home", dest="mac_home", help="Mac home path, e.g. /Users/jake (auto-detected)")
    g.add_argument("--exclude", action="append", help="extra folder to skip, relative to home, e.g. 'Movies/Old'")
    g.add_argument("--legacy-ssh", action="store_true", help="allow old ssh-rsa keys if SSH refuses to connect")
    g.add_argument("--remote-rsync", dest="remote_rsync", help=argparse.SUPPRESS)
    g.add_argument("--ssh-opt", dest="ssh_opt", action="append", help=argparse.SUPPRESS)
    o = common.add_argument_group("options")
    o.add_argument("--jobs", type=int, default=4, help="parallel checks (use 1-2 if Ubuntu disk is a hard drive)")
    o.add_argument("--retries", type=int, default=5, help="automatic resumes after network drops")
    o.add_argument("--quick", action="store_true", help="verify: compare sizes only (no checksums)")
    o.add_argument("--rehash", action="store_true", help="verify: re-checksum everything, ignore cache")
    o.add_argument("--xml", help="music: path to the exported iTunes Library.xml")
    o.add_argument("--deep", action="store_true", help="music: fully decode tracks to find corruption")
    o.add_argument("--sample", type=int, help="music --deep: only decode this many random tracks")
    o.add_argument("--apply", action="store_true", help="nfc: actually rename (default is dry run)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    cmds = {"preflight": cmd_preflight, "transfer": cmd_transfer, "manifest": cmd_manifest,
            "verify": cmd_verify, "repair": cmd_repair, "music": cmd_music, "flac": cmd_flac,
            "nfc": cmd_nfc, "all": cmd_all}
    for name in cmds: sub.add_parser(name, parents=[common])
    args = ap.parse_args()
    ctx = Ctx(args)
    result = cmds[args.cmd](args, ctx)
    sys.exit(1 if result is False else 0)


if __name__ == "__main__":
    main()