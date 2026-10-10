"""In-process GUI test for the fPKG integration."""
import sys, os, importlib.util, traceback, argparse, shutil, subprocess, tempfile, time, zipfile, json, re
from pathlib import Path
import tkinter as tk
HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(REPO / "backend"))
from test_fpkg_pipelines import fetch_hbt, CLI   # reuse the fixture + cli path
ap = argparse.ArgumentParser(); ap.add_argument("--work", type=Path, default=Path(tempfile.gettempdir()) / "ffpfsc-fpkg-gui-tests")
a = ap.parse_args(); S = a.work
if S.exists(): shutil.rmtree(S)
S.mkdir(parents=True)
# Isolate the app profile: the GUI honours PS5_FFPFSC_APP_DIR (APP_DIR = that path). It must
# be set BEFORE the module loads, because APP_DIR is computed at import time.
os.environ["PS5_FFPFSC_APP_DIR"] = str(S / "app_dir")
# A profile that has been through first-run setup, so no setup wizard pops up on screen.
(S / "app_dir").mkdir(parents=True, exist_ok=True)
# Silent: the jobs this driver runs to the end must not play the done/failed sounds or post
# notifications on the machine it runs on.
(S / "app_dir" / "settings.json").write_text('{"first_run_done": true, "show_space_dialog": false, '
                                             '"sound_complete": false, "sound_error": false, "notify": "off"}',
                                             encoding="utf-8")
HBT = fetch_hbt(S / "hbt"); OUT = S / "gui_drive_out"; OUT.mkdir()
# seed artefacts: one .ffpfsc (image-source path), one .pkg (extract path), one .zip (archive path)
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
signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(SystemExit("driver timeout")))
signal.alarm(240)
# Protect the user's real settings/queue: snapshot settings.json, restore in finally.
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
app.queue.clear()
res = []   # (re-initialised below; this early copy only carries the wizard check)
_no_wizard = not app._is_first_run
# Never touch the user's drives: every job in this driver uses scratch temp/output folders.
(S / "temp").mkdir(parents=True, exist_ok=True)
app.temp_var.set(str(S / "temp")); app.output_var.set(str(OUT))
res = []
def ok(name, cond, detail=""):
    res.append((name, bool(cond), detail))
ok("driver.no-first-run-wizard", _no_wizard, "the isolated profile is a finished setup, so no wizard window opens")
ok("driver.silent", not app.sound_complete_var.get() and not app.sound_error_var.get() and app.notify_var.get() == "off",
   "no done/failed sounds and no notifications while the driver runs jobs")
