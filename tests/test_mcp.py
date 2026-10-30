import io
import json
import os
import tempfile
import unittest

from amem.mcp import MemoryServer, handle, serve


class MCPTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.server = MemoryServer(self.path)
        self.server.tool_save({"kind": "decision", "title": "用 SQLite",
                               "body": "因为要本地跑，不依赖服务", "tags": ["db"]})
        self.server.tool_save({"kind": "bugfix", "title": "中文搜索修好了",
                               "body": "FTS5 unicode61 分词器问题，改 bigram"})

    def tearDown(self):
        self.server.store.close()
        os.remove(self.path)

    def call(self, method, params=None, req_id=1):
        return handle({"jsonrpc": "2.0", "id": req_id, "method": method,
                       "params": params or {}}, self.server)

    def test_initialize(self):
        resp = self.call("initialize")
        self.assertEqual(resp["result"]["serverInfo"]["name"], "agentledger")
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_tools_list_has_four_layers(self):
        tools = self.call("tools/list")["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertEqual(names, {"search", "timeline", "get_observations", "save_memory"})
        for t in tools:
            self.assertIn("inputSchema", t)

    def test_search_returns_compact_hits(self):
        resp = self.call("tools/call", {"name": "search", "arguments": {"query": "SQLite"}})
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(len(payload), 1)
        self.assertLessEqual(len(payload[0]["snippet"]), 221)
        self.assertIn("id", payload[0])

    def test_search_kind_filter(self):
        resp = self.call("tools/call", {"name": "search",
                                        "arguments": {"query": "搜索", "kind": "bugfix"}})
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertTrue(all(p["kind"] == "bugfix" for p in payload))

    def test_get_observations_batches(self):
        ids = [self.server.tool_search({"query": "SQLite"})[0]["id"]]
        resp = self.call("tools/call", {"name": "get_observations", "arguments": {"ids": ids}})
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(len(payload), 1)
        self.assertIn("body", payload[0])

    def test_timeline(self):
        hits = self.server.tool_search({"query": "SQLite"})
        resp = self.call("tools/call", {"name": "timeline",
                                        "arguments": {"obs_id": hits[0]["id"]}})
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertGreaterEqual(len(payload), 1)

    def test_unknown_tool_is_error_but_serves(self):
        resp = self.call("tools/call", {"name": "nope", "arguments": {}})
        self.assertTrue(resp["result"]["isError"])

    def test_unknown_method(self):
        resp = self.call("resources/list")
        self.assertEqual(resp["error"]["code"], -32601)

    def test_serve_loop_handles_bad_json(self):
        stdin = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\nnot json\n')
        stdout = io.StringIO()
        serve(self.path, stdin=stdin, stdout=stdout)
        lines = [json.loads(x) for x in stdout.getvalue().splitlines()]
        self.assertEqual(lines[0]["result"], {})
        self.assertEqual(lines[1]["error"]["code"], -32700)


if __name__ == "__main__":
    unittest.main()
