"""Game names by title id from the public title lists (cached, refreshed weekly). The tests
never touch the network: title_db.FETCH is replaced."""
import os, shutil, sys, tempfile, time, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import title_db

PS5 = ("titleId\tconceptId\tname\tcontentId\tregion\tpublisherId\n"
       "PPSA00001_00\t1\tサンプル\tJP0001-PPSA00001_00-SAMPLE0000000000\tJP\tJP0001\n"
       "PPSA00001_00\t1\tSample Quest®\tEP0001-PPSA00001_00-SAMPLE0000000000\tEP\tEP0001\n"
       "PPSA00001_00\t1\tSample Quest™ (US)\tUP0001-PPSA00001_00-SAMPLE0000000000\tUP\tUP0001\n"
       "PPSA00002_00\t2\tOther Game\tEP0002-PPSA00002_00-OTHER00000000000\tEP\tEP0002\n")
PS4 = ("titleId\tconceptId\tname\tcontentId\tregion\tpublisherId\n"
       "CUSA00001_00\t3\tOld Game\tUP0003-CUSA00001_00-OLDGAME000000000\tUP\tUP0003\n")


class TitleDb(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.calls = []
        self._fetch = title_db.FETCH
        title_db.FETCH = self.fake
        title_db.reset()
        self.answer = {"PS5": PS5, "PS4": PS4}

    def tearDown(self):
        title_db.FETCH = self._fetch
        title_db.reset()
        shutil.rmtree(self.dir, ignore_errors=True)

    def fake(self, url):
        self.calls.append(url)
        a = self.answer["PS5" if "PS5" in url else "PS4"]
        if isinstance(a, Exception):
            raise a
        return a.encode("utf-8")

    def test_english_name_first_and_both_lists(self):
        self.assertEqual(title_db.lookup("PPSA00001", self.dir), "Sample Quest™ (US)")
        self.assertEqual(title_db.lookup("ppsa00002", self.dir), "Other Game")
        self.assertEqual(title_db.lookup("CUSA00001", self.dir), "Old Game")
        self.assertIsNone(title_db.lookup("PPSA99999", self.dir))
        self.assertIsNone(title_db.lookup("ABCD00001", self.dir))
        self.assertEqual(len(self.calls), 2)            # one download per list, then the cache

    def test_offline_means_cache_only(self):
        self.assertIsNone(title_db.lookup("PPSA00001", self.dir, online=False))
        self.assertEqual(self.calls, [])
        title_db.lookup("PPSA00001", self.dir)
        title_db.reset()
        self.assertEqual(title_db.lookup("PPSA00001", self.dir, online=False), "Sample Quest™ (US)")

    def test_weekly_refresh_and_a_failed_one_keeps_the_old_list(self):
        title_db.lookup("PPSA00001", self.dir)
        f = self.dir / "PS5_Titles.tsv"
        old = time.time() - 8 * 86400
        os.utime(f, (old, old))
        title_db.reset()
        self.answer["PS5"] = OSError("offline")
        self.assertEqual(title_db.lookup("PPSA00001", self.dir), "Sample Quest™ (US)")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(title_db.lookup("PPSA00002", self.dir), "Other Game")
        self.assertEqual(len(self.calls), 2)            # no new attempt right after a failure

    def test_something_else_than_a_list_is_not_kept(self):
        self.answer["PS5"] = "<html>rate limited</html>"
        self.assertIsNone(title_db.lookup("PPSA00001", self.dir))
        self.assertFalse((self.dir / "PS5_Titles.tsv").exists())

    def test_title_id_from_names(self):
        self.assertEqual(title_db.title_id_in("[site]-PPSA01234.part1.rar", "x"), "PPSA01234")
        self.assertEqual(title_db.title_id_in("archive.7z", "CUSA00042 Something"), "CUSA00042")
        self.assertEqual(title_db.title_id_in("archive.7z", "folder"), "")


if __name__ == "__main__":
    unittest.main()
