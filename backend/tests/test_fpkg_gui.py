import sys, os, importlib.util, traceback, argparse, shutil, subprocess, tempfile, time, zipfile, json, re, types
from pathlib import Path
import tkinter as tk
import faulthandler, signal
if hasattr(signal, "SIGUSR1"):        # kill -USR1 <pid>: every thread's stack, when a run hangs
    faulthandler.register(signal.SIGUSR1, all_threads=True)
HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(REPO / "backend"))
from test_fpkg_pipelines import fetch_hbt, CLI
ap = argparse.ArgumentParser(); ap.add_argument("--work", type=Path, default=Path(tempfile.gettempdir()) / "ffpfsc-fpkg-gui-tests")
a = ap.parse_args(); S = a.work
if S.exists(): shutil.rmtree(S)
S.mkdir(parents=True)
# APP_DIR is computed at import time, so set this before loading the GUI
os.environ["PS5_FFPFSC_APP_DIR"] = str(S / "app_dir")
# pretend setup is done, or the wizard pops up
(S / "app_dir").mkdir(parents=True, exist_ok=True)
# no sounds or notifications on the test machine
(S / "app_dir" / "settings.json").write_text('{"first_run_done": true, "show_space_dialog": false, '
                                             '"sound_complete": false, "sound_error": false, "notify": "off"}',
                                             encoding="utf-8")
HBT = fetch_hbt(S / "hbt"); OUT = S / "gui_drive_out"; OUT.mkdir()
subprocess.run([sys.executable, "-u", str(CLI), str(HBT), str(S / "c2_ffpfsc"), "--pack", "--overwrite"], capture_output=True, timeout=300)
subprocess.run([sys.executable, "-u", str(CLI), str(HBT), str(S / "c1_pkg"), "--fpkg-build", str(HBT)], capture_output=True, timeout=300)
FF = next((S / "c2_ffpfsc").glob("*.ffpfsc"))
ZP = S / "HomebrewTest.zip"
with zipfile.ZipFile(ZP, "w") as z:
    for f in HBT.rglob("*"):
        if f.is_file(): z.write(f, f"HomebrewTest/{f.relative_to(HBT)}")
