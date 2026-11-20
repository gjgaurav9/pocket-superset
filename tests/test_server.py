"""Tests for the Pocket Superset bridge. Run: python3 -m unittest discover -s tests -v"""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

RULE = "─" * 60
IDLE_SCREEN = "\n".join([
    "  - Saved the weekly deck rule to memory.",
    "✻ Crunched for 15s · done 11:18 AM",
    RULE,
    "❯ export the PDF",
    RULE,
    "  ⏵⏵ auto mode on (shift+tab to cycle) · ⧉ deck",
])
EMPTY_INPUT_SCREEN = "\n".join(["done", RULE, "❯ ", RULE, "  ? for shortcuts"])
PERMISSION_SCREEN = "\n".join([
    "╭──────────────────────────────────────────╮",
    "│ Bash command                              │",
    "│   bun test test/sync                      │",
    "│ Do you want to proceed?                   │",
    "│ ❯ 1. Yes                                  │",
    "│   2. Yes, and don't ask again for bun     │",
    "│   3. No, and tell Claude what to do       │",
    "╰──────────────────────────────────────────╯",
])
REPLY_WITH_LIST = "\n".join([
    "Here is the plan:", "1. Fix the lock", "2. Add a test", "3. Ship it",
    RULE, "❯ ", RULE, "  ? for shortcuts"])

TRUST_SCREEN = "\n".join([
    RULE,
    " Accessing workspace:",
    "",
    " /Users/me/worktrees/app",
    "",
    " Quick safety check: Is this a project you created or one you trust?",
    "",
    " ❯ No, exit",
    "   Yes, I trust this folder",
    "",
    " Enter to confirm · Esc to cancel",
])
SHELL_SCREEN = "\n".join([
    "Claude Code didn't start because this folder isn't trusted.",
    "me@laptop build-app % ",
])

WS = "11111111-2222-3333-4444-555555555555"
TERM = "66666666-7777-8888-9999-000000000000"


class ScreenTests(unittest.TestCase):
    def test_input_box_and_footer(self):
        s = server.parse_screen(IDLE_SCREEN)
        self.assertEqual(s["input"], "export the PDF")
        self.assertIn("auto mode on", s["footer"])
        self.assertEqual(s["choices"], [])

    def test_empty_input(self):
        self.assertEqual(server.parse_screen(EMPTY_INPUT_SCREEN)["input"], "")

    def test_permission_prompt(self):
        s = server.parse_screen(PERMISSION_SCREEN)
        self.assertEqual([c["key"] for c in s["choices"]], ["1", "2", "3"])
        self.assertEqual(s["choices"][0]["label"], "Yes")
        self.assertEqual(s["question"], "Do you want to proceed?")

    def test_unnumbered_menu(self):
        s = server.parse_screen(TRUST_SCREEN)
        self.assertTrue(s["menu"])
        self.assertEqual([c["label"] for c in s["choices"]], ["No, exit", "Yes, I trust this folder"])
        self.assertTrue(s["choices"][0]["selected"])
        self.assertIsNone(s["input"])

    def test_input_box_cursor_is_not_a_menu(self):
        s = server.parse_screen(IDLE_SCREEN)
        self.assertFalse(s["menu"])

    def test_shell_has_no_input_box(self):
        s = server.parse_screen(SHELL_SCREEN)
        self.assertIsNone(s["input"])
        self.assertEqual(s["choices"], [])

    def test_numbered_list_in_reply_is_not_a_prompt(self):
        self.assertEqual(server.parse_screen(REPLY_WITH_LIST)["choices"], [])


def rec(kind, content, **kw):
    r = {"type": kind, "uuid": kw.pop("uuid", f"u{time.time_ns()}"), "timestamp": "2026-11-20T08:00:00Z",
         "message": {"content": content, **kw}}
    return r


