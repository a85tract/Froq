"""Verilator build/run helper shared by the hw/tests drivers.

Builds in an ASCII work directory without spaces ($SE_BUILD_ROOT/<name>, default <tmp>/froq_rtl_build/<name>),
copying the RTL and the testbench there.
"""
import os, shutil, subprocess, sys, tempfile
HW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RTL = os.environ.get("SE_RTL_DIR", os.path.join(HW, "rtl"))
TB = os.path.join(HW, "tb")
BUILD_ROOT = os.environ.get("SE_BUILD_ROOT", os.path.join(tempfile.gettempdir(), "froq_rtl_build"))

RTL_ORDER = []                               # no packages; the files in alphabetical order


def rtl_files():
    fs = sorted(f for f in os.listdir(RTL) if f.endswith(".sv") and f not in RTL_ORDER)
    return [f for f in RTL_ORDER if os.path.exists(os.path.join(RTL, f))] + fs


def build(name, top, tb_file, extra_flags=(), force=False, timeout=None):
    name = os.environ.get("SE_BUILD_PREFIX", "") + name
    wdir = os.path.join(BUILD_ROOT, name)
    os.makedirs(wdir, exist_ok=True)
    srcs = rtl_files()
    for f in srcs:                               # contents only: a deployed tree is read-only and its modes must not follow
        shutil.copyfile(os.path.join(RTL, f), os.path.join(wdir, f))
    shutil.copyfile(os.path.join(TB, tb_file), os.path.join(wdir, tb_file))
    exe = os.path.join(wdir, "obj_dir", "run")
    stamp = os.path.join(wdir, ".stamp")
    newest = max(os.path.getmtime(os.path.join(RTL, f)) for f in srcs)
    newest = max(newest, os.path.getmtime(os.path.join(TB, tb_file)))
    defines = os.environ.get("SE_VDEFINES", "")                 # part of the cache identity: -DSE_F_MAX=8 must not reuse the F_MAX = 4 binary
    ident = "defines=%s flags=%s" % (defines, " ".join(extra_flags))
    if not force and os.path.exists(exe) and os.path.exists(stamp) \
            and os.path.getmtime(stamp) >= newest and open(stamp).read() == ident:
        return exe
    cmd = (["verilator", "--binary", "--timing", "-Wno-fatal", "-Wno-lint"] + defines.split() + [
            "-Wno-BLKSEQ", "-Wno-WIDTH", "--top-module", top, "-o", "run",
            "-j", "4", "--x-assign", "0", "--x-initial", "0"]
           + list(extra_flags) + srcs + [tb_file])
    r = subprocess.run(cmd, cwd=wdir, capture_output=True, text=True, timeout=timeout)   # (timeout: default none)
    if r.returncode != 0:
        print(r.stdout[-4000:]); print(r.stderr[-4000:])
        raise SystemExit("verilator build failed for %s" % name)
    open(stamp, "w").write(ident)
    return exe


def run(exe, plusargs, timeout=3600):
    wdir = os.path.dirname(os.path.dirname(exe))
    cmd = [exe] + ["+%s=%s" % (k, v) for k, v in plusargs.items()]
    r = subprocess.run(cmd, cwd=wdir, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        print(r.stdout[-3000:]); print(r.stderr[-3000:])
        raise SystemExit("simulation failed")
    return r.stdout


def workdir(name):
    return os.path.join(BUILD_ROOT, os.environ.get("SE_BUILD_PREFIX", "") + name)
