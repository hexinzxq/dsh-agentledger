import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from amem.cli import build_parser, main


def run(argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class CLITest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, self.db)

    def cli(self, *args) -> tuple[int, str, str]:
        return run(["--db", self.db, *args])

    def test_init_on_empty_db(self):
        code, out, _ = self.cli("init")
        self.assertEqual(code, 0)
        self.assertIn("观察 0", out)
        self.assertIn("FTS5 开", out)

    def test_add_then_search(self):
        code, out, _ = self.cli("add", "中文搜索改成 bigram", "--kind", "bugfix",
                                "--body", "FTS5 分词器不行", "--tag", "search,cjk")
        self.assertEqual(code, 0)
        self.assertIn("记下了 #1", out)
        code, out, _ = self.cli("search", "分词")
        self.assertIn("中文搜索改成 bigram", out)
        self.assertIn("bugfix", out)

    def test_search_miss_has_hint(self):
        self.cli("init")
        code, out, _ = self.cli("search", "根本不存在的东西")
        self.assertEqual(code, 0)
        self.assertIn("没搜到", out)

    def test_search_semantic_after_reindex(self):
        self.cli("add", "把向量换成本地哈希", "--kind", "decision")
        code, out, err = self.cli("search", "哈希", "--semantic")
        self.assertEqual(code, 0, err)

    def test_timeline_marks_target(self):
        self.cli("add", "第一条", "--kind", "fact")
        self.cli("add", "第二条", "--kind", "fact")
        code, out, _ = self.cli("timeline", "1")
        self.assertEqual(code, 0)
        self.assertIn("→ #1", out)
        self.assertIn("#2", out)

    def test_timeline_missing_id(self):
        self.cli("init")
        code, _, err = self.cli("timeline", "42")
        self.assertEqual(code, 1)
        self.assertIn("没有 #42", err)

    def test_summarize_session(self):
        self.cli("add", "选了 SQLite", "--kind", "decision")
        code, out, _ = self.cli("summarize", "1")
        self.assertEqual(code, 0)
        self.assertIn("做了什么决定", out)
        self.assertIn("选了 SQLite", out)

    def test_ingest_jsonl(self):
        path = Path(self.db).with_suffix(".jsonl")
        path.write_text(json.dumps({"role": "assistant", "content": [
            {"type": "tool_use", "name": "bash",
             "input": {"command": "python3 -m unittest"}},
            {"type": "tool_result", "is_error": True,
             "content": "AssertionError: 1 != 2"},
        ]}, ensure_ascii=False) + "\n", encoding="utf-8")
        self.addCleanup(path.unlink)
        code, out, err = self.cli("ingest", str(path))
        self.assertEqual(code, 0, err)
        self.assertIn("观察", out)
        self.assertIn("翻车", out)

    def test_ingest_missing_file(self):
        code, _, err = self.cli("ingest", "/nope/missing.jsonl")
        self.assertEqual(code, 2)
        self.assertIn("文件不存在", err)

    def test_export_writes_file(self):
        self.cli("add", "一条要导出的记忆")
        out_path = Path(self.db).with_suffix(".json")
        self.addCleanup(out_path.unlink, True)
        code, out, _ = self.cli("export", "--out", str(out_path))
        self.assertEqual(code, 0)
        data = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["observations"]), 1)
        self.assertEqual(data["observations"][0]["title"], "一条要导出的记忆")

    def test_parser_requires_subcommand(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            build_parser().parse_args([])


if __name__ == "__main__":
    unittest.main()