class TranscriptTests(unittest.TestCase):
    def test_turns(self):
        recs = [
            rec("user", "fix the lock timeout"),
            rec("assistant", [{"type": "text", "text": "Looking."},
                              {"type": "tool_use", "name": "Bash", "input": {"command": "bun test x"}}]),
            rec("user", [{"type": "tool_result", "content": "ok"}]),
            rec("assistant", [{"type": "text", "text": "Done."}], stop_reason="end_turn"),
            rec("user", "<command-name>/clear</command-name><command-args></command-args>"),
            rec("user", "<local-command-stdout>x</local-command-stdout>"),
        ]
        turns = server.records_to_turns(recs)
        self.assertEqual([t["role"] for t in turns], ["you", "claude", "tool", "claude", "you"])
        self.assertEqual(turns[2]["text"], "Bash: bun test x")
        self.assertEqual(turns[4]["text"], "/clear")

    def test_sidechain_skipped(self):
        r = rec("assistant", [{"type": "text", "text": "subagent"}])
        r["isSidechain"] = True
        self.assertEqual(server.records_to_turns([r]), [])

    def test_classify(self):
        now = time.time()
        done = [rec("user", "go"), rec("assistant", [{"type": "text", "text": "All set."}], stop_reason="end_turn")]
        self.assertEqual(server.classify(done, now, now)["state"], "finished")
        self.assertEqual(server.classify(done, now, now)["last_reply"], "All set.")
        asking = [rec("assistant", [{"type": "tool_use", "name": "AskUserQuestion", "input": {}}])]
        self.assertEqual(server.classify(asking, now, now)["state"], "needs_input")
        working = [rec("user", "go")]
        self.assertEqual(server.classify(working, now, now)["state"], "working")
        self.assertEqual(server.classify(working, now - 7200, now)["state"], "stale")


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = server.Devices(os.path.join(self.tmp, "devices.json"))

    def test_pair_once(self):
        code = self.d.new_code()
        token, err = self.d.redeem(code, "pixel")
        self.assertIsNone(err)
        self.assertEqual(self.d.check(token)["name"], "pixel")
        self.assertIsNone(self.d.redeem(code, "again")[0])           # single use
        with open(os.path.join(self.tmp, "devices.json")) as f:
            self.assertNotIn(token, f.read())                        # only the hash is stored

    def test_wrong_codes_lock_out(self):
        code = self.d.new_code()
        for _ in range(5):
            self.assertIsNone(self.d.redeem("000000" if code != "000000" else "111111", "x")[0])
        self.assertIsNone(self.d.redeem(code, "x")[0])

    def test_unknown_token(self):
        self.assertIsNone(self.d.check("nope"))
        self.assertIsNone(self.d.check(None))


class FakeCli:
    def __init__(self):
        self.screen = EMPTY_INPUT_SCREEN
        self.sent = []

    def read_screen(self, ws, term, lines=45):
        return self.screen

    def send_text(self, ws, term, text):
        self.sent.append(("text", text))

    def send_keys(self, ws, term, keys):
        self.sent.append(("keys", keys))