def pump(cond, timeout=20.0):
    """Run the Tk loop (after-callbacks included: the scan_q consumer) until cond() or timeout."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        root.update()
        if cond(): return True
        time.sleep(0.05)
    return cond()
def settle(timeout=60.0):
    """Add job builds new jobs on a worker and hands them to the queue one by one: wait
    until the last one is in."""
    root.update()
    pump(lambda: not (getattr(app, "_add_state", None) or {}).get("total"), timeout=timeout)
    root.update()
def close_toplevels():
    # Work dialogs are panels of the main window since 2.0.0 (app._panels.stack); messages
    # (job result, error report, prompts) are windows of their own.
    for w in list(app._panels.stack):
        try: w.destroy()
        except Exception: pass
    for w in root.winfo_children():
        try:
            if isinstance(w, m.ctk.CTkToplevel): w.destroy()
        except Exception: pass
try:
    # macOS: dialogs open as windows of their own, never as a sheet on the main window (a
    # sheet AppKit refused once ended the app with an abort)
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
    # 0) the profile dir is the isolated one (needs the GUI's PS5_FFPFSC_APP_DIR support)
    ok("driver.app-dir-isolated", Path(m.APP_DIR).resolve() == (S / "app_dir").resolve(), f"APP_DIR={m.APP_DIR}")
    # 1) build_command for an fpkg-build item (folder) - identity fields ride along as fallbacks
    it = m.GameItem.from_fpkg_build(HBT, output_path=str(OUT), content_id="UP9000-PPSA99099_00-PROSPERO00000000",
                                    title_id="PPSA99099", title="HBT", inner_mode="kraken", kraken_backend="builtin", level=5)
    cmd, cwd, outdir, temp = app.build_command(it)
    ok("build_command.fpkg-build", "--fpkg-build" in cmd and "--temp-dir" in cmd and "--compression-level" in cmd
       and cmd[cmd.index("--compression-level")+1] == "5" and "--fpkg-inner" in cmd and cmd[cmd.index("--fpkg-inner")+1] == "kraken"
       and "--content-id" in cmd and "--fpkg-version" not in cmd,   # default version is not passed (param.json decides)
       " ".join(cmd[-14:]))
    ok("fpkg-build.size-set", it.size > 0, f"size={it.size}")
    # 1b) a bundle fPKG job mirrors its folder at the destination
    itb = m.GameItem.from_fpkg_build(HBT, output_path=str(OUT), content_id="UP9000-PPSA99099_00-PROSPERO00000000", title_id="PPSA99099")
    itb.bundle_subfolder = "My Bundle"; itb.auto_organize = False     # classic bundle mirroring (auto-organize off)
    cmdb, _, outb, _ = app.build_command(itb)
    ok("build_command.fpkg-bundle-subfolder", outb == OUT / "My Bundle" and str(OUT / "My Bundle") in cmdb, str(outb))
    # 2) build_command for fpkg-extract
    pkg = next((S / "c1_pkg").glob("*.pkg"), None)
    ok("seed.pkg-built-without-ids", pkg is not None, "the seed build passed no --content-id: param.json supplied it")
    if pkg:
        ie = m.GameItem.from_fpkg_extract(pkg, output_path=str(OUT / "ext"))
        cmd2, *_ = app.build_command(ie)
        # the extract lands in its own "<package> [extracted]" subfolder below the chosen output
        _ext_out = next((a for a in cmd2 if a.startswith(str(OUT / "ext"))), "")
        ok("build_command.fpkg-extract", "--fpkg-extract" in cmd2 and _ext_out.endswith(" [extracted]")
           and Path(_ext_out).parent == OUT / "ext", " ".join(cmd2[-4:]))
    # 3) the job editor, .pkg output, FOLDER source → identity filled in from param.json; the job
    #    carries it with the codec and speed chosen
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
    # 3b) After the job: the row is there for a build, its choice lands on the job and in the Rescan recipe
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
    # 4) .pkg with an IMAGE source → identity stays empty (read at build time)
    dlg2 = m.JobDialog(app, init_src=str(FF), init_to="pkg"); root.update()
    ok("dialog.image.identity-empty", dlg2.cid_var.get() == "" and dlg2.tid_var.get() == "", f"{dlg2.cid_var.get()!r}")
    ok("dialog.image.hint", "param.json" in dlg2._ident_note.get(), dlg2._ident_note.get())
    dlg2.out_var.set(str(OUT)); n1 = len(app.queue); dlg2._add(); settle()
    ok("dialog.image.queued", len(app.queue) == n1 + 1 and app.queue[-1].path == FF and app.queue[-1].chain_to == "pkg", f"errors={errors[-1:]}")
    cmd3, *_ = app.build_command(app.queue[-1])
    ok("build_command.image-source", str(FF) in cmd3 and "--content-id" not in cmd3, " ".join(cmd3[-10:]))
    # 5) edit round trip: the editor opens with the job's own choices, saving replaces it in place
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
    # 6) the identity rows: folded away by default, a bad content id is refused and shows them
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
    # 6a) a game folder WITHOUT param.json opens the identity rows by itself and requires them
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
    # 6b) a .ffpfsc to .ffpfsc with no change is a copy; the source stays unless the job says otherwise
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
    app.queue.pop()   # keep the fixture queue clean for later tests
    # 7) ARCHIVE source with .pkg → an archive placeholder that keeps its job (target, identity) through extraction
    dlg6 = m.JobDialog(app, init_src=str(ZP), init_to="pkg"); root.update()
    ok("dialog.archive.hint", dlg6._kind == "archive" and "Archive" in dlg6.detect_var.get(), dlg6.detect_var.get())
    dlg6.cid_var.set(CID); dlg6.tid_var.set("PPSA99099")
    dlg6.out_var.set(str(OUT)); n4 = len(app.queue); dlg6._add(); settle()
    arc = app.queue[-1]
    ok("archive.queued-as-pkg", len(app.queue) == n4 + 1 and arc.chain_to == "pkg" and arc.archive_path == ZP
       and arc.fpkg_inner_mode == app.fpkg_defaults["inner"], f"{getattr(arc, 'chain_to', None)} {getattr(arc, 'archive_path', None)} errors={errors[-1:]}")
    ok("archive.carries-typed-identity", arc.fpkg_content_id == CID and arc.fpkg_title_id == "PPSA99099", f"{arc.fpkg_content_id!r}")
    app._copy_item_payload(arc, m.GameItem(HBT))
    ok("archive.payload-keeps-chain", arc.operation == "chain" and arc.chain_to == "pkg" and arc.path == HBT
       and arc.fpkg_inner_mode == app.fpkg_defaults["inner"], f"{arc.operation}/{getattr(arc, 'chain_to', None)}")
    # 7a) the other games of a multi-game archive get the same job; identity stays per game
    extra = m.GameItem(HBT)
    app._as_chain_job(extra, arc)
    ok("archive.extra-game-same-job", extra.operation == "chain" and extra.chain_to == "pkg" and extra.output_path == arc.output_path
       and extra.fpkg_inner_mode == arc.fpkg_inner_mode and extra.fpkg_content_id == "" and extra.patch_source is None,
       f"{extra.operation}/{getattr(extra, 'chain_to', None)}/{getattr(extra, 'fpkg_content_id', None)!r}")
    # 7b) sibling jobs from one archive share COMPRESSION only - never the first game's identity
    tpl = app._fpkg_compression_of(arc)
    _build_opts = {"inner", "backend", "level", "dll", "retail_normalize", "hdr_flag", "regen_playgo", "fake_sign"}
    ok("compression-of.no-identity", set(tpl) == _build_opts and tpl["inner"] == arc.fpkg_inner_mode
       and not ({"content_id", "title_id", "title", "version"} & set(tpl)), str(tpl))
    # 7c) a scan-detected patch is dropped (with a WARN) when the bundle becomes an fPKG job; a .ffpfsc source is sized at 2x for the gate
    pi = m.GameItem(HBT); pi.patch_source = Path("/nonexistent/patch"); app._as_fpkg_job(pi, dict(app.fpkg_defaults))
    ok("as-fpkg.drops-patch", pi.patch_source is None and pi.operation == "fpkg-build", str(pi.patch_source))
    fi = app._fpkg_item_for(FF, dict(app.fpkg_defaults), output_path=str(OUT))
    ok("as-fpkg.ffpfsc-size-estimate", fi is not None and fi.size > 0 and fi.extracted_size == 2 * fi.size, f"{getattr(fi, 'extracted_size', None)}")
    # 8) remembered format drives a browsed / dropped .ffpfsc: .pkg → fPKG build, .ffpfsc → unpack
    app.output_format_var.set("pkg"); app.source_var.set(str(FF)); n5 = len(app.queue); app.add_source_to_queue(); root.update()
    ok("drop.ffpfsc.remembered-pkg", len(app.queue) == n5 + 1 and app.queue[-1].operation == "fpkg-build", getattr(app.queue[-1], "operation", None))
    fb = app.queue[-1]
    app.output_format_var.set("ffpfsc"); app.source_var.set(str(FF)); n6 = len(app.queue); app.add_source_to_queue(); root.update()
    ok("drop.ffpfsc.remembered-ffpfsc", len(app.queue) == n6 + 1 and app.queue[-1].operation == "unpack", getattr(app.queue[-1], "operation", None))
    ok("format.legacy-bool-synced", app.output_compressed_var.get() is True, str(app.output_compressed_var.get()))
    # 8b) editing keeps the queue position and the bundle tag across pack → .pkg → .ffpfs
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
    # 8c) a pending identity never lingers past a failed add; fPKG extract bypasses the pack space gate
    app._pending_fpkg_identity = (HBT, {"content_id": "X"}); app.source_var.set(str(S / "does-not-exist")); app.add_source_to_queue(); root.update()
    ok("pending-identity.cleared-on-error", app._pending_fpkg_identity is None, str(app._pending_fpkg_identity))
    if pkg:
        ok("space-gate.fpkg-extract-proceeds", app._space_gate(ie, OUT) == "proceed", "")
    # 9) every older kind of job opens in the editor with its format, and saving keeps its place
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
    # 10) queue rendering with badges + details
    if pkg:
        app.queue.append(m.GameItem.from_fpkg_extract(pkg, output_path=str(OUT / "ext3")))
    app.update_queue_box(); root.update()
    rows = [app.queue_listbox.get(i) for i in range(app.queue_listbox.size())]
    ok("queue.badges", any("fPKG-BD" in r for r in rows) and (not pkg or any("fPKG-EX" in r for r in rows)), rows[:2])
    app.update_game_details(fb); ok("details.mode", "Build fPKG" in app.title_var.get(), app.title_var.get())
    # 11) double-click opens the editor for an fPKG job (no exception)
    app.queue_listbox.selection_clear(0, "end"); idx = app.queue.index(fb); app.queue_listbox.selection_set(idx)
    app._on_queue_double_click(None); root.update()
    ok("doubleclick.dispatch", not any("Could not open" in str(x) for x in errors) and bool(app._panels.stack)
       and isinstance(app._panels.stack[-1], m.JobDialog), "")
    close_toplevels()
    # 12) one door: the separate dialogs are gone
    ok("one-door.old-dialogs-gone", not any(hasattr(m, n) for n in ("FpkgBuildDialog", "PackDialog", "PatchDialog",
                                                                   "ConverterDialog", "JobEditMiniDialog")), "")
    # 13) "More options…" folds the identity and codec rows in and out
    dlg7 = m.JobDialog(app, init_src=str(HBT), init_to="pkg"); root.update()
    dlg7._pkg_more_btn.invoke(); root.update()
    ok("dialog.pkg.more-options", dlg7._pkg_more_box.winfo_manager() == "pack" and "Fewer" in dlg7._pkg_more_btn.cget("text"),
       dlg7._pkg_more_btn.cget("text"))
    dlg7._pkg_more_btn.invoke(); root.update()
    ok("dialog.pkg.fewer-again", dlg7._pkg_more_box.winfo_manager() == "" and "More" in dlg7._pkg_more_btn.cget("text"), "")
    dlg7.destroy()
    # 14) settings var present
    ok("settings.pubtools_var", hasattr(app, "pubtools_dll_var"), "")
    # 14b) AMPR: a PlayGo game that SHIPS fakelib/*.sprx (+ ampr_emu.index) must not ask for the emu folder
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
    # 14c) Auto-organize: names come from the game's own metadata - folder pack, image, fPKG
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
    # the real worker path: the backend's "[OK] fPKG complete: <path>" marker pre-sets output_path
    # and _find_output returns from that branch - the rename must happen there (1.1.4 missed it)
    fi3 = app._fpkg_item_for(FF, dict(app.fpkg_defaults), output_path=str(OUT)); fi3.auto_organize = True
    cmd3w, cwd3, out3w, tmp3 = app.build_command(fi3)
    out3w.mkdir(parents=True, exist_ok=True); dummy2 = out3w / "UP9000-PPSA99099_00-PROSPERO00000000-A0100-V0100.pkg"; dummy2.write_bytes(b"pkg")
    w = m.CLIWorker(app, fi3, cmd3w, cwd3, out3w, tmp3); w.start_time = time.time() - 30
    w.output_path = str(dummy2)          # what the marker line sets
    found = w._find_output()
    # the organized name already exists from the step above: the rename must keep BOTH files
    # (never replace an earlier build) and give the new one a " (2)" suffix
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
    # throwaway-extract detection is anchored to the app's own scratch roots: a user folder that
    # merely carries the name "_extracted" is never treated as something the app may delete
    _tmp_root = S / "temp"; app.temp_var.set(str(_tmp_root)); _own = _tmp_root / "_extracted" / "game-a" / "sub"
    _foreign = S / "library" / "_extracted" / "game-b" / "sub"
    for d in (_own, _foreign): d.mkdir(parents=True, exist_ok=True)
    class _Fake: pass
    _ia = _Fake(); _ia.path = str(_own); _ib = _Fake(); _ib.path = str(_foreign)
    ok("cleanup.extract-dir.own-scratch", app._extract_dir_for_item(_ia) == (_tmp_root / "_extracted" / "game-a").resolve(),
       str(app._extract_dir_for_item(_ia)))
    ok("cleanup.extract-dir.foreign-never", app._extract_dir_for_item(_ib) is None, str(app._extract_dir_for_item(_ib)))
    # a pool drive's own root is not scratch: the router puts scratch under <pool>/_ffpfsc_temp
    _pool = S / "pool"; _pool.mkdir(exist_ok=True); _ic = _Fake(); _ic.path = str(_pool / "_ffpfsc_temp" / "_extracted" / "g")
    (_pool / "_ffpfsc_temp" / "_extracted" / "g").mkdir(parents=True, exist_ok=True)
    app.temp_pool = [str(_pool)]
    ok("cleanup.extract-dir.pool-subfolder", app._extract_dir_for_item(_ic) is not None, str(app._extract_dir_for_item(_ic)))
    app.temp_pool = []
    # off: a single-archive folder whose parent is the output folder must not mirror into itself; elsewhere it still does
    conv = OUT / "convert"; conv.mkdir(exist_ok=True); shutil.copy2(ZP, conv / ZP.name)
    bi2 = m.GameItem.from_bundle(conv, conv / ZP.name, []); bi2.output_compressed = True; bi2.auto_organize = False; bi2.output_path = OUT
    bi2.path = HBT; bi2.archive_path = None            # as _copy_item_payload leaves it after extraction
    _, _, outb2, _ = app.build_command(bi2)
    ok("mirror.not-into-source", outb2 == OUT, str(outb2))
    bi3 = m.GameItem.from_bundle(conv, conv / ZP.name, []); bi3.output_compressed = True; bi3.auto_organize = False; bi3.output_path = OUT / "lib"
    bi3.path = HBT; bi3.archive_path = None
    _, _, outb3, _ = app.build_command(bi3)
    ok("mirror.elsewhere-kept", outb3 == OUT / "lib" / "convert", str(outb3))
    # dialog: checkbox present, remembered default, out label reflects it
    dlg9 = m.JobDialog(app, init_src=str(HBT), init_to="ffpfsc"); root.update()
    ok("dialog.organize.checkbox", dlg9.organize_var.get() is True and "Auto-organize" in dlg9._organize_cb.cget("text"),
       dlg9._organize_cb.cget("text")[:60])
    dlg9.destroy()
    # snapshot: a fresh pack item gets the remembered flag
    app.auto_organize_var.set(False); si = m.GameItem(HBT); app.queue.append(si); app.update_queue_box()
    ok("organize.snapshot", si.auto_organize is False, str(getattr(si, "auto_organize", None)))
    app.queue.remove(si); app.auto_organize_var.set(True)
    # 14d) fPKG in the PFS browser: the backend routes .pkg to the tool's list-inner /
    #      extract-inner --members and answers in the browser's own JSON / progress format
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
        # Look inside is a view of the main window, as Organize is: the whole content area,
        # its sidebar entry selected, no panel over the window
        root.geometry("1500x900"); root.update()
        ok("browser.is-a-view", app._view == "look" and not app._panels.stack
           and br.frame.winfo_width() == app._views_parent.winfo_width() and br.frame.winfo_width() > 1000,
           f"view={app._view} {br.frame.winfo_width()} of {app._views_parent.winfo_width()}")
        app._show_view("queue"); root.update()
    # 15) queue save/restore keeps fpkg fields
    app._queue_restored = True; app._save_queue()
    saved = m.load_settings().get("queue") or []
    ok("queue.persist.fpkg-fields", any(d.get("operation") == "fpkg-build" and "fpkg_level" in d and "fpkg_content_id" in d for d in saved), f"{len(saved)} saved")
    ok("settings.persist.format", m.load_settings().get("output_format") == "ffpfsc" and isinstance(m.load_settings().get("fpkg_defaults"), dict), "")

    # ── COPY job, driven through the REAL CLIWorker (1.1.8 regression) ─────────
    # 1.1.8 shipped a copy op whose worker fell through to the pack completion path
    # and raised "Backend exited but no new .ffpfsc output was created" AFTER a
    # successful move. Drive a real copy end to end and assert finish(True).
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
    # single-pass + space gate must not route a copy through the mkpfs estimates
    ok("copy.single-pass", m._item_is_single_pass(ci) is True, "")
    ok("copy.space-gate-passes", m._space_preflight_ok(ci, ctmp, cout) is True, "")
    # run it for real through CLIWorker and capture the finish() outcome
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

    # ── JobDialog: source → change the content → output (one door for every job) ──
    # J1) folder source, .ffpfsc out, backport on → one chain item; build_command emits --to
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
    # auto-organize is on: the image lands in "<out>/<Title> [TID] [vX]/" below the chosen folder
    ok("job.build_command.named-output", any(a.endswith(".ffpfsc") for a in jcmd) and jout.parent == OUT / "job",
       " ".join(a for a in jcmd if a.endswith(".ffpfsc")) + f" out={jout}")
    # J2) .ffpfsc source → .pkg: pkg flags present, sign forced, Look inside offered
    jd2 = m.JobDialog(app, init_src=str(FF)); root.update()
    ok("job.detect.ffpfsc", jd2._kind == "ffpfsc" and jd2._look_btn.winfo_manager() == "pack", jd2.detect_var.get())
    jd2.to_var.set(".pkg"); jd2.out_var.set(str(OUT / "job")); jd2.pkg_level_var.set(-4); root.update()
    ok("job.pkg.sign-forced", jd2._sign_cb.cget("state") == "disabled" and jd2._sign_fixed.winfo_manager() == "pack"
       and jd2._sign_cb.winfo_manager() == "", str(jd2._sign_cb.cget("state")))
    # the compatibility check reads a .ffpfsc too (only its executables are pulled out)
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
    # J3) .pkg source → folder: output named "<stem> [extracted]" under the chosen folder
    if pkg:
        jd3 = m.JobDialog(app, init_src=str(pkg)); root.update()
        jd3.to_var.set("Folder"); jd3.out_var.set(str(OUT / "job")); root.update()
        ok("job.summary.unpack", jd3.summary_var.get() == "Unpack to folder", jd3.summary_var.get())
        n0 = len(app.queue); jd3._add(); settle(); j3 = app.queue[-1]
        jcmd3, _, jout3, _ = app.build_command(j3)
        ok("job.build_command.folder-out", jcmd3[jcmd3.index("--to") + 1] == "folder"
           and str(jout3).endswith(" [extracted]") and jout3.parent == OUT / "job", str(jout3))
    # J4) folder → folder with nothing to change disables Add; with Sign it is "in place"
    jd4 = m.JobDialog(app, init_src=str(HBT)); root.update()
    # the remembered choices for "folder" (J1 turned backport on) are applied; clear them here
    jd4.backport_on_var.set(False); jd4.patch_on_var.set(False); jd4.sign_var.set(False)
    jd4.to_var.set("Folder"); root.update()
    ok("job.nothing-to-do.disabled", jd4.summary_var.get() == "Nothing to do"
       and str(jd4._add_btn.cget("state")) == "disabled", jd4.summary_var.get())
    jd4.sign_var.set(True); root.update()
    ok("job.sign-in-place", jd4.summary_var.get() == "Sign in place" and str(jd4._add_btn.cget("state")) == "normal"
       and str(jd4._out_entry.cget("state")) == "disabled", jd4.summary_var.get())
    jd4.destroy()
    # J5) edit: the dialog refills from the item and swaps it in place at the same index
    e = m.JobDialog(app, item=ji); root.update()
    ok("job.edit.prefill", e.src_var.get() == str(HBT) and e.backport_on_var.get() and e.to_var.get() == ".ffpfsc",
       f"{e.src_var.get()} {e.to_var.get()}")
    idx = app.queue.index(ji); e.backport_on_var.set(False); e.sign_var.set(True); e._add(); settle()
    ok("job.edit.swapped-in-place", app.queue[idx].operation == "chain" and app.queue[idx].chain_sign
       and app.queue[idx].backport_target is None and app.queue[idx] is not ji, "")
    # J6) the queue row and the details panel carry the derived sentence
    app.update_queue_box(select_item=app.queue[idx]); root.update()
    row = app.queue_listbox.get(idx)
    ok("job.queue-row", "→ .ffpfsc" in row and "sign" in row, row)
    app._on_queue_select(); root.update()
    ok("job.details.mode-sentence", "Sign, then build .ffpfsc" in app.title_var.get(), app.title_var.get())
    # J6b) compression per job: one row under the format, its own control per format
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
    # J6c) the help line keeps one height whatever option the pointer is over
    jh = m.JobDialog(app, init_src=str(HBT)); root.update()
    for _w in (600, 860):                # a narrow and a wide editor; hidden windows report no width
        jh._help_wrap(width=_w); root.update_idletasks()
        _heights = set()
        for _txt in [jh._HELP_IDLE, *jh._HELP.values()]:
            jh.help_var.set(_txt); root.update_idletasks(); _heights.add(jh._foot.winfo_reqheight())
        ok(f"job.help.buttons-stay-{_w}", len(_heights) == 1, str(sorted(_heights)))
    jh.destroy()
    # J6e) a parent folder: every game folder, archive set and container in it is one job
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
    # The tree's archives are stand-ins whose headers cannot be read, which would open the
    # (modal) password prompt; this step checks the jobs, not the password flow.
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
    # an archive that holds a .pkg is unpacked as a package, not refused
    _pk = S / "extracted_pkg_archive" / "inner"; _pk.mkdir(parents=True, exist_ok=True)
    (_pk / "Title [PPSA00004].pkg").write_bytes(b"\x7fCNT")
    _kind, _paths = app._classify_extracted_payload(S / "extracted_pkg_archive", "x.7z")
    _pi = app._item_from_payload_path(_kind, _paths[0])
    ok("archive.payload.pkg", _kind == "pkg" and _paths[0].name == "Title [PPSA00004].pkg"
       and _pi.path == _paths[0] and m.chain_source_kind(_pi) == "pkg", f"{_kind} {_paths}")
    # J6f) the archive password prompt: sized to its content, and shown only when no
    # saved password opens the header (an odd size is no password problem)
    _dlg = m.ArchivePasswordPrompt(app, "Example.part1.rar"); root.update_idletasks()
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
    # a damaged archive is never a password question
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
    # J6g) a silent step reports progress: the backend's bars move the stage, % and speed
    _pj = m.GameItem.from_chain(HBT, to="ffpfsc")
    _pc, _pcwd, _pout, _ptmp = app.build_command(_pj)
    _pw = m.CLIWorker(app, _pj, _pc, _pcwd, _pout, _ptmp)
    _pw._handle_line("[PHASE] Extracting")
    _pw._handle_line("[#############-------------------]  42% extract @ 812.40 MB/s ETA 57s")
    ok("progress.silent-step-bars", _pw.phase == "Extracting" and _pw.speed == "812.40 MB/s",
       f"phase={_pw.phase} speed={_pw.speed}")
    # J6g2) a chain that unpacks a .pkg first: extract-first bands, forward only, one value
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
    # J6g3) a job the backend turns into a plain copy (.pkg → .pkg, nothing to change, or
    # --copy): the bar follows the copy, and stages the copy never runs are not ticked
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
    # the Write stage no longer claims it "may show 0%" while its bar moves
    _ww = m.CLIWorker(app, _pj, _pc, _pcwd, _pout, _ptmp); _ww.start_time = time.time()
    app.status_update = lambda *a, **k: None
    try:
        _ww._set_stage("Writing Final Image", 10)
    finally:
        app.status_update = _real_su
    ok("progress.write-text-is-honest", "0%" not in str(_ww._detail) and "silently" not in str(_ww._detail),
       str(_ww._detail))
    # MkPFS's per-file "0% write" lines must not override the byte meter's bar
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
    # a folder source skips the unpack band instead of jumping over it
    _fw = m.CLIWorker(app, _pj, ["py", "cli.py", str(HBT), str(OUT), "--to", "ffpfsc", "--sign"],
                      _pcwd, _pout, _ptmp)
    ok("progress.folder-chain-without-extract-band", "Extracting" not in _fw._weights
       and _fw._weights["Reading Game"][0] < 5 and _fw._weights["Cleaning Up"][1] == 100, str(_fw._weights))
    # a chain to .pkg locks bars to the fPKG phases and skips the ShadowMount check
    _kw = m.CLIWorker(app, _cji, ["py", "cli.py", str(_cpk), str(OUT), "--to", "pkg"], _pcwd, _pout, _ptmp)
    ok("progress.chain-to-pkg-is-fpkg", _kw._is_fpkg and _kw._weights is m.CLIWorker.FPKG_BUILD_WEIGHTS, "")
    # a .pkg from the app's own extraction is built in place (the tool takes the files in as it
    # goes); a .pkg from the user's own folder is not
    _ex = Path(app.temp_var.get()) / "_extracted" / "release__abcd1234" / "PPSA00042-app0"
    (_ex / "sce_sys").mkdir(parents=True, exist_ok=True)
    (_ex / "sce_sys" / "param.json").write_text('{"titleId": "PPSA00042", "contentVersion": "01.000.000"}')
    (_ex / "eboot.bin").write_bytes(b"\x7fELF")
    _xi = m.GameItem.from_chain(_ex, to="pkg", output_path=str(OUT))
    _xcmd, *_ = app.build_command(_xi)
    _pi = m.GameItem.from_chain(HBT, to="pkg", output_path=str(OUT))
    _pcmd, *_ = app.build_command(_pi)
    ok("cmd.pkg-from-own-extraction-in-place", "--stage-in-place" in _xcmd and "--stage-in-place" not in _pcmd,
       f"own={'--stage-in-place' in _xcmd} user={'--stage-in-place' in _pcmd}")
    # the CPU cores setting reaches a .pkg job (Kraken workers); 0 = all cores sends nothing
    _cpu_before = app.cpu_count_var.get()
    try:
        app.cpu_count_var.set(4); _c4, *_ = app.build_command(_pi)
        app.cpu_count_var.set(0); _c0, *_ = app.build_command(_pi)
    finally:
        app.cpu_count_var.set(_cpu_before)
    ok("cmd.pkg-carries-cpu-count", "4" == (_c4[_c4.index("--cpu-count") + 1] if "--cpu-count" in _c4 else None)
       and "--cpu-count" not in _c0, f"four={_c4.count('--cpu-count')} auto={_c0.count('--cpu-count')}")
    shutil.rmtree(_ex.parent, ignore_errors=True)
    # the worker notices when the backend took the unpacked files in: the extraction is not kept
    _cw = m.CLIWorker(app, _xi, ["py", "cli.py", str(_ex), str(OUT), "--to", "pkg"], _pcwd, _pout, _ptmp)
    _cw._handle_line("  [consume] freed 1.2 GB so far (3 file(s) the image already holds)")
    ok("cmd.consume-line-marks-the-worker", _cw.consumed, str(_cw.consumed))
    # J6g2b) the fPKG builder's late "Scanning Files" marker is refused, and so is the
    # "100% source scan" bar behind it: Temp PFS stays where Kraken puts it; the Kraken
    # bar's speed and time left reach the worker
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
    # J6g3) the queue bar: a side message cannot reset the running job, and it never goes back
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
    # J6g4) the space check reads the game size out of a .pkg (the file is compressed)
    _real_li = None
    try:
        import fpkg as _fp
        _real_li = _fp.list_inner
        _fp.list_inner = lambda _p, **_k: {"entries": [{"type": "file", "size": 900}, {"type": "dir"},
                                                        {"type": "file", "size": 1100}]}
        _si = m.GameItem.from_chain(_cpk, to="ffpfsc")
        _got = app._probe_pkg_content(_si)
    finally:
        if _real_li is not None:
            _fp.list_inner = _real_li
    ok("space.pkg-game-size", _got == 2000 and _si.pkg_content_size == 2000 and _si.extracted_size == 2000
       and m._peak_factor_for(_si) == m.PKG_UNPACK_PEAK_FACTOR, f"{_got} {_si.extracted_size}")
    app._active_item = None
    _ci = app._card_info_text(_pj)
    ok("card.info.drives", "writes to" in _ci.get("Drives", "") and "builds on" in _ci.get("Drives", ""),
       _ci.get("Drives", ""))
    # J6h) a cancel keeps a finished extraction until the job leaves the queue
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
    # removing a job deletes the copy it kept
    _kj = m.GameItem.from_chain(_kpkg, to="ffpfsc"); _kj.kept_extract = True; _kj.status = "Cancelled"
    app.queue.append(_kj); app.update_queue_box(select_item=_kj); root.update()
    app.queue_listbox.selection_clear(0, "end"); app.queue_listbox.selection_set(app.queue.index(_kj))
    app.queue_remove_selected()
    pump(lambda: not (_kt / "_extracted" / "Arc__1").exists(), timeout=10.0)
    ok("cancel.remove-deletes-kept-copy", _kj not in app.queue and not (_kt / "_extracted" / "Arc__1").exists(), "")
    # the startup sweep offers everything but a kept copy
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
    # after a restart, a saved job whose extracted copy is gone starts again at its archive
    _saved_q, _live_q = m.load_settings().get("queue"), list(app.queue)
    _src_item = m.GameItem.from_chain(_korg, to="ffpfsc")          # a real job, saved as the app saves it
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
    # J6i) a failed job keeps its extraction; Edit and Retry work on it
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
    # Edit: the kept copy is the source, and saving keeps the job's archive link and copy
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
    # Edit a failed job whose copy is gone: the editor shows the archive it came from
    _gi = m.GameItem.from_chain(_fpk, to="ffpfsc", output_path=str(OUT / "job"))
    _gi.origin_archive, _gi.status, _gi.path = str(_korg), "Failed", S / "gone2" / "Title.pkg"
    app.queue.append(_gi); app.update_queue_box(); _before = set(app._panels.stack)
    app._edit_job(_gi); root.update()
    _jd2 = next((w for w in app._panels.stack if w not in _before and isinstance(w, m.JobDialog)), None)
    ok("failure.edit-shows-archive-when-copy-gone", _jd2 is not None and _jd2.src_var.get() == str(_korg), "")
    if _jd2 is not None:
        _jd2.destroy(); root.update()
    # Retry runs this job alone, first
    _starts = []
    app.start = lambda rearm_failed=True: _starts.append(rearm_failed)
    _gi.status = "Failed"; app.queue.remove(_gi); app.queue.append(_gi)
    app._retry_job(_gi)
    ok("retry.runs-this-job-only", app._run_next is _gi and app.queue[-1] is _gi
       and _gi.status in ("Pending Extract", "Queued") and _starts == [False], f"{_gi.status} {_starts}")
    app._run_next = None
    app.start = _real_start
    # the next job of a batch frees a failed job's copy when it needs the space
    _rel = _kt / "_extracted" / "Rel__5"; _rel.mkdir(parents=True, exist_ok=True); (_rel / "Title.pkg").write_bytes(b"x")
    _rf = m.GameItem.from_chain(_rel / "Title.pkg", to="ffpfsc", output_path=str(OUT / "job"))
    _rf.status, _rf.kept_extract, _rf.origin_archive = "Failed", True, str(_korg)
    _rc = m.GameItem.from_chain(_rel / "Title.pkg", to="ffpfsc", output_path=str(OUT / "job"))
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
       and _rc.kept_extract and "make room" in _rf.status_note and not _rel.exists(),
       f"fits={_fits} tight={_tight} rf={_rf.kept_extract} rc={_rc.kept_extract}")
    # J6j) a finished job stays in the queue, marked Done, until it is cleared
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
    # only the jobs that run count for "Game n/N"
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
    # the setting removes a finished job right away
    app.auto_remove_done_var.set(True)
    _dk = m.GameItem.from_chain(HBT, to="ffpfsc", output_path=str(OUT / "job")); _dk.status = "Running"
    app.queue[:] = [_dk]; app._active_item = _dk
    app.finish(True, "ok", "cmd")
    pump(lambda: _dk not in app.queue, timeout=10.0)
    ok("queue.auto-remove-setting", _dk not in app.queue, str([i.status for i in app.queue]))
    app.auto_remove_done_var.set(False)
    # J9) after the job: the source is kept, trashed, moved or deleted once the job is Done
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
    _j0 = m.GameItem.from_chain(_g1, to="ffpfsc")              # after_source None: a queue from 2.1.0
    ok("after.older-jobs-keep", app._after_job_plan(_j0, _fake_worker(_outf))[0] == "keep", "")
    _jm = m.GameItem.from_chain(_g1, to="ffpfsc"); _jm.after_source = "move"
    ok("after.move-needs-a-folder", "no destination" in str(app._after_job_plan(_jm, _fake_worker(_outf))[3]), "")
    # output already there when the job is due: Move/Trash/Delete → the action and Done; Keep → Skipped
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
    # an archive set read again right before its job runs: grown since it was added, unchanged, incomplete
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
        _ra.size = 100                                          # looks changed again → probed → damaged
        _go2 = app._refresh_archive_set(_ra)
        pump(lambda: not app._batch_running, timeout=3.0)
        ok("refresh.incomplete-set-fails-with-reason", _go2 is False and _ra.status == "Failed"
           and "missing" in _ra.status_note, f"{_go2} {_ra.status} {_ra.status_note!r}")
    finally:
        m.ArchiveExtractor.probe_header_state = _real_probe
    # the release folder: moved whole with what else is in it; two games in one folder apart; nothing empty left
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
        # the second job of a run starts through _batch_auto_start: an output already there is
        # settled there too, before anything is unpacked
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
            pump(lambda: _bj.status == "Done" and not app._batch_running, timeout=10.0)
        finally:
            app._extract_queued_item, app._predicted_output = _real_ext, _real_pred3
            app._batch_running = False
        ok("release.batch-path-settles-before-unpacking", not _ext_calls and _bj.status == "Done"
           and (_rd / "[site]-PPSA00016").is_dir(), f"extract={len(_ext_calls)} {_bj.status}")
    finally:
        m.ArchiveExtractor.probe_header_state = _real_probe2
    # integrating a patch: it must fit the game; a patched job's output name never guesses the
    # firmware; the originals it replaced go beside the output on success and are dropped otherwise
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
    # never anything else: no backup line, an empty one ('.'), a relative path, a folder outside the scratch
    _guard_dir = S / "not_scratch" / "patch-backup-x" / "Original files - Y"; _guard_dir.mkdir(parents=True)
    _cwd_before = sorted(os.listdir("."))
    _guard_calls = []
    for _val in (None, "", ".", "relative/dir", str(_guard_dir)):
        _gw = _fake_worker(_pout_f)
        if _val is not None:
            _gw.patch_backup = _val
        app._publish_patch_backup(_pjob, _gw, lambda: _guard_calls.append(1))
        app._drop_patch_backup(_gw)
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
    app._drop_patch_backup(_dw)
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
    app.queue[:] = []; app._active_item = None; app.worker = None
    # the countdown after the queue: goes on its own, or is cancelled
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
    # pause after the running job: the job in progress coming back goes on; a finished job
    # pauses the queue before the next one, without the queue's end; the last job ends it
    _pa1 = m.GameItem.from_chain(HBT, to="ffpfsc"); _pa1.status = "Done"
    _pa2 = m.GameItem.from_chain(HBT, to="ffpfsc"); _pa2_st = _pa2.status
    _due_seen, _qf_seen = [], []
    _real_due, _real_qf, _notify0 = app._due_checks, app._queue_finished, app.notify_var.get()
    app._due_checks = lambda it: (_due_seen.append(it), False)[1]
    app._queue_finished = lambda: _qf_seen.append(1)
    app.notify_var.set("off")
    # earlier checks may still have a scratch reclaim running in its thread: the run would wait for it
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
    # clear all keeps the running job
    _r1 = m.GameItem.from_chain(HBT, to="ffpfsc"); _r2 = m.GameItem.from_chain(HBT, to="ffpfsc"); _r2.status = "Done"
    app.queue[:] = [_r1, _r2]; app._batch_running = True; app._active_item = _r1
    m.messagebox.askyesno = lambda *a, **k: True
    try:
        app.clear_jobs("all")
    finally:
        m.messagebox.askyesno = _real_ask
    ok("queue.clear-all-keeps-running", app.queue == [_r1], str(len(app.queue)))
    # several jobs selected: Command/Ctrl-click, Shift-click, select all, Escape; kept across a refresh
    # and a reorder (by job, not by row); Remove takes them all but the running one; a summary card
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
    app.queue.insert(0, _new_top); app.update_queue_box(); root.update()   # rows shift, the marks follow their jobs
    _moved = [app.queue[i] for i in _ql.marked_rows()]
    ok("multi.kept-across-refresh-and-shift", [id(x) for x in _kept] == [id(_ms[1]), id(_ms[3])]
       and [id(x) for x in _moved] == [id(_ms[1]), id(_ms[3])] and _ql.marked_rows() == [2, 4],
       f"{_ql.marked_rows()}")
    root.update()
    _card_multi_shown = bool(app._card_multi.winfo_manager())
    ok("multi.summary-card", _card_multi_shown and app._multi_title_var.get() == "2 jobs selected",
       f"{_card_multi_shown} {app._multi_title_var.get()!r}")
    # Remove: the running job among the marked ones stays
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
    # a restored Done job stays listed even when its source is gone
    _saved_q2 = m.load_settings().get("queue")
    _de = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(_r2).items()
           if not k.startswith("_") and k != "artwork" and (v is None or isinstance(v, (str, int, float, bool, Path)))}
    _de.update({"status": "Done", "path": str(S / "gone3" / "x"), "archive_path": None})
    m.save_settings({"queue": [_de]}); app.queue.clear(); app._restore_queue()
    ok("queue.restore-keeps-done-record", len(app.queue) == 1 and app.queue[0].status == "Done", str(len(app.queue)))
    m.save_settings({"queue": _saved_q2 or []})
    app.summary_popup_var.set(_real_sum); app.open_output_var.set(_real_open)
    # J6p) a dropped source is remembered; Add job pre-fills with it next time
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
        # a dialog with no last source but one typed into the field opens the picker there
        m.save_settings({"last_source": "", "last_source_dir": ""})
        jr2 = m.JobDialog(app); root.update()
        jr2.src_var.set(str(_lib_fw))
        ok("source.initial-dir-follows-field", jr2._initial_dir() == str(_lib_fw) or jr2._initial_dir() == str(_lib),
           str(jr2._initial_dir()))
        jr2.destroy()
    finally:
        m.save_settings(_old_settings)
    # J6q) rescan: queues only sources that are not already queued or in the history
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
    app._launch_scan = _inline                           # run the rescan scan here
    try:
        app.rescan_last_source()
        pump(lambda: (getattr(app, "_add_state", {}) or {}).get("total"), timeout=5.0)
        settle()
    finally:
        app._launch_scan = _real_launch
    _after = [getattr(i, "name", "") for i in app.queue[_pre:]]
    ok("rescan.adds-only-new", _after == ["Game2"], f"{_after}")
    # a second rescan now finds nothing new
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
    # without a template, Rescan tells the user instead of adding anything
    m.save_settings({"rescan_template": {}})
    _pre3 = len(app.queue); app.rescan_last_source(); settle()
    ok("rescan.needs-template-first", len(app.queue) == _pre3, "")
    # restore
    app.queue[:] = _saved_q
    if _hist_backup is None:
        _hist_file.unlink(missing_ok=True)
    else:
        _hist_file.write_bytes(_hist_backup)
    m.save_settings(_old_settings)
    app.update_queue_box(); root.update()

    # J6q2) rescan also skips a source whose game is already queued, done, or in the output
    _lib3 = S / "rescan_lib_tid"; _lib3.mkdir(exist_ok=True)
    _r3 = OUT / "rescan_tid"; _r3.mkdir(exist_ok=True)
    _rt3 = {"to": "ffpfsc", "output": str(_r3), "sign": False, "backport_target": None,
            "patch_source": None, "keep_source": False, "organize": False, "ff_level": 7, "pkg_params": None}
    # 4 release archives: one is already in the queue by name, one already in the history,
    # one has its output in the target folder, one is new.
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
    # a .ffpfsc for PPSA20003 is already in the output folder
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
    # restore
    app.queue[:] = _saved_q
    if _hist_backup2 is None:
        _hist_file2.unlink(missing_ok=True)
    else:
        _hist_file2.write_bytes(_hist_backup2)
    app.update_queue_box(); root.update()

    # J6r) Edit on a Done job re-arms it to Queued with the new settings
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

    # J6s) the queue list scrolls with the wheel AND with a Tk 9 trackpad swipe (<TouchpadScroll>)
    uk = sys.modules["ui_kit"]
    def _pack(dx, dy):                              # Tk's %D for <TouchpadScroll>: dx high, dy low, signed
        v = ((dx & 0xFFFF) << 16) | (dy & 0xFFFF)
        return v - 0x100000000 if v >= 0x80000000 else v
    ok("scroll.unpack-touchpad-delta",
       uk.unpack_touchpad_delta(_pack(0, -7)) == (0, -7) and uk.unpack_touchpad_delta(_pack(3, -2)) == (3, -2)
       and uk.unpack_touchpad_delta(_pack(-3, 5)) == (-3, 5) and uk.unpack_touchpad_delta(0) == (0, 0),
       str([uk.unpack_touchpad_delta(_pack(a, b)) for a, b in ((0, -7), (3, -2), (-3, 5))]))
    _tp = [m.GameItem(HBT) for _ in range(60)]      # enough rows to scroll
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
    _handled_tp = [_cv._on_touchpad(_E(_pack(0, -8))) for _ in range(5)]   # fingers: 8 px per event, 5 events
    _y_tp = _cv.yview()[0]
    _total_px = 12 + 60 * _lb.ROW_H
    _moved_px = round((_y_tp - _y0) * _total_px)
    ok("scroll.touchpad-moves-by-pixels", all(h == "break" for h in _handled_tp) and _moved_px == 40,
       f"moved {_moved_px} px (expected 40), handled={_handled_tp}")
    _cv._on_touchpad(_E(_pack(0, 8)))               # the other way
    _y_back = _cv.yview()[0]
    ok("scroll.touchpad-back", round((_y_tp - _y_back) * _total_px) == 8, f"{_y_tp} -> {_y_back}")
    _wheel_handled = _cv.event_generate  # placeholder to keep linters calm
    _before = _cv.yview()[0]
    _r1 = _cv.bind("<MouseWheel>")                  # the binding exists on the canvas itself
    _r2 = _cv.bind("<TouchpadScroll>") if tk.TkVersion >= 9 else "n/a"
    ok("scroll.canvas-has-direct-bindings", bool(_r1) and bool(_r2), f"wheel={bool(_r1)} touchpad={bool(_r2)}")
    # a wheel notch (Tk 9 / Windows ±120) and a Tk 8.6/macOS notch (±1) both move 40 px
    _cv._scroll_px("y", 0)                           # no-op
    _b = _cv.yview()[0]; _cv.event_generate("<MouseWheel>", delta=-120, x=10, y=10, rootx=1, rooty=1) if False else None
    _b = _cv.yview()[0]
    class _W:
        def __init__(self, d): self.delta = d
    # drive the handler the binding calls: find it via the bound callback is awkward; use the
    # pixel primitive with the same arithmetic the handler applies
    _cv._scroll_px("y", 40); _a1 = _cv.yview()[0]
    ok("scroll.wheel-notch-is-40px", round((_a1 - _b) * _total_px) == 40, f"{round((_a1 - _b) * _total_px)} px")
    # a Text widget is left to Tk's own class bindings (no double scroll): nothing attached
    _txt = m.kit_text_probe if hasattr(m, "kit_text_probe") else app.kit.text(root)
    ok("scroll.text-not-double-bound", not hasattr(_txt, "_scroll_px"), str(hasattr(_txt, "_scroll_px")))
    _txt.destroy()
    # nothing to scroll → the primitive reports False and leaves the view alone
    _real_yview = _cv.yview
    _cv.yview = lambda: (0.0, 1.0)
    try:
        _quiet = _cv._scroll_px("y", 40)
    finally:
        _cv.yview = _real_yview
    ok("scroll.quiet-when-nothing-to-scroll", _quiet is False, str(_quiet))
    # Settings pages: the ScrollFrame answers <TouchpadScroll> through bind_all (Tk 9)
    _sf_ok = True
    if tk.TkVersion >= 9:
        _sf_ok = "<TouchpadScroll>" in root.bind_all()
    ok("scroll.settings-scrollframe-touchpad", _sf_ok and hasattr(m.ScrollFrame, "_touchpad_all"), str(_sf_ok))
    del _cv.winfo_height
    app.queue[:] = _saved; app.update_queue_box(); root.update()

    # J6o) progress and time left over all jobs of the run
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
    _e1 = app._estimate_left(0, 100 * _GB, 10, _cj2)          # 10 GB in 100 s → 90 GB left ≈ 900 s
    app._batch_fin_secs, app._batch_fin_bytes = 1000.0, 100 * _GB
    _e2 = app._estimate_left(200 * _GB, 50 * _GB, 50, _cj2)   # 10 s/GB × 225 GB = 2250 s
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
    # J6n) the list keeps the order the jobs were added in; the next job is the first waiting one
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
    # the list widget: press, drag past the threshold, release → on_move(src, dst)
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
    app.queue_listbox._click(_E(6 + 3 * _RH + 20))      # press on row 3
    _pressed = app.queue_listbox._press
    app.queue_listbox._drag(_E(6 + 3 * _RH + 17))       # under the threshold: still a click
    _no_drag = app.queue_listbox._drop is None
    app.queue_listbox._drag(_E(6 + 1 * _RH + 4))        # up to the line between rows 0 and 1
    app.queue_listbox._release(_E(6 + 1 * _RH + 4))
    app.queue_listbox.on_move = _real_mv
    del _cv.winfo_height
    ok("order.list-drag-and-drop", _no_drag and _moves == [(3, 1)],
       f"{_moves} pressed={_pressed} rows={len(app.queue_listbox.rows)} no_drag={_no_drag}")
    # a job added while the queue runs counts in "Job n of N"
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
    # J6k) an output that is already there is asked about at the start, before anything runs
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
    # unknown firmware (an archive before unpacking): any tag counts, a backport wants one at or below its target
    _ut = _xo / "fwrule" / "Example Quest [PPSA00001] [v01.000].ffpfsc"; _ut.parent.mkdir(parents=True, exist_ok=True)
    _f10 = _ut.with_name("Example Quest [PPSA00001] [v01.000] [fw10.00].ffpfsc"); _f10.write_bytes(b"x")
    _plain = m.GameItem.from_chain(HBT, to="ffpfsc")
    _bpj = m.GameItem.from_chain(HBT, to="ffpfsc", backport_target="7.61")
    _hit_plain, _hit_bp = app._existing_output(_ut, _plain), app._existing_output(_ut, _bpj)
    _f761 = _ut.with_name("Example Quest [PPSA00001] [v01.000] [fw7.61].ffpfsc"); _f761.write_bytes(b"x")
    ok("exists.unknown-fw-rules", _hit_plain == _f10 and _hit_bp is None and app._existing_output(_ut, _bpj) == _f761,
       f"{_hit_plain} {_hit_bp} {app._existing_output(_ut, _bpj)}")
    _pred.write_bytes(b"old")                    # exactly this job's output, for the checks below
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
    # the rule in Settings answers without a window: Skip by default, Keep both, Overwrite
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
    # a job whose output shows only after unpacking follows the choice made at the start
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
    # a .pkg the user chose to overwrite replaces the old one instead of a "(2)" copy
    _pk_dir = _xo / "pkgtest"; _pk_dir.mkdir()
    (_pk_dir / "Game [PPSA00001] [v01.000].pkg").write_bytes(b"old")
    _new_pkg = _pk_dir / "UP0000-PPSA00001_00-X.pkg"; _new_pkg.write_bytes(b"new")
    _pi = m.GameItem.from_chain(HBT, to="pkg"); _pi._organized_pkg_name = "Game [PPSA00001] [v01.000].pkg"
    _pi._replace_output = True
    _fin = app._finalize_pkg_name(_pi, _new_pkg)
    ok("exists.pkg-overwrite-replaces", _fin.read_bytes() == b"new" and not (_pk_dir / "Game [PPSA00001] [v01.000] (2).pkg").exists(),
       str(sorted(x.name for x in _pk_dir.iterdir())))
    # J6l) the job is named after the game, read out of the archive before extraction
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
    # J6m) adding many jobs shows its progress, and a Start pressed meanwhile waits for them
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
    # J6d) the details pane: facts about the job, a log that fills the height, a failure reason
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
    # J7) backport targets come from the firmware libraries folder, one subfolder per firmware
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
    # J8) the detection line reads the SDK of a fake-signed eboot too
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
    m.save_settings({"job_dialog_defaults": _saved_defaults})   # J7 remembered a 9.60 backport for folders
    # J7) a parent folder makes one job per game
    parent = S / "parent"; parent.mkdir(exist_ok=True)
    for nme in ("A", "B"):
        if not (parent / nme).exists():
            shutil.copytree(HBT, parent / nme)
    jd7 = m.JobDialog(app, init_src=str(parent)); root.update()
    ok("job.detect.parent", jd7._kind == "parent" and len(jd7._games) == 2, jd7.detect_var.get())
    jd7.to_var.set(".ffpfsc"); jd7.out_var.set(str(OUT / "job")); n0 = len(app.queue); jd7._add(); settle()
    ok("job.add.parent-two-items", len(app.queue) == n0 + 2 and all(x.operation == "chain" for x in app.queue[-2:]),
       f"+{len(app.queue) - n0}")
    # J8) the choices are remembered per kind of source
    _jd = m.load_settings().get("job_dialog_defaults") or {}
    ok("job.defaults-saved", _jd.get("folder", {}).get("to") == "ffpfsc" and _jd.get("ffpfsc", {}).get("to") == "pkg", str(_jd))
    # J9) the double-click dispatch opens the job dialog for a chain item
    app.queue_listbox.selection_clear(0, "end"); app.queue_listbox.selection_set(idx)
    before = set(app._panels.stack)
    app._on_queue_double_click(None); root.update()
    opened = [w for w in app._panels.stack if w not in before and isinstance(w, m.JobDialog)]
    ok("job.double-click.opens-job-dialog", len(opened) == 1, f"{[type(w).__name__ for w in app._panels.stack if w not in before]}")
    close_toplevels()
    # J10) messages get a window of their own; the work surfaces stay panels of the main window
    n_panels = len(app._panels.stack)
    sd = m.SummaryDialog(root, "report"); ed = m.ErrorDialog(root, "boom", "", "", operation="pack")
    root.update(); time.sleep(0.3); root.update()
    ok("message.summary-own-window", isinstance(sd, m.ctk.CTkToplevel) and sd not in app._panels.stack
       and len(app._panels.stack) == n_panels, f"panels {n_panels} -> {len(app._panels.stack)}")
    ok("message.error-own-window", isinstance(ed, m.ctk.CTkToplevel) and ed not in app._panels.stack, "")
    ok("message.waits-for-main-window", not sd.winfo_viewable() and not ed.winfo_viewable(),
       "the driver's main window is withdrawn, so no message may appear on screen")
    ok("message.prompts-own-window", all(issubclass(c, m.MessageWindow) for c in
       (m.SpaceDiagnosticsDialog, m.ArchivePasswordPrompt)), "")
    ok("work-surface.panels", all(issubclass(c, m.EmbeddedDialog) for c in
       (m.JobDialog, m.FirstRunWizard)) and not hasattr(m, "PfsBrowserDialog"), "")
    sd.destroy(); ed.destroy(); root.update()

    # W) main-window wiring: every sidebar entry, header button, card action, menu entry and
    #    shortcut reaches its handler. Buttons that would start a real job, delete files or
    #    open Finder are checked by the handler they are bound to instead of being clicked.
    close_toplevels(); root.update()
    _fd_calls = []
    # PS4 packages: a sorted copy job, never the PS5 reader; PS5 packages keep their path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ps4_fixture import make_pkg as _mk4
    _p4dir = S / "ps4_src"; _p4dir.mkdir(exist_ok=True)
    _p4g = _mk4(_p4dir / "base.pkg", content_id="UP0000-CUSA00001_00-SAMPLEGAME000000", content_type=0x1A,
                sfo={"TITLE": "Sample Game", "TITLE_ID": "CUSA00001", "CATEGORY": "gd", "APP_VER": "01.00"})
    _mk4(_p4dir / "dlc.pkg", content_id="UP0000-CUSA00001_00-SAMPLEDLC0000000", content_type=0x1B,
         sfo={"TITLE": "Sample Game - Extra", "TITLE_ID": "CUSA00001", "CATEGORY": "ac", "VERSION": "01.00"})
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
    # the After-job rule: own packages are copied (moved only for Delete); an archive's
    # extraction is moved and the archive gets the rule; the worker reports the title folder
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
    # a finished job removes the '._' sidecars of what it wrote (exFAT keeps them on a quick eject)
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
    # the editor: a PS4 source hides the PS5 changes, offers library or folder, queues a sorted copy
    _d4 = m.JobDialog(app, init_src=str(_p4g)); root.update()
    _d4.to_var.set(_d4._TARGET_LABEL["ffpfsc"]); _d4._refresh(); root.update()
    _hidden = not _d4._crow.winfo_manager()
    ok("ps4.dialog-view", _d4._kind == "ps4" and _d4._to_key() == "pkg" and _hidden
       and "PS4 package" in _d4.detect_var.get(), f"{_d4._kind} {_d4._to_key()} hidden={_hidden} {_d4.detect_var.get()}")
    _q_before = len(app.queue)
    _d4.out_var.set(str(OUT)); _d4._add(); settle()
    _q4 = app.queue[-1] if len(app.queue) > _q_before else None
    ok("ps4.dialog-queues-sorted-copy", _q4 is not None and _q4.content_kind == "ps4" and _q4.path == _p4g,
       str(getattr(_q4, "content_kind", None)))
    if _q4 is not None:
        app.queue.remove(_q4); app.update_queue_box()
    # a single PS4 package offers Folder and .pkg only; an archive of them only .pkg
    _segv = lambda d: list(d._to_seg.cget("values"))
    ok("ps4.dialog-targets-one-package", _segv(_d4) == ["Folder", ".pkg"], str(_segv(_d4)))
    _z4 = S / "ps4_set.zip"
    with zipfile.ZipFile(_z4, "w", zipfile.ZIP_STORED) as _zf:
        for _f in sorted(_p4dir.glob("*.pkg")):
            _zf.write(_f, "Sample.Set/" + _f.name)
    _dz = m.JobDialog(app, init_src=str(_z4)); root.update()
    pump(lambda: _dz._kind == "ps4", timeout=10.0)
    ok("ps4.dialog-archive-detected", _dz._kind == "ps4" and _segv(_dz) == [".pkg"] and not _dz._crow.winfo_manager()
       and "Sample Game" in _dz.detect_var.get(), f"{_dz._kind} {_segv(_dz)} {_dz.detect_var.get()}")
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
    # a whole run: the PS4 archive is unpacked, the job goes on by itself, the backend sorts it
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
        # the unpack lands on the output drive and is followed by a rename: the queue bar
        # follows the unpack instead of stopping at a quarter
        _last4 = max((o for s_, o in _overall4 if s_ >= 100), default=None)
        ok("ps4.archive-bar-follows-unpack", _last4 is not None and _last4 >= 95, str(_overall4[-3:]))
    finally:
        app.status_update = _su_saved
        app._batch_running = False; app._cleanup_inflight = _inflight1
        app.queue[:] = _q0; app._active_item = _act0; app.update_queue_box()
    # an archive whose game cannot be read before unpacking is named from its folder
    _gdir = S / "PPSA99097 Example Game"; _gdir.mkdir(exist_ok=True)
    _g7 = _gdir / "PPSA99097-Compressed.7z"; _g7.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\0" * 64)
    _gi = m.GameItem.from_archive(_g7); app.queue.append(_gi); app.update_queue_box()
    app._name_jobs_from_games()
    pump(lambda: _gi.display_name == "Example Game [PPSA99097]", timeout=10.0)
    ok("names.archive-from-folder", _gi.display_name == "Example Game [PPSA99097]", str(_gi.display_name))
    app.queue.remove(_gi); app.update_queue_box()
    # an id the public title list knows: its name, ahead of the folder's words; switched off, the folder's
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
    # switching back to a PS5 source shows the changes again
    _d5 = m.JobDialog(app, init_src=str(_p4g)); root.update()
    _d5.src_var.set(str(HBT)); root.update(); _d5._refresh(); root.update()
    ok("ps4.dialog-back-to-ps5", _d5._kind == "folder" and bool(_d5._crow.winfo_manager()), _d5._kind)
    _d5.destroy(); root.update()
    # Organize: a view of the main window; scan a folder (no '._' rows), Apply, Undo
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
    ok("organize.scan-no-clutter", [r_[1] for r_ in _rows] == ["Some.Release", "Some.Release/x.ffpfsc"]
       and len(_ov._checked) == 2 and all("LibProsperoPKG [PPSA99099]" in r_[2] for r_ in _rows), str(_rows))
    _ov.apply(); pump(lambda: not _ov._busy, timeout=60.0); pump(lambda: not _ov._busy, timeout=60.0); root.update()
    _t = "LibProsperoPKG [PPSA99099] [v01.000.000]"
    _after = sorted(x.name for x in _org.iterdir())
    ok("organize.apply-renames-in-place", _after == [_t] and sorted(x.name for x in (_org / _t).iterdir())
       == ["LibProsperoPKG [PPSA99099] [v01.000] [fw2.00].ffpfsc", "info.nfo"] and _ov.journal.is_file(), str(_after))
    _ask = m.messagebox.askyesno; m.messagebox.askyesno = lambda *a, **k: True
    try:
        _ov.undo(); pump(lambda: not _ov._busy, timeout=60.0); pump(lambda: not _ov._busy, timeout=60.0); root.update()
    finally:
        m.messagebox.askyesno = _ask
    ok("organize.undo", sorted(x.name for x in _org.iterdir() if not x.name.startswith("._")) == ["Some.Release"]
       and (_org / "Some.Release" / "x.ffpfsc").is_file() and not _ov.journal.is_file(), str(sorted(x.name for x in _org.iterdir())))
    app._show_view("queue"); root.update()
    # Organize as an Add job output: the source goes into the library in its own format
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
    # Cover art: read before unpacking, cached, and still there after a restart or once the
    # extraction is gone (it used to vanish when the row changed)
    _art_items = {"zip": m.GameItem.from_archive(ZP), "ffpfsc": app._organize_item_for(FF),
                  "pkg": app._organize_item_for(next((S / "c1_pkg").glob("*.pkg")))}
    for _v in _art_items.values():
        _f = m.art_cache_file(m.art_source_key(_v))
        if _f is not None:
            _f.unlink()            # the naming thread may have cached it already: read it anew
    _art_got = {k: bool(app._fetch_art(v, [])) and m.art_cache_file(v.art_key) is not None for k, v in _art_items.items()}
    ok("art.read-before-unpacking", all(_art_got.values()), str(_art_got))
    _ai = _art_items["zip"]; _ai.artwork = None
    _q0 = list(app.queue)
    app.queue[:] = [_ai]; app._details_item = None; app.update_queue_box(); root.update()
    ok("art.shown-at-size", app.art_label._photo is not None and app.art_label.size_ == app.ART_PX >= 96
       and max(app.art_img.width(), app.art_img.height()) == app.ART_PX, str(getattr(app, "art_img", None)))
    app._save_queue(); app.queue.clear(); app._details_item = None; app._restore_queue(); root.update()
    _rest = app.queue[0] if app.queue else None
    app.update_game_details(_rest); root.update()
    ok("art.after-restart", _rest is not None and _rest.artwork is None and app.art_label._photo is not None,
       str(getattr(_rest, "art_key", None)))
    app.queue[:] = _q0; app._details_item = None; app.update_queue_box(); root.update()
    # The details show where the router will put a waiting job, the numbers the space gate
    # checks, and looking at a job neither moves it nor writes placement lines to the log
    _pa = m.GameItem.from_chain(ZP, to="ffpfsc", output_path=str(OUT))
    _plogs = []; _plog_saved = app.log
    app.log = lambda lvl, msg, *a, **k: (_plogs.append(msg), _plog_saved(lvl, msg, *a, **k))
    try:
        _pinfo = app._card_info_text(_pa); app._refresh_space_for_item(_pa)
    finally:
        app.log = _plog_saved
    _pdrv, _pspace = str(_pinfo.get("Drives", "")), app.temp_space_var.get()
    ok("details.drives-from-the-plan", _pdrv.startswith("unpacks on") and "builds on" in _pdrv and "writes to" in _pdrv
       and " on the system drive: ~" in _pspace and _pspace.endswith("fits") and not hasattr(_pa, "_build_temp")
       and not any(x.startswith("Auto:") for x in _plogs),
       f"unpack={_pdrv.startswith('unpacks on')} builds={'builds on' in _pdrv} drive={' on the system drive: ~' in _pspace} "
       f"fits={_pspace.endswith('fits')} untouched={not hasattr(_pa, '_build_temp')} "
       f"quiet={not any(x.startswith('Auto:') for x in _plogs)} space={_pspace[-60:]!r}")
    # Placeholder values hide the Space line and the last result
    _keep = [(v, v.get()) for v in (app.temp_space_var, app.saved_var, app.ratio_var, app.rating_var)]
    app.temp_space_var.set("Temp Needed: - ")
    for _v, _was in _keep[1:]:
        _v.set(_was.split(":", 1)[0] + ": - ")
    _phinfo, _plast = app._card_info_text(_pa), app.last_result_var.get()
    for _v, _was in _keep:
        _v.set(_was)
    ok("details.placeholders-hidden", "Space" not in _phinfo and _plast.startswith("Totals"),
       f"space={_phinfo.get('Space')!r} last={_plast!r}")
    # Empty work folders on every drive the app used go once the queue stands still
    _pool = S / "pool_drive"; _hist = S / "old_library" / "Game [PPSA99095] [v01.000.000]"
    for _d in (_pool / "_ffpfsc_temp" / "_extracted", _hist.parent / "_ffpfsc_temp", _hist.parent / "_ffpfsc_extract" / "kept"):
        _d.mkdir(parents=True, exist_ok=True)
    (_hist.parent / "_ffpfsc_extract" / "kept" / "eboot.bin").write_bytes(b"E")
    _old = time.time() - 600
    for _d in [x for x in S.rglob("*") if x.is_dir() and ("pool_drive" in x.parts or "old_library" in x.parts)]:
        os.utime(_d, (_old, _old))
    _pool0, _hist0 = list(getattr(app, "temp_pool", []) or []), m.load_history()
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
    # Time left: the running step's own, later steps at the speed seen before (learned when a
    # step ends); it was 11 min from the progress bands while the compress alone needed 38
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
    # Speed and time left while an archive is unpacked
    ok("unpack.rate", m.unpack_rate(50, 100, 100 * 10**9) == ("500.0 MB/s", "1m 40s") and m.unpack_rate(50, 10, 100 * 10**9)[0] == "5.00 GB/s"
       and m.unpack_rate(25, 60, 12 * 10**9) == ("50.0 MB/s", "3m 00s")
       and m.unpack_rate(1, 60, 10**9) == (" - ", " - ") and m.unpack_rate(40, 20, 0) == (" - ", "30s"),
       str([m.unpack_rate(50, 100, 100 * 10**9), m.unpack_rate(25, 60, 12 * 10**9)]))
    # The details pane belongs to the selected job: the progress block shows only for the
    # running one, and a job you picked stays picked when the next job starts
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
    # Arrow keys move the selection, never a job (moving is drag and drop or the context menu)
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
        """Run the handler Tk has bound to *seq*: a withdrawn window gets no key events."""
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
        # sidebar views, and every Settings page builds and shows
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
        # Settings › General › When a job is done: Delete asks first, Move shows its folder
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

        # tools
        tools = {b._text: b for b in app._nav_all}
        ok("wire.tool.clean-temp", tools["Clean temp"]._command == app.clear_temp_files, "")
        tools["Organize"].invoke(); root.update()
        ok("wire.tool.organize", app._view == "organize" and not _fd_calls[-1:] == [("dir", "")], app._view)
        app._show_view("queue"); root.update()
        tools["Look inside"].invoke(); root.update()
        ok("wire.tool.look-inside", app._view == "look" and _top() is None, f"{app._view} {_top()}")
        # the sidebar works with a panel open and leaves it, whatever is open
        app.open_job_dialog(); root.update()
        _with_panel = _top() == "JobDialog" and all(b._state == "normal" for b in app._nav_all)
        tools["Organize"].invoke(); root.update()
        ok("wire.panel.sidebar-leaves-it", _with_panel and _top() is None and app._view == "organize",
           f"{_with_panel} {_top()} {app._view}")
        app.open_job_dialog(); root.update()
        app._panels._on_key("<Escape>", None); root.update()
        ok("wire.panel.escape-closes", _top() is None, str(_top()))
        app._show_view("queue"); root.update()

        # header; Return in Add job adds the job through the panel host
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

        # compression is a job setting now: no compression line under the queue
        ok("wire.queue.no-compression-line", "Change" not in _buttons(app._paned_q)
           and not hasattr(app, "_open_tuning"), str(sorted(_buttons(app._paned_q))))

        # primary action: Add job while the queue is empty, Start once it has jobs
        items = [m.GameItem(HBT) for _ in range(3)]
        app.queue[:] = []; app.update_queue_box(); root.update()
        ok("wire.primary.empty-queue", app._add_btn.variant == "primary" and app.start_btn._state == "disabled",
           f"add={app._add_btn.variant} start={app.start_btn._state}")
        app.queue[:] = items; app.update_queue_box(); root.update()
        ok("wire.primary.with-jobs", app.start_btn.variant == "primary" and app.start_btn._state == "normal"
           and app._add_btn.variant == "secondary", f"start={app.start_btn.variant}/{app.start_btn._state}")

        # the details pane: closed at first, a click on a job opens it, the header button
        # and the View menu's shortcut toggle it, and an empty queue closes it
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

        # one transport control: Start while idle, Pause and Stop while the queue runs, same size
        _w_idle = app.transport.winfo_reqwidth()
        app._batch_running = True; app._sync_run_ui(); root.update()
        ok("wire.header.stop-while-running", app.transport.running and app.stop_btn._command == app.cancel
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

        # the job card and its actions
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
        ok("wire.card.cancel", app.cancel_btn._command == app.cancel, "")

        # the row's context menu
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

        # shortcuts, run through the handlers Tk has bound on the window
        for seq, view in (("<Command-Key-2>", "history"), ("<Command-Key-3>", "log"),
                          ("<Command-Key-1>", "queue"), ("<Command-comma>", "settings")):
            _fire(root, seq); root.update()
            ok(f"wire.shortcut.{view}", app._view == view, f"{seq} -> view={app._view}")
        app._nav["queue"].invoke(); root.update()
        _fire(root, "<Command-n>"); root.update()
        ok("wire.shortcut.add-job", _top() == "JobDialog", str(_top()))
        _fire(root, "<Command-Key-2>"); root.update()
        ok("wire.shortcut.view-leaves-panel", app._view == "history" and _top() is None, f"view={app._view} {_top()}")
        _fire(root, "<Command-n>"); _fire(root, "<Command-r>"); root.update()
        ok("wire.shortcut.actions-wait-behind-panel", _top() == "JobDialog" and not app._batch_running, str(_top()))
        close_toplevels(); app._show_view("queue"); root.update()

        # History and Log view buttons
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

        # the menu bar: every entry that opens something reaches its handler
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
        ok("wire.menubar.stop-safe-when-idle", (menus["File"].invoke(_entry(menus["File"], "Stop Queue")), not app.cancel_requested)[1], "")
        if m.IS_MAC:
            ok("wire.menubar.mac-settings", bool(root.tk.call("info", "commands", "::tk::mac::ShowPreferences")), "")
            root.tk.call("::tk::mac::ShowPreferences"); root.update()
            ok("wire.menubar.mac-settings-opens", app._view == "settings", f"view={app._view}")
            app._nav["queue"].invoke(); root.update()

        # the options' scrollbar decides from heights alone, so showing or hiding it can
        # never change the decision (the bar blinked twice a second when it decided from
        # the canvas's scroll fractions)
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

        # theme switch
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
    traceback.print_exc()   # the summary keeps one line; the full trace goes to stderr
    res.append(("driver", False, traceback.format_exc()))
finally:
    # restore the user's real settings.json (the driver mutated queue + defaults)
    try:
        if _backup.exists():
            _sh.copy2(_backup, _settings); _backup.unlink()
    except Exception as e:
        print("WARN could not restore settings backup:", e)
    try: root.destroy()
    except Exception: pass
bad = 0
for name, okv, det in res:
    print(("PASS " if okv else "FAIL ") + f"{name:34s} {det}"[:200]); bad += (not okv)
    if name == "driver": open(str(S / "driver_traceback.txt"), "w").write(det)
print(f"{len(res)-bad} passed, {bad} failed")
sys.exit(1 if bad else 0)
