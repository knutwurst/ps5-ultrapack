"""PS5 repair: the checks and repairs that need no package (firmware, keystone, param.json)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import ps5_repair as r  # noqa: E402


def _keystone(fmt: int = 3, fp: bytes = bytes(range(64, 96))) -> bytes:
    return b"keystone" + bytes([fmt, 0, 1, 0]) + bytes(20) + fp + bytes(32)


class Firmware(unittest.TestCase):
    def test_param_words_and_targets(self):
        self.assertEqual(r.fw_text("0x0450000000000000"), "4.50")
        self.assertEqual(r.fw_text("0x1060000000000000"), "10.60")
        self.assertEqual(r.fw_text("rubbish"), "")
        self.assertEqual(r.fw_tuple("7.61"), (7, 61))
        self.assertEqual(r.fw_tuple("10.xx"), (10, 0))
        self.assertIsNone(r.fw_tuple("seven"))

    def test_newer_firmware_than_the_console_is_a_warning_with_a_fix(self):
        info = {"param": {"requiredSystemSoftwareVersion": "0x0900000000000000"}}
        row = next(x for x in r.findings(info, "7.61") if x["check"] == "console.firmware")
        self.assertEqual((row["level"], row["fix"]), ("warn", r.FIX_FIRMWARE))
        self.assertEqual(row["text"], "needs system software 9.00, the console runs 7.61")
        row = next(x for x in r.findings(info, "10.xx") if x["check"] == "console.firmware")
        self.assertEqual(row["level"], "pass")


class Keystone(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(r.keystone_problem(_keystone()), "")
        self.assertIn("PS4 keystone", r.keystone_problem(_keystone(fmt=2)))
        self.assertIn("96 bytes", r.keystone_problem(_keystone()[:-1]))
        self.assertIn("does not start", r.keystone_problem(b"x" * 96))
        default = _keystone(fp=bytes.fromhex(r.DEFAULT_KEYSTONE_FINGERPRINT))
        self.assertIn("default keystone", r.keystone_problem(default))

    def test_sources(self):
        ks = _keystone()
        self.assertEqual(r.keystone_from(ks.hex())[0], ks)
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "keystone"
            f.write_bytes(ks)
            self.assertEqual(r.keystone_from(str(f))[0], ks)
            game = Path(d) / "game"
            (game / "sce_sys").mkdir(parents=True)
            self.assertIn("no sce_sys/keystone", r.keystone_from(str(game))[1])
            (game / "sce_sys" / "keystone").write_bytes(ks)
            self.assertEqual(r.keystone_from(str(game))[0], ks)
            img = Path(d) / "game.ffpfsc"
            img.write_bytes(b"\0" * 16)
            self.assertIn("cannot read inside", r.keystone_from(str(img))[1])
            got = r.keystone_from(str(img), image_member=lambda p, m, dest: str(f))
            self.assertEqual(got[0], ks)
        self.assertIsNone(r.keystone_from("/no/such/thing")[0])

    def test_default_and_missing_keystone_are_flagged(self):
        rows = r.findings({"keystone": True, "keystone_fp": r.DEFAULT_KEYSTONE_FINGERPRINT})
        self.assertEqual([(x["level"], x["fix"]) for x in rows if x["check"] == "image.keystone"],
                         [("warn", r.FIX_KEYSTONE)])
        rows = r.findings({"keystone": False})
        self.assertEqual(rows[0]["fix"], r.FIX_KEYSTONE)
        rows = r.findings({"keystone": True, "keystone_fp": "AB" * 32})
        self.assertEqual(rows[0]["level"], "pass")


class ParamJson(unittest.TestCase):
    PARAM = {"titleId": "PPSA00001", "versionFileUri": "http://example.invalid/v.xml",
             "originContentVersion": "01.000.000", "targetContentVersion": "01.010.000",
             "addcont": {"serviceIdForSharing": ["UP0000-PPSA00001_00"]}}

    def test_standalone_clears_url_markers_and_sharing(self):
        p = json.loads(json.dumps(self.PARAM))
        self.assertEqual(sorted(r.make_standalone(p)), sorted(["originContentVersion", "targetContentVersion",
                                                                "versionFileUri", "addcont.serviceIdForSharing"]))
        self.assertEqual(p, {"titleId": "PPSA00001", "versionFileUri": "", "addcont": {"serviceIdForSharing": []}})
        self.assertEqual(r.make_standalone(p), [])

    def test_repair_folder_writes_only_when_something_changes(self):
        with tempfile.TemporaryDirectory() as d:
            pj = Path(d) / "sce_sys" / "param.json"
            pj.parent.mkdir()
            pj.write_text(json.dumps(self.PARAM), encoding="utf-8")
            self.assertEqual(r.repair_folder(d, log=lambda _l: None), ["standalone"])
            self.assertEqual(json.loads(pj.read_text())["versionFileUri"], "")
            self.assertEqual(r.repair_folder(d, log=lambda _l: None), [])

    def test_findings_for_a_full_game_and_a_patch(self):
        rows = {x["check"]: x for x in r.findings({"param": self.PARAM, "sfo": True})}
        self.assertEqual((rows["param.patch"]["level"], rows["param.patch"]["fix"]), ("warn", r.FIX_REBUILD))
        self.assertEqual((rows["param.versionFileUri"]["level"], rows["param.versionFileUri"]["fix"]),
                         ("info", r.FIX_STANDALONE))
        self.assertEqual(rows["image.param.sfo"]["level"], "fail")
        patch = {x["check"] for x in r.findings({"param": self.PARAM, "ident": {"package_type": "PatchDebug"}})}
        self.assertFalse({"param.patch", "param.versionFileUri"} & patch)

    def test_validate_rows_map_to_repairs_and_sort_by_level(self):
        info = {"results": [
            {"Check": "file", "Level": "pass", "Message": "1 bytes"},
            {"Check": "cnt.playgo-layout", "Level": "fail", "Message": "the set does not describe this image"},
            {"Check": "image.icon0.png", "Level": "warn", "Message": "interlaced"},
            {"Check": "eboot.magic", "Level": "fail", "Message": "raw ELF (0x7F454C46) - not fake-signed"},
            {"Check": "cnt.wrap", "Level": "fail", "Message": "did not verify"}]}
        rows = r.findings(info)
        self.assertEqual([x["level"] for x in rows], ["fail", "fail", "fail", "warn", "pass"])
        fix = {x["check"]: x["fix"] for x in rows}
        self.assertEqual(fix, {"file": None, "cnt.playgo-layout": r.FIX_PLAYGO, "image.icon0.png": r.FIX_REBUILD,
                               "eboot.magic": r.FIX_REBUILD, "cnt.wrap": None})
        s = r.summary(rows)
        self.assertEqual((s["fail"], s["warn"], s["unfixable"], s["fixes"]),
                         (3, 1, 1, {r.FIX_PLAYGO, r.FIX_REBUILD}))


def _ucp(entries, good=True) -> bytes:
    """A UCP archive built from the documented layout."""
    import hashlib
    import struct
    entries = sorted(entries)
    table_end = 0x60 + 0x40 * len(entries)
    recs, blobs, off = b"", b"", table_end
    for name, data in entries:
        recs += name.encode().ljust(0x20, b"\0") + struct.pack(">QQ", off, len(data)) + bytes(16)
        pad = (len(data) // 16 + 1) * 16 - len(data)
        blobs += data + bytes(pad)
        off += len(data) + pad
    buf = bytearray(struct.pack(">IIQII", 0xB228C60A, 1, table_end + len(blobs), len(entries), 0x40)
                    + bytes(0x60 - 0x18) + recs + blobs)
    buf[0x1C:0x30] = hashlib.sha1(bytes(buf)).digest() if good else b"\1" * 20
    return bytes(buf)


def _ampr_index(rows: dict) -> bytes:
    """An AMPRIDX3 index of {path: size} (records and blob only; the hash table is not read)."""
    import struct
    keys = sorted(rows)
    blob, recs = b"", b""
    for k in keys:
        recs += struct.pack("<IIQq", len(blob), len(k), rows[k], 0)
        blob += k.encode() + b"\0"
    return struct.pack("<8sIIQQQII", b"AMPRIDX3", 3, 24, len(keys), len(blob), 48 + len(recs) + len(blob), 16, 0) \
        + recs + blob


class Trophies(unittest.TestCase):
    def test_structure_and_digest(self):
        good = _ucp([("trophy.json", b"{}"), ("icon0.png", b"x" * 40)])
        self.assertEqual(r.ucp_problem(good), "")
        self.assertTrue(r.ucp_digest_ok(good))
        bad = _ucp([("trophy.json", b"{}")], good=False)
        self.assertEqual(r.ucp_problem(bad), "")
        self.assertFalse(r.ucp_digest_ok(bad))
        self.assertIn("size field", r.ucp_problem(good + b"\0" * 16))
        self.assertEqual(r.ucp_problem(b"\0" * 0x60), "not a UCP archive")

    def test_repair_writes_only_wrong_checksums(self):
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "sce_sys" / "trophy2"
            t.mkdir(parents=True)
            (t / "trophy00.ucp").write_bytes(_ucp([("a", b"1")], good=False))
            (t / "trophy01.ucp").write_bytes(_ucp([("a", b"1")]))
            before = (t / "trophy01.ucp").read_bytes()
            self.assertEqual(r.repair_folder(d, log=lambda _l: None, standalone=False, trophies=True), ["trophies"])
            self.assertTrue(r.ucp_digest_ok((t / "trophy00.ucp").read_bytes()))
            self.assertEqual((t / "trophy01.ucp").read_bytes(), before)
            self.assertEqual(r.repair_folder(d, log=lambda _l: None, standalone=False, trophies=True), [])

    def test_findings(self):
        info = {"ucp": [{"path": "sce_sys/trophy2/trophy00.ucp", "problem": "", "digest_ok": False},
                        {"path": "sce_sys/uds/uds00.ucp", "problem": "", "digest_ok": True},
                        {"path": "sce_sys/trophy2/trophy01.ucp", "problem": "not a UCP archive", "digest_ok": False}]}
        rows = {x["check"]: x for x in r.findings(info)}
        self.assertEqual((rows["ucp.trophy00.ucp"]["fix"], rows["ucp.trophy00.ucp"]["group"]), (r.FIX_TROPHY, "Trophies"))
        self.assertEqual((rows["ucp.uds00.ucp"]["level"], rows["ucp.uds00.ucp"]["group"]), ("pass", "Activities"))
        self.assertEqual((rows["ucp.trophy01.ucp"]["level"], rows["ucp.trophy01.ucp"]["fix"]), ("fail", None))


class AmprIndex(unittest.TestCase):
    def test_index_against_the_files(self):
        idx = r.ampr_index_rows(_ampr_index({"/app0/eboot.bin": 10, "/app0/data/a.bin": 5,
                                             "/app0/sce_sys/param.json": 7}))
        self.assertEqual(idx["/app0/data/a.bin"], 5)
        files = {"eboot.bin": 10, "data/a.bin": 5, "sce_sys/param.json": 9, "ampr_emu.index": 99}
        self.assertEqual(r.ampr_mismatch(idx, files), [])          # sce_sys is not compared
        self.assertEqual(r.ampr_mismatch(idx, dict(files, **{"data/a.bin": 6, "data/b.bin": 1})),
                         ["data/a.bin", "data/b.bin"])
        with self.assertRaises(ValueError):
            r.ampr_index_rows(b"nope")

    def test_findings(self):
        def lv(ampr):
            row = next(x for x in r.findings({"ampr": ampr}) if x["check"] == "ampr.index")
            return row["level"], row["fix"]
        self.assertEqual(lv({"emu": True, "index": False, "bad": []}), ("fail", r.FIX_REBUILD))
        self.assertEqual(lv({"emu": True, "index": True, "bad": ["data/a.bin"]}), ("fail", r.FIX_REBUILD))
        self.assertEqual(lv({"emu": False, "index": True, "bad": ["data/a.bin"]}), ("warn", None))
        self.assertEqual(lv({"emu": True, "index": True, "bad": []}), ("pass", None))
        self.assertFalse([x for x in r.findings({"ampr": {"emu": False, "index": False, "bad": []}})
                          if x["check"] == "ampr.index"])


class UpdatePackages(unittest.TestCase):
    UPD = {"ident": {"package_type": "Delta"}, "param": {"contentVersion": "01.010.000",
                                                          "originContentVersion": "01.000.000"}}

    def test_an_update_is_checked_against_the_game_next_to_it(self):
        game = {"update": False, "version": "01.000.000", "name": "game.pkg", "origin": ""}
        rows = {x["check"]: x for x in r.findings(dict(self.UPD, related=[game]))}
        self.assertEqual(rows["update.version"]["level"], "pass")
        self.assertNotIn("update.origin", rows)
        rows = {x["check"]: x for x in r.findings(dict(self.UPD, related=[dict(game, version="01.020.000")]))}
        self.assertEqual(rows["update.version"]["level"], "fail")
        self.assertEqual(rows["update.origin"]["level"], "warn")
        rows = {x["check"]: x for x in r.findings(dict(self.UPD, related=[]))}
        self.assertEqual(rows["update.base"]["level"], "info")

    def test_an_update_gets_no_repair(self):
        info = dict(self.UPD, results=[{"Check": "image.icon0.png", "Level": "warn", "Message": "interlaced"}],
                    keystone=False, sfo=True)
        self.assertTrue(r.is_update(info))
        self.assertFalse([x for x in r.findings(info) if x["fix"]])
        self.assertTrue(r.is_update({"ident": {"entries": [{"name": "target-deltainfo.dat"}]}}))
        self.assertFalse(r.is_update({"ident": {"package_type": "FullDebug"}}))

    def test_a_game_checks_the_updates_next_to_it(self):
        game = {"ident": {"package_type": "FullDebug"}, "param": {"contentVersion": "01.000.000"}}
        upd = {"update": True, "version": "01.010.000", "origin": "01.000.000", "name": "u.pkg"}
        rows = {x["check"]: x for x in r.findings(dict(game, related=[upd]))}
        self.assertEqual(rows["update.u.pkg"]["level"], "pass")
        rows = {x["check"]: x for x in r.findings(dict(game, related=[dict(upd, origin="01.005.000")]))}
        self.assertEqual(rows["update.u.pkg"]["level"], "warn")
        rows = {x["check"]: x for x in r.findings(dict(game, related=[dict(upd, version="01.000.000")]))}
        self.assertEqual(rows["update.u.pkg"]["level"], "fail")


if __name__ == "__main__":
    unittest.main()
