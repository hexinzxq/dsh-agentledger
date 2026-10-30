import json
import os
import tempfile
import unittest

try:
    from fastapi.testclient import TestClient

    from amem.server import create_app, render_markdown_summary
    HAS_SERVER = True
except SystemExit:  # fastapi not installed
    HAS_SERVER = False
except ImportError:
    HAS_SERVER = False


@unittest.skipUnless(HAS_SERVER, "server extras not installed")
class ServerTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.client = TestClient(create_app(self.db))

    def tearDown(self):
        self.client.app.state.store.close()
        os.remove(self.db)

    def new_session(self, **kw):
        body = {"harness": "dsh", "project": "demo", **kw}
        return self.client.post("/api/sessions", json=body).json()["id"]

    def test_health_reports_counts(self):
        data = self.client.get("/api/health").json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["observations"], 0)

    def test_session_lifecycle_and_observations(self):
        sid = self.new_session()
        r = self.client.post(f"/api/sessions/{sid}/observations",
                             json={"kind": "decision", "title": "用 SQLite",
                                   "body": "本地优先，不引服务"})
        self.assertEqual(r.status_code, 201)
        oid = r.json()["id"]
        self.assertEqual(self.client.get(f"/api/observations/{oid}").json()["title"], "用 SQLite")
        self.assertEqual(len(self.client.get("/api/sessions").json()["items"]), 1)
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/end").status_code, 200)

    def test_missing_session_is_404(self):
        self.assertEqual(self.client.get("/api/sessions/99/observations").status_code, 404)
        self.assertEqual(self.client.post("/api/sessions/99/observations",
                                          json={"title": "x"}).status_code, 404)

    def test_search_modes(self):
        sid = self.new_session()
        self.client.post(f"/api/sessions/{sid}/observations",
                         json={"kind": "bugfix", "title": "中文分词修好了",
                               "body": "FTS5 改成 bigram 检索"})
        kw = self.client.get("/api/search", params={"q": "分词"}).json()
        self.assertEqual(kw["mode"], "keyword")
        self.assertEqual(len(kw["items"]), 1)
        sem = self.client.get("/api/search", params={"q": "分词", "mode": "semantic"}).json()
        self.assertEqual(sem["mode"], "semantic")
        self.assertGreaterEqual(len(sem["items"]), 0)

    def test_summarize_writes_summary(self):
        sid = self.new_session()
        self.client.post(f"/api/sessions/{sid}/observations",
                         json={"kind": "bug", "title": "连接跨线程崩了"})
        out = self.client.post(f"/api/sessions/{sid}/summarize").json()
        self.assertIn("踩了什么坑", out["summary"])
        self.assertEqual(len(self.client.get("/api/summaries").json()["items"]), 1)

    def test_summarize_empty_session_is_400(self):
        sid = self.new_session()
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/summarize").status_code, 400)

    def test_ingest_transcript(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"role": "user", "content": "帮我把测试跑通"}) + "\n")
            f.write(json.dumps({"role": "assistant", "content": [
                {"type": "tool_use", "name": "bash",
                 "input": {"command": "python3 -m pytest"}}]}) + "\n")
            f.write(json.dumps({"role": "user", "content": [
                {"type": "tool_result", "is_error": True,
                 "content": "ModuleNotFoundError: No module named 'pytest'"}]}) + "\n")
        try:
            out = self.client.post("/api/ingest",
                                   json={"transcript": path, "harness": "dsh"}).json()
            self.assertEqual(out["events"], 3)
            self.assertGreaterEqual(out["observations"], 1)
            self.assertEqual(out["indexed"], out["observations"])
        finally:
            os.remove(path)

    def test_static_assets_served(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertIn("Agent", self.client.get("/").text)
        self.assertEqual(self.client.get("/app.js").status_code, 200)
        self.assertEqual(self.client.get("/style.css").status_code, 200)

    def test_export_shape(self):
        sid = self.new_session()
        self.client.post(f"/api/sessions/{sid}/observations", json={"title": "一条记忆"})
        data = self.client.get("/api/export").json()
        self.assertEqual(len(data["observations"]), 1)
        self.assertEqual(data["sessions"][0]["id"], sid)


class SummaryRenderTest(unittest.TestCase):
    def test_sections_and_empty_markers(self):
        obs = [{"kind": "decision", "title": "选了 SQLite"},
               {"kind": "todo", "title": "补 CLI"}]
        text = render_markdown_summary(obs) if HAS_SERVER else ""
        if not HAS_SERVER:
            self.skipTest("server extras not installed")
        self.assertIn("- 选了 SQLite", text)
        self.assertIn("- 补 CLI", text)
        self.assertIn("- 无", text)


if __name__ == "__main__":
    unittest.main()
