import os
import tempfile
import unittest

from amem.store import Observation, Store, bigrams, has_cjk


class StoreTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = Store(self.path)
        self.sid = self.store.start_session("dsh", "demo", "/tmp/demo")

    def tearDown(self):
        self.store.close()
        os.remove(self.path)

    def test_session_roundtrip(self):
        s = self.store.get_session(self.sid)
        self.assertEqual(s["harness"], "dsh")
        self.assertIsNone(s["ended_at"])
        self.store.end_session(self.sid)
        self.assertIsNotNone(self.store.get_session(self.sid)["ended_at"])

    def test_observation_tags_are_json(self):
        oid = self.store.add_observation(
            self.sid, Observation(kind="decision", title="用 SQLite", body="本地优先", tags=["db", "架构"])
        )
        got = self.store.get_observation(oid)
        self.assertEqual(got["tags"], ["db", "架构"])
        self.assertEqual(got["kind"], "decision")

    def test_english_search_uses_fts(self):
        self.assertTrue(self.store.stats()["fts"])
        self.store.add_observation(
            self.sid, Observation(kind="bugfix", title="FTS tokenizer", body="unicode61 swallows CJK runs")
        )
        hits = self.store.search("tokenizer")
        self.assertEqual(len(hits), 1)
        self.assertIn("FTS", hits[0]["title"])

    def test_chinese_search_matches_substring(self):
        self.store.add_observation(
            self.sid, Observation(kind="fact", title="架构选择", body="用 FTS5 做全文检索，中文走 bigram")
        )
        for q in ("架构", "检索", "中文走"):
            with self.subTest(q=q):
                hits = self.store.search(q)
                self.assertEqual(len(hits), 1, f"{q} should hit")
                self.assertGreaterEqual(hits[0]["score"], 60)

    def test_empty_query(self):
        self.assertEqual(self.store.search("   "), [])

    def test_timeline_window(self):
        ids = self.store.add_many(self.sid, [Observation(kind="fact", title=f"o{i}", body="x") for i in range(7)])
        window = self.store.timeline(ids[3], before=1, after=1)
        self.assertEqual([w["id"] for w in window], [ids[2], ids[3], ids[4]])

    def test_summary_latest(self):
        self.store.set_summary(self.sid, "first")
        self.store.set_summary(self.sid, "second", obs_count=3)
        got = self.store.latest_summary(self.sid)
        self.assertEqual(got["text"], "second")
        self.assertEqual(got["obs_count"], 3)

    def test_embeddings_roundtrip(self):
        oid = self.store.add_observation(self.sid, Observation(kind="fact", title="t", body="b"))
        self.store.put_embedding(oid, [0.1, 0.2, 0.3], "tfidf")
        pairs = self.store.embeddings("tfidf")
        self.assertEqual(pairs[0][0], oid)
        self.assertAlmostEqual(pairs[0][1][1], 0.2)


class HelperTest(unittest.TestCase):
    def test_bigrams(self):
        self.assertEqual(bigrams("本地优先"), {"本地", "地优", "优先"})
        self.assertEqual(bigrams("a"), {"a"})
        self.assertEqual(bigrams(""), {""})

    def test_has_cjk(self):
        self.assertTrue(has_cjk("本地"))
        self.assertTrue(has_cjk("テスト"))
        self.assertFalse(has_cjk("local first"))


if __name__ == "__main__":
    unittest.main()
