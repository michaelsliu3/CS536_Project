"""Codex CLI as the comparison: its sessions read and its own reviews scored, per hunk (the
comparison) and per PR (src/harness_context/codex.py, scripts/harness/record_codex.py)."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.harness_context import codex  # noqa: E402
from src.review_outcomes import table  # noqa: E402
from src.review_outcomes.profiles import read_notebook_profile  # noqa: E402
from src.review_outcomes.table import Cell  # noqa: E402


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("/", "_"), PROJECT_ROOT / "scripts" / "harness" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = load_script("record_codex")
fake = load_script("fake_model_server")

REPO = "/work/repos/o__r"
REVIEW_JSON = json.dumps({
    "findings": [{"title": "[P1] Check the pointer", "body": "`p` may be null here.", "confidence_score": 0.8, "priority": 1,
                  "code_location": {"absolute_file_path": f"{REPO}/src/a.c", "line_range": {"start": 12, "end": 13}}},
                 {"title": "[P3] Rename", "body": "Unclear name.", "confidence_score": 0.3, "priority": 3,
                  "code_location": {"absolute_file_path": "/somewhere/else/docs/b.md", "line_range": {"start": 2, "end": 2}}}],
    "overall_correctness": "patch is incorrect", "overall_explanation": "One real problem.", "overall_confidence_score": 0.7})


def item(kind: str, **fields) -> str:
    return json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": kind, **fields}})


def line(kind: str, **payload) -> str:
    return json.dumps({"type": kind, "payload": payload})


def usage(input_tokens: int, cached: int, output: int) -> dict:
    return {"input_tokens": input_tokens, "cached_input_tokens": cached, "cache_write_input_tokens": 0, "output_tokens": output,
            "reasoning_output_tokens": 0, "total_tokens": input_tokens + output}


def review_session() -> list[str]:
    """The session file of a review conversation: one command, then the review."""
    shown = "Chunk ID: a1\nWall time: 0.1 seconds\nProcess exited with code 0\nOriginal token count: 321\nOutput:\ndiff --git a/src/a.c b/src/a.c\n"
    return [
        line("session_meta", id="child", session_id="parent", parent_thread_id="parent", source={"subagent": "review"}, cli_version="0.171.0"),
        line("response_item", type="message", role="developer", content=[{"type": "input_text", "text": "# Review guidelines"}]),
        line("response_item", type="message", role="user", content=[{"type": "input_text", "text": "Review the code changes"}]),
        line("response_item", type="function_call", name="exec_command", call_id="c1", arguments=json.dumps({"cmd": "git diff abc1234"})),
        line("token_usage_record", usage=usage(4000, 0, 30)),
        line("event_msg", type="token_count", info={"total_token_usage": usage(4000, 0, 30), "last_token_usage": usage(4000, 0, 30)}),
        line("response_item", type="function_call_output", call_id="c1", output=shown),
        line("response_item", type="message", role="assistant", content=[{"type": "output_text", "text": REVIEW_JSON}]),
        line("token_usage_record", usage=usage(5000, 4000, 70)),
        line("event_msg", type="token_count", info={"total_token_usage": usage(9000, 4000, 100), "last_token_usage": usage(5000, 4000, 70)}),
        "not json",
    ]


EXEC_SESSION = [line("session_meta", id="parent", session_id="parent", source="exec"),
                line("response_item", type="message", role="assistant", content=[{"type": "output_text", "text": "the rendered review"}])]


class EventTests(unittest.TestCase):
    def test_a_stream_gives_the_commands_the_review_and_the_cost(self):
        stream = [
            '{"type":"thread.started","thread_id":"t1"}', "not json", '{"type":"turn.started"}',
            '{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"bash -lc ls","status":"in_progress"}}',
            item("command_execution", command="bash -lc ls", aggregated_output="a.py\n", exit_code=0, status="completed"),
            item("reasoning", text="thinking"), item("agent_message", text="first"), item("agent_message", text="the review"),
            item("error", message="Model metadata not found"), '{"type":"error","message":"Reconnecting..."}',
            '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":40,"output_tokens":7}}',
            '{"type":"turn.completed","usage":{"input_tokens":50,"cached_input_tokens":0,"output_tokens":3}}',
        ]
        got = codex.parse_events(stream)
        self.assertEqual((got["thread_id"], got["turns"], got["final_message"], got["reasoning_items"]), ("t1", 2, "the review", 1))
        self.assertEqual(got["commands"], [{"command": "bash -lc ls", "output": "a.py\n", "exit_code": 0, "status": "completed"}])
        self.assertEqual(got["usage"], {"input_tokens": 150, "cached_input_tokens": 40, "output_tokens": 10})
        self.assertEqual((got["errors"], len(got["notices"])), ([], 2))  # a notice is not a failure

    def test_a_failed_turn_is_an_error(self):
        got = codex.parse_events(['{"type":"turn.failed","error":{"message":"stream closed"}}'])
        self.assertEqual((got["errors"], got["turns"]), (["stream closed"], 0))


class SessionTests(unittest.TestCase):
    def test_a_session_file_gives_the_reply_the_commands_and_the_token_counts(self):
        got = codex.parse_session(review_session())
        self.assertEqual((got["id"], got["session_id"], got["parent"], got["source"]), ("child", "parent", "parent", "review"))
        self.assertEqual(got["reply"], REVIEW_JSON)
        self.assertEqual((got["requests"], got["usage"]["input_tokens"], got["usage"]["cached_input_tokens"], got["usage"]["output_tokens"]),
                         (2, 9000, 4000, 100))  # the running total after the last request, not the sum of the totals
        self.assertEqual([u["input_tokens"] for u in got["request_usage"]], [4000, 5000])
        self.assertNotIn("total_tokens", got["usage"])
        command, = got["commands"]
        self.assertEqual((command["command"], command["exit_code"], command["output_tokens"]), ("git diff abc1234", 0, 321))
        self.assertIn("diff --git a/src/a.c", command["output"])

    def test_input_tokens_are_split_by_what_is_resent(self):
        session = codex.parse_session(review_session())  # two requests: 4,000 then 5,000 input tokens
        got = codex.input_sources([session], request_tokens=1000)
        self.assertEqual(got, {"requests": 2, "harness": 6000, "request": 2000, "conversation": 1000, "total": 9000})
        self.assertEqual(got["harness"] + got["request"] + got["conversation"], got["total"])
        self.assertEqual(codex.input_sources([], 10)["total"], 0)

    def test_a_review_run_is_two_conversations_and_the_review_is_in_the_second(self):
        sessions = [codex.parse_session(EXEC_SESSION), codex.parse_session(review_session())]
        self.assertEqual([s["source"] for s in sessions], ["exec", "review"])
        self.assertEqual(codex.review_reply(sessions), REVIEW_JSON)  # the JSON, not the first conversation's rendered copy
        self.assertEqual(codex.review_reply(sessions[:1]), "")
        totals = codex.session_totals(sessions)
        self.assertEqual((totals["usage"]["input_tokens"], totals["requests"], len(totals["commands"])), (9000, 2, 1))
        self.assertEqual(codex.session_totals([])["usage"], {})


class CommandTests(unittest.TestCase):
    def test_the_shell_wrapper_is_not_shown(self):
        self.assertEqual(codex.shown_command("/bin/zsh -lc \"sed -n '1,80p' src/a.py\""), "sed -n '1,80p' src/a.py")
        self.assertEqual(codex.shown_command("bash -lc ls"), "ls")
        self.assertEqual(codex.shown_command("rg foo src"), "rg foo src")
        self.assertEqual(codex.shown_command("bash -lc 'unclosed"), "bash -lc 'unclosed")

    def test_a_command_that_leaves_the_checkout_is_recognised(self):
        for inside in ("sed -n 1,9p src/a.py", "git diff aacr-base...HEAD -- src/a.py", "rg -n 'a/b' src 2>/dev/null",
                       f"cat {REPO}/src/a.py", "/usr/bin/git log -1", "ls ./src/../src", "sed -n '/def f/,/^$/p' src/a.py",
                       "rg -n \"/api/v1\" src", "cat src/var/isFunction.js", "ls lib/tmp/ src/etc/x"):
            self.assertFalse(codex.leaves_repository(f"bash -lc {json.dumps(inside)}", REPO), inside)
            self.assertFalse(codex.leaves_repository(inside, REPO), inside)  # a session file has the command without the wrapper
        for outside in ("cat ~/.ssh/id_rsa", "ls $HOME", "cat /etc/passwd", "cat ../other/secret", "cat /work/repos/other/a.py",
                        "find / -name '*.pem'", "cat '/etc/hosts'", "cat /private/tmp/x"):
            self.assertTrue(codex.leaves_repository(f"bash -lc {json.dumps(outside)}", REPO), outside)


class ReviewTests(unittest.TestCase):
    def test_the_pr_request_is_codexs_own_plus_the_pull_requests_text(self):
        text = codex.review_instructions("aacr-base", "abc1234", " Fix the parser ", "It broke on empty input.\n")
        self.assertTrue(text.startswith("Review the code changes against the base branch 'aacr-base'. The merge base commit for this "
                                        "comparison is abc1234. Run `git diff abc1234` to inspect the changes relative to aacr-base. "
                                        "Provide prioritized, actionable findings.\n\n"))
        self.assertIn("Title: Fix the parser\n\nDescription:\nIt broke on empty input.\n", text)
        self.assertNotIn("```diff", text)  # the change itself is not sent: Codex reads it
        self.assertIn("Description:\n(none)", codex.review_instructions("b", "abc1234", "T", ""))

    def test_the_hunk_prompt_allows_exploring_and_then_asks_for_the_review(self):
        prompt = codex.agent_prompt("REVIEW REQUEST", "aacr-base")
        self.assertTrue(prompt.endswith("REVIEW REQUEST"))
        self.assertIn("`aacr-base` and HEAD", prompt)
        self.assertIn("read-only", prompt)
        self.assertNotEqual(codex.settings_key({"prompt": "a"}), codex.settings_key({"prompt": "b"}))

    def test_a_pr_review_is_parsed_with_paths_inside_the_repository(self):
        parsed = codex.pr_review(REVIEW_JSON, REPO, known_paths=("docs/b.md",))
        self.assertEqual(parsed["status"], "ok")
        first, second = parsed["findings"]
        self.assertEqual((first["path"], first["from_line"], first["to_line"], first["priority"]), ("src/a.c", 12, 13, 1))
        self.assertEqual(second["path"], "docs/b.md")  # another prefix: recognised by the PR's known files
        self.assertEqual(codex.pr_review("The patch looks fine to me.", REPO)["status"], "malformed")  # scored as no findings
        self.assertEqual(codex.pr_review("", REPO)["status"], "empty_output")

    def test_a_hunk_review_counts_only_findings_on_the_hunk(self):
        diff = "diff --git a/src/a.c b/src/a.c\n--- a/src/a.c\n+++ b/src/a.c\n@@ -10,2 +10,3 @@\n context\n+free(p);\n more\n"
        reply = "<diff>free(p);</diff><side>right</side><note>Double free.</note>\n<diff>elsewhere()</diff><note>Not this hunk.</note><end/>"
        parsed = codex.hunk_review(reply, "src/a.c", diff)
        self.assertEqual([(f["path"], f["from_line"]) for f in parsed["findings"]], [("src/a.c", 11)])
        self.assertEqual(len(parsed["off_hunk_findings"]), 1)

    def test_codexs_own_review_is_scored_by_the_projects_judge(self):
        run = {"key": "k", "codex": "codex-cli 0.171.0", "model": "M", "seconds": 300.0, "requests": 7, "commands": 5,
               "commands_outside": 0, "commit": "abc", "usage": usage(90000, 70000, 2500)}
        review = codex.review_record("o__r__1", codex.PR_LEVEL, REVIEW_JSON, codex.pr_review(REVIEW_JSON, REPO), run)
        self.assertEqual((review["status"], review["input_tokens"], review["reply"]["completion_tokens"]), ("ok", 90000, 2500))
        self.assertNotEqual(review["key"], codex.review_record("o__r__1", codex.PR_LEVEL, "{}", codex.pr_review("{}", REPO), run)["key"])
        references = {"o__r__1": {"project_main_language": "C", "comments": [
            {"note": "p can be null", "path": "src/a.c", "from_line": 12, "to_line": 12, "side": "right"},
            {"note": "leak", "path": "src/c.c", "from_line": 3, "to_line": 3, "side": "right"}]}}
        asked = []

        def judge(prompt: str) -> str:
            asked.append(prompt)
            return "It is the same problem. [1]"

        with tempfile.TemporaryDirectory() as folder:
            judgments = table.run_judgments([review], references, judge, Path(folder))
            rows, coverage = table.build_table([review], judgments, references)
        self.assertEqual(len(asked), 1)  # the finding in a file with no reference is a miss without a call
        row, = rows
        self.assertEqual((row["level"], row["expected"], row["generated"], row["covered"], row["recall"], row["precision"]),
                         (codex.PR_LEVEL, 2, 2, 1, 0.5, 0.5))
        self.assertEqual((row["input_tokens"], row["output_tokens"], row["review_seconds"]), (90000, 2500, 300.0))
        self.assertEqual(cli.in_reference_files(judgments[0], references["o__r__1"]["comments"]), 1)
        self.assertEqual([c["covered"] for c in coverage], [True, False])


class RunTests(unittest.TestCase):
    def test_a_limit_spreads_over_the_prs_and_prefers_hunks_with_a_comment(self):
        cells = [Cell(f"p{p}__h000{h}", "H01_min", "", 1, pr=f"p{p}", file=f"f{h}.py", span=(1, 5, 1, 5)) for p in (1, 2) for h in (1, 2, 3)]
        references = {"p1": {"comments": [{"note": "x", "path": "f3.py", "side": "right", "from_line": 2, "to_line": 2}]},
                      "p2": {"comments": []}}
        self.assertEqual([c.entry for c in cli.choose(cells, references, None, 3)], ["p1__h0003", "p2__h0001", "p1__h0001"])
        self.assertEqual(len(cli.choose(cells, references, None, None)), 6)
        self.assertEqual([c.entry for c in cli.choose(cells, references, ["p2__h0002"], None)], ["p2__h0002"])
        with self.assertRaises(SystemExit):
            cli.choose(cells, references, ["nope"], None)

    def test_the_settings_keep_codex_read_only_and_off_the_network(self):
        import tomllib
        settings = tomllib.loads(cli.SETTINGS.read_text(encoding="utf-8"))
        self.assertEqual((settings["sandbox_mode"], settings["approval_policy"], settings["web_search"]), ("read-only", "never", "disabled"))
        provider = settings["model_providers"][settings["model_provider"]]
        self.assertEqual((provider["wire_api"], provider["base_url"]), ("responses", "http://localhost:8000/v1"))
        self.assertEqual(settings["model"], read_notebook_profile("cloud-ccrb-hunk")["reviewer"]["model"])
        # The model sees as much of a command's output as Codex shows its own models, from a plain shell.
        self.assertEqual((settings["tool_output_token_limit"], settings["allow_login_shell"]), (10000, False))

    def test_a_saved_session_is_reused_only_when_it_finished_with_the_same_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(cli.saved_run(Path(folder)))
            (Path(folder) / "run.json").write_text(json.dumps({"status": "ok", "key": "k1"}), encoding="utf-8")
            self.assertEqual(cli.saved_run(Path(folder), "k1")["key"], "k1")
            self.assertIsNone(cli.saved_run(Path(folder), "k2"))
            self.assertIsNotNone(cli.saved_run(Path(folder)))  # scoring takes any finished session
            (Path(folder) / "run.json").write_text(json.dumps({"status": "timeout", "key": "k1"}), encoding="utf-8")
            self.assertIsNone(cli.saved_run(Path(folder), "k1"))

    def test_commands_are_kept_from_reading_outside_the_checkout(self):
        import types
        setup = types.SimpleNamespace(empty_home=Path("/Users/me/project/data/harness/empty_home"))
        flags = cli.read_block(setup, Path("/Users/me/project/data/repos/o__r"))
        self.assertEqual(flags[:4], ("-c", 'default_permissions="review"', "-c", 'permissions.review.extends=":read-only"'))
        table = flags[5]
        self.assertTrue(table.startswith("permissions.review.filesystem={ "))
        for root in cli.blocked_roots():
            self.assertIn(f'"{root}" = "deny"', table)
        self.assertIn('"/Users/me/project/data/repos/o__r" = "read"', table)   # the checkout stays readable
        self.assertIn('"/Users/me/project/data/harness/empty_home" = "read"', table)
        self.assertIn(f'"{cli.CODEX.parents[2].resolve()}" = "read"', table)    # Codex's own ripgrep
        self.assertNotIn("write", table)

    def test_codexs_recorded_context_is_not_a_level_of_any_profile(self):
        with self.assertRaises(ValueError):
            read_notebook_profile("cloud-ccrb-hunk-codex")
        levels = read_notebook_profile("cloud-ccrb-hunk")["levels"]
        self.assertFalse({codex.PR_LEVEL, codex.HUNK_LEVEL} & set(levels))
        self.assertEqual(codex.REQUEST_LEVEL, levels[0])  # the per-hunk request is the reviewer's own, with no context
        self.assertLess(codex.HUNK_LEVEL, codex.PR_LEVEL)  # the per-hunk run is the comparison and comes first in a table
        self.assertFalse(hasattr(codex, "harness_level"))


class FakeServerTests(unittest.TestCase):
    """The stand-in model server of a rehearsal: a command first, then a review the parser reads."""

    def request(self, *items) -> dict:
        user = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"<cwd>{REPO}</cwd>"}]}
        ask = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Run `git diff abc1234` to inspect."}]}
        return {"stream": True, "tools": [{"type": "function", "name": "exec_command"}], "input": [user, ask, *items]}

    def test_the_first_reply_runs_the_diff_the_request_names(self):
        reply = fake.scripted_reply(self.request())
        self.assertEqual((reply["type"], reply["name"], json.loads(reply["arguments"])), ("function_call", "exec_command", {"cmd": "git diff abc1234"}))

    def test_the_second_reply_is_a_review_on_a_file_of_the_diff(self):
        output = {"type": "function_call_output", "call_id": "c", "output": "Output:\ndiff --git a/src/a.c b/src/a.c\n"}
        reply = fake.scripted_reply(self.request(output))
        parsed = codex.pr_review(reply["content"][0]["text"], REPO)
        self.assertEqual([(f["path"], f["from_line"]) for f in parsed["findings"]], [("src/a.c", 1)])
        given = fake.scripted_reply(self.request(output), "<note>x</note>")
        self.assertEqual(given["content"][0]["text"], "<note>x</note>")


if __name__ == "__main__":
    unittest.main()