class BoardTests(unittest.TestCase):
    def test_dead_terminal_does_not_take_live_transcript(self):
        tmp = tempfile.mkdtemp()
        old = server.PROJECTS_DIR
        server.PROJECTS_DIR = tmp
        self.addCleanup(setattr, server, "PROJECTS_DIR", old)
        cwd = "/Users/me/worktrees/app"
        d = os.path.join(tmp, server.encode_cwd(cwd))
        os.makedirs(d)
        lines = [{"type": "ai-title", "aiTitle": "Live session"},
                 rec("user", "go"), rec("assistant", [{"type": "text", "text": "pong"}], stop_reason="end_turn")]
        with open(os.path.join(d, "s1.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(x) for x in lines) + "\n")
        dead, alive = "aaaaaaaa-0000-0000-0000-000000000001", "aaaaaaaa-0000-0000-0000-000000000002"

        class Cli:
            def projects(self):
                return []

            def workspaces(self):
                return [{"id": WS, "worktreePath": cwd, "name": "w", "projectName": "p", "branch": "b"}]

            def snapshot(self):
                return {"workspaces": [{"id": WS, "terminals": [
                    {"id": dead, "title": None, "agent": {"id": "claude"}},
                    {"id": alive, "title": "◑ Live session", "agent": {"id": "claude"}}]}]}

        b = server.Board(Cli())
        b.refresh()
        self.assertEqual(b.get(WS, alive)["state"], "finished")
        self.assertEqual(b.get(WS, alive)["last_reply"], "pong")
        self.assertEqual(b.get(WS, dead)["state"], "unknown")


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        server.DATA_DIR = self.tmp
        server.AUDIT_LOG = os.path.join(self.tmp, "audit.log")
        self.cli = FakeCli()
        board = server.Board(self.cli)
        board.cards = {(WS, TERM): {"ws": WS, "term": TERM, "workspace": "w", "project": "p", "branch": "b",
                                    "title": "t", "state": "finished", "label": "", "last_reply": None,
                                    "last_prompt": None, "updated_at": time.time(), "_transcript": None}}
        self.devices = server.Devices(os.path.join(self.tmp, "devices.json"))
        handler = type("H", (server.Handler,), {})
        handler.board, handler.devices, handler.cli = board, self.devices, self.cli
        handler.phone_prompts = {}
        handler.allow_login = None
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.board = board

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def req(self, method, path, body=None, headers=None, form=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = dict(headers or {})
        data = None
        if body is not None:
            if form:
                data = body.encode()
                h["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(body).encode()
                h.setdefault("Content-Type", "application/json")
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r, raw

    def pair(self):
        code = self.devices.new_code()
        r, _ = self.req("POST", "/pair", f"code={code}&name=test", form=True)
        self.assertEqual(r.status, 303)
        cookie = r.getheader("Set-Cookie").split(";")[0]
        self.assertIn("HttpOnly", r.getheader("Set-Cookie"))
        return {"Cookie": cookie}

    def test_api_needs_pairing(self):
        r, _ = self.req("GET", "/api/cards")
        self.assertEqual(r.status, 401)
        r, _ = self.req("POST", "/api/prompt", {"ws": WS, "term": TERM, "text": "hi"})
        self.assertEqual(r.status, 401)
        self.assertEqual(self.cli.sent, [])

    def test_local_pair_page_refuses_forwarded(self):
        r, _ = self.req("GET", "/local/pair", headers={"X-Forwarded-For": "100.64.0.2"})
        self.assertEqual(r.status, 403)
        r, _ = self.req("GET", "/local/pair", headers={"Tailscale-User-Login": "me@example.com"})
        self.assertEqual(r.status, 403)
        r, raw = self.req("GET", "/local/pair")
        self.assertEqual(r.status, 200)

    def test_cards_hide_transcript_path(self):
        h = self.pair()
        r, raw = self.req("GET", "/api/cards", headers=h)
        self.assertEqual(r.status, 200)
        self.assertNotIn("_transcript", raw.decode())

    def test_prompt_sends(self):
        h = self.pair()
        r, _ = self.req("POST", "/api/prompt", {"ws": WS, "term": TERM, "text": "add a test"}, headers=h)
        self.assertEqual(r.status, 200)
        self.assertEqual(self.cli.sent, [("text", "add a test")])
        with open(server.AUDIT_LOG) as f:
            self.assertIn("add a test", f.read())

    def test_prompt_unknown_session(self):
        h = self.pair()
        other = "aaaaaaaa-2222-3333-4444-555555555555"
        r, _ = self.req("POST", "/api/prompt", {"ws": other, "term": TERM, "text": "x"}, headers=h)
        self.assertEqual(r.status, 404)
        r, _ = self.req("POST", "/api/prompt", {"ws": "../etc", "term": TERM, "text": "x"}, headers=h)
        self.assertEqual(r.status, 404)
        self.assertEqual(self.cli.sent, [])

    def test_prompt_guards(self):
        h = self.pair()
        self.cli.screen = IDLE_SCREEN
        r, raw = self.req("POST", "/api/prompt", {"ws": WS, "term": TERM, "text": "x"}, headers=h)
        self.assertEqual(r.status, 409)
        self.assertEqual(json.loads(raw)["reason"], "input_not_empty")
        r, _ = self.req("POST", "/api/prompt", {"ws": WS, "term": TERM, "text": "x", "mode": "clear"}, headers=h)
        self.assertEqual(r.status, 200)
        self.assertEqual(self.cli.sent, [("keys", "ctrl+u"), ("text", "x")])
        self.cli.sent.clear()
        self.cli.screen = PERMISSION_SCREEN
        r, raw = self.req("POST", "/api/prompt", {"ws": WS, "term": TERM, "text": "x", "mode": "anyway"}, headers=h)
        self.assertEqual(json.loads(raw)["reason"], "choice_open")
        self.cli.screen = EMPTY_INPUT_SCREEN
        self.board.cards[(WS, TERM)]["state"] = "working"
        r, raw = self.req("POST", "/api/prompt", {"ws": WS, "term": TERM, "text": "x"}, headers=h)
        self.assertEqual(json.loads(raw)["reason"], "busy")
        self.assertEqual(self.cli.sent, [])

    def test_never_types_into_a_shell(self):
        h = self.pair()
        self.cli.screen = SHELL_SCREEN
        for mode in ("safe", "anyway", "clear"):
            r, raw = self.req("POST", "/api/prompt", {"ws": WS, "term": TERM, "text": "rm -rf x", "mode": mode}, headers=h)
            self.assertEqual(r.status, 409)
            self.assertEqual(json.loads(raw)["reason"], "no_input_box")
        self.assertEqual(self.cli.sent, [])

    def test_choose_in_menu(self):
        h = self.pair()
        self.cli.screen = TRUST_SCREEN
        r, _ = self.req("POST", "/api/key", {"ws": WS, "term": TERM, "choose": 1,
                                              "label": "Yes, I trust this folder"}, headers=h)
        self.assertEqual(r.status, 200)
        self.assertEqual(self.cli.sent, [("keys", "down,enter")])

    def test_choose_refuses_stale_menu(self):
        h = self.pair()
        self.cli.screen = TRUST_SCREEN
        r, _ = self.req("POST", "/api/key", {"ws": WS, "term": TERM, "choose": 1, "label": "Something else"}, headers=h)
        self.assertEqual(r.status, 409)
        r, _ = self.req("POST", "/api/key", {"ws": WS, "term": TERM, "choose": 5}, headers=h)
        self.assertEqual(r.status, 409)
        self.cli.screen = EMPTY_INPUT_SCREEN
        r, _ = self.req("POST", "/api/key", {"ws": WS, "term": TERM, "choose": 0}, headers=h)
        self.assertEqual(r.status, 409)
        self.assertEqual(self.cli.sent, [])

    def test_keys_whitelist(self):
        h = self.pair()
        r, _ = self.req("POST", "/api/key", {"ws": WS, "term": TERM, "key": "ctrl+c"}, headers=h)
        self.assertEqual(r.status, 400)
        r, _ = self.req("POST", "/api/key", {"ws": WS, "term": TERM, "key": "1"}, headers=h)
        self.assertEqual(r.status, 200)
        self.assertEqual(self.cli.sent, [("keys", "1")])

    def test_cross_site_refused(self):
        h = self.pair()
        h["Origin"] = "https://evil.example"
        r, _ = self.req("POST", "/api/prompt", {"ws": WS, "term": TERM, "text": "x"}, headers=h)
        self.assertEqual(r.status, 403)
        self.assertEqual(self.cli.sent, [])

    def test_form_post_refused_for_api(self):
        h = self.pair()
        r, _ = self.req("POST", "/api/prompt", "ws=x", headers=h, form=True)
        self.assertEqual(r.status, 415)

    def test_allow_login(self):
        self.srv.RequestHandlerClass.allow_login = "me@example.com"
        r, _ = self.req("GET", "/health", headers={"Tailscale-User-Login": "other@example.com"})
        self.assertEqual(r.status, 403)
        r, _ = self.req("GET", "/health", headers={"Tailscale-User-Login": "me@example.com"})
        self.assertEqual(r.status, 200)


if __name__ == "__main__":
    unittest.main()