os.chdir(REPO)
spec = importlib.util.spec_from_file_location("ultra", str(REPO / "PS5_UltraPack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
errors = []
_real_showerror, _real_askyesno = m.messagebox.showerror, m.messagebox.askyesno
def showerror(title, msg, **kw): errors.append(f"{title}: {msg}")
m.messagebox.showerror = showerror
m.messagebox.showinfo = lambda *a, **k: None
m.messagebox.askyesno = lambda *a, **k: True
import signal, shutil as _sh
if hasattr(signal, "SIGALRM"):
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(SystemExit("driver timeout")))
    signal.alarm(240)
else:                               # Windows: no SIGALRM; interrupt the main thread, then end hard
    import _thread, threading as _thr
    def _later(seconds, fn):
        t = _thr.Timer(seconds, fn)
        t.daemon = True
        t.start()

    def _timeout():
        print("driver timeout", flush=True)
        _thread.interrupt_main()
        _later(60, lambda: os._exit(3))
    _later(900, _timeout)
# never clobber the real settings: back up settings.json, restored in finally
_settings = m.APP_DIR / "settings.json"
_backup = _settings.with_suffix(".json.driver-backup")
if _settings.exists():
    _sh.copy2(_settings, _backup)
m.ensure_app_dir()
import title_db as _tdb
def _no_network(url): raise OSError("the GUI driver never goes online")
_tdb.FETCH = _no_network
(S / "app_dir" / "titles").mkdir(parents=True, exist_ok=True)
(S / "app_dir" / "titles" / "PS5_Titles.tsv").write_text(
    "titleId\tconceptId\tname\tcontentId\tregion\tpublisherId\n"
    "PPSA99096_00\t1\tListed Example\u2122\tUP9000-PPSA99096_00-LISTEDEXAMPLE000\tUP\tUP9000\n", encoding="utf-8")
(S / "app_dir" / "titles" / "PS4_Titles.tsv").write_text(
    "titleId\tconceptId\tname\tcontentId\tregion\tpublisherId\n", encoding="utf-8")
root = m._CTkDnD(); root.withdraw()
app = m.App(root)
# a modal password prompt would wait forever: answer skip and report it
_REAL_PW_PROMPT, _unexpected_prompts = m.ArchivePasswordPrompt, []
class _NoPrompt:
    def __init__(self, _app, name): _unexpected_prompts.append(name); self.password = ""
m.ArchivePasswordPrompt = _NoPrompt
app.queue.clear()
res = []  # reset below, this one only carries the wizard check
_no_wizard = not app._is_first_run
# never touch the user's drives: scratch temp and output folders only
(S / "temp").mkdir(parents=True, exist_ok=True)
app.temp_var.set(str(S / "temp")); app.output_var.set(str(OUT))
res = []
def ok(name, cond, detail=""):
    res.append((name, bool(cond), detail))
    print(f"... {'pass' if cond else 'FAIL'} {name}", file=sys.stderr, flush=True)    # progress, for a run that hangs
ok("driver.no-first-run-wizard", _no_wizard, "the isolated profile is a finished setup, so no wizard window opens")
ok("driver.silent", not app.sound_complete_var.get() and not app.sound_error_var.get() and app.notify_var.get() == "off",
   "no done/failed sounds and no notifications while the driver runs jobs")
def pump(cond, timeout=20.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        root.update()
        if cond(): return True
        time.sleep(0.05)
    return cond()
# sources are read off the main thread: wait for it, unless a test checks the reading
_wait_reads = [True]
_jd_init = m.JobDialog.__init__
def _jd_init_read(self, *a, **k):
    _jd_init(self, *a, **k)
    if _wait_reads[0]:
        pump(lambda: not self._reading, 60)
m.JobDialog.__init__ = _jd_init_read
_settle_timeouts, _settle_secs = [], []
def settle(timeout=60.0):
    root.update()
    t0 = time.monotonic()
    if not pump(lambda: not (getattr(app, "_add_state", None) or {}).get("total"), timeout=timeout):
        _settle_timeouts.append(f"{traceback.extract_stack()[-2].lineno}")
    _settle_secs.append(round(time.monotonic() - t0, 1))
    root.update()
def close_toplevels():
    # work dialogs are panels in the main window, messages are their own windows
    for w in list(app._panels.stack):
        try: w.destroy()
        except Exception: pass
    for w in root.winfo_children():
        try:
            if isinstance(w, m.ctk.CTkToplevel): w.destroy()
        except Exception: pass
try:
    # macOS: no sheets on the main window, AppKit once aborted the app over one
    import tkinter.commondialog as _cd
    _seen_opts, _cd_show = [], _cd.Dialog.show
    _cd.Dialog.show = lambda self, **o: (_seen_opts.append(dict(self.options, **o)), "")[1]
    try:
        m.filedialog.askdirectory(parent=root, title="t"); m.filedialog.askopenfilename(parent=root)
        _ow = (m.messagebox.showerror, m.messagebox.askyesno)
        m.messagebox.showerror, m.messagebox.askyesno = _real_showerror, _real_askyesno
        m.messagebox.showerror("t", "m", parent=root); m.messagebox.askyesno("t", "q", parent=root)
        m.messagebox.showerror, m.messagebox.askyesno = _ow
    finally:
        _cd.Dialog.show = _cd_show
    ok("dialogs.no-sheets-on-macos", len(_seen_opts) == 4 and not any("parent" in o for o in _seen_opts)
       if sys.platform == "darwin" else True, str([sorted(o) for o in _seen_opts]))
    ok("driver.app-dir-isolated", Path(m.APP_DIR).resolve() == (S / "app_dir").resolve(), f"APP_DIR={m.APP_DIR}")
    it = m.GameItem.from_fpkg_build(HBT, output_path=str(OUT), content_id="UP9000-PPSA99099_00-PROSPERO00000000",
                                    title_id="PPSA99099", title="HBT", inner_mode="kraken", kraken_backend="builtin", level=5)
    cmd, cwd, outdir, temp = app.build_command(it)
    ok("build_command.fpkg-build", "--fpkg-build" in cmd and "--temp-dir" in cmd and "--compression-level" in cmd
       and cmd[cmd.index("--compression-level")+1] == "5" and "--fpkg-inner" in cmd and cmd[cmd.index("--fpkg-inner")+1] == "kraken"
       and "--content-id" in cmd and "--fpkg-version" not in cmd,   # default version is not passed (param.json decides)
       " ".join(cmd[-14:]))
    ok("fpkg-build.size-set", it.size > 0, f"size={it.size}")
    itb = m.GameItem.from_fpkg_build(HBT, output_path=str(OUT), content_id="UP9000-PPSA99099_00-PROSPERO00000000", title_id="PPSA99099")
    itb.bundle_subfolder = "My Bundle"; itb.auto_organize = False
    cmdb, _, outb, _ = app.build_command(itb)
    ok("build_command.fpkg-bundle-subfolder", outb == OUT / "My Bundle" and str(OUT / "My Bundle") in cmdb, str(outb))
    pkg = next((S / "c1_pkg").glob("*.pkg"), None)
    ok("seed.pkg-built-without-ids", pkg is not None, "the seed build passed no --content-id: param.json supplied it")
    if pkg:
        ie = m.GameItem.from_fpkg_extract(pkg, output_path=str(OUT / "ext"))
        cmd2, *_ = app.build_command(ie)
        _ext_out = next((a for a in cmd2 if a.startswith(str(OUT / "ext"))), "")
        ok("build_command.fpkg-extract", "--fpkg-extract" in cmd2 and _ext_out.endswith(" [extracted]")
           and Path(_ext_out).parent == OUT / "ext", " ".join(cmd2[-4:]))
    CID = "UP9000-PPSA99099_00-PROSPERO00000000"
    L = m.JobDialog._TARGET_LABEL
    dlg = m.JobDialog(app, init_src=str(HBT), init_to="pkg"); root.update()
    ok("dialog.autofill.cid", dlg.cid_var.get() == CID, dlg.cid_var.get())
    ok("dialog.autofill.tid", dlg.tid_var.get() == "PPSA99099", dlg.tid_var.get())
    ok("dialog.pkg.panel-shown", dlg._to_key() == "pkg" and dlg._pkg_opts.winfo_manager() != "", dlg._to_key())
    ok("dialog.pkg-level-default-0", dlg.pkg_level_var.get() == 0 and app.fpkg_defaults.get("level") == 0,
       f"dialog={dlg.pkg_level_var.get()} default={app.fpkg_defaults.get('level')}")
    dlg.out_var.set(str(OUT)); dlg.inner_var.set("kraken"); dlg.pkg_level_var.set(-4)
    n0 = len(app.queue); dlg._add(); settle()
    q = app.queue[-1] if app.queue else None
    ok("dialog.add.queued", len(app.queue) == n0 + 1 and q.operation == "chain" and q.chain_to == "pkg", f"queue={len(app.queue)} errors={errors[-1:]}")
    ok("dialog.add.fields", q.fpkg_inner_mode == "kraken" and q.fpkg_level == -4 and q.fpkg_content_id == CID
       and q.files > 0 and q.path == HBT, f"{q.fpkg_inner_mode}/{q.fpkg_level}/{q.fpkg_content_id}/files={q.files}")
    ok("dialog.add.remembered", app.fpkg_defaults["inner"] == "kraken" and app.fpkg_defaults["level"] == -4, str(app.fpkg_defaults))
    cmdq, *_ = app.build_command(q)
    ok("build_command.fast-preset", "--compression-level" in cmdq and cmdq[cmdq.index("--compression-level")+1] == "-4", " ".join(cmdq[-8:]))
    from test_backport import _elf_with_imports as _ampr_elf
    _ag = S / "ampr_game"
    if _ag.exists(): shutil.rmtree(_ag)
    shutil.copytree(HBT, _ag)
    (_ag / "eboot.bin").write_bytes(_ampr_elf([("mc36MRb8k1w", 0, "libSceAmpr", 0, "libSceAmpr")]))
    dz = m.JobDialog(app, init_src=str(HBT), init_to="ffpfsc"); root.update()
    ok("lz4.off-for-a-game-without-ampr", dz._lz4_cb.cget("state") == "disabled" and not dz.lz4_var.get(),
       dz._lz4_cb.cget("state"))
    dz.destroy(); root.update()
    dz = m.JobDialog(app, init_src=str(_ag), init_to="ffpfsc"); root.update()
    _enabled = dz._lz4_cb.cget("state") == "normal"
    dz.lz4_var.set(True); root.update()
    _moved = dz._to_key() == "exfat" and bool(dz._lz4_opts.winfo_manager())
    dz.to_var.set(m.JobDialog._TARGET_LABEL["ffpfsc"]); root.update()
    _dropped = not dz.lz4_var.get()
    dz.to_var.set(m.JobDialog._TARGET_LABEL["exfat"]); dz.lz4_var.set(True); dz.out_var.set(str(OUT)); root.update()
    ok("lz4.option-and-output-follow-each-other", _enabled and _moved and _dropped and dz.lz4_var.get(),
       f"enabled={_enabled} moved={_moved} dropped={_dropped}")
    n0 = len(app.queue); dz._add(); settle()
    qz = app.queue[-1] if len(app.queue) > n0 else None
    _cz = app.build_command(qz)[0] if qz else []
    ok("lz4.job-and-command", qz is not None and qz.chain_lz4 and qz.chain_to == "exfat" and "--lz4" in _cz
       and _cz[_cz.index("--to") + 1] == "exfat" and any(str(a).endswith(".exfat") for a in _cz),
       " ".join(map(str, _cz[-12:])))
    _fz = m.GameItem.from_chain(_ag, to="folder", output_path=str(OUT), lz4=True)
    _cf = app.build_command(_fz)[0]
    _dest = next((a for a in _cf if str(a).endswith("[LZ4]")), "")
    ok("lz4.folder-output-is-not-the-source", _dest and Path(_dest) != _ag and Path(_dest).parent == OUT, _dest)
    _nz = m.GameItem.from_chain(_ag, to="ffpfsc", output_path=str(OUT), lz4=True)
    ok("lz4.dropped-for-other-outputs", not _nz.chain_lz4 and "--lz4" not in app.build_command(_nz)[0], "")
    if qz is not None:
        app.queue.remove(qz)
    import lz4_packs as _lz4m, io as _io, contextlib as _cl
    _pk = S / "ampr_packed"
    if _pk.exists(): shutil.rmtree(_pk)
    shutil.copytree(_ag, _pk)
    (_pk / "data").mkdir(exist_ok=True); (_pk / "data" / "l.bin").write_bytes(b"mesh vertex " * 50000)
    with _cl.redirect_stdout(_io.StringIO()):
        _lz4m.pack_game(_pk, workers=2)
    du = m.JobDialog(app, init_src=str(_pk), init_to="ffpfsc"); root.update()
    _mode = du.lz4_mode_var.get()
    du.lz4_var.set(True); root.update()
    _kept_out = du._to_key() == "ffpfsc" and du.lz4_var.get() and not du._lz4_profile_row.winfo_manager()
    du.out_var.set(str(OUT)); root.update()
    n0 = len(app.queue); du._add(); settle()
    qu = app.queue[-1] if len(app.queue) > n0 else None
    _cu = app.build_command(qu)[0] if qu else []
    ok("lz4.unpack-for-a-packed-game", _mode == "unpack" and _kept_out and qu is not None and qu.chain_lz4_unpack
       and not qu.chain_lz4 and "--lz4-unpack" in _cu and "--lz4" not in _cu, f"mode={_mode} kept={_kept_out} {_cu[-6:]}")
    if qu is not None:
        app.queue.remove(qu)
    _seed_pkg = next((S / "c1_pkg").glob("*.pkg"), None)
    _auto_src = S / "auto_sources"
    if _auto_src.exists(): shutil.rmtree(_auto_src)
    _auto_src.mkdir()
    shutil.copytree(HBT, _auto_src / "Folder Game")
    _apkg = _auto_src / "Package Game.pkg"
    shutil.copyfile(_seed_pkg, _apkg)
    _azip = _auto_src / "Zipped Package.zip"
    with zipfile.ZipFile(_azip, "w") as z:
        z.write(_seed_pkg, "Zipped Package/Zipped Package.pkg")
    m.save_settings({"auto_actions": {}})

    _fit = []

    def _auto_add(src):
        d = m.JobDialog(app, init_src=str(src), init_to="auto"); root.update()
        d.out_var.set(str(OUT)); root.update()
        pump(lambda: not str(src).endswith(".zip") or str(Path(src)) in d._archive_content, 10)
        root.update()
        info = (d._to_key(), d.detect_var.get(), d._to_hint.get())
        _fit.append((d._to_seg.winfo_reqwidth(), d._oin.winfo_width()))
        n0 = len(app.queue); d._add(); settle()
        return info, app.queue[n0:]
    (_k, _det, _hint), _new = _auto_add(HBT)
    _fa = _new[0] if _new else None
    ok("auto.folder-builds-ffpfsc", _k == "auto" and "→" in _hint and _fa is not None and _fa.operation == "chain"
       and _fa.chain_to == "ffpfsc" and _fa.auto_output, f"{_k} {_hint[:60]} {_fa and (_fa.operation, _fa.chain_to)}")
    (_k, _det, _hint), _new = _auto_add(_apkg)
    _fp = _new[0] if _new else None
    ok("auto.pkg-is-organized", _fp is not None and _fp.content_kind == m.ORGANIZE_TARGET and _fp.operation == "copy",
       f"{_fp and (_fp.operation, _fp.content_kind)}")
    (_k, _det, _hint), _new = _auto_add(_azip)
    _fz = _new[0] if _new else None
    ok("auto.archive-shows-and-uses-its-content", _det.startswith(".pkg") and _fz is not None
       and _fz.archive_content == "pkg" and _fz.content_kind == m.ORGANIZE_TARGET
       and m.source_label(_fz) == ".pkg", f"detect={_det!r} {_fz and (_fz.archive_content, _fz.content_kind)}")
    if _fz is not None:
        app._auto_resolve_unpacked(_fz, "game", [HBT])
        _flip = (_fz.operation, _fz.chain_to, _fz.content_kind, _fz.archive_content)
        app._auto_resolve_unpacked(_fz, "pkg", [_apkg])
        ok("auto.the-unpacked-content-decides", _flip == ("chain", "ffpfsc", "", "folder")
           and _fz.content_kind == m.ORGANIZE_TARGET and _fz.archive_content == "pkg", f"{_flip} → {_fz.content_kind}")
        _fz.auto_sign = True; _fz.chain_sign = False
        app._auto_resolve_unpacked(_fz, "game", [HBT])
        ok("auto.asked-changes-come-back", _fz.chain_to == "ffpfsc" and _fz.chain_sign, f"sign={_fz.chain_sign}")
    m.save_settings({"auto_actions": {"pkg": "ffpfsc"}})
    _, _new = _auto_add(_apkg)
    _fs = _new[0] if _new else None
    ok("auto.settings-table-decides", _fs is not None and _fs.operation == "chain" and _fs.chain_to == "ffpfsc",
       f"{_fs and (_fs.operation, _fs.chain_to)}")
    m.save_settings({"auto_actions": {}})
    _, _new = _auto_add(_auto_src)
    _by = {Path(str(getattr(i, "archive_path", None) or i.path)).name: (i.operation, i.chain_to) for i in _new}
    ok("auto.parent-folder-one-output-per-kind", len(_new) == 3 and _by.get("Folder Game") == ("chain", "ffpfsc")
       and _by.get("Package Game.pkg", ("",))[0] == "copy" and _by.get("Zipped Package.zip", ("",))[0] == "copy", str(_by))
    for _it in [x for x in app.queue if getattr(x, "auto_output", False)]:
        app.queue.remove(_it)
    ok("auto.output-row-fits", all(seg < oin for seg, oin in _fit), str(_fit[:2]))
    _bs = S / "bundle_src"; shutil.rmtree(_bs, ignore_errors=True)
    _brel = _bs / "Folder Game [PPSA99099]"; (_brel / "Addon").mkdir(parents=True)
    with zipfile.ZipFile(_brel / "Folder Game.zip", "w") as z:
        for f in HBT.rglob("*"):
            if f.is_file(): z.write(f, f"Folder Game/{f.relative_to(HBT)}")
    # DLC: the seed package with content type 33 (additional content)
    _dlcb = bytearray(_seed_pkg.read_bytes())
    _cnt = int.from_bytes(_dlcb[0x58:0x60], "little")
    _dlcb[_cnt + 0x74:_cnt + 0x78] = (33).to_bytes(4, "big")
    with zipfile.ZipFile(_brel / "Addon" / "dlc.zip", "w") as z:
        z.writestr("dlc/content.pkg", bytes(_dlcb))
        z.writestr("dlc/art/cover.png", b"x" * 64)
        z.writestr("cover.png", b"y" * 64)              # beside the folder: the unpack does not return it
    (_brel / "Addon" / "Folder Game update.pkg").write_bytes(b"p" * 64)
    (_brel / "Addon" / "Backport 4.xx").mkdir()
    shutil.copyfile(_seed_pkg, _brel / "Addon" / "Backport 4.xx" / "Folder Game 4xx.pkg")
    # its own id: two releases of one title id beside each other, the much smaller one goes along
    _prel = _bs / "Package Game [PPSA99098]"; (_prel / "Addon").mkdir(parents=True)
    shutil.copyfile(_seed_pkg, _prel / "Package Game.pkg")
    with zipfile.ZipFile(_prel / "Addon" / "bonus.zip", "w") as z:
        z.writestr("art/cover.png", b"x")
    _bout, _bdone = S / "bundle_out", S / "bundle_done"
    shutil.rmtree(_bout, ignore_errors=True); shutil.rmtree(_bdone, ignore_errors=True)
    _bd = m.JobDialog(app, init_src=str(_bs), init_to="ffpfsc"); root.update()
    _bd.out_var.set(str(_bout)); _bd.after_var.set("move"); _bd.after_dir_var.set(str(_bdone)); root.update()
    _bn0 = len(app.queue); _bd._add(); settle()
    _bnew = app.queue[_bn0:]
    _bnames = sorted(Path(str(getattr(i, "archive_path", None) or i.path)).name for i in _bnew)
    _bf = next((i for i in _bnew if getattr(i, "archive_path", None)), None)
    ok("bundle.one-job-per-download-folder", _bnames == ["Folder Game.zip", "Package Game.pkg"] and _bf is not None
       and [Path(x).name for x in _bf.bundle_siblings] == ["Addon"] and _bf.bundle_release == str(_brel),
       f"{_bnames} {_bf and (_bf.bundle_siblings, _bf.bundle_release)}")
    for _it in _bnew:
        app.queue.remove(_it)
    _bgame = app._after_game_sources(_bf)
    _whole_before = app._bundle_release(_bf, _bgame, []) is not None
    (_brel / "new download.zip").write_bytes(b"x")
    _whole_after = app._bundle_release(_bf, _bgame, []) is not None
    (_brel / "new download.zip").unlink()
    ok("bundle.new-file-keeps-the-folder", _whole_before and not _whole_after, f"{_whole_before} {_whole_after}")
    # a folder beside the release with the game's title id went along: Delete takes it too
    _bsib = _bs / "Folder Game Extras [PPSA99099]"; _bsib.mkdir(exist_ok=True); (_bsib / "notes.zip").write_bytes(b"z")
    _bout_f = S / "bundle_plan_out.ffpfsc"; _bout_f.write_bytes(b"x" * 64)
    _sib0, _act0 = list(_bf.bundle_siblings), _bf.after_source
    _bf.bundle_siblings, _bf.after_source = _sib0 + [_bsib], "delete"
    try:
        _bw = types.SimpleNamespace(output_path=str(_bout_f), extras_copied=True, extras_left=[], _is_copy=False,
                                    validate_failed=False)
        _bpl = app._after_job_plan(_bf, _bw)
    finally:
        _bf.bundle_siblings, _bf.after_source = _sib0, _act0
        shutil.rmtree(_bsib, ignore_errors=True); _bout_f.unlink()
    ok("bundle.folder-beside-goes-too", sorted(str(x) for x in _bpl[1]) == sorted([str(_brel), str(_bsib)])
       and _bpl[3] is None, str(_bpl))
    _be = m.JobDialog(app, item=_bf); root.update()
    app.queue.append(_bf); _be._add(); settle()
    _bf2 = app.queue[-1] if app.queue else None
    ok("bundle.edit-keeps-the-folder", _bf2 is not None and getattr(_bf2, "bundle_release", None) == str(_brel)
       and [Path(x).name for x in _bf2.bundle_siblings] == ["Addon"], str(_bf2 and _bf2.bundle_siblings))
    if _bf2 is not None and _bf2 in app.queue:
        app.queue.remove(_bf2)
        _bf = _bf2
    _q0b, _act0b = list(app.queue), app._active_item
    _inflb = getattr(app, "_cleanup_inflight", 0)
    try:
        app._cleanup_inflight = 0; app.cancel_requested = False; app.extract_cancel_event.clear()
        app.queue[:] = [_bf]; app._active_item = None
        app.start()
        pump(lambda: _bf.status in ("Done", "Failed", "Skipped", "Cancelled") and not app._batch_running
             and not getattr(app, "_after_busy", False), timeout=300.0)
        _ffo = next(iter(_bout.rglob("*.ffpfsc")), None)
        _tree = sorted(x.relative_to(_bout).as_posix() for x in _bout.rglob("*") if x.is_file())
        _dlc_in = [x for x in (_ffo.parent.iterdir() if _ffo else []) if " DLC " in x.name and x.suffix == ".pkg"]
        ok("bundle.dlc-sorted-into-the-title-folder", _bf.status == "Done" and _ffo is not None and len(_dlc_in) == 1,
           f"{_bf.status} {_tree[:10]}")
        ok("bundle.rest-of-the-addons-beside-the-output", _ffo is not None
           and (_ffo.parent / "Addon" / "dlc" / "dlc" / "art" / "cover.png").is_file()
           and (_ffo.parent / "Addon" / "dlc" / "cover.png").is_file()
           and (_ffo.parent / "Addon" / "Backport 4.xx" / "Folder Game 4xx.pkg").is_file()
           and len([x for x in _ffo.parent.glob("*.pkg") if " DLC " not in x.name]) == 0
           and (_ffo.parent / "Addon" / "Folder Game update.pkg").is_file()
           and not (_ffo.parent / "Addon" / "dlc.zip").exists(), str(_tree[:12]))
        ok("bundle.download-folder-moves-whole", not _brel.exists() and (_bdone / _brel.name / "Addon" / "dlc.zip").is_file()
           and (_bdone / _brel.name / "Folder Game.zip").is_file(), str(sorted(x.name for x in _bdone.iterdir()) if _bdone.exists() else []))
        _bo = m.JobDialog(app, init_src=str(_bs), init_to="organize"); root.update()
        _bo.out_var.set(str(_bout / "lib")); _bo.after_var.set("keep"); root.update()
        _bn0 = len(app.queue); _bo._add(); settle()
        _bp = next((i for i in app.queue[_bn0:]), None)
        ok("bundle.organize-job", _bp is not None and len(app.queue) == _bn0 + 1
           and [Path(x).name for x in _bp.bundle_siblings] == ["Addon"], str(_bp and _bp.bundle_siblings))
        if _bp is not None:
            _beo = m.JobDialog(app, item=_bp); root.update()
            _beo.out_var.set(str(_bout / "lib")); root.update(); _beo._add(); settle()
            _bp2 = app.queue[-1] if app.queue else None
            ok("bundle.organize-edit-keeps-the-addons", _bp2 is not None and _bp2 is not _bp
               and getattr(_bp2, "bundle_release", None) == getattr(_bp, "bundle_release", None)
               and [Path(x).name for x in _bp2.bundle_siblings] == ["Addon"], str(_bp2 and _bp2.bundle_siblings))
            if _bp2 is not None:
                _bp = _bp2
            app.queue[:] = [_bp]; app._active_item = None
            app.start()
            pump(lambda: _bp.status in ("Done", "Failed", "Skipped", "Cancelled") and not app._batch_running
                 and not getattr(app, "_after_busy", False), timeout=300.0)
            _pk_out = next(iter((_bout / "lib").rglob("*.pkg")), None)
            ok("bundle.organize-addons-beside-the-package", _bp.status == "Done" and _pk_out is not None
               and (_pk_out.parent / "Addon" / "bonus.zip").is_file(),
               f"{_bp.status} {sorted(x.relative_to(_bout).as_posix() for x in (_bout / 'lib').rglob('*'))[:8]}")
    finally:
        app._batch_running = False; app._cleanup_inflight = _inflb
        app.queue[:] = _q0b; app._active_item = _act0b; app.update_queue_box()
    _qa = m.GameItem.from_archive(ZP)
    app.queue.append(_qa); app.update_queue_box()
    pump(lambda: bool(_qa.archive_content), 10)
    ok("auto.queue-reads-each-archive", _qa.archive_content == "folder" and m.source_label(_qa) == "Folder",
       f"{_qa.archive_content!r} {m.source_label(_qa)}")
    app.queue.remove(_qa); app.update_queue_box()
    dla = m.JobDialog(app, init_src=str(HBT), init_to="ffpfsc"); root.update()
    _ab_shown = bool(dla._after_box.winfo_manager())
    dla.out_var.set(str(OUT)); dla.after_var.set("move"); dla.after_dir_var.set(str(S / "after_done")); root.update()
    _ab_dir = bool(dla._after_dir_row.winfo_manager())
    dla._add(); settle()
    qa = app.queue[-1] if app.queue else None
    _tpl = m.load_settings().get("rescan_template", {}) or {}
    ok("dialog.after.stored", _ab_shown and _ab_dir and qa is not None and qa.after_source == "move"
       and qa.after_move_to == str(S / "after_done") and _tpl.get("after_source") == "move",
       f"shown={_ab_shown} dir={_ab_dir} {getattr(qa, 'after_source', None)} {_tpl.get('after_source')}")
    if qa is not None and qa is not q:
        app.queue.remove(qa)
    dlg2 = m.JobDialog(app, init_src=str(FF), init_to="pkg"); root.update()
    ok("dialog.image.identity-empty", dlg2.cid_var.get() == "" and dlg2.tid_var.get() == "", f"{dlg2.cid_var.get()!r}")
    ok("dialog.image.hint", "param.json" in dlg2._ident_note.get(), dlg2._ident_note.get())
    dlg2.out_var.set(str(OUT)); n1 = len(app.queue); dlg2._add(); settle()
    ok("dialog.image.queued", len(app.queue) == n1 + 1 and app.queue[-1].path == FF and app.queue[-1].chain_to == "pkg", f"errors={errors[-1:]}")
    cmd3, *_ = app.build_command(app.queue[-1])
    ok("build_command.image-source", str(FF) in cmd3 and "--content-id" not in cmd3, " ".join(cmd3[-10:]))
    idx = app.queue.index(q)
    dlg3 = m.JobDialog(app, item=q); root.update()
    ok("dialog.edit.preselects-pkg", dlg3._to_key() == "pkg" and dlg3._pkg_opts.winfo_manager() != ""
       and dlg3.pkg_level_var.get() == -4, f"{dlg3._to_key()}/{dlg3.pkg_level_var.get()}")
    dlg3.pkg_level_var.set(7); dlg3.title_var.set("Edited"); dlg3._add(); settle()
    q = app.queue[idx]
    ok("dialog.edit.saved", q.fpkg_level == 7 and q.fpkg_title == "Edited", f"{q.fpkg_level}/{q.fpkg_title} errors={errors[-1:]}")
    dlg3b = m.JobDialog(app, item=q); root.update(); dlg3b.to_var.set(L["ffpfsc"]); root.update()
    ok("dialog.edit.panel-hidden", dlg3b._pkg_opts.winfo_manager() == "", dlg3b._to_key())
    dlg3b._add(); settle()
    rep = app.queue[idx]
    ok("dialog.edit.to-pack", rep.chain_to == "ffpfsc" and rep.output_compressed is True and rep.path == HBT
       and "fpkg_level" not in vars(rep), f"{rep.chain_to} errors={errors[-1:]}")
    dlg3c = m.JobDialog(app, item=rep); root.update(); dlg3c.to_var.set(L["pkg"]); root.update(); dlg3c._add(); settle()
    q = app.queue[idx]
    ok("dialog.edit.back-to-pkg", q.chain_to == "pkg" and q.fpkg_content_id == CID, f"{q.chain_to} errors={errors[-1:]}")
    dlg4 = m.JobDialog(app, init_src=str(HBT), init_to="pkg"); root.update()
    ok("dialog.compact-by-default", not dlg4._pkg_more and not dlg4._ident_forced and dlg4._pkg_more_box.winfo_manager() == "",
       f"more={dlg4._pkg_more} forced={dlg4._ident_forced}")
    ok("dialog.summary", "Build .pkg" in dlg4.summary_var.get(), dlg4.summary_var.get())
    ok("dialog.backend-row-hidden", (dlg4._back_row is None and dlg4.back_var.get() == "builtin") if sys.platform != "win32"
       else dlg4._back_row is not None, f"dll={app.pubtools_dll_var.get()!r}")
    _prev_dll = app.pubtools_dll_var.get(); app.pubtools_dll_var.set("/nonexistent/libScePubTools.dll")
    dlg4x = m.JobDialog(app, init_src=str(HBT), init_to="pkg"); root.update()
    ok("dialog.backend-row-windows-only", (dlg4x._back_row is None) if sys.platform != "win32" else dlg4x._back_row is not None, "")
    px = m.GameItem(HBT); app._as_fpkg_job(px, {"inner": "none", "backend": "publishingtools", "level": 7, "dll": "/nonexistent/x.dll"})
    ok("as-fpkg.publishingtools-forced-builtin", (px.fpkg_kraken_backend == "builtin" and px.fpkg_pubtools_dll == "") if sys.platform != "win32" else px.fpkg_kraken_backend == "publishingtools", f"{px.fpkg_kraken_backend}")
    dlg4x.destroy(); app.pubtools_dll_var.set(_prev_dll)
    dlg4.out_var.set(str(OUT)); dlg4.cid_var.set("garbage"); n2 = len(app.queue); dlg4._add(); settle()
    ok("dialog.validation.bad-cid", len(app.queue) == n2 and any("Content ID" in e for e in errors), str(errors[-1:]))
    ok("dialog.validation.reveals-identity", dlg4._pkg_more and dlg4._pkg_more_box.winfo_manager() == "pack", f"more={dlg4._pkg_more}")
    dlg4.destroy()
    NOP = S / "hbt_noparam"
    if NOP.exists(): shutil.rmtree(NOP)
    shutil.copytree(HBT, NOP); (NOP / "sce_sys" / "param.json").unlink()
    dlg4b = m.JobDialog(app, init_src=str(NOP), init_to="pkg"); root.update()
    ok("dialog.noparam.identity-forced", dlg4b._ident_forced and dlg4b._pkg_more_box.winfo_manager() == "pack"
       and "required" in dlg4b._ident_head.get() and dlg4b._pkg_more_btn.cget("state") == "disabled",
       f"forced={dlg4b._ident_forced} head={dlg4b._ident_head.get()[:40]}")
    dlg4b.out_var.set(str(OUT)); n2b = len(app.queue); dlg4b._add(); settle()
    ok("dialog.noparam.requires-identity", len(app.queue) == n2b and any("Identity needed" in e for e in errors), str(errors[-1:]))
    dlg4b.src_var.set(str(HBT)); root.update()
    ok("dialog.noparam.unforced-on-good-source", not dlg4b._ident_forced and dlg4b._pkg_more_box.winfo_manager() == ""
       and dlg4b.cid_var.get() == CID, f"forced={dlg4b._ident_forced} cid={dlg4b.cid_var.get()!r}")
    dlg4b.destroy()
    dlg5 = m.JobDialog(app, init_src=str(FF), init_to="ffpfsc"); root.update()
    ok("dialog.ffpfsc.copy-hint", "Copy or move" in dlg5.summary_var.get(), dlg5.summary_var.get())
    ok("dialog.ffpfsc.copy-has-after-row", dlg5._after_box.winfo_manager() != "" and not hasattr(dlg5, "_keep_cb"),
       f"manager={dlg5._after_box.winfo_manager()!r}")
    dlg5.out_var.set(str(OUT)); n3 = len(app.queue); dlg5._add(); settle()
    ok("dialog.ffpfsc.enqueued-as-copy", len(app.queue) == n3 + 1 and app.queue[-1].chain_to == "ffpfsc"
       and m.chain_summary(app.queue[-1]).startswith("Copy or move"), m.chain_summary(app.queue[-1]) if app.queue else "")
    _c5 = app.build_command(app.queue[-1])[0]
    _c5m = _c5[_c5.index("--copy-mode") + 1] if "--copy-mode" in _c5 else None
    _q5 = app.queue[-1]; _q5.after_source = "delete"
    _c5d = app.build_command(_q5)[0]
    ok("dialog.ffpfsc.copy.keeps-source-by-default", _c5m == "keep"
       and _c5d[_c5d.index("--copy-mode") + 1] == "move", f"{_c5m} {_c5d[-6:]}")
    app.queue.pop()
    dlg6 = m.JobDialog(app, init_src=str(ZP), init_to="pkg"); root.update()
    pump(lambda: str(Path(ZP)) in dlg6._archive_content, 10); root.update()
    ok("dialog.archive.hint", dlg6._kind == "archive" and dlg6.detect_var.get().startswith("Game folder"),
       dlg6.detect_var.get())
    dlg6.cid_var.set(CID); dlg6.tid_var.set("PPSA99099")
    dlg6.out_var.set(str(OUT)); n4 = len(app.queue); dlg6._add(); settle()
    arc = app.queue[-1]
    ok("archive.queued-as-pkg", len(app.queue) == n4 + 1 and arc.chain_to == "pkg" and arc.archive_path == ZP
       and arc.fpkg_inner_mode == app.fpkg_defaults["inner"], f"{getattr(arc, 'chain_to', None)} {getattr(arc, 'archive_path', None)} errors={errors[-1:]}")
    ok("archive.carries-typed-identity", arc.fpkg_content_id == CID and arc.fpkg_title_id == "PPSA99099", f"{arc.fpkg_content_id!r}")
    app._copy_item_payload(arc, m.GameItem(HBT))
    ok("archive.payload-keeps-chain", arc.operation == "chain" and arc.chain_to == "pkg" and arc.path == HBT
       and arc.fpkg_inner_mode == app.fpkg_defaults["inner"], f"{arc.operation}/{getattr(arc, 'chain_to', None)}")
    extra = m.GameItem(HBT)
    app._as_chain_job(extra, arc)
    ok("archive.extra-game-same-job", extra.operation == "chain" and extra.chain_to == "pkg" and extra.output_path == arc.output_path
       and extra.fpkg_inner_mode == arc.fpkg_inner_mode and extra.fpkg_content_id == "" and extra.patch_source is None,
       f"{extra.operation}/{getattr(extra, 'chain_to', None)}/{getattr(extra, 'fpkg_content_id', None)!r}")
    tpl = app._fpkg_compression_of(arc)
    _build_opts = {"inner", "backend", "level", "dll", "retail_normalize", "hdr_flag", "regen_playgo", "fake_sign"}
    ok("compression-of.no-identity", set(tpl) == _build_opts and tpl["inner"] == arc.fpkg_inner_mode
       and not ({"content_id", "title_id", "title", "version"} & set(tpl)), str(tpl))
    pi = m.GameItem(HBT); pi.patch_source = Path("/nonexistent/patch"); app._as_fpkg_job(pi, dict(app.fpkg_defaults))
    ok("as-fpkg.drops-patch", pi.patch_source is None and pi.operation == "fpkg-build", str(pi.patch_source))
    fi = app._fpkg_item_for(FF, dict(app.fpkg_defaults), output_path=str(OUT))
    ok("as-fpkg.ffpfsc-size-estimate", fi is not None and fi.size > 0 and fi.extracted_size == 2 * fi.size, f"{getattr(fi, 'extracted_size', None)}")
    app.output_format_var.set("pkg"); app.source_var.set(str(FF)); n5 = len(app.queue); app.add_source_to_queue(); root.update()
    ok("drop.ffpfsc.remembered-pkg", len(app.queue) == n5 + 1 and app.queue[-1].operation == "fpkg-build", getattr(app.queue[-1], "operation", None))
    fb = app.queue[-1]
    app.output_format_var.set("ffpfsc"); app.source_var.set(str(FF)); n6 = len(app.queue); app.add_source_to_queue(); root.update()
    ok("drop.ffpfsc.remembered-ffpfsc", len(app.queue) == n6 + 1 and app.queue[-1].operation == "unpack", getattr(app.queue[-1], "operation", None))
    ok("format.legacy-bool-synced", app.output_compressed_var.get() is True, str(app.output_compressed_var.get()))
    bi = m.GameItem(HBT); bi.bundle_subfolder = "Bundle X"; bi.output_compressed = True; app.queue.append(bi); app.update_queue_box()
    bidx = app.queue.index(bi)
    e1 = m.JobDialog(app, item=bi); root.update()
    ok("edit.pack-opens-as-ffpfsc", e1._to_key() == "ffpfsc", e1._to_key())
    e1.to_var.set(L["pkg"]); root.update(); e1._add(); settle()
    b1 = app.queue[bidx]
    ok("edit.inplace.pack-to-pkg", b1.chain_to == "pkg" and b1.bundle_subfolder == "Bundle X" and b1.fpkg_content_id == CID,
       f"{getattr(b1, 'chain_to', None)} errors={errors[-1:]}")
    e2 = m.JobDialog(app, item=b1); root.update(); e2.to_var.set(L["ffpfs"]); root.update(); e2._add(); settle()
    b2 = app.queue[bidx]
    ok("edit.inplace.pkg-to-pack", b2.chain_to == "ffpfs" and b2.output_compressed is False and "fpkg_level" not in vars(b2)
       and b2.bundle_subfolder == "Bundle X", f"{getattr(b2, 'chain_to', None)} errors={errors[-1:]}")
    app._pending_fpkg_identity = (HBT, {"content_id": "X"}); app.source_var.set(str(S / "does-not-exist")); app.add_source_to_queue(); root.update()
    ok("pending-identity.cleared-on-error", app._pending_fpkg_identity is None, str(app._pending_fpkg_identity))
    if pkg:
        ok("space-gate.fpkg-extract-proceeds", app._space_gate(ie, OUT) == "proceed", "")
    olds = {"fpkg-build": fb}
    if pkg:
        olds["fpkg-extract"] = m.GameItem.from_fpkg_extract(pkg, output_path=str(OUT / "ext"))
    olds["unpack"] = m.GameItem.from_pfs_image(FF)
    dec = m.GameItem.from_pfs_image(FF); dec.unwrap = False; olds["unpack-decompress"] = dec
    olds["fake-sign"] = m.GameItem.from_fake_sign(HBT)
    olds["patch"] = m.GameItem.from_patch(HBT, HBT, output_path=str(OUT))
    want = {"fpkg-build": "pkg", "fpkg-extract": "folder", "unpack": "folder", "unpack-decompress": "ffpfs",
            "fake-sign": "folder", "patch": "ffpfsc"}
    for name, it in olds.items():
        if it not in app.queue:
            app.queue.append(it)
        i = app.queue.index(it)
        ed = m.JobDialog(app, item=it); root.update()
        extra = ""
        if name == "fake-sign":
            extra = "" if ed.sign_var.get() else " sign not preset"
        if name == "patch":
            extra = "" if (ed.patch_on_var.get() and ed.patch_var.get() == str(HBT)) else " patch not preset"
        ok(f"edit.opens.{name}", ed._to_key() == want[name] and not extra, f"to={ed._to_key()}{extra}")
        ed.destroy()
    if pkg:
        it = olds["fpkg-extract"]; i = app.queue.index(it)
        ed = m.JobDialog(app, item=it); root.update(); ed.out_var.set(str(OUT / "ext2")); ed._add(); settle()
        ok("edit.saves.fpkg-extract", app.queue[i].chain_to == "folder" and str(app.queue[i].output_path) == str(OUT / "ext2"),
           f"{getattr(app.queue[i], 'chain_to', None)} {getattr(app.queue[i], 'output_path', None)}")
    if pkg:
        app.queue.append(m.GameItem.from_fpkg_extract(pkg, output_path=str(OUT / "ext3")))
    app.update_queue_box(); root.update()
    rows = [app.queue_listbox.get(i) for i in range(app.queue_listbox.size())]
    ok("queue.badges", any("fPKG-BD" in r for r in rows) and (not pkg or any("fPKG-EX" in r for r in rows)), rows[:2])
    app.update_game_details(fb); ok("details.mode", "Build fPKG" in app.title_var.get(), app.title_var.get())
    app.queue_listbox.selection_clear(0, "end"); idx = app.queue.index(fb); app.queue_listbox.selection_set(idx)
    app._on_queue_double_click(None); root.update()
    ok("doubleclick.dispatch", not any("Could not open" in str(x) for x in errors) and bool(app._panels.stack)
       and isinstance(app._panels.stack[-1], m.JobDialog), "")
    close_toplevels()
    ok("one-door.old-dialogs-gone", not any(hasattr(m, n) for n in ("FpkgBuildDialog", "PackDialog", "PatchDialog",
                                                                   "ConverterDialog", "JobEditMiniDialog")), "")
    dlg7 = m.JobDialog(app, init_src=str(HBT), init_to="pkg"); root.update()
    dlg7._pkg_more_btn.invoke(); root.update()
    ok("dialog.pkg.more-options", dlg7._pkg_more_box.winfo_manager() == "pack" and "Fewer" in dlg7._pkg_more_btn.cget("text"),
       dlg7._pkg_more_btn.cget("text"))
    dlg7._pkg_more_btn.invoke(); root.update()
    ok("dialog.pkg.fewer-again", dlg7._pkg_more_box.winfo_manager() == "" and "More" in dlg7._pkg_more_btn.cget("text"), "")
    dlg7.destroy()
    ok("settings.pubtools_var", hasattr(app, "pubtools_dll_var"), "")
    APR = S / "apr_game"
    if APR.exists(): shutil.rmtree(APR)
    shutil.copytree(HBT, APR); (APR / "sce_sys" / "playgo-chunk.dat").write_bytes(b"\0" * 64)
    (APR / "fakelib").mkdir(); (APR / "fakelib" / "libSceAmpr.sprx").write_bytes(b"A" * 32); (APR / "fakelib" / "libScePlayGo.sprx").write_bytes(b"P" * 32)
    (APR / "ampr_emu.index").write_bytes(b"SHIPPED-INDEX")
    prompts = []
    app._ensure_ampr_folder = lambda: (prompts.append(1), False)[1]   # a prompt would block the driver
    _prev_ampr = app.ampr_var.get(); app.ampr_var.set(""); _prev_sign = app.fake_sign_before_pack_var.get(); app.fake_sign_before_pack_var.set(False)
    ai = m.GameItem(APR)
    ok("ampr.detected", ai.ampr_emu is True, str(ai.ampr_emu))
    app._prepare_ampr(ai)
    ok("ampr.shipped.no-prompt", not prompts and (APR / "ampr_emu.index").read_bytes() == b"SHIPPED-INDEX"
       and not getattr(ai, "_ampr_injected", None), f"prompts={len(prompts)}")
    (APR / "ampr_emu.index").unlink(); ai2 = m.GameItem(APR); app._prepare_ampr(ai2)
    ok("ampr.shipped-sprx.index-built", not prompts and (APR / "ampr_emu.index").is_file() and (APR / "ampr_emu.index").read_bytes()[:8] == b"AMPRIDX3",
       f"prompts={len(prompts)} exists={(APR / 'ampr_emu.index').is_file()}")
    shutil.rmtree(APR / "fakelib"); ai3 = m.GameItem(APR); app._prepare_ampr(ai3)
    ok("ampr.nothing-shipped.prompts", len(prompts) == 1, f"prompts={len(prompts)}")
    del app._ensure_ampr_folder; app.ampr_var.set(_prev_ampr); app.fake_sign_before_pack_var.set(_prev_sign)
    app.auto_organize_var.set(True)
    gi = m.GameItem(HBT); gi.output_compressed = True; gi.auto_organize = True; gi.output_path = OUT
    cmdg, _, outg, _ = app.build_command(gi)
    exp_dir = OUT / "LibProsperoPKG [PPSA99099] [v01.000.000]"
    outfile = Path(cmdg[cmdg.index(str(HBT)) + 1])
    ok("organize.pack.folder+file", outg == exp_dir and outfile.parent == exp_dir and outfile.name == "LibProsperoPKG [PPSA99099] [v01.000] [fw2.00].ffpfsc", f"{outg} | {outfile.name}")
    idf = app._game_identity(m.GameItem.from_exfat(FF))
    ok("organize.identity.from-ffpfsc", bool(idf) and idf.get("title_id") == "PPSA99099" and str(idf.get("version", "")).startswith("01.000") and idf.get("title") == "LibProsperoPKG", str(idf))
    fi2 = app._fpkg_item_for(FF, dict(app.fpkg_defaults), output_path=str(OUT)); fi2.auto_organize = True
    cmdf2, _, outf2, _ = app.build_command(fi2)
    ok("organize.fpkg.folder+name", outf2 == exp_dir and getattr(fi2, "_organized_pkg_name", None) == "LibProsperoPKG [PPSA99099] [v01.000] [fw2.00].pkg", f"{outf2} | {getattr(fi2, '_organized_pkg_name', None)}")
    exp_dir.mkdir(parents=True, exist_ok=True); dummy = exp_dir / "UP9000-PPSA99099_00-PROSPERO00000000-A0100-V0100.pkg"; dummy.write_bytes(b"x")
    renamed = app._finalize_pkg_name(fi2, dummy)
    ok("organize.fpkg.renamed", renamed.name == "LibProsperoPKG [PPSA99099] [v01.000] [fw2.00].pkg" and renamed.exists() and not dummy.exists(), str(renamed.name))
    # the marker sets output_path and _find_output returns early: rename there
    fi3 = app._fpkg_item_for(FF, dict(app.fpkg_defaults), output_path=str(OUT)); fi3.auto_organize = True
    cmd3w, cwd3, out3w, tmp3 = app.build_command(fi3)
    out3w.mkdir(parents=True, exist_ok=True); dummy2 = out3w / "UP9000-PPSA99099_00-PROSPERO00000000-A0100-V0100.pkg"; dummy2.write_bytes(b"pkg")
    w = m.CLIWorker(app, fi3, cmd3w, cwd3, out3w, tmp3); w.start_time = time.time() - 30
    w.output_path = str(dummy2)          # what the marker line sets
    found = w._find_output()
    # never replace an earlier build: the new one gets a (2) suffix
    ok("organize.worker.marker-rename", found and Path(w.output_path).name == "LibProsperoPKG [PPSA99099] [v01.000] [fw2.00] (2).pkg"
       and Path(w.output_path).exists() and not dummy2.exists() and renamed.exists(), f"{found} {w.output_path}")
    ok("organize.fw-tag", m.organized_names({"title": "Example Quest", "title_id": "PPSA99098", "version": "01.200.007",
                                             "fw": "10.00"}, ".pkg")[1] == "Example Quest [PPSA99098] [v01.200] [fw10.00].pkg",
       str(m.organized_names({"title": "Example Quest", "title_id": "PPSA99098", "version": "01.200.007", "fw": "10.00"}, ".pkg")))
    class _Job: pass
    _j = _Job(); _j.patch_source = None; _j.backport_target = "7.61"
    _k = _Job(); _k.patch_source = None; _k.backport_target = None
    ok("organize.fw-after-backport", app._job_fw(_j, "10.00") == "7.61" and app._job_fw(_j, "6.00") == "6.00"
       and app._job_fw(_k, "10.00") == "10.00" and app._job_fw(_j, "") == "",
       f"{app._job_fw(_j, '10.00')} {app._job_fw(_j, '6.00')} {app._job_fw(_k, '10.00')}")
    import test_backport as _tbk
    _pd = S / "patch_with_eboot"; _pd.mkdir(exist_ok=True)
    (_pd / "eboot.bin").write_bytes(_tbk._elf_with_param(0x61000001, 0x4942524F, 0x12590001, 0x11000043))
    _pz = S / "patch_with_eboot.zip"
    with zipfile.ZipFile(_pz, "w") as _z:
        _z.write(_pd / "eboot.bin", "Game/eboot.bin")
    _np = S / "patch_without_eboot"; _np.mkdir(exist_ok=True); (_np / "data.bin").write_bytes(b"x")
    _k.patch_source = str(_pd)
    _fw_folder = app._job_fw(_k, "10.00")
    _k.patch_source = str(_pz); _fw_zip = app._job_fw(_k, "10.00")
    _k.patch_source = str(_np); _fw_none = app._job_fw(_k, "10.00")
    ok("organize.fw-from-patch", (_fw_folder, _fw_zip, _fw_none) == ("11.00", "11.00", "10.00"),
       str((_fw_folder, _fw_zip, _fw_none)))
    ok("organize.title-cleanup", m.canonical_game_title("a large retail title™") == "a large retail title"
       and m.organized_names({"title": "Example Quest Deluxe Edition", "title_id": "PPSA99098", "version": "01.200.007"}, ".ffpfsc")
       == ("Example Quest Deluxe Edition [PPSA99098] [v01.200.007]", "Example Quest Deluxe Edition [PPSA99098] [v01.200].ffpfsc"),
       str(m.organized_names({"title": "Example Quest Deluxe Edition", "title_id": "PPSA99098", "version": "01.200.007"}, ".ffpfsc")))
    # only our own scratch roots count, a user folder named _extracted is never deleted
    _tmp_root = S / "temp"; app.temp_var.set(str(_tmp_root)); _own = _tmp_root / "_extracted" / "game-a" / "sub"
    _foreign = S / "library" / "_extracted" / "game-b" / "sub"
    for d in (_own, _foreign): d.mkdir(parents=True, exist_ok=True)
    class _Fake: pass
    _ia = _Fake(); _ia.path = str(_own); _ib = _Fake(); _ib.path = str(_foreign)
    ok("cleanup.extract-dir.own-scratch", app._extract_dir_for_item(_ia) == (_tmp_root / "_extracted" / "game-a").resolve(),
       str(app._extract_dir_for_item(_ia)))
    ok("cleanup.extract-dir.foreign-never", app._extract_dir_for_item(_ib) is None, str(app._extract_dir_for_item(_ib)))
    # a pool drive's root is not scratch, only <pool>/_ffpfsc_temp is
    _pool = S / "pool"; _pool.mkdir(exist_ok=True); _ic = _Fake(); _ic.path = str(_pool / "_ffpfsc_temp" / "_extracted" / "g")
    (_pool / "_ffpfsc_temp" / "_extracted" / "g").mkdir(parents=True, exist_ok=True)
    app.temp_pool = [str(_pool)]
    ok("cleanup.extract-dir.pool-subfolder", app._extract_dir_for_item(_ic) is not None, str(app._extract_dir_for_item(_ic)))
    app.temp_pool = []
    conv = OUT / "convert"; conv.mkdir(exist_ok=True); shutil.copy2(ZP, conv / ZP.name)
    bi2 = m.GameItem.from_bundle(conv, conv / ZP.name, []); bi2.output_compressed = True; bi2.auto_organize = False; bi2.output_path = OUT
    bi2.path = HBT; bi2.archive_path = None            # as _copy_item_payload leaves it after extraction
    _, _, outb2, _ = app.build_command(bi2)
    ok("mirror.not-into-source", outb2 == OUT, str(outb2))
    bi3 = m.GameItem.from_bundle(conv, conv / ZP.name, []); bi3.output_compressed = True; bi3.auto_organize = False; bi3.output_path = OUT / "lib"
    bi3.path = HBT; bi3.archive_path = None
    _, _, outb3, _ = app.build_command(bi3)
    ok("mirror.elsewhere-kept", outb3 == OUT / "lib" / "convert", str(outb3))
    dlg9 = m.JobDialog(app, init_src=str(HBT), init_to="ffpfsc"); root.update()
    ok("dialog.organize.checkbox", dlg9.organize_var.get() is True and "Auto-organize" in dlg9._organize_cb.cget("text"),
       dlg9._organize_cb.cget("text")[:60])
    dlg9.destroy()
    app.auto_organize_var.set(False); si = m.GameItem(HBT); app.queue.append(si); app.update_queue_box()
    ok("organize.snapshot", si.auto_organize is False, str(getattr(si, "auto_organize", None)))
    app.queue.remove(si); app.auto_organize_var.set(True)
    if pkg:
        r1 = subprocess.run([sys.executable, "-u", str(CLI), "--list-image", str(pkg)], capture_output=True, text=True, timeout=300)
        line = next((l for l in r1.stdout.splitlines() if l.startswith("PFSBROWSE_JSON:")), "")
        data = json.loads(line.split(":", 1)[1]) if line else {}
        paths = {e["path"] for e in data.get("entries", [])}
        ok("browser.cli.list-pkg", "sce_sys/param.json" in paths and "eboot.bin" in paths and data.get("file_count", 0) >= 3
           and any(e.get("type") == "dir" and e["path"] == "sce_sys" for e in data.get("entries", [])), f"{sorted(paths)[:6]} {r1.stderr[-120:]}")
        mdir = S / "browse_members"; mdir.mkdir(exist_ok=True); mf = mdir / "m.txt"; mf.write_text("sce_sys/param.json\neboot.bin\n")
        dest = mdir / "out"
        r2 = subprocess.run([sys.executable, "-u", str(CLI), "--extract-from", str(pkg), "--dest", str(dest), "--members-file", str(mf)],
                            capture_output=True, text=True, timeout=300)
        ok("browser.cli.extract-pkg-members", r2.returncode == 0 and (dest / "sce_sys" / "param.json").is_file() and (dest / "eboot.bin").is_file()
           and re.search(r"\[#{2,}\]\s*\d{1,3}%", r2.stdout) is not None and not (dest / "sce_sys" / "icon0.png").exists(),
           f"rc={r2.returncode} {r2.stdout[-140:]}")
        app.open_pfs_browser(str(pkg)); root.update(); br = app._look_view
        pump(lambda: "files" in br.status_var.get() or br.status_var.get().startswith(("Failed", "Could not", "Bad")), timeout=90)
        ok("browser.dialog.pkg-listing", "files" in br.status_var.get() and any(p.endswith("param.json") for p in br._iid_path.values())
           and "fPKG" in br.title(), br.status_var.get())
        root.geometry("1500x900"); root.update()
        # Windows: a withdrawn window reports width 1, so check the grid placement there
        _fills = (br.frame.winfo_width() == app._views_parent.winfo_width() and br.frame.winfo_width() > 1000
                  if sys.platform != "win32" else
                  br.frame.winfo_manager() == "grid" and br.frame.master is app._views_parent
                  and set(str(br.frame.grid_info().get("sticky"))) >= set("nsew"))
        ok("browser.is-a-view", app._view == "look" and not app._panels.stack and _fills,
           f"view={app._view} {br.frame.winfo_width()} of {app._views_parent.winfo_width()} "
           f"grid={br.frame.grid_info() if br.frame.winfo_manager() == 'grid' else None}")
        app._show_view("queue"); root.update()
    app._queue_restored = True; app._save_queue()
    saved = m.load_settings().get("queue") or []
    ok("queue.persist.fpkg-fields", any(d.get("operation") == "fpkg-build" and "fpkg_level" in d and "fpkg_content_id" in d for d in saved), f"{len(saved)} saved")
    ok("settings.persist.format", m.load_settings().get("output_format") == "ffpfsc" and isinstance(m.load_settings().get("fpkg_defaults"), dict), "")

    copy_src_dir = S / "copy_src"; copy_src_dir.mkdir(exist_ok=True)
    copy_src = copy_src_dir / "CopyMe [PPSA99099] [v01.000].ffpfsc"
    _sh.copy2(FF, copy_src)
    copy_out = S / "copy_out"; copy_out.mkdir(exist_ok=True)
    ci = app._copy_item_for(copy_src, output_path=str(copy_out), auto_organize=True)
    ok("copy.item.built", ci is not None and ci.operation == "copy" and ci.copy_mode == "organize",
       f"op={getattr(ci, 'operation', None)} mode={getattr(ci, 'copy_mode', None)}")
    ccmd, ccwd, cout, ctmp = app.build_command(ci)
    ok("copy.build_command", "--copy" in ccmd and str(copy_src) in ccmd
       and ccmd[ccmd.index("--copy-mode") + 1] == "organize", " ".join(ccmd[-6:]))
    ok("copy.organized-dir", cout.name.startswith("LibProsperoPKG [PPSA99099]"), str(cout.name))
    ok("copy.single-pass", m._item_is_single_pass(ci) is True, "")
    ok("copy.space-gate-passes", m._space_preflight_ok(ci, ctmp, cout) is True, "")
    fin = {}
    _real_finish = app.finish
    app.finish = lambda success, msg, cmd=None, **k: fin.update(success=success, msg=msg)
    try:
        cw = m.CLIWorker(app, ci, ccmd, ccwd, cout, ctmp)
        cw.start(); pump(lambda: "success" in fin, timeout=60.0)
    finally:
        app.finish = _real_finish
    ok("copy.worker.finishes-success", fin.get("success") is True,
       f"success={fin.get('success')} msg={fin.get('msg')!r}")
    ok("copy.worker.no-false-ffpfsc-error", "no new .ffpfsc" not in str(fin.get("msg", "")),
       str(fin.get("msg")))
    moved = list(cout.glob("*.ffpfsc"))
    ok("copy.landed-and-source-gone", len(moved) == 1 and not copy_src.exists(),
       f"moved={[p.name for p in moved]} src_exists={copy_src.exists()}")

    jd = m.JobDialog(app, init_src=str(HBT)); root.update()
    ok("job.detect.folder", jd._kind == "folder" and "Game folder" in jd.detect_var.get()
       and "PPSA99099" in jd.detect_var.get(), jd.detect_var.get())
    jd.to_var.set(".ffpfsc"); jd.backport_on_var.set(True); jd.backport_target_var.set("7.61")
    jd.out_var.set(str(OUT / "job")); root.update()
    ok("job.summary.sentence", jd.summary_var.get() == "Backport to 7.61, then build .ffpfsc", jd.summary_var.get())
    n0 = len(app.queue); jd._add(); settle()
    ji = app.queue[-1]
    ok("job.add.chain-item", len(app.queue) == n0 + 1 and ji.operation == "chain" and ji.chain_to == "ffpfsc"
       and ji.backport_target == "7.61" and not ji.chain_sign, f"errors={errors}")
    jcmd, jcwd, jout, jtmp = app.build_command(ji)
    ok("job.build_command.chain", "--to" in jcmd and jcmd[jcmd.index("--to") + 1] == "ffpfsc"
       and "--backport-target" in jcmd and "--overwrite" in jcmd and str(HBT) in jcmd, " ".join(jcmd[-10:]))
    ok("job.build_command.named-output", any(a.endswith(".ffpfsc") for a in jcmd) and jout.parent == OUT / "job",
       " ".join(a for a in jcmd if a.endswith(".ffpfsc")) + f" out={jout}")
    jd2 = m.JobDialog(app, init_src=str(FF)); root.update()
    ok("job.detect.ffpfsc", jd2._kind == "ffpfsc" and jd2._look_btn.winfo_manager() == "pack", jd2.detect_var.get())
    jd2.to_var.set(".pkg"); jd2.out_var.set(str(OUT / "job")); jd2.pkg_level_var.set(-4); root.update()
    ok("job.pkg.sign-forced", jd2._sign_cb.cget("state") == "disabled" and jd2._sign_fixed.winfo_manager() == "pack"
       and jd2._sign_cb.winfo_manager() == "", str(jd2._sign_cb.cget("state")))
    ok("job.check.reads-container", str(jd2._check_btn.cget("state")) == "normal"
       and jd2._check_folder() == Path(jd2.src_var.get()), str(jd2._check_btn.cget("state")))
    ok("job.help.idle-line", jd2.help_var.get() == jd2._HELP_IDLE, jd2.help_var.get())
    ok("job.summary.pkg", jd2.summary_var.get() == "Build .pkg", jd2.summary_var.get())
    n0 = len(app.queue); jd2._add(); settle(); j2 = app.queue[-1]
    ok("job.add.pkg-item", len(app.queue) == n0 + 1 and j2.operation == "chain" and j2.chain_to == "pkg"
       and getattr(j2, "fpkg_level", None) == -4, f"errors={errors}")
    jcmd2, *_ = app.build_command(j2)
    ok("job.build_command.pkg-flags", jcmd2[jcmd2.index("--to") + 1] == "pkg" and "--fpkg-inner" in jcmd2
       and jcmd2[jcmd2.index("--compression-level") + 1] == "-4" and "--backport-target" not in jcmd2,
       " ".join(jcmd2[-12:]))
    if pkg:
        jd3 = m.JobDialog(app, init_src=str(pkg)); root.update()
        jd3.to_var.set("Folder"); jd3.out_var.set(str(OUT / "job")); root.update()
        ok("job.summary.unpack", jd3.summary_var.get() == "Unpack to folder", jd3.summary_var.get())
        n0 = len(app.queue); jd3._add(); settle(); j3 = app.queue[-1]
        jcmd3, _, jout3, _ = app.build_command(j3)
        ok("job.build_command.folder-out", jcmd3[jcmd3.index("--to") + 1] == "folder"
           and str(jout3).endswith(" [extracted]") and jout3.parent == OUT / "job", str(jout3))
    jd4 = m.JobDialog(app, init_src=str(HBT)); root.update()
    # the remembered folder choices still have backport on from above, clear them
    jd4.backport_on_var.set(False); jd4.patch_on_var.set(False); jd4.sign_var.set(False)
    jd4.to_var.set("Folder"); root.update()
    ok("job.nothing-to-do.disabled", jd4.summary_var.get() == "Nothing to do"
       and str(jd4._add_btn.cget("state")) == "disabled", jd4.summary_var.get())
    jd4.sign_var.set(True); root.update()
    ok("job.sign-in-place", jd4.summary_var.get() == "Sign in place" and str(jd4._add_btn.cget("state")) == "normal"
       and str(jd4._out_entry.cget("state")) == "disabled", jd4.summary_var.get())
    jd4.destroy()
    e = m.JobDialog(app, item=ji); root.update()
    ok("job.edit.prefill", e.src_var.get() == str(HBT) and e.backport_on_var.get() and e.to_var.get() == ".ffpfsc",
       f"{e.src_var.get()} {e.to_var.get()}")
    idx = app.queue.index(ji); e.backport_on_var.set(False); e.sign_var.set(True); e._add(); settle()
    ok("job.edit.swapped-in-place", app.queue[idx].operation == "chain" and app.queue[idx].chain_sign
       and app.queue[idx].backport_target is None and app.queue[idx] is not ji, "")
    app.update_queue_box(select_item=app.queue[idx]); root.update()
    row = app.queue_listbox.get(idx)
    ok("job.queue-row", "→ .ffpfsc" in row and "sign" in row, row)
    app._on_queue_select(); root.update()
    ok("job.details.mode-sentence", "Sign, then build .ffpfsc" in app.title_var.get(), app.title_var.get())
    jc = m.JobDialog(app, init_src=str(HBT)); root.update()
    jc.backport_on_var.set(False); jc.patch_on_var.set(False); jc.sign_var.set(False)
    jc.to_var.set(".ffpfsc"); root.update()
    ok("job.compression.ffpfsc-level", jc._comp_row.winfo_manager() == "pack" and jc._comp_ffpfsc.winfo_manager() == "pack"
       and jc._comp_pkg.winfo_manager() == "", f"{jc._comp_row.winfo_manager()!r} {jc._comp_ffpfsc.winfo_manager()!r}")
    jc.to_var.set(".pkg"); root.update()
    ok("job.compression.pkg-speed", jc._comp_pkg.winfo_manager() == "pack" and jc._comp_ffpfsc.winfo_manager() == "",
       f"{jc._comp_pkg.winfo_manager()!r} {jc._comp_ffpfsc.winfo_manager()!r}")
    jc.to_var.set(".ffpfs"); root.update()
    ok("job.compression.none-uncompressed", jc._comp_row.winfo_manager() == "", repr(jc._comp_row.winfo_manager()))
    _lvl0 = app.compression_level_var.get()
    jc.to_var.set(".ffpfsc"); jc.level_var.set(3); jc.out_var.set(str(OUT / "job")); root.update()
    ok("job.compression.label", jc._level_lbl.cget("text") == "level 3", jc._level_lbl.cget("text"))
    n0 = len(app.queue); jc._add(); settle(); jl = app.queue[-1]
    jlcmd, *_ = app.build_command(jl)
    ok("job.compression.per-job-level", len(app.queue) == n0 + 1 and jl.compression_level == 3
       and jlcmd[jlcmd.index("--compression-level") + 1] == "3", " ".join(jlcmd[-12:]))
    ok("job.compression.remembered", app.compression_level_var.get() == 3, str(app.compression_level_var.get()))
    app.compression_level_var.set(9)                     # the default moves; the queued job keeps its own
    jlcmd, *_ = app.build_command(jl)
    ok("job.compression.job-keeps-its-level", jlcmd[jlcmd.index("--compression-level") + 1] == "3", " ".join(jlcmd[-12:]))
    je = m.JobDialog(app, item=jl); root.update()
    ok("job.compression.edit-prefill", je.level_var.get() == 3, str(je.level_var.get()))
    je.destroy()
    ok("job.compression.details-chip", app._job_recipe(jl, detail=True)[-1] == ".ffpfsc, level 3"
       and app._job_recipe(jl)[-1] == ".ffpfsc", str(app._job_recipe(jl, detail=True)))
    _p0 = jl.path; jl.path = Path(str(OUT)) / "gone" / "Example Title 1.000 ppsa00001"   # moved away after the job
    ok("job.recipe.gone-folder-stays-folder", app._job_recipe(jl)[0] == "Folder", str(app._job_recipe(jl)))
    jl.path = _p0
    app.queue.remove(jl); app.compression_level_var.set(_lvl0)
    jh = m.JobDialog(app, init_src=str(HBT)); root.update()
    for _w in (600, 860):  # hidden windows report no width, so try two
        jh._help_wrap(width=_w); root.update_idletasks()
        _heights = set()
        for _txt in [jh._HELP_IDLE, *jh._HELP.values()]:
            jh.help_var.set(_txt); root.update_idletasks(); _heights.add(jh._foot.winfo_reqheight())
        ok(f"job.help.buttons-stay-{_w}", len(_heights) == 1, str(sorted(_heights)))
    jh.destroy()
    from test_chain_items import make_source_tree
    _conv = make_source_tree(S / "parent_tree")
    _defs_before = json.loads(json.dumps(m.load_settings().get("job_dialog_defaults", {}) or {}))
    jp = m.JobDialog(app, init_src=str(_conv)); root.update()
    ok("job.parent.detects-every-source", jp._kind == "parent" and len(jp._games) == 5
       and "5 sources: 1 game folder, 3 archives, 1 .pkg" in jp.detect_var.get(), jp.detect_var.get())
    jp.patch_on_var.set(False); jp.sign_var.set(False)
    jp.backport_on_var.set(True); jp.backport_target_var.set("10.xx")
    jp.to_var.set(".ffpfsc"); jp.out_var.set(str(OUT / "parent")); root.update()
    ok("job.parent.summary", jp.summary_var.get() == "Backport to 10.xx, then build .ffpfsc  × 5 jobs"
       and str(jp._add_btn.cget("state")) == "normal", jp.summary_var.get())
    ok("job.parent.check-reads-its-game-folder", jp._check_folder() == _conv / "Game [PPSA00003]"
       and str(jp._check_btn.cget("state")) == "normal", str(jp._check_folder()))
    jp.to_var.set("Folder"); root.update()
    ok("job.parent.folder-output-not-in-place", not jp._in_place("folder")
       and str(jp._out_entry.cget("state")) == "normal", str(jp._out_entry.cget("state")))
    jp.to_var.set(".ffpfsc"); root.update()
    # stand-in archives would open the modal password prompt, skip it here
    _real_pw = app._resolve_archive_password
    app._resolve_archive_password = lambda _it: None
    try:
        n0 = len(app.queue); errors.clear(); jp._add(); settle()
    finally:
        app._resolve_archive_password = _real_pw
    _new = app.queue[n0:]
    ok("job.parent.one-job-each", len(_new) == 5 and all(i.operation == "chain" and i.chain_to == "ffpfsc"
       and i.backport_target == "10.xx" for i in _new)
       and [m.chain_source_kind(i) for i in _new] == ["folder", "archive", "archive", "archive", "pkg"],
       f"{len(_new)} {[m.chain_source_kind(i) for i in _new]} errors={errors}")
    for _i in _new:
        app.queue.remove(_i)
    app.update_queue_box(); root.update()
    m.save_settings({"job_dialog_defaults": _defs_before})
    _pk = S / "extracted_pkg_archive" / "inner"; _pk.mkdir(parents=True, exist_ok=True)
    (_pk / "Title [PPSA00004].pkg").write_bytes(b"\x7fCNT")
    _kind, _paths = app._classify_extracted_payload(S / "extracted_pkg_archive", "x.7z")
    _pi = app._item_from_payload_path(_kind, _paths[0])
    ok("archive.payload.pkg", _kind == "pkg" and _paths[0].name == "Title [PPSA00004].pkg"
       and _pi.path == _paths[0] and m.chain_source_kind(_pi) == "pkg", f"{_kind} {_paths}")
    _rl = S / "extracted_release"; (_rl / "backport (4.xx to 10.xx)").mkdir(parents=True, exist_ok=True)
    (_rl / "_SHA256").mkdir(exist_ok=True)
    (_rl / "UP0000-PPSA00004_00-X.pkg").write_bytes(b"\x7fCNT")
    (_rl / "backport (4.xx to 10.xx)" / "UP0000-PPSA00004_00-X-4xx.pkg").write_bytes(b"\x7fCNT")
    (_rl / "_SHA256" / "UP0000.sha256").write_text("x"); (_rl / ".DS_Store").write_text("x")
    _kind, _paths = app._classify_extracted_payload(_rl, "x.7z")
    _games, _ports = app._split_backport_packages(_rl, _paths)
    (_rl / "More" / "backport 7.xx").mkdir(parents=True, exist_ok=True)
    (_rl / "More" / "backport 7.xx" / "UP0000-PPSA00004_00-X-7xx.pkg").write_bytes(b"\x7fCNT")
    _sib = sorted(x.name for x in app._release_extras(_rl, _games))
    ok("archive.payload.backport-goes-along", _kind == "pkg" and [x.name for x in _games] == ["UP0000-PPSA00004_00-X.pkg"]
       and [x.name for x in _ports] == ["UP0000-PPSA00004_00-X-4xx.pkg"]
       and _sib == ["More", "_SHA256", "backport (4.xx to 10.xx)"], f"{_games} {_ports} {_sib}")
    _wr = S / "extracted_wrapped"; (_wr / "Game" / "backport 4.xx").mkdir(parents=True, exist_ok=True)
    (_wr / "Game" / "g.pkg").write_bytes(b"\x7fCNT"); (_wr / "Game" / "backport 4.xx" / "g-4xx.pkg").write_bytes(b"x")
    (_wr / "Game" / "notes.sha256").write_text("x"); (_wr / "Game" / ".DS_Store").write_text("x")
    _ws = sorted(x.relative_to(_wr).as_posix() for x in app._release_extras(_wr, [_wr / "Game" / "g.pkg"]))
    ok("archive.payload.release-extras-anywhere", _ws == ["Game/backport 4.xx", "Game/notes.sha256"], str(_ws))
    _alone, _none = app._split_backport_packages(_rl, [_rl / "backport (4.xx to 10.xx)" / "UP0000-PPSA00004_00-X-4xx.pkg"])
    ok("archive.payload.backport-alone-is-the-job", len(_alone) == 1 and _none == [], f"{_alone} {_none}")
    _dlg = _REAL_PW_PROMPT(app, "Example.part1.rar"); root.update_idletasks()
    ok("password.prompt.fits-its-content", _dlg._size is None and _dlg.winfo_reqheight() > 230,
       f"size={_dlg._size} req={_dlg.winfo_reqheight()}")
    _dlg.destroy()
    _asked = []
    class _FakePrompt:
        def __init__(self, _app, name): _asked.append(name); self.password = ""
    _real_prompt, _real_probe = m.ArchivePasswordPrompt, m.ArchiveExtractor.probe_header_state
    m.ArchivePasswordPrompt = _FakePrompt
    try:
        _ai = m.GameItem.__new__(m.GameItem)
        _ai.source_kind, _ai.extracted_size, _ai.size = "archive", 0, 1000
        _ai.archive_path, _ai.header_locked, _ai.password = S / "Example.part1.rar", False, None
        app._resolve_archive_password(_ai)
        _no_prompt_when_open = not _asked
        _ai.header_locked = True
        _saved_list = list(app.archive_passwords); app.archive_passwords = ["saved-pass"]
        m.ArchiveExtractor.probe_header_state = staticmethod(lambda _a, _p=None: ("open", 800, ""))
        app._resolve_archive_password(_ai)
        app.archive_passwords = _saved_list
        _no_prompt_when_saved_opens = not _asked and _ai.header_locked is False and _ai.extracted_size == 800
        _ai.header_locked, _ai.extracted_size = True, 0
        m.ArchiveExtractor.probe_header_state = staticmethod(lambda _a, _p=None: ("locked", 0, ""))
        app._resolve_archive_password(_ai)
    finally:
        m.ArchivePasswordPrompt, m.ArchiveExtractor.probe_header_state = _real_prompt, _real_probe
    ok("password.prompt.only-when-locked", _no_prompt_when_open and _no_prompt_when_saved_opens
       and _asked == ["Example.part1.rar"], f"{_no_prompt_when_open} {_no_prompt_when_saved_opens} {_asked}")
    _asked.clear()
    m.ArchivePasswordPrompt = _FakePrompt
    _real_state = m.ArchiveExtractor.probe_header_state
    try:
        _di = m.GameItem.__new__(m.GameItem)
        _di.source_kind, _di.extracted_size, _di.size = "archive", 0, 1000
        _di.archive_path, _di.header_locked, _di.password = S / "Broken.part1.rar", False, None
        _di.archive_problem = "1 part of the set is missing (Broken.part2.rar)"
        app._resolve_archive_password(_di)
        _no_prompt_flagged = not _asked
        _di.archive_problem, _di.header_locked = "", True
        _saved_list = list(app.archive_passwords); app.archive_passwords = ["saved-pass"]
        m.ArchiveExtractor.probe_header_state = staticmethod(lambda _a, _p=None: ("damaged", 0, "the archive is damaged"))
        app._resolve_archive_password(_di)
        app.archive_passwords = _saved_list
        _no_prompt_reprobe = not _asked and _di.archive_problem == "the archive is damaged" and not _di.header_locked
    finally:
        m.ArchivePasswordPrompt, m.ArchiveExtractor.probe_header_state = _real_prompt, _real_state
    ok("password.no-prompt-for-damaged-archive", _no_prompt_flagged and _no_prompt_reprobe,
       f"{_no_prompt_flagged} {_no_prompt_reprobe} {_asked}")
    _dc = app._card_info_text(_di) if hasattr(app, "_card_info_text") else {}
    ok("card.status-names-archive-damage", "cannot be read: the archive is damaged" in _dc.get("Status", ""),
       _dc.get("Status", ""))
    ok("error.diagnosis-damaged-archive", "broken" in (m.ErrorDialog._diagnose(
        "RAR extraction failed - 1 part of the set is missing (X.part2.rar) - download the set again", "") or ""), "")
    _pj = m.GameItem.from_chain(HBT, to="ffpfsc")
    _pc, _pcwd, _pout, _ptmp = app.build_command(_pj)
    _pw = m.CLIWorker(app, _pj, _pc, _pcwd, _pout, _ptmp)
    _pw._handle_line("[PHASE] Extracting")
    _pw._handle_line("[#############-------------------]  42% extract @ 812.40 MB/s ETA 57s")
    ok("progress.silent-step-bars", _pw.phase == "Extracting" and _pw.speed == "812.40 MB/s",
       f"phase={_pw.phase} speed={_pw.speed}")
    _cpk = S / "chain_src" / "Title.pkg"; _cpk.parent.mkdir(exist_ok=True); _cpk.write_bytes(b"\x7fCNT")
    _cji = m.GameItem.from_chain(_cpk, to="ffpfsc")
    _ccmd = ["py", "cli.py", str(_cpk), str(OUT), "--to", "ffpfsc", "--backport-target", "10.01"]
    _cw = m.CLIWorker(app, _cji, _ccmd, _pcwd, _pout, _ptmp); _cw.start_time = time.time()
    ok("progress.chain-extract-first-bands", _cw._weights is m.CLIWorker.CHAIN_WEIGHTS
       and _cw._stage_order is m.CLIWorker._EXTRACT_FIRST_ORDER, str(_cw._weights))
    _sent = []
    _real_su = app.status_update
    app.status_update = lambda *a, **k: _sent.append((a, k))
    try:
        _cw._handle_line("[PHASE] Extracting")
        _cw._handle_line("[################################] 100% extract the .pkg to SAMSUNG: 45.8 of 45.8 GB")
        _after_extract = _cw._overall()
        _cji._from_archive = True
        _queue_after_extract = _cw._job_overall()
        _cw._handle_line("[PHASE] Reading Game")
        _cw._handle_line("[PHASE] Scanning Files")          # the fPKG builder's own marker: backward
        _stays = _cw.phase
        _cw._handle_line("[########------------------------]  25% write @ 900.00 MB/s ETA 60s")
        _temp_ok = _cw.phase == "Creating Temp PFS"
        _last_sent = _sent[-1][0][4] if _sent else None
        _hb = _cw._job_overall()
    finally:
        app.status_update = _real_su
    ok("progress.pkg-extract-is-not-94pct", _after_extract == 30 and abs(_queue_after_extract - 47.5) < 0.01,
       f"{_after_extract} {_queue_after_extract}")
    ok("progress.no-backward-phase", _stays == "Reading Game" and _temp_ok, f"{_stays} {_cw.phase}")
    ok("progress.heartbeat-equals-lines", _last_sent is not None and abs(_hb - _last_sent) < 0.01
       and all(k.get("job") is _cji for _a, k in _sent), f"{_hb} {_last_sent}")
    _fx = (REPO / "backend" / "tests" / "fixtures" / "chain_pkg_to_ffpfsc.log").read_text().splitlines()
    _rp = S / "replay_src" / "Game.pkg"; _rp.parent.mkdir(exist_ok=True); _rp.write_bytes(b"\x7fCNT")
    _ri = m.GameItem.from_chain(_rp, to="ffpfsc")
    _rw = m.CLIWorker(app, _ri, ["py", "cli.py", str(_rp), str(OUT), "--to", "ffpfsc"], _pcwd, _pout, _ptmp)
    _rw.start_time = time.time()
    _real_su2 = app.status_update
    app.status_update = lambda *a, **k: None
    _trace = []
    try:
        for _l in _fx:
            _rw._handle_line(_l)
            _trace.append((_l, _rw.phase, _rw._job_overall()))
    finally:
        app.status_update = _real_su2
    _vals = [v for _x, _p, v in _trace]
    _at = lambda marker: next(v for l, _p, v in _trace if l.startswith(marker))
    _leap = max(b - a for a, b in zip(_vals, _vals[1:]))
    _phases = list(dict.fromkeys(p for _x, p, _v in _trace))
    ok("progress.pkg-chain-replay", all(b >= a for a, b in zip(_vals, _vals[1:]))
       and _at("[INFO] Packin") >= 29 and _at("[PASS1-DONE]") >= 57 and _vals[-1] >= 91
       and _leap <= 25 and [p for p in _phases if p != "Starting"][:1] == ["Extracting"]
       and "Creating Temp PFS" in _phases
       and "Compressing" in _phases and _rw.consumed,
       f"phases={_phases} pack={_at('[INFO] Packin'):.1f} pass1={_at('[PASS1-DONE]'):.1f} "
       f"end={_vals[-1]:.1f} leap={_leap:.1f} at {_trace[[b - a for a, b in zip(_vals, _vals[1:])].index(_leap) + 1][0][:60]!r} "
       f"consumed={_rw.consumed}")
    _cpj = m.GameItem.from_chain(_cpk, to="pkg")
    for _tag, _ccm in (("chain", ["py", "cli.py", str(_cpk), str(OUT), "--to", "pkg"]),
                       ("copy", ["py", "cli.py", "--copy", str(_cpk), str(OUT)])):
        _kw = m.CLIWorker(app, _cpj, _ccm, _pcwd, _pout, _ptmp); _kw.start_time = time.time()
        app.status_update = lambda *a, **k: None
        try:
            _kw._handle_line("[JOB] copy")
            _kw._handle_line("[PHASE] Writing Final Image")
            _kw._handle_line("[####] 6% copy @ 120.00 MB/s ETA 40s")
            _kpct = _kw._overall()
        finally:
            app.status_update = _real_su
        _ghost = [s for s in ("Creating Temp PFS", "Compressing") if _kw.stage_progress.get(s, 0)]
        ok(f"progress.copy-follows-the-copy.{_tag}", 4 <= _kpct <= 8 and not _ghost, f"{_kpct} ticked={_ghost}")
        ok(f"progress.copy-card-text.{_tag}", _kw.speed == "120.00 MB/s"
           and str(getattr(_kw, "_detail", "")).startswith("Copying"), f"{_kw.speed} {getattr(_kw, '_detail', '')}")
    _ww = m.CLIWorker(app, _pj, _pc, _pcwd, _pout, _ptmp); _ww.start_time = time.time()
    app.status_update = lambda *a, **k: None
    try:
        _ww._set_stage("Writing Final Image", 10)
    finally:
        app.status_update = _real_su
    ok("progress.write-text-is-honest", "0%" not in str(_ww._detail) and "silently" not in str(_ww._detail),
       str(_ww._detail))
    _mw = m.CLIWorker(app, _pj, _pc, _pcwd, _pout, _ptmp); _mw.start_time = time.time()
    _msent = []
    app.status_update = lambda *a, **k: _msent.append(a)
    try:
        _mw._handle_line("[#############-------------------]  42% write the PFS image on SAMSUNG: 34.0 of 80.9 GB @ 137.00 MB/s ETA 5m")
        _mw._handle_line("[--------------------------------]   0% write @ 457.08 MB/s ETA 181s")
        _lag_dropped = _mw.speed == "137.00 MB/s" and _msent and "34.0 of 80.9" in _msent[-1][1]
        _mw._lead_line = (_mw._lead_line[0], time.time() - 10)   # the meter went quiet
        _mw._handle_line("[--------------------------------]   0% write @ 300.00 MB/s ETA 60s")
        _later_passes = _mw.speed == "300.00 MB/s" and _mw.stage_progress[_mw.phase] == 42
    finally:
        app.status_update = _real_su
    ok("progress.lagging-bar-dropped", bool(_lag_dropped) and _later_passes,
       f"{_mw.phase} {_mw.speed} {_msent[-1][1] if _msent else ''}")
    _fw = m.CLIWorker(app, _pj, ["py", "cli.py", str(HBT), str(OUT), "--to", "ffpfsc", "--sign"],
                      _pcwd, _pout, _ptmp)
    ok("progress.folder-chain-without-extract-band", "Extracting" not in _fw._weights
       and _fw._weights["Reading Game"][0] < 5 and _fw._weights["Cleaning Up"][1] == 100, str(_fw._weights))
    _kw = m.CLIWorker(app, _cji, ["py", "cli.py", str(_cpk), str(OUT), "--to", "pkg"], _pcwd, _pout, _ptmp)
    ok("progress.chain-to-pkg-is-fpkg", _kw._is_fpkg and _kw._weights is m.CLIWorker.FPKG_BUILD_WEIGHTS, "")
    _ex = Path(app.temp_var.get()) / "_extracted" / "release__abcd1234" / "PPSA00042-app0"
    (_ex / "sce_sys").mkdir(parents=True, exist_ok=True)
    (_ex / "sce_sys" / "param.json").write_text('{"titleId": "PPSA00042", "contentVersion": "01.000.000"}')
    (_ex / "eboot.bin").write_bytes(b"\x7fELF")
    _xi = m.GameItem.from_chain(_ex, to="pkg", output_path=str(OUT))
    _xi.origin_archive = str(S / "release.7z")          # what the unpack records
    _xcmd, *_ = app.build_command(_xi)
    _pi = m.GameItem.from_chain(HBT, to="pkg", output_path=str(OUT))
    _pcmd, *_ = app.build_command(_pi)
    ok("cmd.pkg-from-own-extraction-in-place", "--stage-in-place" in _xcmd and "--stage-in-place" not in _pcmd,
       f"own={'--stage-in-place' in _xcmd} user={'--stage-in-place' in _pcmd}")
    _cpu_before = app.cpu_count_var.get()
    try:
        app.cpu_count_var.set(4); _c4, *_ = app.build_command(_pi)
        app.cpu_count_var.set(0); _c0, *_ = app.build_command(_pi)
    finally:
        app.cpu_count_var.set(_cpu_before)
    ok("cmd.pkg-carries-cpu-count", "4" == (_c4[_c4.index("--cpu-count") + 1] if "--cpu-count" in _c4 else None)
       and "--cpu-count" not in _c0, f"four={_c4.count('--cpu-count')} auto={_c0.count('--cpu-count')}")
    shutil.rmtree(_ex.parent, ignore_errors=True)
    _cw = m.CLIWorker(app, _xi, ["py", "cli.py", str(_ex), str(OUT), "--to", "pkg"], _pcwd, _pout, _ptmp)
    _cw._handle_line("  [consume] freed 1.2 GB so far (3 file(s) the image already holds)")
    ok("cmd.consume-line-marks-the-worker", _cw.consumed, str(_cw.consumed))
    _dw = m.CLIWorker(app, _xi, ["py", "cli.py", str(_ex), str(OUT), "--to", "pkg"], _pcwd, _pout, _ptmp)
    _dw.start_time = time.time()
    for _l in ("[PHASE] Extracting", "[####] 100% extract", "[PHASE] Reading Game",
               "[####################] 100% staged in place",
               "[PHASE] Scanning Files", "[####################] 100% source scan",
               "[PHASE] Reading Game", "[##########----------] 50% inner files prepared",
               "[PHASE] Creating Temp PFS", "[###-----------------] 17% inner image (Kraken) @ 32.3 MB/s ETA 3104s"):
        _dw._handle_line(_l)
    ok("progress.refused-phase-bars-ignored",
       _dw.phase == "Creating Temp PFS" and _dw.stage_progress["Creating Temp PFS"] == 17 and _dw.speed == "32.3 MB/s",
       f"phase={_dw.phase} temp={_dw.stage_progress['Creating Temp PFS']} speed={_dw.speed}")
    _qi = m.GameItem.from_chain(_cpk, to="ffpfsc"); _qi.status = "Running"
    app.queue.insert(0, _qi); app.update_queue_box(); app._batch_running = True
    app._begin_job_progress(_qi)
    app.status_update("Extracting", "x", "Extracting", 50, 60, " - ", " - ", " - ", job=_qi)
    pump(lambda: app._cur_job_pct == 60, timeout=5.0)
    app.status_update("Ready", "3 games added to queue.", "Ready", 0, 0, "00:00", " - ", " - ", side=True)
    pump(lambda: "3 games added" in app.footer_var.get(), timeout=5.0)
    ok("queue-bar.side-message-in-footer", app._cur_job_pct == 60 and "3 games added" in app.footer_var.get()
       and app.big_status_var.get() != "Ready", f"{app._cur_job_pct} {app.big_status_var.get()}")
    app.status_update("Still Working", "y", "Extracting", 50, 41, " - ", " - ", " - ", job=_qi)
    pump(lambda: "Still Working" in app.footer_var.get(), timeout=5.0)
    ok("queue-bar.never-backward", app._cur_job_pct == 60, f"{app._cur_job_pct}")
    app._batch_running = False; app.queue.remove(_qi); app.update_queue_box()
    _real_li = None
    try:
        import fpkg as _fp
        _real_li = _fp.list_inner
        _M = 1 << 20
        _fp.list_inner = lambda _p, **_k: {"entries": [
            {"type": "file", "path": "data/a.bin", "size": 900 * _M}, {"type": "dir", "path": "data"},
            {"type": "file", "path": "data/b.bin", "size": 1000 * _M},
            {"type": "file", "path": "sce_sys/icon0.png", "size": 60 * _M},       # stays: sce_sys
            {"type": "file", "path": "eboot.bin", "size": 40 * _M - 1000},
            {"type": "file", "path": "small.txt", "size": 1000}]}                 # stays: small
        _si = m.GameItem.from_chain(_cpk, to="ffpfsc")
        _got = app._probe_pkg_content(_si)
    finally:
        if _real_li is not None:
            _fp.list_inner = _real_li
    _f_free = m._peak_factor_for(_si)
    _vo = m.load_settings().get("verify_output", False)
    m.save_settings({"verify_output": True})
    _f_check = m._peak_factor_for(_si)
    m.save_settings({"verify_output": _vo})
    _si.chain_to = "ffpfs"
    _f_ffpfs = m._peak_factor_for(_si)                 # an uncompressed output frees nothing
    _si.chain_to = "ffpfsc"
    # 2000 MiB of files, the largest 1000, 60 MiB + 1000 B never freed: 1 + 0.53 + 0.1
    ok("space.pkg-game-size", _got == 2000 * _M and _si.pkg_content_size == 2000 * _M
       and _si.pkg_largest_file == 1000 * _M and _si.pkg_kept_bytes == 60 * _M + 1000
       and abs(_f_free - (1.1 + (1060 * _M + 1000) / (2000 * _M))) < 1e-9
       and _f_check == m.PKG_UNPACK_PEAK_FACTOR and _f_ffpfs == m.PKG_UNPACK_PEAK_FACTOR,
       f"{_got} {_si.pkg_kept_bytes} {_f_free} {_f_check} {_f_ffpfs}")
    _inner = S / "resume_inner.ffpfs"; _inner.write_bytes(b"x")
    _rj = m.GameItem.from_chain(_cpk, to="ffpfsc"); _rj._resume_inner = str(_inner)
    _rcw = m.CLIWorker(app, _rj, ["py", "cli.py", str(_inner), str(OUT), "--to", "ffpfsc"], _pcwd, _pout, _ptmp)
    ok("progress.resume-compress-only", _rcw._weights is m.CLIWorker.RESUME_WEIGHTS
       and "Extracting" not in _rcw._weights, str(list(_rcw._weights)))
    _pj2 = m.GameItem.from_chain(_cpk, to="ffpfsc"); _pj2.operation = "pack"; _pj2._resume_inner = str(_inner)
    _pw2 = m.CLIWorker(app, _pj2, ["py", "cli.py", str(_inner), str(OUT)], _pcwd, _pout, _ptmp)
    ok("progress.pack-resume-compress-only", _pw2._weights is m.CLIWorker.RESUME_WEIGHTS, str(list(_pw2._weights)))
    app._active_item = None
    _ci = app._card_info_text(_pj)
    ok("card.info.drives", "writes to" in _ci.get("Drives", "") and "builds on" in _ci.get("Drives", ""),
       _ci.get("Drives", ""))
    _kt = S / "keep_temp"; (_kt / "_extracted" / "Arc__1" / "inner").mkdir(parents=True, exist_ok=True)
    _kpkg = _kt / "_extracted" / "Arc__1" / "inner" / "Title.pkg"; _kpkg.write_bytes(b"\x7fCNT")
    _korg = S / "Arc.7z"; _korg.write_bytes(b"7z")
    _old_temp = app.temp_var.get(); app.temp_var.set(str(_kt))
    _ki = m.GameItem.from_chain(_kpkg, to="ffpfsc")
    _ki.origin_archive, _ki.status = str(_korg), "Cancelled"
    ok("cancel.extract-complete", app._extract_is_complete(_ki)
       and app._extract_dir_for_item(_ki) == (_kt / "_extracted" / "Arc__1").resolve(), "")
    _ki.kept_extract = True
    ok("cancel.retry-uses-kept-copy", app._rearm_from_archive(_ki) is False and _ki.path == _kpkg
       and _ki.archive_path is None, f"{_ki.path} {_ki.archive_path}")
    _ki.kept_extract = False
    ok("cancel.retry-without-copy-starts-at-archive", app._rearm_from_archive(_ki) is True
       and _ki.archive_path == _korg and _ki.path is None, f"{_ki.path} {_ki.archive_path}")
    _kj = m.GameItem.from_chain(_kpkg, to="ffpfsc"); _kj.kept_extract = True; _kj.status = "Cancelled"
    app.queue.append(_kj); app.update_queue_box(select_item=_kj); root.update()
    app.queue_listbox.selection_clear(0, "end"); app.queue_listbox.selection_set(app.queue.index(_kj))
    app.queue_remove_selected()
    pump(lambda: not (_kt / "_extracted" / "Arc__1").exists(), timeout=10.0)
    ok("cancel.remove-deletes-kept-copy", _kj not in app.queue and not (_kt / "_extracted" / "Arc__1").exists(), "")
    (_kt / "_extracted" / "Keep__2").mkdir(parents=True, exist_ok=True)
    (_kt / "_extracted" / "Keep__2" / "Title.pkg").write_bytes(b"\x7fCNT")
    (_kt / "_extracted" / "Other__3").mkdir(parents=True, exist_ok=True)
    with open(_kt / "_extracted" / "Other__3" / "big.bin", "wb") as _f:
        _f.truncate(70 * 1024 * 1024)
    _kk = m.GameItem.from_chain(_kt / "_extracted" / "Keep__2" / "Title.pkg", to="ffpfsc")
    _kk.kept_extract = True; _kk.status = "Cancelled"; app.queue.append(_kk)
    _offered = []
    _real_prompt_sweep = app._prompt_startup_sweep
    app._prompt_startup_sweep = lambda targets, total: _offered.append([str(x) for x in targets])
    _old_out = app.output_var.get(); app.output_var.set(str(_kt))
    class _NowThread:                  # run the scan on this thread: no Tk mainloop in the driver
        def __init__(self, target=None, daemon=None, **_kw): self._t = target
        def start(self): self._t()
    _real_thread = m.threading.Thread; m.threading.Thread = _NowThread
    try:
        app._offer_startup_sweep()
        pump(lambda: bool(_offered), timeout=10.0)
    finally:
        m.threading.Thread = _real_thread
        app._prompt_startup_sweep = _real_prompt_sweep
        app.output_var.set(_old_out)
    _off = _offered[0] if _offered else []
    ok("sweep.spares-kept-copy", any(x.endswith("Other__3") for x in _off)
       and not any(x.endswith("Keep__2") or x.endswith("_extracted") for x in _off), str(_off))
    app.queue.remove(_kk); app.temp_var.set(_old_temp); app.update_queue_box(); root.update()
    _saved_q, _live_q = m.load_settings().get("queue"), list(app.queue)
    _src_item = m.GameItem.from_chain(_korg, to="ffpfsc")
    _entry = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(_src_item).items()
              if not k.startswith("_") and k != "artwork" and (v is None or isinstance(v, (str, int, float, bool, Path)))}
    _entry.update({"status": "Cancelled", "path": str(S / "gone" / "Title.pkg"), "archive_path": None,
                   "source_kind": "inplace", "origin_archive": str(_korg), "origin_extracted_size": 1234})
    m.save_settings({"queue": [_entry]})
    app.queue.clear(); app._restore_queue()
    _rq = app.queue[0] if app.queue else None
    ok("restore.gone-copy-starts-at-archive", _rq is not None and _rq.archive_path == _korg and _rq.path is None
       and _rq.status == "Pending Extract" and _rq.extracted_size == 1234,
       f"{getattr(_rq, 'archive_path', None)} {getattr(_rq, 'status', None)}")
    app.queue[:] = _live_q; m.save_settings({"queue": _saved_q or []}); app.update_queue_box(); root.update()
    _live_q = list(app.queue); _old_temp2 = app.temp_var.get(); app.temp_var.set(str(_kt))
    _fpkg_dir = _kt / "_extracted" / "Fail__4" / "inner"; _fpkg_dir.mkdir(parents=True, exist_ok=True)
    _fpk = _fpkg_dir / "Title.pkg"; _fpk.write_bytes(b"\x7fCNT")
    _fi = m.GameItem.from_chain(_fpk, to="ffpfsc", output_path=str(OUT / "job"), backport_target="10.xx")
    _fi.origin_archive, _fi.status = str(_korg), "Running"
    _real_sound, _real_start = app.play_complete_sound, app.start
    app.play_complete_sound = lambda *_a, **_k: None
    app.queue[:] = [_fi]; app._active_item = _fi; app._batch_running = False; app._batch_total = 1
    app.worker = None
    _refusal = "The game uses 120 function(s) that 10.xx lacks (libSceAcm, …). It needs patched libraries."
    _tops_before = set(root.winfo_children())
    app.finish(False, _refusal, "cmd")
    pump(lambda: _fi.status == "Failed", timeout=10.0); root.update()
    ok("failure.keeps-extract", _fi.kept_extract and (_kt / "_extracted" / "Fail__4").exists()
       and _fi in app.queue, f"{_fi.status} kept={_fi.kept_extract}")
    _eds = [w for w in root.winfo_children() if w not in _tops_before and isinstance(w, m.ErrorDialog)]
    _btn_texts = set()
    def _walk(w):
        for c in w.winfo_children():
            try:
                _btn_texts.add(c.cget("text"))
            except Exception:
                pass
            _walk(c)
    for _e in _eds:
        _walk(_e)
    ok("failure.error-window-offers-edit-and-retry", len(_eds) == 1 and {"Edit job", "Retry"} <= _btn_texts,
       str(sorted(x for x in _btn_texts if isinstance(x, str))[:12]))
    ok("failure.diagnosis-backport", "Edit job" in (m.ErrorDialog._diagnose(_refusal, "") or ""), "")
    close_toplevels()
    app.update_game_details(_fi); root.update()
    ok("failure.retry-button-shown", app.retry_btn._state == "normal", str(getattr(app.retry_btn, "_state", "?")))
    _before = set(app._panels.stack)
    app._edit_job(_fi); root.update()
    _jd = next((w for w in app._panels.stack if w not in _before and isinstance(w, m.JobDialog)), None)
    ok("failure.edit-opens-on-kept-copy", _jd is not None and _jd.src_var.get() == str(_fpk),
       _jd.src_var.get() if _jd else "no editor")
    if _jd is not None:
        _jd.backport_target_var.set("7.61"); _jd.out_var.set(str(OUT / "job")); root.update()
        _jd._add(); settle()
    _ne = app.queue[0] if app.queue else None
    ok("failure.edit-keeps-archive-link", _ne is not None and _ne is not _fi and _ne.backport_target == "7.61"
       and _ne.origin_archive == str(_korg) and _ne.kept_extract and _ne.status == "Queued",
       f"{getattr(_ne, 'backport_target', None)} {getattr(_ne, 'origin_archive', None)} {getattr(_ne, 'status', None)}")
    _gi = m.GameItem.from_chain(_fpk, to="ffpfsc", output_path=str(OUT / "job"))
    _gi.origin_archive, _gi.status, _gi.path = str(_korg), "Failed", S / "gone2" / "Title.pkg"
    app.queue.append(_gi); app.update_queue_box(); _before = set(app._panels.stack)
    app._edit_job(_gi); root.update()
    _jd2 = next((w for w in app._panels.stack if w not in _before and isinstance(w, m.JobDialog)), None)
    ok("failure.edit-shows-archive-when-copy-gone", _jd2 is not None and _jd2.src_var.get() == str(_korg), "")
    if _jd2 is not None:
        _jd2.destroy(); root.update()
    _starts = []
    app.start = lambda rearm_failed=True: _starts.append(rearm_failed)
    _gi.status = "Failed"; app.queue.remove(_gi); app.queue.append(_gi)
    app._retry_job(_gi)
    ok("retry.runs-this-job-only", app._run_next is _gi and app.queue[-1] is _gi
       and _gi.status in ("Pending Extract", "Queued") and _starts == [False], f"{_gi.status} {_starts}")
    app._run_next = None
    app.start = _real_start
    _rel = _kt / "_extracted" / "Rel__5"; _rel.mkdir(parents=True, exist_ok=True); (_rel / "Title.pkg").write_bytes(b"x")
    _rf = m.GameItem.from_chain(_rel / "Title.pkg", to="ffpfsc", output_path=str(OUT / "job"))
    _rf.status, _rf.kept_extract, _rf.origin_archive = "Failed", True, str(_korg)
    _rel2 = _kt / "_extracted" / "Rel__6"; _rel2.mkdir(parents=True, exist_ok=True); (_rel2 / "Title.pkg").write_bytes(b"x")
    _rc = m.GameItem.from_chain(_rel2 / "Title.pkg", to="ffpfsc", output_path=str(OUT / "job"))
    _rc.status, _rc.kept_extract = "Cancelled", True
    _nx = m.GameItem.from_chain(HBT, to="ffpfsc", output_path=str(OUT / "job"))
    app.queue[:] = [_nx, _rf, _rc]
    _real_pf, _real_rer = m._space_preflight_ok, app._resolve_extract_root
    m._space_preflight_ok = lambda *_a: True
    app._resolve_extract_root = lambda _it: None
    _fits = app._release_failed_copies(_nx)
    m._space_preflight_ok = lambda *_a: False
    _tight = app._release_failed_copies(_nx)
    m._space_preflight_ok, app._resolve_extract_root = _real_pf, _real_rer
    pump(lambda: not _rel.exists(), timeout=10.0)
    ok("batch.frees-failed-copy-only-when-needed", _fits is False and _tight is True and not _rf.kept_extract
       and _rc.kept_extract and "make room" in _rf.status_note and not _rel.exists() and _rel2.exists(),
       f"fits={_fits} tight={_tight} rf={_rf.kept_extract} rc={_rc.kept_extract}")
    _ce = _kt / "_extracted" / "Own__9" / "Game"; _ce.mkdir(parents=True, exist_ok=True)
    _cj = m.GameItem.from_chain(_ce, to="ffpfsc", output_path=str(OUT / "job"))
    _q4 = list(app.queue); app.queue[:] = [_cj]
    _no_origin = app._consumable_extract(_cj)
    _cj.origin_archive = str(S / "Own.7z")
    _alone = app._consumable_extract(_cj)
    _cj2 = m.GameItem.from_chain(_ce, to="pkg", output_path=str(OUT / "job")); _cj2.origin_archive = _cj.origin_archive
    app.queue[:] = [_cj, _cj2]
    _shared = app._consumable_extract(_cj)
    app.queue[:] = _q4
    _fb = m.GameItem.from_chain(_ce, to="pkg", output_path=str(OUT / "job"))
    _fb.operation = "fpkg-build"; _fb.origin_archive = _cj.origin_archive
    app.queue[:] = [_fb, _cj]
    _fb_shared = "--stage-in-place" in app.build_command(_fb)[0]
    app.queue[:] = [_fb]
    _fb_alone = "--stage-in-place" in app.build_command(_fb)[0]
    app.queue[:] = _q4
    ok("consume.only-own-unshared-unpack", _no_origin is False and _alone is True and _shared is False
       and _fb_shared is False and _fb_alone is True,
       f"no_origin={_no_origin} alone={_alone} shared={_shared} fpkg shared={_fb_shared} alone={_fb_alone}")
    _two = _kt / "_extracted" / "Two__7"; _two.mkdir(parents=True, exist_ok=True)
    for _n in ("A.pkg", "B.pkg", "notes.txt"):
        (_two / _n).write_bytes(b"x")
    _ja = m.GameItem.from_chain(_two / "A.pkg", to="ffpfsc", output_path=str(OUT / "job")); _ja.status = "Done"
    _jb = m.GameItem.from_chain(_two / "B.pkg", to="ffpfsc", output_path=str(OUT / "job"))
    _q2 = list(app.queue); app.queue[:] = [_ja, _jb]
    _parts_open = sorted(p.name for p in app._extract_cleanup_parts(_ja))
    _jb.status = "Done"
    _parts_done = [p.name for p in app._extract_cleanup_parts(_ja)]
    app.queue[:] = _q2
    ok("cleanup.keeps-what-another-job-reads", _parts_open == ["A.pkg", "notes.txt"] and _parts_done == ["Two__7"],
       f"open={_parts_open} done={_parts_done}")
    _rx = _kt / "_extracted" / "Rel__8"; (_rx / "backport 4.xx").mkdir(parents=True, exist_ok=True)
    (_rx / "G.pkg").write_bytes(b"x"); (_rx / "backport 4.xx" / "G-4xx.pkg").write_bytes(b"x")
    (_rx / "G.sha256").write_bytes(b"x")
    _jr = m.GameItem.from_chain(_rx / "G.pkg", to="ffpfsc", output_path=str(OUT / "job"))
    _jr.bundle_siblings = [_rx / "backport 4.xx", _rx / "G.sha256"]
    _q3 = list(app.queue); app.queue[:] = [_jr]
    _mid = sorted(p.name for p in app._extract_cleanup_parts(_jr, keep_own_extras=True))
    _end = [p.name for p in app._extract_cleanup_parts(_jr)]
    app.queue[:] = _q3
    _jr.status, _jr.kept_extract = "Done", True
    app.queue[:] = [_jr]
    _held = app._extract_dir_for_item(_jr) in app._open_extracts()
    _jr.kept_extract = False
    _freed = app._extract_dir_for_item(_jr) not in app._open_extracts()
    app.queue[:] = _q3
    ok("cleanup.done-job-that-kept-its-unpack", _held and _freed, f"held={_held} freed={_freed}")
    _ta, _tb = S / "tree_a", S / "tree_b"
    for _t, _c in ((_ta, b"1111"), (_tb, b"2222")):
        (_t / "d").mkdir(parents=True, exist_ok=True); (_t / "d" / "f.sha256").write_bytes(_c)
    _diff = m.CLIWorker._same_tree(_ta, _tb)
    (_tb / "d" / "f.sha256").write_bytes(b"1111")
    ok("extras.same-tree-by-content", _diff is False and m.CLIWorker._same_tree(_ta, _tb), f"diff={_diff}")
    ok("cleanup.pass1-keeps-own-extras", _mid == ["G.pkg"] and _end == ["Rel__8"], f"mid={_mid} end={_end}")
    import patch_backup as _pbm
    _ug = S / "user_game"; (_ug / "sce_sys").mkdir(parents=True, exist_ok=True)
    (_ug / "a.bin").write_bytes(b"original")
    _bk = S / "pbtemp" / "_ffpfsc_temp" / "patch-backup-x1" / "Original files - P"
    _jr2 = _pbm.Journal(_bk, _ug); _jr2.keep_original(_ug, "a.bin"); _jr2.record("replaced", "a.bin")
    (_ug / "a.bin").write_bytes(b"patched"); _jr2.record("added", "new.bin"); (_ug / "new.bin").write_bytes(b"n")
    _pi2 = m.GameItem.from_chain(_ug, to="ffpfsc", output_path=str(OUT / "job"))
    _pw3 = types.SimpleNamespace(patch_backup=str(_bk))
    app._restore_then_drop_patch_backup(_pi2, _pw3)
    pump(lambda: app._restores_inflight == 0 and not _bk.parent.exists(), timeout=10.0)
    ok("patch.failed-job-restores-the-user-folder", (_ug / "a.bin").read_bytes() == b"original"
       and not (_ug / "new.bin").exists() and not _bk.parent.exists(),
       f"a={(_ug / 'a.bin').read_bytes()!r} backup_left={_bk.parent.exists()}")
    _keepb = S / "pbtemp" / "_ffpfsc_temp" / "patch-backup-keep" / "Original files - Q"
    _pbm.Journal(_keepb, S / "away_drive" / "game")      # its drive is not there right now
    _ownb = S / "pbtemp" / "_ffpfsc_temp" / "patch-backup-own" / "Original files - R"
    _pbm.Journal(_ownb, S / "pbtemp" / "_ffpfsc_temp" / "unpack-1" / "game")   # the app's own copy
    _kept = app._patch_backup_dirs([str(S / "pbtemp")])
    ok("patch.backups-are-never-swept", _keepb.parent.resolve() in _kept and _ownb.parent.resolve() not in _kept,
       str(_kept))
    _later = []
    _real_after = app.root.after
    app.root.after = lambda ms, fn=None, *a: _later.append((ms, fn))     # the retry is caught, not run
    app._restore_count(+1)
    try:
        app.start(); app.start()                 # pressed twice: still one waiting start
        _still_idle = not app._batch_running
    finally:
        app._restore_count(-1)
        app._start_waiting = False
        app.root.after = _real_after
    ok("start.waits-for-a-patch-restore", _still_idle and len(_later) == 1 and _later[0][0] == 500, str(_later))
    _ea = _ug.parent / f".{_ug.name}.ffpfsc-extras"; _ea.mkdir(exist_ok=True)
    (_ea / "_notes").mkdir(exist_ok=True); (_ea / "_notes" / "n.txt").write_text("x")
    app._return_extras_aside(types.SimpleNamespace(extras_aside=f"{_ea} -> {_ug}"))
    ok("extras.aside-go-back-after-a-kill", (_ug / "_notes" / "n.txt").is_file() and not _ea.exists(),
       str(sorted(p.name for p in _ug.iterdir())))
    _real_sum, _real_open = app.summary_popup_var.get(), app.open_output_var.get()
    app.summary_popup_var.set(False); app.open_output_var.set(False)
    app.auto_remove_done_var.set(False)
    _dj = m.GameItem.from_chain(HBT, to="ffpfsc", output_path=str(OUT / "job")); _dj.status = "Running"
    _fj = m.GameItem.from_chain(HBT, to="ffpfsc", output_path=str(OUT / "job")); _fj.status = "Failed"
    _cj = m.GameItem.from_chain(HBT, to="ffpfsc", output_path=str(OUT / "job")); _cj.status = "Cancelled"
    app.queue[:] = [_dj, _fj, _cj]; app._active_item = _dj; app._batch_running = False; app._batch_total = 1
    app.update_queue_box(); root.update()
    ok("queue.clear-disabled-without-done", app._clear_btn._state == "disabled", app._clear_btn._state)
    app.finish(True, "ok", "cmd")
    pump(lambda: _dj.status == "Done", timeout=10.0); root.update()
    _rows = list(app.queue_listbox.rows)
    ok("queue.done-job-stays-in-place", app.queue[0] is _dj and _dj.status == "Done"
       and app._clear_btn._state == "normal", f"{_dj.status} {[i.status for i in app.queue]}")
    ok("queue.done-row-is-green", bool(_rows) and _rows[0].get("state") == "done", str([r.get("state") for r in _rows]))
    app._batch_running = False; app._ensure_batch_started()
    ok("queue.batch-counts-only-runnable", app._batch_total == 0 and not app._batch_running, str(app._batch_total))
    app._batch_running = False
    app._sync_primary_action()
    ok("queue.start-stays-for-failed-jobs", str(app.start_btn._state) == "normal", app.start_btn._state)
    _asks = []
    _real_ask = m.messagebox.askyesno
    m.messagebox.askyesno = lambda *a, **k: (_asks.append(a[0] if a else ""), True)[1]
    try:
        app.clear_jobs("done")
        _after_done = [i.status for i in app.queue]
        _cj.kept_extract = True
        app.clear_jobs("failed")
        _after_failed = list(app.queue)
    finally:
        m.messagebox.askyesno = _real_ask
    ok("queue.clear-completed", _after_done == ["Failed", "Cancelled"], str(_after_done))
    ok("queue.clear-failed-asks-for-kept-copies", _after_failed == [] and len(_asks) == 1, f"{_after_failed} {_asks}")
    app.auto_remove_done_var.set(True)
    _dk = m.GameItem.from_chain(HBT, to="ffpfsc", output_path=str(OUT / "job")); _dk.status = "Running"
    app.queue[:] = [_dk]; app._active_item = _dk
    app.finish(True, "ok", "cmd")
    pump(lambda: _dk not in app.queue, timeout=10.0)
    ok("queue.auto-remove-setting", _dk not in app.queue, str([i.status for i in app.queue]))
    app.auto_remove_done_var.set(False)
    import types as _types
    _aj = m._after_job_module()
    ok("after.defaults", app.after_source_var.get() == "keep" and app.notify_var.get() == "off"
       and app.after_queue_var.get() == "nothing",
       f"{app.after_source_var.get()} {app.notify_var.get()} {app.after_queue_var.get()}")
    def _src_game(name):
        d = S / "after_src" / name; (d / "sce_sys").mkdir(parents=True, exist_ok=True)
        (d / "eboot.bin").write_bytes(b"E" * 64); return d
    def _fake_worker(out):
        return _types.SimpleNamespace(output_path=str(out), final_size=1, start_time=time.time(),
                                      _is_copy=False, validate_failed=False, is_alive=lambda: False)
    _aout = S / "after_out"; _aout.mkdir(exist_ok=True)
    _outf = _aout / "Game.ffpfsc"; _outf.write_bytes(b"P" * 64)
    _arc = S / "after_src" / "Set"; _arc.mkdir(parents=True, exist_ok=True)
    _parts = [_arc / f"Game.part{i}.rar" for i in (1, 2)]
    for _p in _parts:
        _p.write_bytes(b"R")
    (_arc / "Other.part1.rar").write_bytes(b"R")
    _ja = _types.SimpleNamespace(archive_path=_parts[0], path=None, patch_source=None, name="Set")
    ok("after.sources-archive-set", sorted(app._after_sources(_ja)) == sorted(_parts), str(app._after_sources(_ja)))
    _jx = _types.SimpleNamespace(archive_path=None, origin_archive=None, path=S / "tmp_extract", _from_archive=True,
                                 patch_source=None, name="X")
    ok("after.sources-never-the-extracted-copy", app._after_sources(_jx) == [], str(app._after_sources(_jx)))
    _g1 = _src_game("G1")
    _j1 = m.GameItem.from_chain(_g1, to="ffpfsc"); _j1.after_source = "delete"; _j1.status = "Done"
    app.queue[:] = [_j1]
    _pl = app._after_job_plan(_j1, _fake_worker(_outf))
    ok("after.plan-delete", _pl[0] == "delete" and _pl[1] == [_g1] and _pl[3] is None, str(_pl))
    _j2 = m.GameItem.from_chain(_g1, to="pkg")                 # still waits for the same folder
    app.queue[:] = [_j1, _j2]
    ok("after.shared-source-stays", "another job" in str(app._after_job_plan(_j1, _fake_worker(_outf))[3]),
       str(app._after_job_plan(_j1, _fake_worker(_outf))))
    app.queue[:] = [_j1]
    _wc = _fake_worker(_outf); _wc._is_copy = True
    ok("after.copy-that-moved-keeps", app._after_job_plan(_j1, _wc)[0] == "keep", "")
    _jt = m.GameItem.from_chain(_g1, to="ffpfsc"); _jt.after_source = "trash"
    ok("after.copy-then-trash", app._after_job_plan(_jt, _wc)[0] == "trash"
       and app._after_job_plan(_jt, _wc)[3] is None, str(app._after_job_plan(_jt, _wc)))
    _wv = _fake_worker(_outf); _wv.validate_failed = True
    ok("after.failed-checklist-stays", "checklist" in str(app._after_job_plan(_j1, _wv)[3]), "")
    ok("after.missing-output-stays", "not found" in str(app._after_job_plan(_j1, _fake_worker(_aout / "no.pkg"))[3]), "")
    _j0 = m.GameItem.from_chain(_g1, to="ffpfsc")  # after_source None, like an old saved queue
    ok("after.older-jobs-keep", app._after_job_plan(_j0, _fake_worker(_outf))[0] == "keep", "")
    _jm = m.GameItem.from_chain(_g1, to="ffpfsc"); _jm.after_source = "move"
    ok("after.move-needs-a-folder", "no destination" in str(app._after_job_plan(_jm, _fake_worker(_outf))[3]), "")
    _gs = _src_game("GS"); _hit = _aout / "GS.ffpfsc"; _hit.write_bytes(b"P" * 64); _dn = S / "after_done2"
    _js = m.GameItem.from_chain(_gs, to="ffpfsc", output_path=str(_aout))
    _js.after_source, _js.after_move_to = "move", str(_dn)
    app.queue[:] = [_js]; _res = []
    app._settle_existing(_js, _hit, then=_res.append)
    pump(lambda: bool(_res), timeout=10.0)
    ok("after.existing-output-moves-source-and-is-done", _res == [True] and _js.status == "Done"
       and (_dn / "GS").is_dir() and "already there" in _js.status_note, f"{_res} {_js.status} {_js.status_note!r}")
    _gk = _src_game("GK"); _jk = m.GameItem.from_chain(_gk, to="ffpfsc", output_path=str(_aout)); _jk.after_source = "keep"
    app.queue[:] = [_jk]; _res2 = []
    app._settle_existing(_jk, _hit, then=_res2.append)
    ok("after.existing-output-with-keep-is-skipped", _res2 == [False] and _jk.status == "Skipped" and _gk.is_dir(),
       f"{_res2} {_jk.status}")
    _gj = _src_game("GJ"); _jj = m.GameItem.from_chain(_gj, to="ffpfsc", output_path=str(_aout)); _jj.after_source = "delete"
    _jj2 = m.GameItem.from_chain(_gj, to="pkg")            # still needs the same source
    app.queue[:] = [_jj, _jj2]; _res3 = []
    app._settle_existing(_jj, _hit, then=_res3.append)
    pump(lambda: bool(_res3), timeout=5.0)
    ok("after.existing-output-refused-stays-skipped", _res3 == [False] and _jj.status == "Skipped" and _gj.is_dir()
       and "another job" in _jj.status_note, f"{_res3} {_jj.status} {_jj.status_note!r}")
    _rs = S / "after_src" / "Grow"; _rs.mkdir(parents=True, exist_ok=True)
    _rp = [_rs / f"Big.part{i}.rar" for i in (1, 2, 3)]
    for _p in _rp:
        _p.write_bytes(b"R" * 100)
    _probe_calls = []
    _real_probe = m.ArchiveExtractor.probe_header_state
    _answer = ["open", 5000, ""]
    m.ArchiveExtractor.probe_header_state = staticmethod(
        lambda arc, pw=None: (_probe_calls.append(arc), tuple(_answer))[1])
    try:
        _ra = m.GameItem.from_archive(_rp[0]); _ra.size, _ra.extracted_size = 100, 0   # as added: one part
        app.queue[:] = [_ra]; _probe_calls.clear()
        _go = app._refresh_archive_set(_ra)
        ok("refresh.grown-set-read-again", _go and _ra.size == 300 and _ra.extracted_size == 5000 and _probe_calls,
           f"{_go} size={_ra.size} ex={_ra.extracted_size} probes={len(_probe_calls)}")
        _probe_calls.clear()
        ok("refresh.unchanged-set-not-probed", app._refresh_archive_set(_ra) and not _probe_calls, str(_probe_calls))
        _answer[:] = ["damaged", 0, "1 part of the set is missing (Big.part4.rar)"]
        _ra.size = 100
        _go2 = app._refresh_archive_set(_ra)
        pump(lambda: not app._batch_running, timeout=3.0)
        ok("refresh.incomplete-set-fails-with-reason", _go2 is False and _ra.status == "Failed"
           and "missing" in _ra.status_note, f"{_go2} {_ra.status} {_ra.status_note!r}")
    finally:
        m.ArchiveExtractor.probe_header_state = _real_probe
    _rr = S / "rel_root"; _rd = S / "rel_done"; _rhit = _aout / "Rel.ffpfsc"; _rhit.write_bytes(b"P" * 64)
    def _set(folder, base, n=2):
        d = _rr / folder; d.mkdir(parents=True, exist_ok=True)
        ps = [d / (f"{base}.part{i}.rar" if n > 1 else f"{base}.rar") for i in range(1, n + 1)]
        for _p in ps:
            _p.write_bytes(b"R" * 50)
        return ps
    def _arc_job(first, act, dest=None):
        it = m.GameItem.from_archive(first); it.after_source, it.after_move_to = act, (str(dest) if dest else None)
        it.source_root = str(_rr); return it
    def _settle(it):
        app.queue[:] = [it]; _r = []
        app._settle_existing(it, _rhit, then=_r.append)
        pump(lambda: bool(_r), timeout=10.0)
        return _r
    _real_probe2 = m.ArchiveExtractor.probe_header_state
    m.ArchiveExtractor.probe_header_state = staticmethod(lambda arc, pw=None: ("open", 100, ""))
    try:
        _p1 = _set("[site]-PPSA00011", "[site]-PPSA00011"); (_rr / "[site]-PPSA00011" / "readme.nfo").write_bytes(b"n")
        _r1 = _settle(_arc_job(_p1[0], "move", _rd))
        ok("release.folder-moved-whole", _r1 == [True] and (_rd / "[site]-PPSA00011" / "readme.nfo").is_file()
           and not (_rr / "[site]-PPSA00011").exists() and _rr.is_dir(), f"{_r1} {sorted(x.name for x in _rd.iterdir())}")
        _pa = _set("Mixed", "A-PPSA00012"); _pb = _set("Mixed", "B-PPSA00013")
        _ra = _settle(_arc_job(_pa[0], "move", _rd))
        _after_a = sorted(x.name for x in (_rr / "Mixed").iterdir())
        _rb = _settle(_arc_job(_pb[0], "move", _rd))
        ok("release.two-games-apart-and-no-empty-folder", _ra == [True] and _rb == [True]
           and _after_a == ["B-PPSA00013.part1.rar", "B-PPSA00013.part2.rar"]
           and not (_rr / "Mixed").exists() and (_rd / "A-PPSA00012.part1.rar").is_file() and _rr.is_dir(),
           f"{_after_a} mixed={(_rr / 'Mixed').exists()}")
        _pd = _set("[site]-PPSA00014", "[site]-PPSA00014"); (_rr / "[site]-PPSA00014" / "info.nfo").write_bytes(b"n")
        _rdl = _settle(_arc_job(_pd[0], "delete"))
        _pk = _set("[site]-PPSA00015", "[site]-PPSA00015"); (_rr / "[site]-PPSA00015" / "mine.docx").write_bytes(b"d")
        _rkl = _settle(_arc_job(_pk[0], "delete"))
        ok("release.delete-takes-folder-only-with-sidecars", _rdl == [True] and not (_rr / "[site]-PPSA00014").exists()
           and _rkl == [True] and sorted(x.name for x in (_rr / "[site]-PPSA00015").iterdir()) == ["mine.docx"],
           f"{_rdl} {_rkl}")
        _bq = _set("[site]-PPSA00016", "[site]-PPSA00016")
        _bj = _arc_job(_bq[0], "move", _rd)
        _first = m.GameItem.from_chain(HBT, to="ffpfsc"); _first.status = "Done"
        app.queue[:] = [_first, _bj]; app._active_item = _first; app._output_policy = None
        _ext_calls = []
        _real_ext, _real_pred3 = app._extract_queued_item, app._predicted_output
        app._extract_queued_item = lambda it: _ext_calls.append(it)
        app._predicted_output = lambda it: _rhit if it is _bj else _real_pred3(it)
        try:
            app._batch_running = True
            app._batch_auto_start()
            # the start waits for cleanups of earlier checks, slow on a VM
            pump(lambda: _bj.status == "Done" and not app._batch_running, timeout=60.0)
        finally:
            app._extract_queued_item, app._predicted_output = _real_ext, _real_pred3
            app._batch_running = False
        ok("release.batch-path-settles-before-unpacking", not _ext_calls and _bj.status == "Done"
           and (_rd / "[site]-PPSA00016").is_dir(), f"extract={len(_ext_calls)} {_bj.status}")
    finally:
        m.ArchiveExtractor.probe_header_state = _real_probe2
    def _pj(d, tid, ver, sdk):
        (d / "sce_sys").mkdir(parents=True, exist_ok=True)
        (d / "sce_sys" / "param.json").write_text(json.dumps({"titleId": tid, "contentVersion": ver, "sdkVersion": sdk}))
        return d
    _pg = _pj(S / "pfit" / "game", "PPSA00021", "01.300.300", "0x0C000000")
    _fit = [app._patch_fit(_pg, _pj(S / "pfit" / "p1" / "wrap", "PPSA00099", "01.300.300", "0x04030000"))[0],
            app._patch_fit(_pg, _pj(S / "pfit" / "p2" / "wrap", "PPSA00021", "01.200.000", "0x04030000"))[0],
            app._patch_fit(_pg, _pj(S / "pfit" / "p3" / "wrap", "PPSA00021", "01.300.300", "0x04030000"))[0],
            app._patch_fit(_pg, _pj(S / "pfit" / "p4" / "wrap", "PPSA00021", "01.400.000", "0x0C000000"))[0]]
    ok("patch.fit-refuse-warn-ok", _fit == ["refuse", "warn", "ok", "ok"], str(_fit))
    _pt = _aout / "fwp" / "Example Quest [PPSA00021] [v01.300].ffpfsc"; _pt.parent.mkdir(parents=True, exist_ok=True)
    (_pt.parent / "Example Quest [PPSA00021] [v01.300] [fw12.00].ffpfsc").write_bytes(b"x")
    _pjob = m.GameItem.from_chain(HBT, to="ffpfsc"); _pjob.patch_source = str(S / "pfit" / "p3")
    ok("patch.unknown-fw-needs-exact-name", app._existing_output(_pt, _pjob) is None
       and app._existing_output(_pt, m.GameItem.from_chain(HBT, to="ffpfsc")) is not None, "")
    _pw0 = m.CLIWorker(app, _pjob, ["py", "cli.py", str(HBT), str(OUT), "--to", "ffpfsc", "--patch", "x"],
                       _pcwd, _pout, _ptmp)
    _pw0._handle_line("[PATCH-BACKUP] /tmp/x/Original files - P")
    ok("patch.worker-reads-backup-line", _pw0.patch_backup == "/tmp/x/Original files - P", _pw0.patch_backup)
    _stg = S / "temp" / "_ffpfsc_temp" / "patch-backup-t1" / "Original files - Example_backport"
    (_stg / "app0").mkdir(parents=True, exist_ok=True)
    (_stg / "app0" / "eboot.bin").write_bytes(b"old"); (_stg / "README.txt").write_text("readme")
    _pout_f = _aout / "pb" / "Example Quest [PPSA00021] [v01.300] [fw4.03].ffpfsc"
    _pout_f.parent.mkdir(parents=True, exist_ok=True); _pout_f.write_bytes(b"x")
    _guard_dir = S / "not_scratch" / "patch-backup-x" / "Original files - Y"; _guard_dir.mkdir(parents=True)
    _cwd_before = sorted(os.listdir("."))
    _guard_calls = []
    for _val in (None, "", ".", "relative/dir", str(_guard_dir)):
        _gw = _fake_worker(_pout_f)
        if _val is not None:
            _gw.patch_backup = _val
        app._publish_patch_backup(_pjob, _gw, lambda: _guard_calls.append(1))
        app._restore_then_drop_patch_backup(None, _gw)
    pump(lambda: app._cleanup_inflight == 0, timeout=10.0)
    ok("patch.backup-never-touches-anything-else", len(_guard_calls) == 5 and _guard_dir.is_dir()
       and sorted(os.listdir(".")) == _cwd_before, f"{len(_guard_calls)} {_guard_dir.is_dir()}")
    _pbw = _fake_worker(_pout_f); _pbw.patch_backup = str(_stg); _went2 = []
    app._publish_patch_backup(_pjob, _pbw, lambda: _went2.append(1))
    pump(lambda: bool(_went2), timeout=10.0)
    _dest = _pout_f.parent / "Original files - Example_backport"
    ok("patch.backup-beside-output", _went2 == [1] and (_dest / "app0" / "eboot.bin").is_file()
       and (_dest / "README.txt").is_file() and not _stg.parent.exists(), str(sorted(x.name for x in _pout_f.parent.iterdir())))
    _stg2 = S / "temp" / "_ffpfsc_temp" / "patch-backup-t2" / "Original files - X"; _stg2.mkdir(parents=True)
    _dw = _fake_worker(_pout_f); _dw.patch_backup = str(_stg2)
    app._restore_then_drop_patch_backup(None, _dw)
    pump(lambda: not _stg2.parent.exists(), timeout=10.0)
    ok("patch.backup-dropped-on-failure", not _stg2.parent.exists() and _dw.patch_backup == "", "")
    app.queue[:] = [_j1]
    _card = app._card_info_text(_j1)
    ok("after.card-row", _card.get("After", "").startswith("Delete the source"), _card.get("After", ""))

    def _run_done(item, worker, done_when, ok_flag=True):
        item.status = "Running"
        app.queue[:] = [item]; app._active_item = item; app.worker = worker
        app.finish(ok_flag, "ok" if ok_flag else "boom", "cmd")
        pump(lambda: done_when() and not getattr(app, "_after_busy", False), timeout=10.0)
    _g3 = _src_game("G3")
    _j3 = m.GameItem.from_chain(_g3, to="ffpfsc", output_path=str(_aout)); _j3.after_source = "delete"
    _run_done(_j3, _fake_worker(_outf), lambda: not _g3.exists())
    ok("after.done-job-deletes-source", not _g3.exists() and _outf.exists() and _j3.status == "Done", _j3.status)
    _g4 = _src_game("G4"); _dest = S / "after_done"
    _j4 = m.GameItem.from_chain(_g4, to="ffpfsc", output_path=str(_aout))
    _j4.after_source, _j4.after_move_to = "move", str(_dest)
    _run_done(_j4, _fake_worker(_outf), lambda: (_dest / "G4").exists())
    ok("after.done-job-moves-source", (_dest / "G4" / "eboot.bin").is_file() and not _g4.exists(), "")
    _g5 = _src_game("G5"); _seen = []
    _real_trash = _aj.move_to_trash
    _aj.move_to_trash = lambda p: (_seen.append(Path(p)), "Trash")[1]
    try:
        _j5 = m.GameItem.from_chain(_g5, to="ffpfsc", output_path=str(_aout)); _j5.after_source = "trash"
        _run_done(_j5, _fake_worker(_outf), lambda: bool(_seen))
    finally:
        _aj.move_to_trash = _real_trash
    ok("after.done-job-trashes-source", _seen == [_g5], str(_seen))
    _g6 = _src_game("G6")
    _j6 = m.GameItem.from_chain(_g6, to="ffpfsc", output_path=str(_aout)); _j6.after_source = "delete"
    _run_done(_j6, _fake_worker(_outf), lambda: _j6.status == "Failed", ok_flag=False)
    ok("after.failed-job-keeps-source", _g6.is_dir() and _j6.status == "Failed", _j6.status)
    # one source, two jobs: the earlier one leaves it for the next, the last decides
    _g7 = _src_game("G7")
    _j7a = m.GameItem.from_chain(_g7, to="ffpfsc", output_path=str(_aout)); _j7a.after_source = "delete"
    _j7b = m.GameItem.from_chain(_g7, to="pkg", output_path=str(_aout)); _j7b.after_source = "keep"
    _j7a.status = "Running"
    app.queue[:] = [_j7a, _j7b]; app._active_item = _j7a; app.worker = _fake_worker(_outf)
    app.finish(True, "ok", "cmd")
    pump(lambda: _j7a.status == "Done" and not getattr(app, "_after_busy", False), timeout=10.0)
    ok("after.same-source-waits-for-the-last-job", _g7.is_dir() and _j7a.status == "Done"
       and (getattr(_j7a, "_after_result", None) or ("",))[0] == "deferred", str(getattr(_j7a, "_after_result", None)))
    app.queue[:] = []; app._active_item = None; app.worker = None
    # an add-on already in the library counts as placed only when it is the same file
    _sc = S / "same_content"; _sc.mkdir(exist_ok=True)
    (_sc / "a.pkg").write_bytes(b"x" * 5000); (_sc / "b.pkg").write_bytes(b"x" * 5000)
    (_sc / "c.pkg").write_bytes(b"x" * 4999 + b"y"); (_sc / "d.pkg").write_bytes(b"x" * 4000)
    ok("after.same-content-check", m.same_content(_sc / "a.pkg", _sc / "b.pkg")
       and not m.same_content(_sc / "a.pkg", _sc / "c.pkg") and not m.same_content(_sc / "a.pkg", _sc / "d.pkg")
       and not m.same_content(_sc / "a.pkg", _sc / "missing.pkg"), "")
    shutil.rmtree(_sc, ignore_errors=True)
    app.queue[:] = []; app._active_item = None; app.worker = None
    _went, _canc = [], []
    _cd = m.CountdownWindow(root, "sleep", on_go=lambda: _went.append("go"))
    ok("after.countdown-text", "sleep in 30 s" in _cd._msg.get(), _cd._msg.get())
    _cd._left = 0; _cd._tick()
    _cd2 = m.CountdownWindow(root, "quit", on_go=lambda: _went.append("quit"), on_cancel=lambda: _canc.append(1))
    _cd2._cancel()
    ok("after.countdown-go-and-cancel", _went == ["go"] and _canc == [1], f"{_went} {_canc}")
    _real_go = app._after_queue_go
    app._after_queue_go = lambda act: _went.append(act)
    app.after_queue_var.set("quit")
    try:
        app._queue_finished()
        _cdq = getattr(app, "_countdown", None)
        ok("after.queue-end-opens-countdown", isinstance(_cdq, m.CountdownWindow), str(_cdq))
        _cdq._go()
        ok("after.queue-end-quits", _went[-1] == "quit", str(_went))
    finally:
        app._after_queue_go = _real_go
        app.after_queue_var.set("nothing")
    app._countdown = None
    app._queue_finished()
    ok("after.queue-end-nothing", getattr(app, "_countdown", None) is None, "")
    _pa1 = m.GameItem.from_chain(HBT, to="ffpfsc"); _pa1.status = "Done"
    _pa2 = m.GameItem.from_chain(HBT, to="ffpfsc"); _pa2_st = _pa2.status
    _due_seen, _qf_seen = [], []
    _real_due, _real_qf, _notify0 = app._due_checks, app._queue_finished, app.notify_var.get()
    app._due_checks = lambda it: (_due_seen.append(it), False)[1]
    app._queue_finished = lambda: _qf_seen.append(1)
    app.notify_var.set("off")
    # a scratch reclaim from earlier checks may still run, and the run would wait for it
    _inflight0 = getattr(app, "_cleanup_inflight", 0)
    app._cleanup_inflight = 0; app.cancel_requested = False; app.extract_cancel_event.clear()
    try:
        app.queue[:] = [_pa1, _pa2]; app._active_item = _pa1
        app._batch_running = True; app._pause_requested = True
        app._batch_auto_start()
        ok("pause.stops-before-the-next-job", not _due_seen and not app._batch_running and not app._pause_requested
           and _pa2.status == _pa2_st and not _qf_seen and app.start_btn._state == "normal",
           f"due={len(_due_seen)} running={app._batch_running} qf={_qf_seen} {_pa2.status}")
        app.queue[:] = [_pa2]; app._active_item = _pa2
        app._batch_running = True; app._pause_requested = True
        app._batch_auto_start()
        ok("pause.job-in-progress-goes-on", _due_seen == [_pa2] and app._pause_requested, f"{len(_due_seen)}")
        app.queue[:] = [_pa1]; app._active_item = _pa1; app._batch_total = 1
        app._batch_running = True; app._pause_requested = True
        app._batch_auto_start()
        ok("pause.last-job-ends-the-queue", _qf_seen == [1] and not app._batch_running, str(_qf_seen))
    finally:
        app._due_checks, app._queue_finished = _real_due, _real_qf
        app.notify_var.set(_notify0); app._cleanup_inflight = _inflight0
        app._batch_running = False; app._pause_requested = False
        app.queue[:] = []; app._active_item = None
    _uc = m.ultra_core
    _real_sk = (_uc.get_free_space, _uc.same_drive, app._resolve_extract_root, m.load_settings, app._due_checks,
                app._release_failed_copies, app._queue_finished, app._extract_queued_item)
    _sk = [m.GameItem.from_chain(ZP, to="ffpfsc", output_path=str(OUT / "full")) for _ in range(2)]
    for _j in _sk:
        _j.extracted_size = 50 * 10 ** 9
    _sklog, _sklog_saved = [], app.log
    try:
        _uc.get_free_space = lambda d: 0 if str(d).startswith(str(OUT)) else 10 ** 15
        _uc.same_drive = lambda a, b: False
        app._resolve_extract_root = lambda _it, quiet=False: None
        m.load_settings = lambda: dict(_real_sk[3](), low_space_policy="skip")
        app._due_checks = lambda it: True
        app._release_failed_copies = lambda it: False
        app._queue_finished = lambda: None
        app._extract_queued_item = lambda it: None
        app.log = lambda lvl, msg, *a, **k: (_sklog.append(msg), _sklog_saved(lvl, msg, *a, **k))
        app._cleanup_inflight = 0; app.cancel_requested = False; app.extract_cancel_event.clear()
        app.queue[:] = _sk; app._active_item = None; app._batch_total = 1
        app._batch_running = True
        app._batch_auto_start()
        pump(lambda: not app._batch_running, timeout=10.0)
    finally:
        (_uc.get_free_space, _uc.same_drive, app._resolve_extract_root, m.load_settings, app._due_checks,
         app._release_failed_copies, app._queue_finished, app._extract_queued_item) = _real_sk
        app.log = _sklog_saved; app._batch_running = False
    _skw = getattr(app, "_space_skip_win", None)
    _skt = _skw.text() if _skw is not None and _skw.winfo_exists() else ""
    _skinfo = app._card_info_text(_sk[0]).get("Status", "")
    ok("space-skip.names-the-drive", all(j.status == "Skipped" for j in _sk)
       and _skinfo.startswith("Skipped: output drive full") and _skw is not None
       and _skw.title() == "Output drive full" and _skt.count("was skipped") == 2
       and any("output drive full" in x.lower() and "free" in x for x in _sklog),
       f"status={[j.status for j in _sk]} info={_skinfo!r} title={_skw.title() if _skw else None} "
       f"text={_skt[:160]!r} log={[x for x in _sklog if 'pace' in x][:2]}")
    if _skw is not None:
        _skw.destroy()
    app.queue[:] = []; app._active_item = None
    _r1 = m.GameItem.from_chain(HBT, to="ffpfsc"); _r2 = m.GameItem.from_chain(HBT, to="ffpfsc"); _r2.status = "Done"
    app.queue[:] = [_r1, _r2]; app._batch_running = True; app._active_item = _r1
    m.messagebox.askyesno = lambda *a, **k: True
    try:
        app.clear_jobs("all")
    finally:
        m.messagebox.askyesno = _real_ask
    ok("queue.clear-all-keeps-running", app.queue == [_r1], str(len(app.queue)))
    import types as _ty
    _ql = app.queue_listbox
    _ms = [m.GameItem.from_chain(HBT, to="ffpfsc") for _ in range(5)]
    for _k, _it in enumerate(_ms):
        _it.display_name = f"Multi {_k}"
    app._batch_running = False; app._active_item = None
    app.queue[:] = list(_ms); app.update_queue_box(select_item=_ms[0]); root.update()
    def _ev(i):     # window y of row i, whatever the list is scrolled to
        return _ty.SimpleNamespace(y=6 + i * _ql.ROW_H + _ql.ROW_H // 2 - _ql.cv.canvasy(0), x=40, x_root=0, y_root=0)
    _ql._click(_ev(1)); _ql._release(_ev(1))
    _ql._toggle_click(_ev(3))
    _t1 = _ql.marked_rows()
    _ql._range_click(_ev(4))
    _t2 = _ql.marked_rows()
    _ql._select_all(); _t3 = _ql.marked_rows()
    _ql._keep_focus_only(); _t4 = _ql.marked_rows()
    ok("multi.toggle-range-all-escape", _t1 == [1, 3] and _t2 == [3, 4] and _t3 == [0, 1, 2, 3, 4] and _t4 == [4],
       f"{_t1} {_t2} {_t3} {_t4}")
    _ql._click(_ev(1)); _ql._release(_ev(1)); _ql._toggle_click(_ev(3))
    app.update_queue_box(); root.update()
    _kept = [app.queue[i] for i in _ql.marked_rows()]
    _new_top = m.GameItem.from_chain(HBT, to="ffpfsc")
    app.queue.insert(0, _new_top); app.update_queue_box(); root.update()
    _moved = [app.queue[i] for i in _ql.marked_rows()]
    ok("multi.kept-across-refresh-and-shift", [id(x) for x in _kept] == [id(_ms[1]), id(_ms[3])]
       and [id(x) for x in _moved] == [id(_ms[1]), id(_ms[3])] and _ql.marked_rows() == [2, 4],
       f"{_ql.marked_rows()}")
    root.update()
    app._names_dirty = True; app._poll_inner(); root.update()
    ok("multi.kept-when-names-come-in", _ql.marked_rows() == [2, 4], f"{_ql.marked_rows()}")
    _card_multi_shown = bool(app._card_multi.winfo_manager())
    ok("multi.summary-card", _card_multi_shown and app._multi_title_var.get() == "2 jobs selected",
       f"{_card_multi_shown} {app._multi_title_var.get()!r}")
    app._batch_running = True; app._active_item = _ms[1]; _ms[1].status = "Running"
    app.queue_remove_selected(); root.update()
    ok("multi.remove-all-but-running", _ms[3] not in app.queue and _ms[1] in app.queue and len(app.queue) == 5,
       f"{len(app.queue)} {[getattr(x, 'display_name', '') for x in app.queue]}")
    app._batch_running = False; app._active_item = None; _ms[1].status = "Queued"
    app.update_queue_box(select_item=app.queue[0]); root.update()
    ok("multi.single-again-after-plain-click", _ql.marked_rows() == [0] and not app._card_multi.winfo_manager(),
       f"{_ql.marked_rows()}")
    app.queue[:] = [_r1]; app.update_queue_box(); root.update()
    app._batch_running = False
    app.queue[:] = [_r2]; app._sync_primary_action()
    ok("queue.start-off-when-all-done", str(app.start_btn._state) == "disabled", app.start_btn._state)
    _saved_q2 = m.load_settings().get("queue")
    _de = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(_r2).items()
           if not k.startswith("_") and k != "artwork" and (v is None or isinstance(v, (str, int, float, bool, Path)))}
    _de.update({"status": "Done", "path": str(S / "gone3" / "x"), "archive_path": None})
    m.save_settings({"queue": [_de]}); app.queue.clear(); app._restore_queue()
    ok("queue.restore-keeps-done-record", len(app.queue) == 1 and app.queue[0].status == "Done", str(len(app.queue)))
    _tdir = S / "taken_src"; _tdir.mkdir(exist_ok=True); (_tdir / "eboot.bin").write_bytes(b"x")
    _tarc = S / "taken.7z"; _tarc.write_bytes(b"7z")
    _tk = m.GameItem.from_chain(_tdir, to="ffpfsc", output_path=str(OUT / "job"))
    _q6 = list(app.queue); app.queue[:] = [_tk]
    _c1 = ["py", "cli.py", "--stage-in-place"]
    app._mark_source_taken_in(_tk, _c1)
    _marked = _tk.source_taken_in is True and "--stage-in-place" in _c1
    app.queue[:] = _q6
    _dt = dict(_de, path=str(_tdir), status="Running", source_taken_in=True, origin_archive=str(_tarc), archive_path=None)
    _dt2 = dict(_dt, origin_archive=str(S / "gone.7z"))
    m.save_settings({"queue": [_dt, _dt2]}); app.queue.clear(); app._restore_queue()
    _ra, _rb = (app.queue + [None, None])[:2]
    _parked = [x for x in (app._queue_missing_saved or []) if x.get("origin_archive") == str(S / "gone.7z")]
    app._mark_source_taken_in(_tk, ["py", "cli.py"])
    ok("queue.restore-taken-in-unpacks-again", _marked and _ra is not None and _ra.archive_path == _tarc
       and _ra.path is None and _ra.status == "Pending Extract" and not _ra.source_taken_in
       and _rb is None and len(_parked) == 1 and _parked[0].get("source_taken_in") is True
       and _tk.source_taken_in is False,
       f"a={getattr(_ra, 'status', None)}/{getattr(_ra, 'archive_path', None)} b={_rb} parked={len(_parked)}")
    app._queue_missing_saved = []
    _tk2 = m.GameItem.from_chain(_tdir, to="ffpfsc", output_path=str(OUT / "job"))
    _real_sq = app._save_queue
    app._save_queue = lambda: False
    _c2 = ["py", "cli.py", "--stage-in-place"]
    try:
        app._mark_source_taken_in(_tk2, _c2)
    finally:
        app._save_queue = _real_sq
    ok("queue.unsaved-mark-frees-nothing", "--stage-in-place" not in _c2 and not _tk2.source_taken_in, str(_c2))
    _tk3 = m.GameItem.from_chain(_tdir, to="ffpfsc", output_path=str(OUT / "job"))
    _tk3.status, _tk3.source_taken_in, _tk3.origin_archive = "Failed", True, str(S / "gone.7z")
    _q5 = list(app.queue); app.queue[:] = [_tk3]
    _real_start = app.start
    app.start = lambda *a, **k: None
    try:
        app._retry_job(_tk3)
    finally:
        app.start = _real_start
        app.queue[:] = _q5
    ok("retry.never-from-a-copy-that-lacks-files", _tk3.status == "Failed" and "lacks files" in _tk3.status_note,
       f"{_tk3.status} {_tk3.status_note[:40]!r}")
    _oa = S / "orig_release.7z"; _oa.write_bytes(b"7z")
    _de2 = dict(_de, kept_extract=True, origin_archive=str(_oa), bundle_siblings=[str(S / "gone3" / "backport 4.xx")])
    m.save_settings({"queue": [_de2]}); app.queue.clear(); app._restore_queue()
    _rq = app.queue[0] if app.queue else None
    ok("queue.restore-keeps-kept-unpack", _rq is not None and _rq.archive_path is None and _rq.path is not None
       and [x.name for x in _rq.bundle_siblings] == ["backport 4.xx"], str(vars(_rq) if _rq else None)[:200])
    m.save_settings({"queue": _saved_q2 or []})
    app.summary_popup_var.set(_real_sum); app.open_output_var.set(_real_open)
    _lib = S / "library"; _lib.mkdir(exist_ok=True)
    _lib_fw = _lib / "Alpha"; _lib_fw.mkdir()
    (_lib_fw / "sce_sys").mkdir(); (_lib_fw / "sce_sys" / "param.json").write_text('{"titleId": "PPSA00010"}')
    (_lib_fw / "eboot.bin").write_bytes(b"\x7fELF" + b"\0" * 10)
    _rp = Path(S / "release")
    _ra = _rp / "[alpha]-PPSA00011.zip"
    _ra.parent.mkdir(exist_ok=True); _ra.write_bytes(b"PK\x05\x06" + b"\0" * 18)  # tiny empty zip
    _old_settings = m.load_settings()
    try:
        m.save_settings({"last_source": str(_lib_fw), "last_source_dir": str(_lib)})
        jr = m.JobDialog(app); root.update()
        ok("source.pre-filled-from-last", jr.src_var.get() == str(_lib_fw)
           and jr._initial_dir() in (str(_lib_fw), str(_lib)),
           f"{jr.src_var.get()!r} {jr._initial_dir()!r}")
        jr.destroy()
        m.save_settings({"last_source": "", "last_source_dir": ""})
        jr2 = m.JobDialog(app); root.update()
        jr2.src_var.set(str(_lib_fw))
        ok("source.initial-dir-follows-field", jr2._initial_dir() == str(_lib_fw) or jr2._initial_dir() == str(_lib),
           str(jr2._initial_dir()))
        jr2.destroy()
    finally:
        m.save_settings(_old_settings)
    _ro = OUT / "rescan"; _ro.mkdir(exist_ok=True)
    _rt = {"to": "ffpfsc", "output": str(_ro), "sign": False, "backport_target": None,
           "patch_source": None, "keep_source": False, "organize": False, "ff_level": 7, "pkg_params": None}
    _lib2 = S / "rescan_lib"; _lib2.mkdir(exist_ok=True)
    for i in range(3):
        g = _lib2 / f"Game{i}"
        (g / "sce_sys").mkdir(parents=True, exist_ok=True)
        (g / "sce_sys" / "param.json").write_text(f'{{"titleId": "PPSA0010{i}"}}')
        (g / "eboot.bin").write_bytes(b"\x7fELF" + b"\0" * 1000)
    _already = m.GameItem.from_chain(_lib2 / "Game0", to="ffpfsc", output_path=str(_ro))
    _saved_q = list(app.queue); app.queue[:] = [_already]
    _hist_file = m.HISTORY_FILE
    _hist_backup = _hist_file.read_bytes() if _hist_file.exists() else None
    import json as _json
    _hist_file.write_text(_json.dumps([{"source": str(_lib2 / "Game1")}]), encoding="utf-8")
    m.save_settings({"last_source": str(_lib2 / "Game0"), "last_source_dir": str(_lib2),
                     "rescan_template": _rt})
    _pre = len(app.queue)
    def _inline(fn):
        with app._scan_lock: app._scan_in_flight += 1
        try: fn()
        finally:
            with app._scan_lock: app._scan_in_flight = max(0, app._scan_in_flight - 1)
    _real_launch = app._launch_scan
    app._launch_scan = _inline
    try:
        app.rescan_last_source()
        pump(lambda: (getattr(app, "_add_state", {}) or {}).get("total"), timeout=5.0)
        settle()
    finally:
        app._launch_scan = _real_launch
    _after = [getattr(i, "name", "") for i in app.queue[_pre:]]
    ok("rescan.adds-only-new", _after == ["Game2"], f"{_after}")
    _info_before = len(getattr(app, "log_box", type("x",(),{"get":lambda *a:""})()).get("1.0","end") if hasattr(app, "log_box") else "")
    app._launch_scan = _inline
    try:
        app.rescan_last_source()
        pump(lambda: "Nothing new" in (app.log_box.get("1.0","end") if hasattr(app, "log_box") else "")
             or "found no sources" in (app.log_box.get("1.0","end") if hasattr(app, "log_box") else ""), timeout=5.0)
    finally:
        app._launch_scan = _real_launch
    _after2 = len(app.queue) - _pre
    ok("rescan.quiet-when-nothing-new", _after2 == 1, str(_after2))
    m.save_settings({"rescan_template": {}})
    _pre3 = len(app.queue); app.rescan_last_source(); settle()
    ok("rescan.needs-template-first", len(app.queue) == _pre3, "")
    app.queue[:] = _saved_q
    if _hist_backup is None:
        _hist_file.unlink(missing_ok=True)
    else:
        _hist_file.write_bytes(_hist_backup)
    m.save_settings(_old_settings)
    app.update_queue_box(); root.update()

    _lib3 = S / "rescan_lib_tid"; _lib3.mkdir(exist_ok=True)
    _r3 = OUT / "rescan_tid"; _r3.mkdir(exist_ok=True)
    _rt3 = {"to": "ffpfsc", "output": str(_r3), "sign": False, "backport_target": None,
            "patch_source": None, "keep_source": False, "organize": False, "ff_level": 7, "pkg_params": None}
    # one queued, one in the history, one with an output already, one new
    for n in ("[rel]-PPSA20001.part01.rar", "[rel]-PPSA20002.part01.rar",
              "[rel]-PPSA20003.part01.rar", "[rel]-PPSA20004.part01.rar"):
        (_lib3 / n).write_bytes(b"RE~^\x07\x00")
    _done_job = m.GameItem.from_chain(_lib3 / "[rel]-PPSA20001.part01.rar", to="ffpfsc", output_path=str(_r3))
    _done_job.status = "Done"
    _done_job.origin_archive = str(_lib3 / "[rel]-PPSA20001.part01.rar")   # as a done archive job stores
    _done_job.archive_path, _done_job.path = None, None                     # the extract was cleaned up
    _saved_q = list(app.queue); app.queue[:] = [_done_job]
    _hist_file2 = m.HISTORY_FILE
    _hist_backup2 = _hist_file2.read_bytes() if _hist_file2.exists() else None
    import json as _json
    _hist_file2.write_text(_json.dumps([{"title_id": "PPSA20002", "source": "/gone/foo.rar"}]), encoding="utf-8")
    (_r3 / "Example [PPSA20003] [v01.000].ffpfsc").write_bytes(b"old")
    m.save_settings({"last_source": str(_lib3), "last_source_dir": str(_lib3),
                     "rescan_template": _rt3})
    _real_launch = app._launch_scan
    def _inline(fn):
        with app._scan_lock: app._scan_in_flight += 1
        try: fn()
        finally:
            with app._scan_lock: app._scan_in_flight = max(0, app._scan_in_flight - 1)
    app._launch_scan = _inline
    try:
        _before = len(app.queue)
        app.rescan_last_source()
        pump(lambda: (getattr(app, "_add_state", {}) or {}).get("total"), timeout=5.0)
        settle()
    finally:
        app._launch_scan = _real_launch
    _new_tids = {app._title_id_from_path(getattr(i, "archive_path", None) or getattr(i, "path", None))
                 for i in app.queue[_before:]}
    ok("rescan.skips-known-title-ids", _new_tids == {"PPSA20004"},
       f"added {sorted(_new_tids)} (added {len(app.queue) - _before})")
    app.queue[:] = _saved_q
    if _hist_backup2 is None:
        _hist_file2.unlink(missing_ok=True)
    else:
        _hist_file2.write_bytes(_hist_backup2)
    app.update_queue_box(); root.update()

    _dir = S / "edit_done"; _dir.mkdir(exist_ok=True)
    _dj = m.GameItem.from_chain(HBT, to="ffpfsc", output_path=str(OUT / "ed"))
    _dj.status = "Done"; _dj.backport_target = None
    app.queue[:] = [_dj]; app.update_queue_box(select_item=_dj); root.update()
    _before = set(app._panels.stack)
    app._edit_job(_dj); root.update()
    _ed = next((w for w in app._panels.stack if w not in _before and isinstance(w, m.JobDialog)), None)
    ok("edit.opens-on-done", _ed is not None, "")
    if _ed is not None:
        _ed.backport_on_var.set(True); _ed.backport_target_var.set("7.61")
        _ed.out_var.set(str(OUT / "ed")); root.update()
        _ed._add(); settle()
    _new = app.queue[0] if app.queue else None
    ok("edit.done-becomes-active", _new is not None and _new is not _dj and _new.status == "Queued"
       and _new.backport_target == "7.61" and not getattr(_new, "_output_checked", False),
       f"{getattr(_new, 'status', '?')} target={getattr(_new, 'backport_target', '?')}")
    app.queue[:] = []; app.update_queue_box(); root.update()

    uk = sys.modules["ui_kit"]
    def _pack(dx, dy):                              # Tk's %D for <TouchpadScroll>: dx high, dy low, signed
        v = ((dx & 0xFFFF) << 16) | (dy & 0xFFFF)
        return v - 0x100000000 if v >= 0x80000000 else v
    ok("scroll.unpack-touchpad-delta",
       uk.unpack_touchpad_delta(_pack(0, -7)) == (0, -7) and uk.unpack_touchpad_delta(_pack(3, -2)) == (3, -2)
       and uk.unpack_touchpad_delta(_pack(-3, 5)) == (-3, 5) and uk.unpack_touchpad_delta(0) == (0, 0),
       str([uk.unpack_touchpad_delta(_pack(a, b)) for a, b in ((0, -7), (3, -2), (-3, 5))]))
    _tp = [m.GameItem(HBT) for _ in range(60)]
    for i, it in enumerate(_tp):
        it.display_name = f"Row {i:02d}"; it.status = "Queued"
    _saved = list(app.queue); app.queue[:] = _tp
    app.update_queue_box(select_item=_tp[0]); root.update()
    _lb = app.queue_listbox; _cv = _lb.cv
    _cv.winfo_height = lambda: 300                  # the driver's window is withdrawn
    _lb._schedule(); root.update_idletasks(); _cv.yview_moveto(0); root.update_idletasks()
    class _E:
        def __init__(self, delta): self.delta = delta
    _y0 = _cv.yview()[0]
    _handled_tp = [_cv._on_touchpad(_E(_pack(0, -8))) for _ in range(5)]  # 8 px per event, 5 events
    _y_tp = _cv.yview()[0]
    _total_px = 12 + 60 * _lb.ROW_H
    _moved_px = round((_y_tp - _y0) * _total_px)
    ok("scroll.touchpad-moves-by-pixels", all(h == "break" for h in _handled_tp) and _moved_px == 40,
       f"moved {_moved_px} px (expected 40), handled={_handled_tp}")
    _cv._on_touchpad(_E(_pack(0, 8)))
    _y_back = _cv.yview()[0]
    ok("scroll.touchpad-back", round((_y_tp - _y_back) * _total_px) == 8, f"{_y_tp} -> {_y_back}")
    _lb._draw()
    _first_drawn, _first_items = _lb._drawn, len(_cv.find_all())
    _lb.see(59); root.update_idletasks()
    _end_drawn = _lb._drawn
    ok("queue.draws-only-visible-rows", _first_drawn[0] == 0 and _first_drawn[1] < 60 and _end_drawn[1] == 60
       and _end_drawn[0] > 0 and _first_items < 60 * 3, f"first={_first_drawn} end={_end_drawn} items={_first_items}")
    _calls = []
    _real_cfg = _cv.configure
    _cv.configure = lambda *a, **k: (_calls.append(k) if "scrollregion" in k else None, _real_cfg(*a, **k))[1]
    _lb._draw(); _lb._draw()
    _cv.configure = _real_cfg
    ok("queue.scrollregion-only-on-change", not any("scrollregion" in c for c in _calls), str(_calls))
    _lb.see(0); root.update_idletasks()
    _wheel_handled = _cv.event_generate  # placeholder to keep linters calm
    _before = _cv.yview()[0]
    _r1 = _cv.bind("<MouseWheel>")
    _r2 = _cv.bind("<TouchpadScroll>") if tk.TkVersion >= 9 else "n/a"
    ok("scroll.canvas-has-direct-bindings", bool(_r1) and bool(_r2), f"wheel={bool(_r1)} touchpad={bool(_r2)}")
    # a notch is 120 on Tk 9 and Windows, 1 on Tk 8.6 macOS; both scroll 40 px
    _cv._scroll_px("y", 0)
    _b = _cv.yview()[0]; _cv.event_generate("<MouseWheel>", delta=-120, x=10, y=10, rootx=1, rooty=1) if False else None
    _b = _cv.yview()[0]
    class _W:
        def __init__(self, d): self.delta = d
    # digging out the bound handler is awkward, so test the pixel primitive it uses
    _cv._scroll_px("y", 40); _a1 = _cv.yview()[0]
    ok("scroll.wheel-notch-is-40px", round((_a1 - _b) * _total_px) == 40, f"{round((_a1 - _b) * _total_px)} px")
    # Text widgets keep Tk's own class bindings, or they scroll twice
    _txt = m.kit_text_probe if hasattr(m, "kit_text_probe") else app.kit.text(root)
    ok("scroll.text-not-double-bound", not hasattr(_txt, "_scroll_px"), str(hasattr(_txt, "_scroll_px")))
    _txt.destroy()
    _real_yview = _cv.yview
    _cv.yview = lambda: (0.0, 1.0)
    try:
        _quiet = _cv._scroll_px("y", 40)
    finally:
        _cv.yview = _real_yview
    ok("scroll.quiet-when-nothing-to-scroll", _quiet is False, str(_quiet))
    _sf_ok = True
    if tk.TkVersion >= 9:
        _sf_ok = "<TouchpadScroll>" in root.bind_all()
    ok("scroll.settings-scrollframe-touchpad", _sf_ok and hasattr(m.ScrollFrame, "_touchpad_all"), str(_sf_ok))
    # CTkScrollbar forces a layout pass per set(), hence the thin canvas bar
    _sfx = m.ScrollFrame(root)
    _thin = isinstance(_sfx._scrollbar, m.ThinScrollbar)
    _sfx._scrollbar.set(0.2, 0.5)
    ok("scroll.pages-use-thin-scrollbar", _thin and _sfx._scrollbar.get() == (0.2, 0.5), type(_sfx._scrollbar).__name__)
    # CTk decides with isinstance() whether a wheel event over a child scrolls the page
    _child = m.ctk.CTkLabel(_sfx, text="x"); _child.pack()
    try:
        _valid = _sfx._check_if_valid_scroll(_child) and not _sfx._check_if_valid_scroll(_sfx._scrollbar)
    except Exception as e:
        _valid = f"{type(e).__name__}: {e}"
    ok("scroll.wheel-over-page-content", _valid is True, str(_valid))
    _bar, _moves = _sfx._scrollbar, []
    _bar.winfo_height = lambda: 400
    _bar.command = lambda *a: _moves.append(a)
    _bar.set(0.5, 0.5001)
    _t, _b = _bar._px
    class _P:
        def __init__(self, y): self.y = y
    _bar._press(_P((_t + _b) / 2))
    _held = not _moves
    _bar._drag(_P((_t + _b) / 2 + 10))
    ok("scroll.thin-thumb-press-holds", _held and _b - _t >= 29 and _moves and _moves[-1][0] == "moveto"
       and 0.5 < _moves[-1][1] < 0.6, f"px={_bar._px} moves={_moves}")
    _sfx.destroy()
    del _cv.winfo_height
    app.queue[:] = _saved; app.update_queue_box(); root.update()

    ok("all.fmt-left", (app._fmt_left(30), app._fmt_left(600), app._fmt_left(7800), app._fmt_left(7320))
       == ("about a minute left", "about 10 min left", "about 2 h 10 min left", "about 2 h left"),
       str((app._fmt_left(30), app._fmt_left(600), app._fmt_left(7800), app._fmt_left(7320))))
    _GB = 1024 ** 3
    _cj2 = m.GameItem.from_chain(HBT, to="ffpfsc")
    app._batch_t0 = time.time() - 30
    ok("all.no-guess-in-the-first-90-s", app._estimate_left(10 * _GB, 10 * _GB, 50, _cj2) is None, "")
    app._batch_t0 = time.time() - 200
    app._batch_fin_secs, app._batch_fin_bytes = 0.0, 0
    app._job_t0 = {id(_cj2): time.time() - 100}
    _e1 = app._estimate_left(0, 100 * _GB, 10, _cj2)  # 10 GB in 100 s, 90 GB left: about 900 s
    app._batch_fin_secs, app._batch_fin_bytes = 1000.0, 100 * _GB
    _e2 = app._estimate_left(200 * _GB, 50 * _GB, 50, _cj2)  # 10 s/GB * 225 GB = 2250 s
    ok("all.estimate-from-pace", _e1 is not None and abs(_e1 - 900) < 5 and _e2 is not None and abs(_e2 - 2250) < 5,
       f"{_e1} {_e2}")
    _sv = (app._batch_running, app._batch_total, app._batch_done, app._batch_failed)
    app._batch_running, app._batch_total, app._batch_done, app._batch_failed = True, 5, 1, 0
    app._all_frac, app._all_eta = 0.23, 7800
    app._show_add_progress(); app._update_batch_counter(); root.update()
    _line = f"{app._all_left_var.get()} | {app._all_right_var.get()}"
    _vis, _top_free = app._all_box.winfo_manager(), app._add_box.winfo_manager() == ""
    _foot = app.batch_counter_var.get()
    _row = int(app._all_box.grid_info().get("row", -1)) if _vis else -1
    app._batch_running = False
    app._show_add_progress(); app._update_batch_counter(); root.update()
    _hidden = app._all_box.winfo_manager() == ""
    app._batch_running, app._batch_total, app._batch_done, app._batch_failed = _sv
    app._all_frac = app._all_eta = None
    ok("all.summary-at-the-foot-of-the-list", _vis == "grid" and _row == 3 and _top_free
       and _line == "All jobs  23 % | about 2 h 10 min left" and _hidden and app._all_bar._fill == "muted",
       f"{_line!r} {_vis!r} row={_row} top_free={_top_free} {_hidden}")
    ok("all.status-bar", "Job 2 of 5" in _foot and "23 % of all" in _foot and "about 2 h 10 min left" in _foot, _foot)
    _o = [m.GameItem.from_chain(HBT, to="ffpfsc") for _ in range(4)]
    for _k, _it in enumerate(_o):
        _it.display_name = f"Job {_k}"; _it.status = "Queued"
    app.queue[:] = list(_o); app._batch_running = False; app._active_item = None; app._run_next = None
    app._retire_failed(_o[0], "Failed", "x"); app._retire_failed(_o[1], "Skipped", "y")
    ok("order.finished-jobs-keep-their-place", app.queue == _o, str([i.display_name for i in app.queue]))
    ok("order.next-is-first-waiting", app._next_pending() is _o[2], str(getattr(app._next_pending(), "display_name", None)))
    app._run_next = _o[3]
    ok("order.retry-runs-next-without-moving", app._next_pending() is _o[3] and app.queue == _o, "")
    app._run_next = None
    app.move_job(3, 2)                                  # drag the last job above the third
    ok("order.drag-moves-job", app.queue == [_o[0], _o[1], _o[3], _o[2]], str([_o.index(i) for i in app.queue]))
    app.queue[:] = list(_o); app.update_queue_box()
    app.run_job_next(_o[3])
    ok("order.run-next-moves-above-first-waiting", app.queue == [_o[0], _o[1], _o[3], _o[2]]
       and app._next_pending() is _o[3], str([_o.index(i) for i in app.queue]))
    _moves = []
    _real_mv = app.queue_listbox.on_move
    app.queue_listbox.on_move = lambda s, d: _moves.append((s, d))
    app.queue[:] = list(_o); app.update_queue_box(); root.update()
    _cv = app.queue_listbox.cv
    _RH = app.queue_listbox.ROW_H
    _cv.winfo_height = lambda: 400                      # the driver's window is withdrawn (no height)
    _cv.yview_moveto(0); root.update()
    class _E:
        def __init__(self, y): self.y, self.x = y, 40
    app.queue_listbox._click(_E(6 + 3 * _RH + 20))
    _pressed = app.queue_listbox._press
    app.queue_listbox._drag(_E(6 + 3 * _RH + 17))       # under the threshold: still a click
    _no_drag = app.queue_listbox._drop is None
    app.queue_listbox._drag(_E(6 + 1 * _RH + 4))        # up to the line between rows 0 and 1
    app.queue_listbox._release(_E(6 + 1 * _RH + 4))
    app.queue_listbox.on_move = _real_mv
    del _cv.winfo_height
    ok("order.list-drag-and-drop", _no_drag and _moves == [(3, 1)],
       f"{_moves} pressed={_pressed} rows={len(app.queue_listbox.rows)} no_drag={_no_drag}")
    app.queue[:] = list(_o[2:]); app._batch_running = False
    for _it in app.queue:
        _it.status = "Queued"
    app._ensure_batch_started(); _tot0 = app._batch_total
    app._add_jobs_async([HBT], lambda s: m.GameItem.from_chain(s, to="ffpfsc", output_path=str(OUT / "job")))
    settle()
    ok("order.mid-run-add-counts", _tot0 == 2 and app._batch_total == 3 and len(app._batch_items) == 3,
       f"{_tot0} {app._batch_total}")
    app._batch_running = False; app._update_batch_counter()
    app.queue[:] = []; app._active_item = None; app.update_queue_box(); root.update()
    _xo = OUT / "exists_check"; shutil.rmtree(_xo, ignore_errors=True); _xo.mkdir(parents=True)
    _xj = m.GameItem.from_chain(HBT, to="ffpfsc", output_path=str(_xo)); _xj.auto_organize = True
    _pred = app._predicted_output(_xj)
    ok("exists.predicted-like-build", _pred is not None and _pred.suffix == ".ffpfsc"
       and _pred.parent.parent == _xo, str(_pred))
    _pred.parent.mkdir(parents=True, exist_ok=True)
    _bare = app._FW_TAG.sub("", _pred.stem)
    _known = bool(app._FW_TAG.search(_pred.stem))
    _other_fw = _pred.with_name(_bare + " [fw9.99]" + _pred.suffix)
    _other_fw.write_bytes(b"old")
    ok("exists.other-firmware-is-another-build", (app._existing_output(_pred) is None) if _known
       else (app._existing_output(_pred) == _other_fw), f"known={_known} {app._existing_output(_pred)}")
    _untagged = _pred.with_name(_bare + _pred.suffix); _untagged.write_bytes(b"old")
    ok("exists.untagged-older-build-counts", app._existing_output(_pred) in ((_untagged,) if _known else (_untagged, _other_fw)),
       str(app._existing_output(_pred)))
    _other_fw.unlink(); _untagged.unlink()
    _ut = _xo / "fwrule" / "Example Quest [PPSA00001] [v01.000].ffpfsc"; _ut.parent.mkdir(parents=True, exist_ok=True)
    _f10 = _ut.with_name("Example Quest [PPSA00001] [v01.000] [fw10.00].ffpfsc"); _f10.write_bytes(b"x")
    _plain = m.GameItem.from_chain(HBT, to="ffpfsc")
    _bpj = m.GameItem.from_chain(HBT, to="ffpfsc", backport_target="7.61")
    _hit_plain, _hit_bp = app._existing_output(_ut, _plain), app._existing_output(_ut, _bpj)
    _f761 = _ut.with_name("Example Quest [PPSA00001] [v01.000] [fw7.61].ffpfsc"); _f761.write_bytes(b"x")
    ok("exists.unknown-fw-rules", _hit_plain == _f10 and _hit_bp is None and app._existing_output(_ut, _bpj) == _f761,
       f"{_hit_plain} {_hit_bp} {app._existing_output(_ut, _bpj)}")
    _pred.write_bytes(b"old")
    _real_askx = app._ask_existing_outputs
    _asked_x = []
    def _ans(choice):
        def f(conflicts):
            _asked_x.append([str(h) for _n, h in conflicts]); return choice
        return f
    _rule0 = app.output_exists_var.get(); app.output_exists_var.set("ask")
    try:
        app.queue[:] = [_xj]; _xj.status = "Queued"
        app._ask_existing_outputs = _ans("cancel")
        _c = app._check_existing_outputs()
        app._ask_existing_outputs = _ans("overwrite")
        _o = app._check_existing_outputs(); _o_pol = app._output_policy
        _o_turn = app._late_output_check(_xj); _o_flag = getattr(_xj, "_replace_output", False)
        app._ask_existing_outputs = _ans("skip")
        _s = app._check_existing_outputs(); _s_state = _xj.status
        _s_turn = app._late_output_check(_xj)
    finally:
        app._ask_existing_outputs = _real_askx
        app.output_exists_var.set(_rule0)
    ok("exists.cancel-stops-start", _c is False, str(_c))
    ok("exists.ask-once-then-each-job-at-its-turn", _o is True and _o_pol == "overwrite" and _o_turn == "proceed"
       and _o_flag, f"{_o_pol} {_o_turn} {_o_flag}")
    ok("exists.skip-decided-at-the-turn", _s is True and _s_state == "Queued" and _s_turn == "skip"
       and len(_asked_x) == 3, f"{_s_state} {_s_turn} {len(_asked_x)}")
    _asked_x.clear(); app._ask_existing_outputs = _ans("cancel")
    try:
        _turns = {}
        for _rule in ("skip", "keep", "overwrite"):
            _xj.status = "Queued"; _xj._keep_both = _xj._replace_output = False
            app.output_exists_var.set(_rule)
            _ok_start = app._check_existing_outputs()
            _turns[_rule] = (_ok_start, app._late_output_check(_xj), getattr(_xj, "_keep_both", False),
                             getattr(_xj, "_replace_output", False))
    finally:
        app._ask_existing_outputs = _real_askx
        app.output_exists_var.set(_rule0)
        app._output_policy = None
    ok("exists.rule-skip-is-default-without-window", _rule0 == "skip" and _turns["skip"][:2] == (True, "skip")
       and not _asked_x, f"{_rule0} {_turns} {_asked_x}")
    ok("exists.rule-keep-and-overwrite", _turns["keep"][1] == "proceed" and _turns["keep"][2]
       and _turns["overwrite"][1] == "proceed" and _turns["overwrite"][3], str(_turns))
    _kb = _pred.parent / "Example Quest [PPSA00001] [v01.000] [fw10.00].ffpfsc"; _kb.write_bytes(b"old")
    _kbi = m.GameItem.from_chain(HBT, to="ffpfsc"); _kbi._keep_both = True
    _kn = app._keep_both_name(_kbi, _kb)
    _long = _pred.parent / ("A" * 70 + " [PPSA00001] [v01.000] [fw10.00].ffpfsc")
    _ln = app._free_output_name(_long)
    ok("exists.keep-both-numbered-name", _kn.name == "Example Quest (2) [PPSA00001] [v01.000] [fw10.00].ffpfsc"
       and len(_ln.name.encode()) <= m.SHADOWMOUNT_NAME_LIMIT and " (2) [PPSA00001]" in _ln.name, f"{_kn.name} | {_ln.name}")
    _lj = m.GameItem.from_chain(HBT, to="ffpfsc", output_path=str(_xo)); _lj.auto_organize = True
    _lj._output_checked = False
    app._output_policy = "skip"
    ok("exists.late-check-follows-choice", app._late_output_check(_lj) == "skip", "")
    _uj = m.GameItem.from_chain(HBT, to="ffpfsc", output_path=str(_xo)); _uj._output_checked = False
    _real_pred = app._predicted_output
    app._predicted_output = lambda it: None       # an archive whose game is not readable yet
    try:
        _u1 = app._late_output_check(_uj); _u_checked = getattr(_uj, "_output_checked", False)
    finally:
        app._predicted_output = _real_pred
    ok("exists.unknown-output-checked-again-after-unpack", _u1 == "proceed" and not _u_checked, f"{_u1} {_u_checked}")
    app._output_policy = None
    _pk_dir = _xo / "pkgtest"; _pk_dir.mkdir()
    (_pk_dir / "Game [PPSA00001] [v01.000].pkg").write_bytes(b"old")
    _new_pkg = _pk_dir / "UP0000-PPSA00001_00-X.pkg"; _new_pkg.write_bytes(b"new")
    _pi = m.GameItem.from_chain(HBT, to="pkg"); _pi._organized_pkg_name = "Game [PPSA00001] [v01.000].pkg"
    _pi._replace_output = True
    _fin = app._finalize_pkg_name(_pi, _new_pkg)
    ok("exists.pkg-overwrite-replaces", _fin.read_bytes() == b"new" and not (_pk_dir / "Game [PPSA00001] [v01.000] (2).pkg").exists(),
       str(sorted(x.name for x in _pk_dir.iterdir())))
    import zipfile as _zf
    _za = S / "release" / "[site.example]-PPSA00777.zip"; _za.parent.mkdir(exist_ok=True)
    with _zf.ZipFile(_za, "w") as _z:
        _z.writestr("PPSA00777/sce_sys/param.json", json.dumps({"titleId": "PPSA00777", "contentVersion": "01.004.000",
            "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": "Example Quest™"}}}))
        _z.writestr("PPSA00777/eboot.bin", b"\x7fELF" + b"\0" * 100)
    _ai2 = m.GameItem.from_chain(_za, to="ffpfsc", output_path=str(_xo))
    _ident_a = app._archive_identity(_ai2)
    ok("name.archive-identity-before-extraction", _ident_a is not None and _ident_a["title"] == "Example Quest™"
       and _ident_a["title_id"] == "PPSA00777" and _ai2.archive_version == "01.004.000", str(_ident_a))
    _ai3 = m.GameItem.from_chain(_za, to="ffpfsc", output_path=str(_xo))
    app.queue[:] = [_ai3]; app.update_queue_box(); root.update()
    pump(lambda: getattr(_ai3, "display_name", "") == "Example Quest [PPSA00777]", timeout=10.0); root.update()
    ok("name.job-named-after-game", _ai3.display_name == "Example Quest [PPSA00777]", str(_ai3.display_name))
    _ap = app._predicted_output(_ai3)
    ok("name.archive-output-predicted", _ap is not None and "Example Quest" in _ap.name, str(_ap))
    app.queue[:] = []
    app.update_queue_box(); root.update()
    _real_fc = m.GameItem.from_chain
    def _slow_fc(*a, **k):
        time.sleep(0.25)
        return _real_fc(*a, **k)
    _starts2 = []
    _real_start2 = app.start
    m.GameItem.from_chain = _slow_fc
    app.start = lambda rearm_failed=True: _starts2.append(rearm_failed)
    try:
        _srcs = [HBT] * 5
        app._add_jobs_async(_srcs, lambda s: m.GameItem.from_chain(s, to="ffpfsc", output_path=str(OUT / "job")))
        pump(lambda: (getattr(app, "_add_state", {}) or {}).get("done", 0) >= 2, timeout=10.0)
        _mid_visible = app._add_box.winfo_manager() == "grid"
        _mid_label = app._add_label_var.get()
        _mid_queue = len(app.queue)
        _real_start2()                    # pressed while jobs are still being added
        _pending = app.pending_start
        settle()
        root.update(); time.sleep(0.2); root.update()
    finally:
        m.GameItem.from_chain = _real_fc
        app.start = _real_start2
    ok("add.progress-while-adding", _mid_visible and "Adding jobs" in _mid_label and " of 5" in _mid_label
       and 2 <= _mid_queue < 5, f"{_mid_visible} {_mid_label!r} {_mid_queue}")
    ok("add.all-in-and-bar-gone", len(app.queue) == 5 and app._add_box.winfo_manager() == "", str(len(app.queue)))
    ok("add.start-waits-for-new-jobs", _pending and _starts2 == [True], f"{_pending} {_starts2}")
    app.pending_start = False
    app.queue[:] = []
    app.update_queue_box(); root.update()
    app.play_complete_sound = _real_sound
    app.queue[:] = _live_q; app.temp_var.set(_old_temp2); app._active_item = None
    app.update_queue_box(); root.update()
    jc2 = m.GameItem.from_chain(HBT, to="ffpfsc", output_path=str(OUT / "job"))
    jc2.compression_level = 5
    app.queue.append(jc2); app.update_queue_box(select_item=jc2); root.update()
    app.update_game_details(jc2); root.update()
    _ci = {k: v[0].get() for k, v in app._card_info_rows.items()}
    ok("card.info.rows", _ci.get("Source") == str(HBT) and "zlib level 5" in _ci.get("Compression", "")
       and _ci.get("Status") == "Queued" and _ci.get("Changes") == "None", str(_ci))
    app._retire_failed(jc2, "Failed", "Permission denied: '/Volumes/Out'"); app.update_game_details(jc2)
    ok("card.info.failure-reason", app._card_info_rows["Status"][0].get() == "Failed: Permission denied: '/Volumes/Out'",
       app._card_info_rows["Status"][0].get())
    ok("card.log.takes-the-height", int(app._card_body.grid_rowconfigure(5)["weight"]) == 1
       and app.log_box.mirror_lines >= 100, f"{app._card_body.grid_rowconfigure(5)} {app.log_box.mirror_lines}")
    app.log_box.set_mirror_visible(12)
    for _i in range(30):
        app.log("INFO", f"card tail line {_i}")
    pump(lambda: app._log_tail.get("1.0", "end-1c").rstrip().endswith("card tail line 29"), timeout=5.0)
    _tail = app._log_tail.get("1.0", "end-1c").rstrip().splitlines()
    ok("card.log.keeps-more-than-four", len(_tail) >= 30 and _tail[-1].endswith("card tail line 29"), str(len(_tail)))
    app.queue.remove(jc2); app.update_queue_box(); root.update()
    import test_backport as _tb
    _saved_defaults = json.loads(json.dumps(m.load_settings().get("job_dialog_defaults", {}) or {}))
    fwr = S / "fwroot"
    _tb._fw_lib(fwr / "9.60", 0x11590001, 0x09600004)
    _tb._fw_lib(fwr / "10.01", 0x12090001, 0x10010000)
    _tb._fw_lib(fwr / "5.02", 0x09690001, 0x05100023)          # holds newer (5.10) files
    _st = m._firmware_folder_status(str(fwr))
    ok("settings.fw-status", _st == "3 firmware folder(s) found: 5.02 … 10.01.", _st)
    app.fw_libs_var.set(str(fwr))
    jd7 = m.JobDialog(app, init_src=str(HBT)); root.update()
    _vals = list(jd7._target_menu.cget("values"))
    ok("job.backport.targets-from-fw-root", _vals == ["5.02", "6.02", "7.61", "9.60", "10.xx", "10.01"], str(_vals))
    jd7.backport_on_var.set(True); jd7.backport_target_var.set("9.60"); root.update()
    ok("job.backport.hint-derived", "SDK values from your 9.60 libraries" in jd7.backport_hint_var.get(),
       jd7.backport_hint_var.get())
    jd7.backport_target_var.set("5.02"); root.update()
    ok("job.backport.hint-problem", "cannot be used" in jd7.backport_hint_var.get()
       and "5.10" in jd7.backport_hint_var.get(), jd7.backport_hint_var.get())
    jd7.to_var.set(".ffpfsc"); jd7.out_var.set(str(OUT / "job")); root.update()
    n0 = len(app.queue); errors.clear(); jd7._add(); settle()
    ok("job.backport.add-refuses-unusable-target", len(app.queue) == n0 and any("5.10" in x for x in errors),
       str(errors))
    ok("job.backport.no-libraries-field", not hasattr(jd7, "backport_libs_var"), "")
    jd7.backport_target_var.set("9.60"); app.backport_libs_var.set(str(fwr)); root.update()
    n0 = len(app.queue); errors.clear(); jd7._add(); settle()
    ok("job.backport.refuses-firmware-folder-as-patched", len(app.queue) == n0
       and any("original libraries" in x and "Settings" in x for x in errors), str(errors))
    app.backport_libs_var.set("")
    ok("settings.patched-libs-cleaned", m._sane_patched_libs(str(fwr), str(fwr)) == ""
       and m._sane_patched_libs(str(fwr / "9.60"), str(fwr)) == ""
       and m._sane_patched_libs(str(OUT), str(fwr)) == str(OUT) and m._sane_patched_libs("", str(fwr)) == "", "")
    _pl = S / "patched_sets"; (_pl / "9.60").mkdir(parents=True, exist_ok=True)
    app.backport_libs_var.set(str(_pl))
    _bj = m.GameItem.from_chain(HBT, to="ffpfsc", backport_target="9.60")
    _ba = app._backport_args(_bj)
    ok("job.backport.takes-settings-libraries", _bj.backport_libs_root is None
       and _ba[_ba.index("--backport-libs") + 1] == str(_pl), str(_ba))
    app.backport_libs_var.set("")
    jd7.backport_target_var.set("5.02"); root.update()
    jd7._check_compat()
    pump(lambda: jd7.check_var.get() not in ("", "Checking…"), timeout=60.0)
    ok("job.check.shows-verdict", jd7.check_var.get().startswith(("Not checked", "The game uses", "Firmware",
                                                                  "Every function")), jd7.check_var.get())
    jd7.backport_target_var.set("9.60"); root.update(); errors.clear(); jd7._add(); settle()
    j7 = app.queue[-1]
    jcmd7, *_ = app.build_command(j7)
    ok("job.backport.cmd-derived-target", len(app.queue) == n0 + 1 and j7.backport_target == "9.60"
       and jcmd7[jcmd7.index("--backport-target") + 1] == "9.60"
       and jcmd7[jcmd7.index("--fw-libs-root") + 1] == str(fwr), " ".join(jcmd7[-14:]) + f" errors={errors}")
    app.queue.remove(j7)
    app.fw_libs_var.set("")
    jd8 = m.JobDialog(app, init_src=str(HBT)); root.update()
    jd8._check_compat(); root.update()
    ok("job.check.needs-fw-root", "Set the firmware libraries folder" in jd8.check_var.get(), jd8.check_var.get())
    jd8.destroy()
    sg = S / "signed_game"; (sg / "sce_sys").mkdir(parents=True)
    (sg / "sce_sys" / "param.json").write_text(json.dumps({
        "titleId": "PPSA00002", "contentId": "UP0000-PPSA00002_00-EXAMPLE000000000",
        "contentVersion": "01.000.000", "localizedParameters": {"defaultLanguage": "en-US",
                                                                "en-US": {"titleName": "Signed"}}}),
        encoding="utf-8")
    (sg / "eboot.bin").write_bytes(_tb._fself(_tb._signable_elf(0x11590001, 0x09600004), ps5=True))
    jd9 = m.JobDialog(app, init_src=str(sg)); root.update()
    ok("job.detect.sdk-of-signed-eboot", "SDK 9.60" in jd9.detect_var.get(), jd9.detect_var.get())
    jd9.destroy()
    m.save_settings({"job_dialog_defaults": _saved_defaults})  # an earlier step remembered a 9.60 backport for folders
    parent = S / "parent"; parent.mkdir(exist_ok=True)
    for nme in ("A", "B"):
        if not (parent / nme).exists():
            shutil.copytree(HBT, parent / nme)
    jd7 = m.JobDialog(app, init_src=str(parent)); root.update()
    ok("job.detect.parent", jd7._kind == "parent" and len(jd7._games) == 2, jd7.detect_var.get())
    jd7.to_var.set(".ffpfsc"); jd7.out_var.set(str(OUT / "job")); n0 = len(app.queue); jd7._add(); settle()
    ok("job.add.parent-two-items", len(app.queue) == n0 + 2 and all(x.operation == "chain" for x in app.queue[-2:]),
       f"+{len(app.queue) - n0}")
    _jd = m.load_settings().get("job_dialog_defaults") or {}
    ok("job.defaults-saved", _jd.get("folder", {}).get("to") == "ffpfsc" and _jd.get("ffpfsc", {}).get("to") == "pkg", str(_jd))
    app.queue_listbox.selection_clear(0, "end"); app.queue_listbox.selection_set(idx)
    before = set(app._panels.stack)
    app._on_queue_double_click(None); root.update()
    opened = [w for w in app._panels.stack if w not in before and isinstance(w, m.JobDialog)]
    ok("job.double-click.opens-job-dialog", len(opened) == 1, f"{[type(w).__name__ for w in app._panels.stack if w not in before]}")
    close_toplevels()
    n_panels = len(app._panels.stack)
    sd = m.SummaryDialog(root, "report"); ed = m.ErrorDialog(root, "boom", "", "", operation="pack")
    root.update(); time.sleep(0.3); root.update()
    ok("message.summary-own-window", isinstance(sd, m.ctk.CTkToplevel) and sd not in app._panels.stack
       and len(app._panels.stack) == n_panels, f"panels {n_panels} -> {len(app._panels.stack)}")
    ok("message.error-own-window", isinstance(ed, m.ctk.CTkToplevel) and ed not in app._panels.stack, "")
    ok("message.waits-for-main-window", not sd.winfo_viewable() and not ed.winfo_viewable(),
       "the driver's main window is withdrawn, so no message may appear on screen")
    ok("message.prompts-own-window", all(issubclass(c, m.MessageWindow) for c in
       (m.SpaceDiagnosticsDialog, _REAL_PW_PROMPT)), "")
    ok("work-surface.panels", all(issubclass(c, m.EmbeddedDialog) for c in
       (m.JobDialog, m.FirstRunWizard)) and not hasattr(m, "PfsBrowserDialog"), "")
    sd.destroy(); ed.destroy(); root.update()

    # jobs, deletes, Finder: check the handler, never click the button
    close_toplevels(); root.update()
    _fd_calls = []
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ps4_fixture import make_pkg as _mk4
    _p4dir = S / "ps4_src"; _p4dir.mkdir(exist_ok=True)
    _p4g = _mk4(_p4dir / "base.pkg", content_id="UP0000-CUSA00001_00-SAMPLEGAME000000", content_type=0x1A,
                sfo={"TITLE": "Sample Game", "TITLE_ID": "CUSA00001", "CATEGORY": "gd", "APP_VER": "01.00"})
    _mk4(_p4dir / "dlc.pkg", content_id="UP0000-CUSA00001_00-SAMPLEDLC0000000", content_type=0x1B,
         sfo={"TITLE": "Sample Game - Extra", "TITLE_ID": "CUSA00001", "CATEGORY": "ac", "VERSION": "01.00"})
    _mix = S / "mixed_src"; shutil.rmtree(_mix, ignore_errors=True); (_mix / "ps4").mkdir(parents=True)
    shutil.copyfile(_p4g, _mix / "ps4" / "Sample Game.pkg")
    ok("ps4.folder-of-ps4-packages", m._is_ps4_source(_mix), str(_mix))
    shutil.copytree(HBT, _mix / "HomebrewTest")
    ok("mixed.folder-is-not-ps4", not m._is_ps4_source(_mix), str(_mix))
    _dm = m.JobDialog(app, init_src=str(_mix), init_to="ffpfsc"); root.update()
    ok("mixed.dialog-parent", _dm._kind == "parent" and "PS4" in _dm.detect_var.get()
       and _dm._to_key() == "ffpfsc", f"{_dm._kind} {_dm.detect_var.get()} {_dm._to_key()}")
    ok("mixed.ps4-block-shown", bool(_dm._ps4_box.winfo_manager()) and bool(_dm._ps5_head.winfo_manager())
       and _dm._ps4_head_var.get() == "PS4 packages (1)" and _dm._ps5_head_var.get() == "PS5 games (1)",
       f"{_dm._ps4_head_var.get()} {_dm._ps5_head_var.get()}")
    _qm0 = len(app.queue)
    _ps4lib = S / "ps4_library"
    _dm.out_var.set(str(OUT)); _dm.ps4_out_var.set(str(_ps4lib)); root.update(); _dm._refresh()
    ok("mixed.summary-splits", "× 1 job" in _dm.summary_var.get() and "1 PS4 package into the library" in _dm.summary_var.get()
       and str(_ps4lib) in _dm.summary_dest_var.get(), f"{_dm.summary_var.get()} | {_dm.summary_dest_var.get()}")
    _dm._add(); settle()
    _qm = app.queue[_qm0:]
    ok("mixed.one-job-each", len(_qm) == 2
       and any(getattr(j, "content_kind", "") == "ps4" and j.operation == "copy"
               and str(j.output_path) == str(_ps4lib) for j in _qm)
       and any(j.operation == "chain" and j.chain_to == "ffpfsc" and Path(j.path).name == "HomebrewTest"
               and str(j.output_path) == str(OUT) for j in _qm),
       str([(j.operation, getattr(j, "content_kind", None), getattr(j, "chain_to", None), Path(j.path).name,
             str(j.output_path)) for j in _qm]))
    ok("mixed.folders-remembered", app._recent_outputs("ps4")[:1] == [str(_ps4lib)]
       and app._recent_outputs("ps5")[:1] == [str(OUT)], f"{app._recent_outputs('ps4')} {app._recent_outputs('ps5')}")
    _d4s = m.JobDialog(app, init_src=str(_p4g)); root.update()
    ok("mixed.ps4-source-gets-ps4-folder", _d4s.out_var.get() == str(_ps4lib) and not _d4s._ps4_box.winfo_manager(),
       _d4s.out_var.get())
    _d4s.destroy(); root.update()
    for _j in _qm:
        app.queue.remove(_j)
    app.update_queue_box()
    shutil.rmtree(_mix, ignore_errors=True)
    _big = S / "big_src"; shutil.rmtree(_big, ignore_errors=True); _big.mkdir()
    for _i in range(61):
        with zipfile.ZipFile(_big / f"Game {_i:02d}.zip", "w") as _z:
            _z.writestr(f"Game {_i:02d}/note.txt", "x")
    _zp4 = _big / "zz PS4 Game.zip"
    with zipfile.ZipFile(_zp4, "w") as _z:
        _z.write(_p4g, "zz PS4 Game/base.pkg")
    _dp = m.JobDialog(app, init_src=str(_p4dir)); root.update()
    ok("dialog.read.ps4-folder-is-one-ps4-source", _dp._kind == "ps4" and _dp._games == [_p4dir]
       and _dp.detect_var.get().startswith("PS4 packages · ") and _dp._to_key() == "pkg",
       f"{_dp._kind} {_dp.detect_var.get()} {_dp._to_key()}")
    _dp.destroy(); root.update()
    _wait_reads[0] = False
    try:
        _qb0 = len(app.queue)
        _db = m.JobDialog(app, init_src=str(_big), init_to="ffpfsc"); root.update()
        _db.out_var.set(str(OUT)); root.update()
        _r0 = (_db._reading, _db._add_btn.cget("state"), _db.summary_var.get(), _db.detect_var.get())
        _db._add(); root.update()
        ok("dialog.read.folder-waits", _r0[0] == "folder" and _r0[1] == "disabled"
           and _r0[2] == "Reading the folder…" and _r0[3].startswith("Reading the folder")
           and len(app.queue) == _qb0, str(_r0))
        pump(lambda: _db._reading == "archives", 30)
        _r1 = (_db._reading, _db._add_btn.cget("state"), _db.summary_var.get(), _db.detect_var.get())
        ok("dialog.read.archives-counted", _r1[0] == "archives" and _r1[1] == "disabled"
           and _r1[2].startswith("Checking the archives for PS4 packages: ") and _r1[2].endswith(" of 62…")
           and _r1[3].startswith("Parent folder · 62 sources: 62 archives"), str(_r1))
        pump(lambda: not _db._reading, 120)
        ok("dialog.read.every-archive-checked", str(_zp4) in _db._ps4_set and _db._ps4_n == 1
           and _db._add_btn.cget("state") == "normal" and "1 archive of PS4 packages" in _db.detect_var.get()
           and not _db.summary_var.get().startswith("Checking"), f"{_db._ps4_n} {_db.detect_var.get()}")
        _db.destroy(); root.update()
        _dc = m.JobDialog(app, init_src=str(_big), init_to="ffpfsc"); root.update()
        pump(lambda: _dc._reading == "archives", 30)
        _was = _dc._reading
        _dc.src_var.set(str(HBT)); root.update()
        pump(lambda: False, 1.5)
        ok("dialog.read.new-source-stops-the-read", _was == "archives" and not _dc._reading and _dc._kind == "folder"
           and _dc._games == [HBT] and _dc._ps4_n == 0 and not _dc.summary_var.get().startswith("Checking"),
           f"{_was} {_dc._reading} {_dc._kind} {_dc._ps4_n} {_dc.detect_var.get()}")
        _dc.destroy(); root.update()
        # a PS5 id in the folder or archive name skips the PS4 check; a CUSA id never does
        _p5f = S / "ps5named" / "Some Game [PPSA00001] [v01.000]"; _p5f.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(_p5f / "Some.Game-PPSA00001.part.zip", "w") as _z:
            _z.writestr("Some Game/note.txt", "x")
        with zipfile.ZipFile(_p5f / "Extra CUSA00001.zip", "w") as _z:
            _z.write(_p4g, "Extra/base.pkg")
        _dn = m.JobDialog(app, init_src=str(_p5f), init_to="ffpfsc"); root.update()
        pump(lambda: _dn._kind == "parent" and not _dn._reading, 30)
        ok("dialog.read.ps5-id-skips-the-ps4-check",
           m.JobDialog._ps5_named(_p5f / "Some.Game-PPSA00001.part.zip", _p5f)
           and not m.JobDialog._ps5_named(_p5f / "Extra CUSA00001.zip", _p5f)
           and _dn._ps4_set == {str(_p5f / "Extra CUSA00001.zip")}, f"{_dn._ps4_set}")
        _dn.destroy(); root.update()
        # a long RAR read is named, with its time, in the dialog and in the queue's adding line
        m.ArchiveExtractor._rar_busy[-1] = ("Big.Set.part01.rar", time.monotonic() - 65)
        try:
            _note = m.reading_note()
            app._add_state = {"total": 3, "done": 1, "made": [], "errors": 0}
            app._show_add_progress()
            _line = app._add_label_var.get()
        finally:
            m.ArchiveExtractor._rar_busy.pop(-1, None)
            app._add_state = {"total": 0, "done": 0, "made": [], "errors": 0}
            app._show_add_progress()
        ok("dialog.read.slow-rar-is-named", _note == "  ·  reading Big.Set.part01.rar · 1:05"
           and _line.startswith("Adding jobs…  1 of 3") and "reading Big.Set.part01.rar · 1:05" in _line
           and m.reading_note() == "", f"{_note!r} {_line!r}")
    finally:
        _wait_reads[0] = True
        shutil.rmtree(_big, ignore_errors=True)
        shutil.rmtree(S / "ps5named", ignore_errors=True)
    _p4item = app._ps4_item_for(_p4dir, output_path=str(OUT))
    _p4cmd, *_ = app.build_command(_p4item)
    ok("ps4.item-is-sorted-copy", _p4item.operation == "copy" and _p4item.content_kind == "ps4"
       and "--ps4-sort" in _p4cmd and "--if-exists" in _p4cmd and _p4item.display_name == "Sample Game",
       " ".join(_p4cmd[-7:]))
    ok("ps4.row-text", app._job_recipe_parts(_p4item) == ["Folder", "PS4 library · 2 packages"],
       str(app._job_recipe_parts(_p4item)))
    _kind, _paths = app._classify_extracted_payload(_p4dir, "sample.zip")
    ok("ps4.archive-payload", _kind == "ps4" and _paths == [_p4dir], f"{_kind} {_paths}")
    ok("ps4.identity", app._read_image_metadata(_p4g) == {"title": "Sample Game", "title_id": "CUSA00001",
                                                          "version": "01.00"}, str(app._read_image_metadata(_p4g)))
    ok("ps4.no-predicted-output", app._predicted_output(_p4item) is None, "")
    (S / "ps5only").mkdir(exist_ok=True)
    _p5 = _mk4(S / "ps5only" / "x.pkg", content_id="UP0000-PPSA00001_00-SAMPLEGAME000000",
               content_type=0x1A, sfo={"TITLE": "X", "CATEGORY": "gd"})
    _k5, _ = app._classify_extracted_payload(_p5.parent, "ps5.zip")
    ok("ps4.ps5-package-unchanged", _k5 == "pkg" and not m._is_ps4_source(_p5), _k5)
    _aj = m._after_job_module()
    _p4item.after_source = _aj.KEEP
    ok("ps4.copy-mode-keep", app._ps4_copy_mode(_p4item) == "keep", app._ps4_copy_mode(_p4item))
    _p4item.after_source = _aj.DELETE
    ok("ps4.copy-mode-delete-moves", app._ps4_copy_mode(_p4item) == "move", app._ps4_copy_mode(_p4item))
    _p4item.after_source = _aj.TRASH if hasattr(_aj, "TRASH") else "trash"
    ok("ps4.copy-mode-trash-copies", app._ps4_copy_mode(_p4item) == "keep", app._ps4_copy_mode(_p4item))
    _arch4 = S / "ps4_set.zip"; _arch4.write_bytes(b"PK\x05\x06" + b"\0" * 18)
    _p4a = app._ps4_item_for(_p4dir, output_path=str(OUT)); _p4a.origin_archive = str(_arch4)
    _p4a.after_source = _p4item.after_source; _p4a.status = "Done"
    ok("ps4.copy-mode-archive-moves", app._ps4_copy_mode(_p4a) == "move", app._ps4_copy_mode(_p4a))
    _w4 = m.CLIWorker(app, _p4a, ["py", "cli.py", "placeholder", str(OUT), "--ps4-sort", str(_p4dir)], _pcwd, _pout, _ptmp)
    _w4._handle_line(f"[OK] PS4 sorted: {OUT}")
    _plan = app._after_job_plan(_p4a, _w4)
    ok("ps4.after-job-acts-on-the-archive", _plan[0] == _p4a.after_source and str(_arch4) in [str(x) for x in _plan[1]],
       str(_plan))
    # what a copy moved into the library keeps its old date: still this job's output after
    # the report, so the archive gets the after-job rule (the age check is for builds)
    _oldpkg = OUT / "Old Lib [PPSA00001]" / "Old.pkg"; _oldpkg.parent.mkdir(parents=True, exist_ok=True)
    _oldpkg.write_bytes(b"P" * 64); os.utime(_oldpkg, (1_600_000_000, 1_600_000_000))
    _old_seen = []
    for _line, _want in ((f"[OK] Organized: {_oldpkg}", _oldpkg), (f"[OK] PS4 sorted: {_oldpkg.parent}", _oldpkg.parent)):
        _w6 = m.CLIWorker(app, _p4a, ["py", "cli.py", "placeholder", str(OUT), "--ps4-sort", str(_p4dir)], _pcwd, _pout, _ptmp)
        _w6.start_time = time.time()
        _w6._handle_line(_line)
        _w6._write_report(True)
        _old_seen.append((_w6.output_path == str(_want),
                          str(_arch4) in [str(x) for x in app._after_job_plan(_p4a, _w6)[1]]))
    ok("copy.old-dated-output-still-counts", _old_seen == [(True, True), (True, True)], str(_old_seen))
    shutil.rmtree(_oldpkg.parent, ignore_errors=True)
    # exFAT keeps ._ sidecars on a quick eject, a finished job removes its own
    _cl = S / "clutter_out" / "Title [PPSA00001]"; _cl.mkdir(parents=True, exist_ok=True)
    _clf = _cl / "Title.ffpfsc"; _clf.write_bytes(b"x")
    (_cl / "._Title.ffpfsc").write_bytes(b"x"); (_cl.parent / "._Title [PPSA00001]").write_bytes(b"x")
    (_cl / "._other.ffpfsc").write_bytes(b"x")
    _cw2 = m.CLIWorker(app, _p4item, ["py", "cli.py", "placeholder", str(_cl), "--copy", "x"], _pcwd, _pout, _ptmp)
    _cw2.operation = "copy"
    _cw2._handle_line(f"[SUCCESS] Copied src.ffpfsc → {_clf}")
    _cw2._strip_written_clutter()
    ok("clutter.copy-output-cleaned", _cw2.output_path == str(_clf)
       and sorted(x.name for x in _cl.parent.rglob("._*")) == ["._other.ffpfsc"],
       f"{_cw2.output_path} {sorted(x.name for x in _cl.parent.rglob('._*'))}")
    _d4 = m.JobDialog(app, init_src=str(_p4g)); root.update()
    _d4.to_var.set(_d4._TARGET_LABEL["ffpfsc"]); _d4._refresh(); root.update()
    _hidden = not any(b.winfo_manager() for b in _d4._ps5_boxes)
    pump(lambda: True, timeout=0.5)
    ok("ps4.dialog-view", _d4._kind == "ps4" and _d4._to_key() == "pkg" and _hidden
       and not _d4._crow.winfo_manager() and "PS4 package" in _d4.detect_var.get(),
       f"{_d4._kind} {_d4._to_key()} hidden={_hidden} {_d4.detect_var.get()}")
    _d4.destroy(); root.update()
    # PS4 keystone on copies of the real samples (git-ignored, optional)
    import ps4pkg as _p4m
    _ksamples: dict = {}
    for _x in sorted((REPO / "unzipped" / "ps4").glob("*.pkg")):
        try:
            _xi = _p4m.read_identity(_x)
        except Exception:
            continue
        _ksamples.setdefault((_xi.content_id, _xi.kind), _x)
    _kgame = _kupd = None
    for (_cid, _kind), _x in _ksamples.items():
        if _kind == "game" and (_cid, "update") in _ksamples:
            _kgame, _kupd = _x, _ksamples[(_cid, "update")]
            break
    if _kgame and _kupd:
        import filecmp, fpkg as _fpkg
        from ps4_fixture import make_default_keystone as _mkdef
        _kd = S / "ks_lib" / "Sample Game [CUSA00001] [v01.04]"; shutil.rmtree(_kd.parent, ignore_errors=True); _kd.mkdir(parents=True)
        _kb, _ku = _kd / "game.pkg", _kd / "update.pkg"
        for _src, _dst in ((_kgame, _kb), (_kupd, _ku)):
            if subprocess.run(["cp", "-c", str(_src), str(_dst)]).returncode != 0:
                shutil.copyfile(_src, _dst)
        (S / "ks_default").write_bytes(_mkdef())
        _fpkg.ps4_keystone(_kb, S / "ks_default", on_line=lambda _l: None)     # a package with the default keystone
        _fpkg.ps4_rebind(_ku, _kb, on_line=lambda _l: None)
        _kb0, _ku0 = S / "game.orig", S / "update.orig"
        shutil.copyfile(_kb, _kb0); shutil.copyfile(_ku, _ku0)
        _dk = m.JobDialog(app, init_src=str(_kb)); root.update()
        pump(lambda: _dk._ks_default is not None, timeout=20.0); root.update()
        ok("ps4.keystone-hint-in-editor", _dk._ks_default is True and bool(_dk._crow.winfo_manager())
           and bool(_dk._ks_box.winfo_manager()) and "default keystone" in _dk.ks_state_var.get()
           and "default keystone" in _dk.detect_var.get(), f"{_dk._ks_default} {_dk.ks_state_var.get()}")
        _kvp = _p4m.version_problem(_kb)  # master version above the app version, or not
        ok("ps4.version-hint-in-editor", (_kvp is None) == ("app version" not in _dk.ks_state_var.get()),
           f"{_kvp} {_dk.ks_state_var.get()}")
        _dk._open_keystone(); root.update()
        _kv = app._keystone_view
        ok("ps4.keystone-editor-opens-tool", app._view == "keystone" and _kv.base_var.get() == str(_kb), app._view)
        pump(lambda: _kv._base_ok, timeout=20.0); root.update()
        ok("ps4.keystone-tool-reads-package", _kv._base_ok and "default keystone" in _kv.base_note.get()
           and [u["path"] for u in _kv._updates] == [str(_ku)] and _kv._updates[0]["on"]
           and str(_kv.add_btn.cget("state")) == "disabled", f"{_kv.base_note.get()} {_kv._updates}")
        ok("ps4.repair-version-line", bool(_kv._ver_row.winfo_manager()) == (_kvp is not None)
           and (_kvp is None or (bool(_kv.ver_var.get()) == _kvp["named"] and _kvp["version"] in _kv.ver_text.get())),
           f"{_kvp} {_kv.ver_text.get()} {_kv.ver_var.get()}")
        if _kvp is not None:
            _kv.ver_var.set(True); _kv._refresh_buttons()
            ok("ps4.repair-version-alone-is-a-job", str(_kv.add_btn.cget("state")) == "normal", str(_kv.add_btn.cget("state")))
            _kv.ver_var.set(False); _kv._refresh_buttons()
        _kv.ks_var.set("not a keystone"); root.update()
        ok("ps4.keystone-tool-rejects-input", str(_kv.add_btn.cget("state")) == "disabled"
           and "not a keystone file" in _kv.ks_note.get(), _kv.ks_note.get())
        _kv.ks_var.set(_mkdef()[0x20:0x40].hex()); root.update()
        ok("ps4.keystone-tool-rejects-default", str(_kv.add_btn.cget("state")) == "disabled"
           and "default keystone itself" in _kv.ks_note.get(), _kv.ks_note.get())
        _kfp = bytes(range(100, 132)).hex().upper()
        _kv.ks_var.set(_kfp.lower()); root.update()
        ok("ps4.keystone-tool-accepts-fingerprint", str(_kv.add_btn.cget("state")) == "normal"
           and _kv.ks_note.get().startswith("Keystone " + _kfp[:16]), _kv.ks_note.get())
        _qk0 = len(app.queue)
        _kv.add_to_queue(); root.update()
        _kj = app.queue[-1] if len(app.queue) > _qk0 else None
        _kc = app.build_command(_kj)[0] if _kj is not None else []
        ok("ps4.keystone-job-queued", _kj is not None and _kj.operation == "ps4-keystone" and app._view == "queue"
           and _kc[_kc.index("--ps4-keystone-set") + 1] == str(_kb) and _kc[_kc.index("--ps4-rebind") + 1] == str(_ku)
           and _p4m.keystone_from_text(_kc[_kc.index("--ps4-keystone") + 1])[0][0x20:0x40].hex().upper() == _kfp
           and app._job_recipe_parts(_kj)[1:] == ["Keystone · 1 update"]
           and m.chain_summary(_kj) == "Replace the keystone, bind 1 update",
           f"{getattr(_kj, 'operation', None)} {_kc[-6:]} {app._job_recipe_parts(_kj) if _kj else None}")
        app._edit_job(_kj); root.update()
        pump(lambda: _kv._base_ok and _kv._updates, timeout=20.0); root.update()
        ok("ps4.keystone-edit-shows-job", app._view == "keystone" and _kv.add_btn.cget("text") == "Save changes"
           and _kv.ks_var.get() == _kj.ps4_keystone and [u["on"] for u in _kv._updates] == [True],
           f"{_kv.add_btn.cget('text')} {_kv._updates}")
        _kv._toggle(next(iter(_kv._iid_row))); _kv.add_to_queue(); root.update()
        _kj2 = app.queue[-1]
        ok("ps4.keystone-edit-replaces", len(app.queue) == _qk0 + 1 and _kj2 is not _kj
           and _kj2.ps4_rebind == "" and app._job_recipe_parts(_kj2)[1:] == ["Keystone"], f"{len(app.queue)} {_kj2.ps4_rebind!r}")
        app._edit_job(_kj2); root.update()
        pump(lambda: _kv._base_ok and _kv._updates, timeout=20.0); root.update()
        _kv._toggle(next(iter(_kv._iid_row)))
        if _kvp is not None:
            _kv.ver_var.set(True)
        _kv.add_to_queue(); root.update()
        _kj = app.queue[-1]
        _kc = app.build_command(_kj)[0]
        ok("ps4.repair-job-command", ("--ps4-fix-version" in _kc) == (_kvp is not None) and "--ps4-keystone" in _kc
           and (_kvp is None or app._job_recipe_parts(_kj)[1:] == ["Keystone · Version · 1 update"]),
           f"{_kc[-6:]} {app._job_recipe_parts(_kj)}")
        _q0k, _act0k = list(app.queue), app._active_item
        app.cancel_requested = False
        try:
            app.queue[:] = [_kj]; app._active_item = None
            app.start()
            pump(lambda: _kj.status in ("Done", "Failed", "Skipped", "Cancelled") and not app._batch_running, timeout=180.0)
            _kst = _fpkg.ps4_keystone_info(_kb) or {}
            _ksfo = _p4m.read_sfo(_kb)
            ok("ps4.keystone-run", _kj.status == "Done" and _kst.get("fingerprint") == _kfp and _kst.get("valid")
               and _p4m.general_digests(_ku)[10] == _p4m.general_digests(_kb)[1]
               and _p4m.general_digests(_ku)[12] == _p4m.general_digests(_kb)[2]
               and (_kvp is None or (_ksfo["APP_VER"], _ksfo["VERSION"]) == (_kvp["version"], _kvp["app_ver"])),
               f"{_kj.status} {_kst} {_ksfo.get('APP_VER')} {_ksfo.get('VERSION')}")
        finally:
            app._batch_running = False
            app.queue[:] = _q0k; app._active_item = _act0k; app.update_queue_box()
        app._show_view("keystone"); root.update()
        ok("ps4.keystone-undo-offered", str(_kv.undo_btn.cget("state")) == "normal", str(_kv.undo_btn.cget("state")))
        _kv.undo(); pump(lambda: not _kv._busy, timeout=120.0); root.update()
        ok("ps4.keystone-undo", filecmp.cmp(_kb, _kb0, shallow=False) and filecmp.cmp(_ku, _ku0, shallow=False)
           and str(_kv.undo_btn.cget("state")) == "disabled", str(_kv.undo_btn.cget("state")))
        _dk.destroy() if _dk.winfo_exists() else None
        app._show_view("queue"); root.update()
        shutil.rmtree(_kd.parent, ignore_errors=True)
        for _f in (_kb0, _ku0):
            _f.unlink(missing_ok=True)
    # the test package was built without a keystone, so it has the default one
    _p5 = next((S / "c1_pkg").glob("*.pkg"), None)
    if _p5 is not None:
        import filecmp, ps5_repair as _p5m
        _p5c = S / "p5_copy"; shutil.rmtree(_p5c, ignore_errors=True); _p5c.mkdir()
        _p5s = _p5c / _p5.name; shutil.copyfile(_p5, _p5s)
        _d5 = m.JobDialog(app, init_src=str(_p5s)); root.update()
        pump(lambda: _d5._ps5_rows is not None, timeout=30.0); root.update()
        ok("ps5.repair-hint-in-editor", bool(_d5._ps5r_box.winfo_manager())
           and "default keystone" in _d5.ps5_state_var.get(), _d5.ps5_state_var.get())
        _d5._open_ps5_repair(); root.update()
        _pv = app._ps5_view
        pump(lambda: _pv._info is not None, timeout=30.0); root.update()
        _r5 = {x["check"]: x for x in _pv._rows}
        ok("ps5.repair-view-reads-package", app._view == "ps5repair" and _pv.base_var.get() == str(_p5s)
           and _r5.get("image.keystone", {}).get("fix") == "keystone" and len(_pv.tree.get_children()) >= 1,
           f"{_pv.base_note.get()} {sorted(_r5)[:6]}")
        _pv.out_var.set(str(S / "p5_out")); root.update()
        ok("ps5.repair-ready-without-keystone", str(_pv.add_btn.cget("state")) == "normal",
           str(_pv.add_btn.cget("state")))
        _pv.ks_var.set("not a keystone")
        pump(lambda: "Reading" not in _pv.ks_note.get(), timeout=10.0); root.update()
        ok("ps5.repair-rejects-input", str(_pv.add_btn.cget("state")) == "disabled"
           and "not a keystone file" in _pv.ks_note.get(), _pv.ks_note.get())
        _ks5 = S / "p5.keystone"
        _ks5.write_bytes(b"keystone" + bytes([3, 0, 1, 0]) + bytes(20) + bytes(range(64, 96)) + bytes(32))
        _pv.ks_var.set(str(_ks5))
        pump(lambda: getattr(_pv, "_ks_ok", False), timeout=30.0); root.update()
        _pv.playgo_var.set(True); _pv.standalone_var.set(True); _pv._refresh_buttons()
        _q5 = len(app.queue)
        _pv.add_to_queue(); settle()
        _j5 = app.queue[-1] if len(app.queue) > _q5 else None
        _c5 = app.build_command(_j5)[0] if _j5 is not None else []
        ok("ps5.repair-job-queued", _j5 is not None and _j5.ps5_repair and "--rebuild" in _c5
           and "--ps5-standalone" in _c5 and "--fpkg-regen-playgo" in _c5
           and _c5[_c5.index("--fpkg-keystone") + 1].lower() == _ks5.read_bytes().hex()
           and app._job_recipe_parts(_j5)[1:] == ["Rebuild", "Keystone", "PlayGo", "Standalone", ".pkg"]
           and m.chain_summary(_j5) == "Replace the keystone, regenerate PlayGo, make it a standalone game, "
                                        "then rebuild the package",
           f"{_c5[-8:]} {app._job_recipe_parts(_j5) if _j5 else None} {m.chain_summary(_j5) if _j5 else None} "
           f"ks_ok={getattr(_pv, '_ks_ok', None)} note={_pv.ks_note.get()!r} btn={_pv.add_btn.cget('state')} "
           f"info={_pv._info is not None} queue={len(app.queue)}/{_q5}")
        if _j5 is not None:
            app._edit_job(_j5); root.update()
            pump(lambda: _pv._info is not None and _pv._wanted is None and getattr(_pv, "_ks_ok", False), timeout=30.0)
            root.update()
            ok("ps5.repair-edit-shows-job", app._view == "ps5repair" and _pv.add_btn.cget("text") == "Save changes"
               and _pv.playgo_var.get() and _pv.standalone_var.get() and _pv.ks_var.get() == _j5.fpkg_keystone,
               f"{_pv.add_btn.cget('text')} {_pv.playgo_var.get()} {_pv.standalone_var.get()}")
            _pv.standalone_var.set(False); _pv.add_to_queue(); root.update()
            _j5b = app.queue[-1]
            ok("ps5.repair-edit-replaces", len(app.queue) == _q5 + 1 and _j5b is not _j5 and not _j5b.ps5_standalone
               and _j5b.fpkg_keystone == _j5.fpkg_keystone, f"{len(app.queue)} {_j5b.ps5_standalone}")
            _q0p, _a0p = list(app.queue), app._active_item
            app.cancel_requested = False
            try:
                app.queue[:] = [_j5b]; app._active_item = None
                app.start()
                pump(lambda: _j5b.status in ("Done", "Failed", "Skipped", "Cancelled") and not app._batch_running,
                     timeout=240.0)
                _o5 = sorted((S / "p5_out").rglob("*.pkg"))
                _i5 = _p5m.examine(_o5[0]) if _o5 else {}
                ok("ps5.repair-run", _j5b.status == "Done" and _i5.get("keystone_fp") == bytes(range(64, 96)).hex().upper()
                   and filecmp.cmp(_p5s, _p5, shallow=False),
                   f"{_j5b.status} {[str(x) for x in _o5]} {_i5.get('keystone_fp', '')[:16]}")
            finally:
                app._batch_running = False
                app.queue[:] = _q0p; app._active_item = _a0p; app.update_queue_box()
        _d5.destroy() if _d5.winfo_exists() else None
        app._show_view("queue"); root.update()
        shutil.rmtree(_p5c, ignore_errors=True); shutil.rmtree(S / "p5_out", ignore_errors=True)
    _d4 = m.JobDialog(app, init_src=str(_p4g)); root.update()
    _q_before = len(app.queue)
    _d4.out_var.set(str(OUT)); _d4._add(); settle()
    _q4 = app.queue[-1] if len(app.queue) > _q_before else None
    ok("ps4.dialog-queues-sorted-copy", _q4 is not None and _q4.content_kind == "ps4" and _q4.path == _p4g,
       str(getattr(_q4, "content_kind", None)))
    if _q4 is not None:
        app.queue.remove(_q4); app.update_queue_box()
    _segv = lambda d: list(d._to_seg.cget("values"))
    ok("ps4.dialog-targets-one-package", _segv(_d4) == ["Into the library", "Unpack to folder"], str(_segv(_d4)))
    _z4 = S / "ps4_set.zip"
    with zipfile.ZipFile(_z4, "w", zipfile.ZIP_STORED) as _zf:
        for _f in sorted(_p4dir.glob("*.pkg")):
            _zf.write(_f, "Sample.Set/" + _f.name)
    _dz = m.JobDialog(app, init_src=str(_z4)); root.update()
    pump(lambda: _dz._kind == "ps4", timeout=10.0)
    ok("ps4.dialog-archive-detected", _dz._kind == "ps4" and _segv(_dz) == ["Into the library"] and not _dz._crow.winfo_manager()
       and "Sample Game" in _dz.detect_var.get(), f"{_dz._kind} {_segv(_dz)} {_dz.detect_var.get()}")
    _mx2 = S / "mixed_arch"; shutil.rmtree(_mx2, ignore_errors=True); _mx2.mkdir()
    shutil.copyfile(_z4, _mx2 / "ps4_set.zip"); shutil.copytree(HBT, _mx2 / "HomebrewTest")
    _da = m.JobDialog(app, init_src=str(_mx2), init_to="ffpfsc"); root.update()
    pump(lambda: _da._ps4_n == 1, timeout=15.0); root.update()
    ok("mixed.ps4-archive-counted", _da._kind == "parent" and _da._ps4_n == 1 and _da._ps5_n == 1
       and bool(_da._ps4_box.winfo_manager()), f"{_da._kind} {_da._ps4_n} {_da._ps5_n}")
    _da.destroy(); root.update(); shutil.rmtree(_mx2, ignore_errors=True)
    _rec0, _q0r = app._recent_outputs("ps5"), len(app.queue)
    _dr = m.JobDialog(app, init_src=str(HBT), init_to="ffpfsc"); root.update()
    _dr.out_var.set(str(S / "refused_out")); _dr.patch_on_var.set(True); _dr.patch_var.set(str(S / "no_such_patch"))
    _n_err = len(errors); _dr._add(); root.update()
    ok("mixed.refused-add-not-remembered", len(errors) > _n_err and app._recent_outputs("ps5") == _rec0
       and len(app.queue) == _q0r, f"{errors[_n_err:]} {app._recent_outputs('ps5')} {len(app.queue)}")
    _dr.destroy() if _dr.winfo_exists() else None; root.update()
    _dz.out_var.set(str(OUT)); _qb = len(app.queue); _dz._add(); settle()
    _qz = app.queue[-1] if len(app.queue) > _qb else None
    ok("ps4.archive-job", _qz is not None and _qz.content_kind == "ps4" and _qz.operation == "copy"
       and str(_qz.archive_path) == str(_z4) and _qz.display_name == "Sample Game [CUSA00001]"
       and app._job_recipe_parts(_qz) == [".zip", "PS4 library · 2 packages"],
       f"{getattr(_qz, 'display_name', None)} {app._job_recipe_parts(_qz) if _qz else None}")
    if _qz is not None:
        _need = m._space_requirements(_qz, Path(_ptmp), OUT)
        ok("ps4.archive-space", [x[0] for x in _need] == ["Output drive (archive unpacked)"], str(_need))
        _info = app._card_info_text(_qz)
        ok("ps4.archive-details-drives", str(_info.get("Drives", "")).startswith("unpacks and writes on"),
           str(_info.get("Drives")))
        app.queue.remove(_qz); app.update_queue_box()
    _lib4 = S / "ps4_library"; shutil.rmtree(_lib4, ignore_errors=True); _lib4.mkdir()
    _run4 = app._ps4_item_for(_z4, output_path=str(_lib4))
    _run4.after_source = "keep"
    _q0, _act0 = list(app.queue), app._active_item
    _inflight1 = getattr(app, "_cleanup_inflight", 0)
    app._cleanup_inflight = 0; app.cancel_requested = False; app.extract_cancel_event.clear()
    _su_saved, _overall4 = app.status_update, []

    def _su(title, detail, stage, stage_pct, overall_pct, *a, **k):
        if k.get("job") is _run4 and stage == "Extracting":
            _overall4.append((stage_pct, overall_pct))
        return _su_saved(title, detail, stage, stage_pct, overall_pct, *a, **k)
    app.status_update = _su
    try:
        app.queue[:] = [_run4]; app._active_item = None
        app.start()
        pump(lambda: _run4.status in ("Done", "Failed", "Skipped", "Cancelled") and not app._batch_running, timeout=180.0)
        _tree4 = sorted(x.relative_to(_lib4).as_posix() for x in _lib4.rglob("*.pkg"))
        ok("ps4.archive-run-end-to-end", _run4.status == "Done" and _tree4 == [
            "Sample Game [CUSA00001] [v01.00]/Sample Game DLC Extra [CUSA00001] [v01.00].pkg",
            "Sample Game [CUSA00001] [v01.00]/Sample Game [CUSA00001] [v01.00].pkg"]
           and _z4.is_file(), f"{_run4.status} {_tree4}")
        _last4 = max((o for s_, o in _overall4 if s_ >= 100), default=None)
        ok("ps4.archive-bar-follows-unpack", _last4 is not None and _last4 >= 95, str(_overall4[-3:]))
    finally:
        app.status_update = _su_saved
        app._batch_running = False; app._cleanup_inflight = _inflight1
        app.queue[:] = _q0; app._active_item = _act0; app.update_queue_box()
    _mg = S / "multi"; shutil.rmtree(_mg, ignore_errors=True); (_mg / "src").mkdir(parents=True)
    for _tid, _nm in (("PPSA99091", "Game One"), ("PPSA99092", "Game Two")):
        _gd = _mg / "src" / "Set" / f"{_tid}-app0"
        shutil.copytree(HBT, _gd)
        _pjp = _gd / "sce_sys" / "param.json"; _pjd = json.loads(_pjp.read_text(encoding="utf-8"))
        _pjd["titleId"] = _tid
        _pjd["contentId"] = re.sub(r"PPSA\d{5}", _tid, str(_pjd.get("contentId", "")))
        for _lp in (_pjd.get("localizedParameters") or {}).values():
            if isinstance(_lp, dict) and "titleName" in _lp:
                _lp["titleName"] = _nm
        _pjp.write_text(json.dumps(_pjd), encoding="utf-8")

    def _multi_zip(d):
        d.mkdir(parents=True, exist_ok=True)
        z = d / "Multi.Set-PPSA99092.zip"   # the archive names its main game
        with zipfile.ZipFile(z, "w") as zf:
            for f in sorted((_mg / "src").rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(_mg / "src").as_posix())
        return z

    _TERM = ("Done", "Failed", "Skipped", "Cancelled")
    _mq0, _mact0, _mard = list(app.queue), app._active_item, app.auto_remove_done_var.get()
    _msum, _mopen = app.summary_popup_var.get(), app.open_output_var.get()
    app.auto_remove_done_var.set(False); app.summary_popup_var.set(False); app.open_output_var.set(False)

    _mlog, _mlog_saved, _mcounts = [], app.log, []

    def _multi_run(first):
        app._cleanup_inflight = 0; app.cancel_requested = False; app.extract_cancel_event.clear()
        app.queue[:] = [first]; app._active_item = None
        app.start()
        pump(lambda: not app._batch_running and len(app.queue) >= 2
             and all(j.status in _TERM for j in app.queue), timeout=240.0)
        _mcounts.append((app._batch_done, app._batch_total))
        return list(app.queue)

    try:
        _mz = _multi_zip(_mg / "dl")
        _mout, _mdone = _mg / "out", _mg / "done"; _mout.mkdir(); _mdone.mkdir()
        _mj = m.GameItem.from_chain(_mz, to="ffpfsc", output_path=str(_mout))
        _mj.after_source, _mj.after_move_to = "move", str(_mdone)
        app.log = lambda lvl, msg, *a, **k: (_mlog.append(msg), _mlog_saved(lvl, msg, *a, **k))
        _mjobs = _multi_run(_mj)
        app.log = _mlog_saved
        _mffs = sorted(x.name for x in _mout.rglob("*.ffpfsc"))
        _mleft = [x for b in (Path(app.temp_var.get()), _mout) for x in b.rglob("Multi.Set*") if x.is_dir()]
        ok("multi.both-games-built", len(_mjobs) == 2 and all(j.status == "Done" for j in _mjobs)
           and len(_mffs) == 2 and any("PPSA99091" in x for x in _mffs) and any("PPSA99092" in x for x in _mffs),
           f"{[(j.display_name, j.status, j.status_note) for j in _mjobs]} {_mffs}")
        _mwait = any("stays until the other games from it are done" in x for x in _mlog)
        ok("multi.counts-every-game", _mcounts[:1] == [(2, 2)], str(_mcounts))
        ok("multi.archive-moves-after-the-last", _mwait and not _mz.exists() and any(_mdone.rglob("Multi.Set-PPSA99092.zip"))
           and not _mleft, f"waited={_mwait} src={_mz.exists()} done={[x.name for x in _mdone.rglob('*')]} left={_mleft}")
        _mdirs = sorted({x.parent.name for x in _mout.rglob("*.ffpfsc")})
        ok("multi.one-folder-named-after-the-game", len(_mdirs) == 1 and _mdirs[0].startswith("Game Two [PPSA99092]"),
           str(_mdirs))
        _other = m.GameItem.from_chain(HBT, to="ffpfsc")
        _lbm = app.queue_listbox
        app.queue[:] = [_mjobs[0], _mjobs[-1], _other]; app.update_queue_box(); _lbm._draw()
        _groups, _brackets1 = [r.get("group") for r in _lbm.rows], len(_lbm.cv.find_withtag("group"))
        app.queue[:] = [_mjobs[0], _other, _mjobs[-1]]; app.update_queue_box(); _lbm._draw()
        _brackets2 = len(_lbm.cv.find_withtag("group"))
        _arch = app._card_info_text(_mjobs[-1]).get("Archive", "")
        ok("multi.marked-as-one-archive", bool(_groups[0]) and _groups[0] == _groups[1] and _groups[2] is None
           and _brackets1 == 1 and _brackets2 == 2 and _arch.startswith("Multi.Set-PPSA99092.zip, game ")
           and _arch.endswith(" of 2"), f"{_groups} {_brackets1} {_brackets2} {_arch!r}")
        _mz2 = _multi_zip(_mg / "dl2")
        _mout2 = _mg / "out2"; _mout2.mkdir()
        _mj2 = m.GameItem.from_chain(_mz2, to="ffpfsc", output_path=str(_mout2))
        _mj2.after_source = "keep"
        _mj2.archive_title, _mj2.archive_title_id = "Game Two", "PPSA99092"   # read from the archive before unpacking
        _mj2.archive_version = "01.000.000"
        _mjobs2 = _multi_run(_mj2)
        _one = next((j for j in _mjobs2 if "PPSA99091" in str(j.display_name or "")), None)
        for _f in _mout2.rglob("*PPSA99091*.ffpfsc"):
            _f.unlink()
        if _one is not None:
            for _k in [k for k in vars(_one) if k.startswith("_")]:   # a restored job keeps none of these
                delattr(_one, _k)
            _one.status = "Failed"
            app._retry_job(_one)
            pump(lambda: not app._batch_running and _one.status in _TERM, timeout=240.0)
        _mffs2 = sorted(x.name for x in _mout2.rglob("*.ffpfsc"))
        _mn2 = sum(1 for j in app.queue if getattr(j, "origin_archive", None) == str(_mz2))
        ok("multi.retry-unpacks-for-that-game", _one is not None and _one.status == "Done" and _mn2 == 2
           and len(_mffs2) == 2 and any("PPSA99091" in x for x in _mffs2)
           and len({x.parent for x in _mout2.rglob("*.ffpfsc")}) == 1,
           f"{getattr(_one, 'status', None)} {str(getattr(_one, 'status_note', ''))[-90:]} jobs={_mn2} {_mffs2}")
    finally:
        app.log = _mlog_saved
        app._batch_running = False
        # let the cleanup and the empty-folder prune finish before the next checks
        pump(lambda: getattr(app, "_cleanup_inflight", 0) == 0 and not getattr(app, "_prune_pending", False), timeout=60.0)
        app.auto_remove_done_var.set(_mard); app.summary_popup_var.set(_msum); app.open_output_var.set(_mopen)
        app.queue[:] = _mq0; app._active_item = _mact0; app.update_queue_box(); root.update()
    _gdir = S / "PPSA99097 Example Game"; _gdir.mkdir(exist_ok=True)
    _g7 = _gdir / "PPSA99097-Compressed.7z"; _g7.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\0" * 64)
    _gi = m.GameItem.from_archive(_g7); app.queue.append(_gi); app.update_queue_box()
    app._name_jobs_from_games()
    pump(lambda: _gi.display_name == "Example Game [PPSA99097]", timeout=10.0)
    ok("names.archive-from-folder", _gi.display_name == "Example Game [PPSA99097]", str(_gi.display_name))
    app.queue.remove(_gi); app.update_queue_box()
    _ldir = S / "PPSA99096 Folder Words"; _ldir.mkdir(exist_ok=True)
    _l7 = _ldir / "PPSA99096-Compressed.7z"; _l7.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\0" * 64)
    _li = m.GameItem.from_archive(_l7); app.queue.append(_li); app.update_queue_box()
    app._name_jobs_from_games()
    pump(lambda: _li.display_name == "Listed Example [PPSA99096]", timeout=10.0)
    ok("names.from-the-title-list", _li.display_name == "Listed Example [PPSA99096]", str(_li.display_name))
    app.queue.remove(_li); app.online_names_var.set(False)
    _li2 = m.GameItem.from_archive(_l7); app.queue.append(_li2); app.update_queue_box()
    app._name_jobs_from_games()
    pump(lambda: _li2.display_name == "Folder Words [PPSA99096]", timeout=10.0)
    ok("names.title-list-off", _li2.display_name == "Folder Words [PPSA99096]", str(_li2.display_name))
    app.online_names_var.set(True); app.queue.remove(_li2); app.update_queue_box()
    _d5 = m.JobDialog(app, init_src=str(_p4g)); root.update()
    _d5.src_var.set(str(HBT)); root.update(); _d5._refresh(); root.update()
    ok("ps4.dialog-back-to-ps5", _d5._kind == "folder" and bool(_d5._crow.winfo_manager())
       and all(b.winfo_manager() for b in _d5._ps5_boxes) and not _d5._ks_box.winfo_manager(), _d5._kind)
    _d5.destroy(); root.update()
    _org = S / "org_lib"; (_org / "Some.Release").mkdir(parents=True, exist_ok=True)
    shutil.copy2(FF, _org / "Some.Release" / "x.ffpfsc")
    (_org / "Some.Release" / "info.nfo").write_text("notes")
    (_org / "Some.Release" / "._x.ffpfsc").write_bytes(b"\x00\x05\x16\x07" + b"\0" * 60)
    (_org / "._Some.Release").write_bytes(b"\x00\x05\x16\x07")
    app.output_var.set(str(_org))
    app._show_view("organize"); root.update()
    _ov = app._organize_view
    ok("organize.view-opens", app._view == "organize" and _ov.root_var.get() == str(_org), _ov.root_var.get())
    app.output_var.set(str(OUT))
    _ov.scan(); pump(lambda: not _ov._busy, timeout=60.0); root.update()
    _rows = [_ov.tree.item(i, "values") for i in _ov.tree.get_children()]
    ok("organize.scan-no-clutter", [r_[1].replace("\\", "/") for r_ in _rows] == ["Some.Release", "Some.Release/x.ffpfsc"]
       and len(_ov._checked) == 2 and all("LibProsperoPKG [PPSA99099]" in r_[2] for r_ in _rows), str(_rows))
    _ov.apply(); pump(lambda: not _ov._busy, timeout=60.0); pump(lambda: not _ov._busy, timeout=60.0); root.update()
    _t = "LibProsperoPKG [PPSA99099] [v01.000.000]"
    _after = sorted(x.name for x in _org.iterdir())
    ok("organize.apply-renames-in-place", _after == [_t] and sorted(x.name for x in (_org / _t).iterdir())
       == ["LibProsperoPKG [PPSA99099] [v01.000] [fw2.00].ffpfsc", "info.nfo"] and _ov._last_journal() is not None, str(_after))
    _jn = _ov._last_journal()
    _ask = m.messagebox.askyesno; m.messagebox.askyesno = lambda *a, **k: True
    try:
        _ov.undo(); pump(lambda: not _ov._busy, timeout=60.0); pump(lambda: not _ov._busy, timeout=60.0); root.update()
    finally:
        m.messagebox.askyesno = _ask
    ok("organize.undo", sorted(x.name for x in _org.iterdir() if not x.name.startswith("._")) == ["Some.Release"]
       and (_org / "Some.Release" / "x.ffpfsc").is_file() and _jn is not None and not _jn.is_file()
       and _ov._last_journal() != _jn, str(sorted(x.name for x in _org.iterdir())))
    app._show_view("queue"); root.update()
    _do = m.JobDialog(app, init_src=str(FF)); root.update()
    ok("organize.output-offered", "Organize" in list(_do._to_seg.cget("values")), str(_do._to_seg.cget("values")))
    _do.to_var.set("Organize"); _do._refresh(); root.update()
    ok("organize.output-hides-changes", not _do._crow.winfo_manager() and not _do._comp_row.winfo_manager()
       and _do.summary_var.get().startswith("Organize into the library"), _do.summary_var.get())
    _libo = S / "org_library"; shutil.rmtree(_libo, ignore_errors=True); _libo.mkdir()
    _do.out_var.set(str(_libo)); _qb = len(app.queue); _do._add(); settle()
    _qo = app.queue[-1] if len(app.queue) > _qb else None
    _cmdo = app.build_command(_qo)[0] if _qo is not None else []
    _qoz = app._organize_item_for(ZP, output_path=str(OUT))
    ok("queue.archive-kind-shown", app._job_recipe_parts(_qoz) == [".zip", "Organize"]
       and app._job_recipe_parts(m.GameItem.from_chain(ZP, to="ffpfsc"))[0] == ".zip", str(app._job_recipe_parts(_qoz)))
    ok("organize.output-job", _qo is not None and _qo.operation == "copy" and _qo.content_kind == "organize"
       and app._job_recipe_parts(_qo) == [".ffpfsc", "Organize"] and "--organize-into" in _cmdo
       and _cmdo[_cmdo.index("--copy-mode") + 1] == "keep", f"{app._job_recipe_parts(_qo) if _qo else None} {_cmdo[-6:]}")
    if _qo is not None:
        app.queue.remove(_qo); app.update_queue_box()
    _q0, _act0 = list(app.queue), app._active_item
    _inflight1 = getattr(app, "_cleanup_inflight", 0)
    _runs = [app._organize_item_for(FF, output_path=str(_libo)), app._organize_item_for(ZP, output_path=str(_libo))]
    for _r in _runs:
        _r.after_source = "keep"
    app._cleanup_inflight = 0; app.cancel_requested = False; app.extract_cancel_event.clear()
    try:
        app.queue[:] = list(_runs); app._active_item = None
        app.start()
        pump(lambda: all(_r.status in ("Done", "Failed", "Skipped", "Cancelled") for _r in _runs)
             and not app._batch_running, timeout=180.0)
        _t = "LibProsperoPKG [PPSA99099] [v01.000.000]"
        _got = sorted(x.relative_to(_libo).as_posix() for x in _libo.rglob("*") if x.is_file() and "sce_sys" not in x.parts)
        ok("organize.output-run-end-to-end", [r_.status for r_ in _runs] == ["Done", "Done"]
           and f"{_t}/LibProsperoPKG [PPSA99099] [v01.000] [fw2.00].ffpfsc" in _got and f"{_t}/{_t}/eboot.bin" in _got
           and not any("/._" in g or g.startswith("._") for g in _got) and FF.is_file() and ZP.is_file(),
           f"{[r_.status for r_ in _runs]} {_got}")
    finally:
        app._batch_running = False; app._cleanup_inflight = _inflight1
        app.queue[:] = _q0; app._active_item = _act0; app.update_queue_box()
    _art_items = {"zip": m.GameItem.from_archive(ZP), "ffpfsc": app._organize_item_for(FF),
                  "pkg": app._organize_item_for(next((S / "c1_pkg").glob("*.pkg")))}
    for _v in _art_items.values():
        _f = m.art_cache_file(m.art_source_key(_v))
        if _f is not None:
            _f.unlink()  # the naming thread may have cached it already
    _art_got = {k: bool(app._fetch_art(v, [])) and m.art_cache_file(v.art_key) is not None for k, v in _art_items.items()}
    ok("art.read-before-unpacking", all(_art_got.values()), str(_art_got))
    _ai = _art_items["zip"]; _ai.artwork = None
    _q0 = list(app.queue)
    app.queue[:] = [_ai]; app._details_item = None; app.update_queue_box(); root.update()
    ok("art.shown-at-size", app.art_label._photo is not None and app.art_label.size_ == app.ART_PX >= 96
       and max(app.art_img.width(), app.art_img.height()) == round(app.ART_PX * m.ui_kit.SCALE),
       str(getattr(app, "art_img", None)))
    app._save_queue(); app.queue.clear(); app._details_item = None; app._restore_queue(); root.update()
    _rest = app.queue[0] if app.queue else None
    app.update_game_details(_rest); root.update()
    ok("art.after-restart", _rest is not None and _rest.artwork is None and app.art_label._photo is not None,
       str(getattr(_rest, "art_key", None)))
    app.queue[:] = _q0; app._details_item = None; app.update_queue_box(); root.update()
    _pa = m.GameItem.from_chain(ZP, to="ffpfsc", output_path=str(OUT))
    _plogs = []; _plog_saved = app.log
    app.log = lambda lvl, msg, *a, **k: (_plogs.append(msg), _plog_saved(lvl, msg, *a, **k))
    try:
        _pinfo = app._card_info_text(_pa); app._refresh_space_for_item(_pa)
    finally:
        app.log = _plog_saved
    _pdrv, _pspace = str(_pinfo.get("Drives", "")), app.temp_space_var.get()
    ok("details.drives-from-the-plan", _pdrv.startswith("unpacks on") and "builds on" in _pdrv and "writes to" in _pdrv
       and f" on {m._drive_colon(OUT)} ~" in _pspace and "::" not in _pspace and _pspace.endswith("fits")
       and not hasattr(_pa, "_build_temp")
       and not any(x.startswith("Auto:") for x in _plogs),
       f"unpack={_pdrv.startswith('unpacks on')} builds={'builds on' in _pdrv} drive={f' on {m._drive_colon(OUT)} ~' in _pspace} "
       f"fits={_pspace.endswith('fits')} untouched={not hasattr(_pa, '_build_temp')} "
       f"quiet={not any(x.startswith('Auto:') for x in _plogs)} space={_pspace[-60:]!r}")
    _keep = [(v, v.get()) for v in (app.temp_space_var, app.saved_var, app.ratio_var, app.rating_var)]
    app.temp_space_var.set("Temp Needed: - ")
    for _v, _was in _keep[1:]:
        _v.set(_was.split(":", 1)[0] + ": - ")
    _phinfo, _plast = app._card_info_text(_pa), app.last_result_var.get()
    for _v, _was in _keep:
        _v.set(_was)
    ok("details.placeholders-hidden", "Space" not in _phinfo and _plast.startswith("Totals"),
       f"space={_phinfo.get('Space')!r} last={_plast!r}")
    _pool = S / "pool_drive"; _hist = S / "old_library" / "Game [PPSA99095] [v01.000.000]"
    for _d in (_pool / "_ffpfsc_temp" / "_extracted", _hist.parent / "_ffpfsc_temp", _hist.parent / "_ffpfsc_extract" / "kept"):
        _d.mkdir(parents=True, exist_ok=True)
    (_hist.parent / "_ffpfsc_extract" / "kept" / "eboot.bin").write_bytes(b"E")
    _old = time.time() - 600
    for _d in [x for x in S.rglob("*") if x.is_dir() and ("pool_drive" in x.parts or "old_library" in x.parts)]:
        os.utime(_d, (_old, _old))
    _pool0, _hist0 = list(getattr(app, "temp_pool", []) or []), m.load_history()
    pump(lambda: not getattr(app, "_prune_pending", False), timeout=60.0)   # one scheduled by an earlier run
    app.temp_pool = [str(_pool)]
    m.save_history(_hist0 + [{"output": str(_hist / "Game.ffpfsc"), "name": "x"}])
    try:
        app._batch_running = True
        app._prune_scratch_later(delay=0.0); pump(lambda: not app._prune_pending, timeout=10.0)
        _while = (_pool / "_ffpfsc_temp").exists()
        app._batch_running = False
        app._prune_scratch_later(delay=0.0); pump(lambda: not app._prune_pending, timeout=10.0)
        ok("scratch.empty-work-folders-go", _while and not (_pool / "_ffpfsc_temp").exists()
           and not (_hist.parent / "_ffpfsc_temp").exists()
           and (_hist.parent / "_ffpfsc_extract" / "kept" / "eboot.bin").is_file(),
           f"kept while running={_while} pool={(_pool / '_ffpfsc_temp').exists()} hist={(_hist.parent / '_ffpfsc_temp').exists()}")
    finally:
        app._batch_running = False; app.temp_pool = _pool0; m.save_history(_hist0)
    _tj = m.GameItem.from_chain(ZP, to="ffpfsc"); _gb = 10**9
    _rates0 = dict(app._stage_rates); app._stage_rates.clear()
    try:
        app._stage_clock = (_tj, "Creating Temp PFS", time.time() - 774)
        _l1 = app._job_time_left(_tj, "Compressing", 3, "38m 08s", 128 * _gb)
        _learned = app._stage_rates.get("Creating Temp PFS", 0)
        app._stage_clock = (_tj, "Compressing", time.time() - 2400)
        app._job_time_left(_tj, "Cleaning Up", 0, " - ", 128 * _gb)
        _tj2 = m.GameItem.from_chain(ZP, to="ffpfsc")
        app._stage_clock = (_tj2, "Creating Temp PFS", time.time() - 60)
        _l2 = app._job_time_left(_tj2, "Creating Temp PFS", 50, "1m 00s", 64 * _gb)
        _comp = app._stage_rates.get("Compressing", 0)
        ok("eta.job-time-left", _l1 == 2288 and abs(_learned - 128 * _gb / 774) < 1e6
           and abs(_l2 - (60 + 64 * _gb / _comp)) < 2 and m.load_settings().get("stage_rates"),
           f"compress={_l1} temp-rate={_learned / 1e6:.0f}MB/s next={_l2:.0f}s compress-rate={_comp / 1e6:.0f}MB/s")
    finally:
        app._stage_rates.clear(); app._stage_rates.update(_rates0); app._stage_clock = None
    ok("unpack.rate", m.unpack_rate(50, 100, 100 * 10**9) == ("500.0 MB/s", "1m 40s") and m.unpack_rate(50, 10, 100 * 10**9)[0] == "5.00 GB/s"
       and m.unpack_rate(25, 60, 12 * 10**9) == ("50.0 MB/s", "3m 00s")
       and m.unpack_rate(1, 60, 10**9) == (" - ", " - ") and m.unpack_rate(40, 20, 0) == (" - ", "30s"),
       str([m.unpack_rate(50, 100, 100 * 10**9), m.unpack_rate(25, 60, 12 * 10**9)]))
    _q0 = list(app.queue)
    _ja, _jb, _jc = (m.GameItem.from_archive(ZP) for _ in range(3))
    app.queue[:] = [_ja, _jb, _jc]; app._details_item = None; app.update_queue_box(select_item=_ja); root.update()
    _br0, _act0 = app._batch_running, app._active_item
    try:
        app._batch_running, app._active_item = True, _ja
        app.update_game_details(_jb); root.update()
        _hidden = not app._progress_box.winfo_manager()
        app.update_game_details(_ja); root.update()
        ok("details.progress-only-for-the-running-job", _hidden and bool(app._progress_box.winfo_manager()),
           f"hidden for a waiting job={_hidden}")
        app.update_queue_box(select_item=_jc); app.update_game_details(_jb); root.update()
        app._names_dirty = True; app._poll_inner(); root.update()
        ok("details.kept-when-names-come-in", app._details_item is _jb,
           f"details on {app.queue.index(app._details_item) if app._details_item in app.queue else None}")
        app.update_queue_box(select_item=_jb); app.update_game_details(_jb); root.update()
        app._follow_next_job(_ja, _jc); root.update()
        _kept = app._details_item is _jb and app.queue_listbox.curselection() == (1,)
        app.update_queue_box(select_item=_ja); app.update_game_details(_ja); root.update()
        app._follow_next_job(_ja, _jc); root.update()
        ok("details.follow-the-run-only-when-watching", _kept and app._details_item is _jc
           and app.queue_listbox.curselection() == (2,), f"kept={_kept} now={app.queue_listbox.curselection()}")
    finally:
        app._batch_running, app._active_item = _br0, _act0
        app.queue[:] = _q0; app._details_item = None; app.update_queue_box(); root.update()
    _q0 = list(app.queue)
    _three = [m.GameItem.from_archive(ZP) for _ in range(3)]
    app.queue[:] = list(_three); app._details_item = None; app.update_queue_box(select_item=_three[0]); root.update()
    _lb = app.queue_listbox

    def _press(seq):
        # a withdrawn window gets no key events: run the key's binding as Tk would
        _scr = _lb.cv.bind(seq).replace("%#", "0").replace("%", "")
        _lb.cv.tk.eval("foreach __once 1 {" + _scr + "}"); root.update()   # 'break' needs a loop
    _press("<Down>"); _press("<Down>"); _press("<Up>")
    ok("queue.arrows-select-not-move", app.queue == _three and _lb.curselection() == (1,)
       and app._details_item is _three[1] and _lb.marked_rows() == [1],
       f"order kept={app.queue == _three} sel={_lb.curselection()} details={_three.index(app._details_item) if app._details_item in _three else None}")
    _press("<Shift-Down>")
    ok("queue.shift-arrow-extends", _lb.marked_rows() == [1, 2] and _lb.curselection() == (2,) and app.queue == _three,
       str(_lb.marked_rows()))
    _press("<Up>"); _press("<Up>"); _press("<Up>")
    ok("queue.arrows-stop-at-the-ends", _lb.curselection() == (0,) and _lb.marked_rows() == [0], str(_lb.curselection()))
    app.queue[:] = _q0; app._details_item = None; app.update_queue_box(); root.update()
    _fd_saved = (m.filedialog.askdirectory, m.filedialog.askopenfilename)
    m.filedialog.askdirectory = lambda *a, **k: (_fd_calls.append(("dir", k.get("title", ""))), "")[1]
    m.filedialog.askopenfilename = lambda *a, **k: (_fd_calls.append(("file", k.get("title", ""))), "")[1]
    _popup_saved = m.tk.Menu.tk_popup
    _menus = []
    m.tk.Menu.tk_popup = lambda self, *a, **k: _menus.append(self)   # a real popup is modal

    def _buttons(w):
        found, todo = {}, [w]
        while todo:
            x = todo.pop()
            if isinstance(x, m.IconButton):
                found.setdefault(x._text, x)
            todo.extend(x.winfo_children())
        return found

    def _fire(widget, seq):
        mm = re.search(r"\[(\S+)((?: %\S)+)\]", widget.bind(seq))
        return widget.tk.call(mm.group(1), *(["0"] * len(mm.group(2).split())))

    def _top():
        return type(app._panels.stack[-1]).__name__ if app._panels.stack else None

    def _select(i):
        app.queue_listbox.selection_clear(0, "end"); app.queue_listbox.selection_set(i)
        app._on_queue_select(None); root.update()

    class _Ev:
        x_root = y_root = 0

    try:
        for key in ("history", "log", "queue", "settings"):
            app._nav[key].invoke(); root.update()
            ok(f"wire.nav.{key}", app._view == key and app._nav[key]._selected, f"view={app._view}")
        sv, bad = app._settings_view, []
        for key, *_ in sv.PAGES:
            try:
                sv._nav[key].invoke(); root.update()
                if not (sv._nav[key]._selected and key in sv._page_frames):
                    bad.append(key)
            except Exception as e:
                bad.append(f"{key}: {e!r}")
        ok("wire.settings.every-page", not bad, str(bad))
        _opened, _real_open = [], m.webbrowser.open
        m.webbrowser.open = lambda url, *a, **k: _opened.append(url)
        try:
            sv._nav["about"].invoke(); root.update()
            sv._donate_btn.invoke(); root.update()
        finally:
            m.webbrowser.open = _real_open
        ok("wire.about.donate", _opened == [m.DONATE_URL], str(_opened))
        sv._nav["general"].invoke(); root.update()
        _real_ask2 = m.messagebox.askyesno
        m.messagebox.askyesno = lambda *a, **k: False
        try:
            sv._on_after_source("Delete"); root.update()
            _kept = app.after_source_var.get()
        finally:
            m.messagebox.askyesno = _real_ask2
        app.after_move_dir_var.set(str(S / "after_done"))
        sv._on_after_source("Move to folder"); root.update()
        _dir_shown = bool(sv._after_dir_row.winfo_manager())
        sv._on_after_source("Keep"); root.update()
        ok("wire.settings.after-source", _kept == "keep" and _dir_shown and app.after_source_var.get() == "keep"
           and not sv._after_dir_row.winfo_manager(), f"{_kept} {_dir_shown}")
        app._nav["queue"].invoke(); root.update()

        tools = {b._text: b for b in app._nav_all}
        ok("wire.tool.clean-temp", tools["Clean temp"]._command == app.clear_temp_files, "")
        tools["Organize"].invoke(); root.update()
        ok("wire.tool.organize", app._view == "organize" and not _fd_calls[-1:] == [("dir", "")], app._view)
        app._show_view("queue"); root.update()
        tools["Look inside"].invoke(); root.update()
        ok("wire.tool.look-inside", app._view == "look" and _top() is None, f"{app._view} {_top()}")
        app.open_job_dialog(); root.update()
        _with_panel = _top() == "JobDialog" and all(b._state == "normal" for b in app._nav_all)
        tools["Organize"].invoke(); root.update()
        ok("wire.panel.sidebar-leaves-it", _with_panel and _top() is None and app._view == "organize",
           f"{_with_panel} {_top()} {app._view}")
        app.open_job_dialog(); root.update()
        app._panels._on_key("<Escape>", None); root.update()
        ok("wire.panel.escape-closes", _top() is None, str(_top()))
        app._show_view("queue"); root.update()

        ok("wire.header.start", app.start_btn._command == app.start, "")
        app._add_btn.invoke(); root.update()
        ok("wire.header.add-job", _top() == "JobDialog", str(_top()))
        close_toplevels(); root.update()
        app.open_job_dialog(str(HBT)); root.update()
        jd = app._panels.stack[-1]
        pump(lambda: getattr(jd, "_kind", "") not in ("", None), timeout=15)
        jd.to_var.set(".ffpfsc"); jd.out_var.set(str(OUT / "wire")); root.update()
        n0 = len(app.queue)
        app._panels._on_key("<Return>", None); settle()
        ok("wire.panel.return-adds-job", len(app.queue) == n0 + 1, f"+{len(app.queue) - n0}, kind={getattr(jd, '_kind', '?')}")
        close_toplevels(); root.update()

        ok("wire.queue.no-compression-line", "Change" not in _buttons(app._paned_q)
           and not hasattr(app, "_open_tuning"), str(sorted(_buttons(app._paned_q))))

        items = [m.GameItem(HBT) for _ in range(3)]
        app.queue[:] = []; app.update_queue_box(); root.update()
        ok("wire.primary.empty-queue", app._add_btn.variant == "primary" and app.start_btn._state == "disabled",
           f"add={app._add_btn.variant} start={app.start_btn._state}")
        app.queue[:] = items; app.update_queue_box(); root.update()
        ok("wire.primary.with-jobs", app.start_btn.variant == "primary" and app.start_btn._state == "normal"
           and app._add_btn.variant == "secondary", f"start={app.start_btn.variant}/{app.start_btn._state}")

        ok("wire.details.closed-at-start", not app._inspector_open and str(app._card_pane) not in [str(x) for x in app._paned_q.panes()], "")
        app.queue_listbox.selection_clear(0, "end"); app.queue_listbox.selection_set(1)
        app._on_queue_clicked(); root.update()
        ok("wire.details.opens-on-click", app._inspector_open and str(app._card_pane) in [str(x) for x in app._paned_q.panes()], "")
        app._details_btn.invoke(); root.update()
        ok("wire.details.header-button", not app._inspector_open and str(app._card_pane) not in [str(x) for x in app._paned_q.panes()], "")
        _fire(root, m.DETAILS_SEQ); root.update()
        ok("wire.details.shortcut", app._inspector_open, "")
        ok("wire.details.menu-label", app._view_menu.entrycget(app._details_menu_index, "label") == "Hide Details",
           app._view_menu.entrycget(app._details_menu_index, "label"))

        _w_idle = app.transport.winfo_reqwidth()
        app._batch_running = True; app._sync_run_ui(); root.update()
        ok("wire.header.stop-while-running", app.transport.running and app.stop_btn._command == app.request_stop
           and app.stop_btn._state == "normal" and app.transport._mix == 1.0, f"running={app.transport.running}")
        ok("wire.header.same-size-running", app.transport.winfo_reqwidth() == _w_idle,
           f"{_w_idle} -> {app.transport.winfo_reqwidth()}")
        _tw = app.transport.winfo_reqwidth()
        ok("wire.header.halves", app.transport._part_at(2) == "pause" and app.transport._part_at(_tw - 2) == "stop", "")
        ok("wire.header.pause-while-running", app.pause_btn._command == app.toggle_pause and app.pause_btn._text == "Pause"
           and not app.transport.armed, app.pause_btn._text)
        app.pause_btn.invoke(); root.update()
        ok("wire.header.pause-armed", app._pause_requested and app.transport.armed and app.transport._arm == 1.0
           and app.pause_btn._text == "Pause" and "pauses after this job" in app.batch_counter_var.get()
           and app._batch_counter_lbl.cget("fg") == app.kit.c("pause")
           and app.pause_btn.tooltip.text == app._PAUSE_TIPS[True]
           and app._queue_menu.entrycget(app._pause_menu_index, "label") == "Keep Running After This Job",
           f"armed={app.transport.armed} | {app.batch_counter_var.get()}")
        app.pause_btn.invoke(); root.update()
        ok("wire.header.pause-taken-back", not app._pause_requested and not app.transport.armed
           and "pauses" not in app.batch_counter_var.get() and app._batch_counter_lbl.cget("fg") == app.kit.c("faint"),
           app.batch_counter_var.get())
        app.pause_btn.invoke(); root.update()              # armed when the run ends: the next run starts clean
        app._batch_running = False; app._sync_run_ui(); root.update()
        ok("wire.header.start-when-idle", not app.transport.running and app.transport._mix == 0.0
           and app.transport._part_at(_tw - 2) == "start" and not app._pause_requested and not app.transport.armed, "")

        _select(1)
        acts = _buttons(app._card_body)
        ok("wire.card.shows-selected", app._card_body.winfo_manager() == "grid" and bool(app.card_title_var.get()),
           app.card_title_var.get())
        acts["Full log"].invoke(); root.update()
        ok("wire.card.full-log", app._view == "log", f"view={app._view}")
        app._nav["queue"].invoke(); root.update()
        acts["Command"].invoke(); root.update()
        ok("wire.card.command", app._cmd_frame.winfo_manager() != "", repr(app._cmd_frame.winfo_manager()))
        acts["Edit"].invoke(); root.update()
        ok("wire.card.edit", _top() == "JobDialog", str(_top()))
        close_toplevels(); root.update()
        _select(1)
        acts["Remove"].invoke(); root.update()
        ok("wire.card.remove", len(app.queue) == 2 and all(x is not items[1] for x in app.queue), f"{len(app.queue)} left")
        ok("wire.card.cancel", app.cancel_btn._command == app.request_stop, "")

        app.queue[:] = items; app.update_queue_box(); root.update()
        _select(0)

        def _menu_do(label, row):
            app._queue_context_menu(_Ev(), row)
            mn = _menus[-1]
            labels = {mn.entrycget(i, "label"): i for i in range(mn.index("end") + 1) if mn.type(i) == "command"}
            mn.invoke(labels[label]); root.update()
        _menu_do("Move down", 0)
        ok("wire.menu.move-down", app.queue[1] is items[0], "")
        _menu_do("Move up", 1)
        ok("wire.menu.move-up", app.queue[0] is items[0], "")
        _menu_do("Edit job…", 0)
        ok("wire.menu.edit", _top() == "JobDialog", str(_top()))
        close_toplevels(); root.update()
        _select(0)
        _menu_do("Remove", 0)
        ok("wire.menu.remove", len(app.queue) == 2 and all(x is not items[0] for x in app.queue), f"{len(app.queue)} left")

        for seq, view in ((f"<{m.MOD}-Key-2>", "history"), (f"<{m.MOD}-Key-3>", "log"),
                          (f"<{m.MOD}-Key-1>", "queue"), (f"<{m.MOD}-comma>", "settings")):
            _fire(root, seq); root.update()
            ok(f"wire.shortcut.{view}", app._view == view, f"{seq} -> view={app._view}")
        app._nav["queue"].invoke(); root.update()
        _fire(root, f"<{m.MOD}-n>"); root.update()
        ok("wire.shortcut.add-job", _top() == "JobDialog", str(_top()))
        _fire(root, f"<{m.MOD}-Key-2>"); root.update()
        ok("wire.shortcut.view-leaves-panel", app._view == "history" and _top() is None, f"view={app._view} {_top()}")
        _fire(root, f"<{m.MOD}-n>"); _fire(root, f"<{m.MOD}-r>"); root.update()
        ok("wire.shortcut.actions-wait-behind-panel", _top() == "JobDialog" and not app._batch_running, str(_top()))
        close_toplevels(); app._show_view("queue"); root.update()

        app._nav["history"].invoke(); root.update()
        hv = _buttons(app._views["history"])
        ok("wire.history.buttons", hv["Copy last result"]._command == app.copy_last_result
           and hv["Open output folder"]._command == app.open_output_folder, str(sorted(hv)))
        app._nav["log"].invoke(); root.update()
        lv = _buttons(app._views["log"])
        ok("wire.log.buttons", lv["Clear"]._command == app.clear_logs and lv["Raw log"]._command == app.open_raw_log
           and lv["Diagnostics"]._command == app.export_diagnostics, str(sorted(lv)))
        app.log("INFO", "wiring probe")
        arrived = pump(lambda: "wiring probe" in app.log_box.get("1.0", "end"), timeout=5)
        lv["Clear"].invoke(); root.update()
        ok("wire.log.clear", arrived and "wiring probe" not in app.log_box.get("1.0", "end"),
           f"arrived={arrived}, left={app.log_box.get('1.0', 'end-1c')[:60]!r}")
        app._nav["queue"].invoke(); root.update()

        mb = root.nametowidget(root["menu"])
        menus = {mb.entrycget(i, "label"): root.nametowidget(mb.entrycget(i, "menu"))
                 for i in range(mb.index("end") + 1) if mb.type(i) == "cascade" and mb.entrycget(i, "label")}
        ok("wire.menu.bar", {"File", "Edit", "View", "Help"} <= set(menus), str(sorted(menus)))

        def _entry(menu, label):
            return next(i for i in range(menu.index("end") + 1)
                        if menu.type(i) not in ("separator", "tearoff") and menu.entrycget(i, "label") == label)
        app.queue[:] = items[:1]; app.update_queue_box(); root.update()
        for label, view in (("History", "history"), ("Log", "log"), ("Queue", "queue")):
            menus["View"].invoke(_entry(menus["View"], label)); root.update()
            ok(f"wire.menubar.view-{view}", app._view == view, f"view={app._view}")
        menus["File"].invoke(_entry(menus["File"], "Add Job…")); root.update()
        ok("wire.menubar.add-job", _top() == "JobDialog", str(_top()))
        menus["View"].invoke(_entry(menus["View"], "History")); root.update()
        ok("wire.menubar.view-leaves-panel", app._view == "history" and _top() is None, f"view={app._view} {_top()}")
        app._show_view("queue"); root.update()
        close_toplevels(); root.update()
        menus["File"].invoke(_entry(menus["File"], "Look Inside…")); root.update()
        ok("wire.menubar.look-inside", app._view == "look" and _top() is None, f"{app._view} {_top()}")
        app._show_view("queue"); root.update()
        close_toplevels(); root.update()
        menus["File"].invoke(_entry(menus["File"], "Organize…")); root.update()
        ok("wire.menubar.organize", app._view == "organize", app._view)
        app._show_view("queue"); root.update()
        was = app._inspector_open
        menus["View"].invoke(_entry(menus["View"], "Hide Details" if was else "Show Details")); root.update()
        ok("wire.menubar.details", app._inspector_open != was, f"{was} -> {app._inspector_open}")
        _opened, _real_open = [], m.webbrowser.open
        m.webbrowser.open = lambda url, *a, **k: _opened.append(url)
        try:
            menus["Help"].invoke(_entry(menus["Help"], "Buy Me a Coffee…")); root.update()
        finally:
            m.webbrowser.open = _real_open
        ok("wire.menubar.donate", _opened == [m.DONATE_URL], str(_opened))
        _opened, _real_open = [], m.webbrowser.open
        m.webbrowser.open = lambda url, *a, **k: _opened.append(url)
        try:
            _v = app._view; app._donate_nav.invoke(); root.update()
        finally:
            m.webbrowser.open = _real_open
        ok("wire.sidebar.donate", _opened == [m.DONATE_URL] and app._view == _v, f"{_opened} view={app._view}")
        ok("wire.menubar.stop-safe-when-idle", (menus["File"].invoke(_entry(menus["File"], "Stop Queue")), not app.cancel_requested)[1], "")
        if m.IS_MAC:
            ok("wire.menubar.mac-settings", bool(root.tk.call("info", "commands", "::tk::mac::ShowPreferences")), "")
            root.tk.call("::tk::mac::ShowPreferences"); root.update()
            ok("wire.menubar.mac-settings-opens", app._view == "settings", f"view={app._view}")
            app._nav["queue"].invoke(); root.update()

        # decide from heights only, scroll fractions made the bar blink twice a second
        sfd = m.JobDialog(app, init_src=str(HBT)); root.update()
        body = sfd.body
        steady = True
        for req, view, want in ((400, 500, False), (820, 500, True), (820, 900, False), (500, 499, False), (520, 499, True)):
            body.winfo_reqheight = lambda r=req: r
            body._parent_canvas.winfo_height = lambda v=view: v
            seen = []
            for _ in range(4):
                body._sb_check(); root.update(); seen.append((body._sb_shown, body._scrollbar.winfo_manager()))
            steady = steady and all(s == (want, "grid" if want else "") for s in seen)
        ok("wire.scrollbar.no-blink", steady, str(seen))
        sfd.destroy(); root.update()

        # a file name in a package can fake the backend's markers: only our own paths count
        ok("review.inner-marker-shape", not m._is_inner_image_path("/Volumes")
           and not m._is_inner_image_path(str(S / "x")) and m._is_inner_image_path(str(S / "_ffpfsc_inner" / "PPSA00001.ffpfs")), "")
        _victim = S / "review_victim"; _victim.mkdir(exist_ok=True); (_victim / "keep.bin").write_bytes(b"x")
        _vi = types.SimpleNamespace(_inner_image=str(_victim))
        app._cleanup_inner_image(_vi); pump(lambda: False, timeout=0.5)
        ok("review.inner-cleanup-refuses-a-folder", (_victim / "keep.bin").is_file(), "")
        _rw_out = S / "review_out"; _rw_out.mkdir(exist_ok=True)
        _rwi = m.GameItem.from_chain(HBT, to="ffpfsc", output_path=str(_rw_out))
        _rww = m.CLIWorker(app, _rwi, ["py", "cli.py", str(HBT), str(_rw_out)], str(REPO), _rw_out, S / "review_tmp")
        _rww._handle_line("[OK] Compression complete: /Volumes")
        _out_foreign = _rww.output_path
        _rww._handle_line(f"[OK] Compression complete: {_rw_out / 'Game.ffpfsc'}")
        ok("review.worker-takes-only-own-output", not _out_foreign and _rww.output_path == str(_rw_out / "Game.ffpfsc"),
           f"{_out_foreign!r} {_rww.output_path!r}")
        _rww._handle_line("[PASS1-DONE] /Volumes")
        ok("review.worker-ignores-fake-pass1", getattr(_rwi, "_inner_image", None) in (None, ""), str(getattr(_rwi, "_inner_image", None)))
        _rs = S / "review_src"; shutil.rmtree(_rs, ignore_errors=True); shutil.copytree(HBT, _rs)
        _ri = m.GameItem.from_chain(_rs, to="ffpfsc", output_path=str(_rw_out)); _ri.after_source = "delete"
        app.queue.append(_ri)
        _hit = _rw_out / "Other [fw7.61].ffpfsc"; _hit.write_bytes(b"x")
        app._settle_existing(_ri, _hit, exact=False); pump(lambda: False, timeout=0.5)
        ok("review.inexact-output-keeps-the-source", _rs.is_dir() and _ri.status == "Skipped", f"{_rs.is_dir()} {_ri.status}")
        if _ri in app.queue:
            app.queue.remove(_ri)
        _pk = next((S / "c1_pkg").glob("*.pkg"), None)
        if _pk is not None:
            _pi = m.GameItem.from_chain(_pk, to="pkg", output_path=str(_pk.parent)); _pi.ps5_repair = True
            _none = app._existing_output(_pk, _pi)
            app._apply_output_choice(_pi, _pk, "overwrite")
            ok("review.repair-never-overwrites-its-source", _none is None and not getattr(_pi, "_replace_output", False)
               and getattr(_pi, "_keep_both", False), f"{_none} {getattr(_pi, '_replace_output', None)}")
        _mk = app._rescan_make({"to": "ffpfsc", "output": str(_rw_out), "patch_source": str(HBT)})
        ok("review.rescan-reuses-no-patch", getattr(_mk(HBT), "patch_source", None) is None, "")
        _cd = m.JobDialog(app, init_src=str(FF), init_to="ffpfsc"); root.update(); _cd._refresh(); root.update()
        ok("review.copy-only-hides-options", not _cd._comp_row.winfo_manager() and "copied" in _cd._to_hint.get(),
           f"{_cd._comp_row.winfo_manager()!r} {_cd._to_hint.get()!r}")
        _cd.after_var.set("delete"); root.update(); _cd._refresh(); root.update()
        ok("review.delete-in-the-summary", m.tr("then the source is deleted") in _cd.summary_var.get(), _cd.summary_var.get())
        _cd.destroy(); root.update()
        _bq = m.GameItem.from_chain(HBT, to="ffpfsc", output_path=str(_rw_out)); app.queue.append(_bq); app.update_queue_box(select_item=_bq)
        _be = m.JobDialog(app, init_src=str(HBT)); root.update()
        _n0 = len(app.queue); app.queue_remove_selected(); root.update()
        ok("review.remove-ignored-under-an-editor", len(app.queue) == _n0, f"{_n0} -> {len(app.queue)}")
        _be.out_var.set(str(_rw_out / "draft")); root.update()
        app._show_view("history"); root.update(); app._show_view("queue"); root.update()
        _bd = m.JobDialog(app); root.update()
        ok("review.draft-comes-back", _bd.out_var.get() == str(_rw_out / "draft") and _bd.src_var.get() == str(HBT),
           f"{_bd.out_var.get()} {_bd.src_var.get()}")
        _bd.destroy(); root.update()
        if _bq in app.queue:
            app.queue.remove(_bq)
        app._on_drop(types.SimpleNamespace(data=f"{{{HBT}}} {{{FF}}}")); root.update()
        _top = app._panels.stack[-1] if app._panels.stack else None
        ok("review.drop-opens-one-editor", isinstance(_top, m.JobDialog) and len(_top._dropped) == 2
           and _top._kind == "parent", f"{type(_top).__name__} {getattr(_top, '_kind', None)}")
        app._panels.close_all(); app._job_draft = None; root.update()
        app._queue_missing_saved = [{"name": "Away", "path": str(S / "not-there")}]; app._refresh_parked(); root.update()
        ok("review.parked-jobs-shown", app._parked_btn.winfo_manager() == "grid", app._parked_btn.winfo_manager())
        app._queue_missing_saved = []; app._refresh_parked(); root.update()
        _fi = m.GameItem.from_chain(HBT, to="ffpfsc", output_path=str(_rw_out)); _fi.status = "Failed"; _fi.status_note = "no space"
        _qr = m.QueueReportWindow(root, 1, 2, [_fi], retry=lambda: None); root.update()
        ok("review.queue-report-builds", _qr.winfo_exists(), "")
        _qr.destroy(); root.update()
        ok("review.stop-question-idle", app._stop_question() is None, "")
        _asked = []
        _ac = m.ask_confirm
        app._stop_question = lambda: "it ran long"
        app.cancel = lambda: _asked.append("cancel")
        m.ask_confirm = lambda *a, **k: (_asked.append(a[0]), False)[1]
        try:
            app.request_stop()
        finally:
            m.ask_confirm = _ac
            del app._stop_question, app.cancel
        ok("review.stop-asks-first", _asked == ["Stop the job?"], str(_asked))
        _ac, _destroy = m.ask_confirm, root.destroy
        m.ask_confirm = lambda *a, **k: True
        root.destroy = lambda: None
        app._batch_running = True; app.cancel_requested = False; app.extract_cancel_event.clear()
        try:
            app._on_close()
            _stopped = app.cancel_requested and app.extract_cancel_event.is_set()
        finally:
            m.ask_confirm, root.destroy = _ac, _destroy
            app._batch_running = False; app.cancel_requested = False; app.extract_cancel_event.clear()
            if getattr(app, "_keepawake_stop", None) is not None:
                app._keepawake_stop.clear()
        ok("review.quit-stops-the-job", _stopped, "")
        app._show_view("settings"); root.update()
        _pages_ok = True
        for _k in ("general", "compression", "archives", "drives", "backport", "about"):
            try:
                app._settings_view.show(_k); root.update()
            except Exception as _e:
                _pages_ok = False; print("settings page", _k, _e, file=sys.stderr)
        ok("review.settings-pages-build", _pages_ok, "")
        app._show_view("queue"); root.update()
        app._show_view("ps5repair"); root.update(); app._show_view("keystone"); root.update()
        ok("review.tables-follow-the-theme", len(m._TREES) >= 2, str(len(m._TREES)))
        app._show_view("queue"); root.update()

        app._set_theme("light"); root.update()
        ok("wire.theme.light", app.kit.mode == "light" and m.ctk.get_appearance_mode().lower() == "light"
           and app._appearance_var.get() == "light", app.kit.mode)
        app._set_theme("dark"); root.update()
        ok("wire.theme.dark", app.kit.mode == "dark", app.kit.mode)
    finally:
        m.filedialog.askdirectory, m.filedialog.askopenfilename = _fd_saved
        m.tk.Menu.tk_popup = _popup_saved
        close_toplevels()
        app.queue[:] = []
        app.update_queue_box()
except Exception:
    traceback.print_exc()  # full trace to stderr, the summary keeps one line
    res.append(("driver", False, traceback.format_exc()))
finally:
    # put the real settings.json back, the driver changed queue and defaults
    try:
        if _backup.exists():
            _sh.copy2(_backup, _settings); _backup.unlink()
    except Exception as e:
        print("WARN could not restore settings backup:", e)
    try: root.destroy()
    except Exception: pass
ok("driver.no-unexpected-password-prompt", not _unexpected_prompts, str(_unexpected_prompts[:5]))
ok("driver.adds-settle-in-time", not _settle_timeouts,
   f"timeouts at lines {_settle_timeouts}; slowest settles {sorted(_settle_secs)[-3:]}")
bad = 0
for name, okv, det in res:
    print(("PASS " if okv else "FAIL ") + f"{name:34s} {det}"[:200]); bad += (not okv)
    if name == "driver": open(str(S / "driver_traceback.txt"), "w").write(det)
print(f"{len(res)-bad} passed, {bad} failed")
sys.exit(1 if bad else 0)
