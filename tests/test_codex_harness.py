"""What Codex CLI reads, recorded as a context level (src/harness_context/codex.py)."""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.harness_context import codex  # noqa: E402
from src.review_outcomes.profiles import read_notebook_profile  # noqa: E402
from src.review_outcomes.table import Cell  # noqa: E402
from src.tracer_context.build import input_sections, split_input  # noqa: E402

_spec = importlib.util.spec_from_file_location("record_codex_cli", PROJECT_ROOT / "scripts/harness/record_codex.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


def item(kind: str, **fields) -> str:
    return json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": kind, **fields}})


def command(text: str, output: str = "out", exit_code: int | None = 0, status: str = "completed") -> dict:
    return {"command": text, "output": output, "exit_code": exit_code, "status": status}


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


class CommandTests(unittest.TestCase):
    def test_the_shell_wrapper_is_not_shown(self):
        self.assertEqual(codex.shown_command("/bin/zsh -lc \"sed -n '1,80p' src/a.py\""), "sed -n '1,80p' src/a.py")
        self.assertEqual(codex.shown_command("bash -lc ls"), "ls")
        self.assertEqual(codex.shown_command("rg foo src"), "rg foo src")
        self.assertEqual(codex.shown_command("bash -lc 'unclosed"), "bash -lc 'unclosed")

    def test_a_command_that_leaves_the_checkout_is_recognised(self):
        repo = "/work/repos/o__r"
        for inside in ("sed -n 1,9p src/a.py", "git diff aacr-base...HEAD -- src/a.py", "rg -n 'a/b' src 2>/dev/null",
                       f"cat {repo}/src/a.py", "/usr/bin/git log -1", "ls ./src/../src", "sed -n '/def f/,/^$/p' src/a.py",
                       "rg -n \"/api/v1\" src", "cat src/var/isFunction.js", "ls lib/tmp/ src/etc/x"):
            self.assertFalse(codex.leaves_repository(f"bash -lc {json.dumps(inside)}", repo), inside)
        for outside in ("cat ~/.ssh/id_rsa", "ls $HOME", "cat /etc/passwd", "cat ../other/secret", "cat /work/repos/other/a.py",
                        "find / -name '*.pem'", "cat '/etc/hosts'", "cat /private/tmp/x"):
            self.assertTrue(codex.leaves_repository(f"bash -lc {json.dumps(outside)}", repo), outside)

    def test_only_useful_commands_are_rendered_once_within_the_budget(self):
        commands = [command("bash -lc 'cat a.py'", "print(1)\n"), command("bash -lc 'cat a.py'", "print(1)\n"),
                    command("bash -lc 'cat missing'", "No such file", exit_code=1, status="failed"), command("bash -lc true", " "),
                    command("bash -lc 'cat ~/.netrc'", "secret"), command("bash -lc 'cat b.md'", "```py\nx\n```\n"),
                    command("bash -lc 'cat big'", "x " * 500)]
        text, counts = codex.render_commands(commands, lambda t: len(t.split()), budget=40, repo="/r")
        self.assertEqual(counts, {"commands": 7, "included": 2, "failed_or_empty": 2, "repeated": 1, "outside_repository": 1, "over_budget": 1})
        self.assertEqual(text, "### $ cat a.py\n\n```\nprint(1)\n```\n\n### $ cat b.md\n\n````\n```py\nx\n```\n````")
        self.assertNotIn("secret", text)


class LevelTests(unittest.TestCase):
    def setUp(self):
        pr, diff = "## Pull request\n\nTitle: T\n\nBody", "## Diff\n\n```diff\n@@ -1 +1 @@\n-a\n+b\n```"
        self.parent = {"text": pr + "\n\n" + diff, "sections": input_sections(pr, diff, "", ""), "input_tokens": 20,
                       "pr_tokens": 5, "diff_tokens": 9}
        self.count = lambda text: len(text.split())

    def test_the_level_is_the_hunk_plus_what_the_agent_read(self):
        context, counts = codex.render_commands([command("bash -lc 'cat a.py'", "print(1)\n")], self.count, 100)
        level = codex.harness_level(self.parent, context, counts, self.count, 1000, {"key": "k", "usage": {"input_tokens": 900}})
        self.assertTrue(level["text"].startswith(self.parent["text"] + "\n\n## Repository context\n\n### $ cat a.py"))
        parts = split_input(level["text"], level["sections"])  # the reviewer prompt takes the level apart like this
        self.assertEqual((parts["pr_title"], parts["diff"], parts["contexts"]), ("T", "@@ -1 +1 @@\n-a\n+b", context))
        self.assertEqual((level["parent"], level["same_as_parent"], level["items"], level["included_by_kind"]), ("H01_min", False, 1, {"command": 1}))
        self.assertEqual((level["input_tokens"], level["context_tokens"], level["token_budget"]), (self.count(level["text"]), self.count(context), 980))
        self.assertEqual((level["harness"]["usage"], level["harness"]["commands"]["included"], level["config_sha256"]), ({"input_tokens": 900}, 1, "k"))

    def test_a_session_that_read_nothing_gives_the_parent_level(self):
        level = codex.harness_level(self.parent, "", {"commands": 0, "included": 0}, self.count, 1000, {"key": "k"})
        self.assertEqual((level["text"], level["same_as_parent"], level["input_tokens"], level["sections"]), (None, True, 20, self.parent["sections"]))


class RecordTests(unittest.TestCase):
    def test_the_hunk_keeps_the_profiles_levels_and_gets_the_recorded_one_after_them(self):
        levels = {"H01_min": {"parent": None, "text": "a"}, "H02_function": {"parent": "H01_min", "text": None},
                  "H03_file": {"parent": "H02_function", "text": "c"}, "H04_callees1": {"parent": "H03_file", "text": None},
                  "H05_callers1": {"parent": "H04_callees1", "text": "e"}}
        hunk = {"id": "p__h0001", "file": "a.py", "levels": levels}
        recorded = {"parent": "H01_min", "text": "x"}
        got = codex.with_level(hunk, recorded, ["H01_min", "H02_function", "H03_file", "H04_callees1"])
        self.assertEqual(list(got["levels"]), ["H01_min", "H02_function", "H03_file", "H04_callees1", codex.LEVEL])
        self.assertEqual((got["id"], got["file"], got["levels"][codex.LEVEL]), ("p__h0001", "a.py", recorded))
        self.assertEqual(list(codex.with_level(hunk, recorded, ["H04_callees1"])["levels"]),
                         ["H01_min", "H02_function", "H03_file", "H04_callees1", codex.LEVEL])  # its parents come along
        self.assertEqual(len(codex.with_level(hunk, recorded)["levels"]), 6)
        self.assertEqual(list(codex.with_level(hunk, recorded, [])["levels"]), ["H01_min", codex.LEVEL])

    def test_the_built_file_reads_like_the_builders(self):
        from src.tracer_context.dataset import hunk_units
        pr, diff = "## Pull request\n\nTitle: T\n\nBody", "## Diff\n\n```diff\n@@ -1 +1 @@\n-a\n+b\n```"
        parent = {"parent": None, "text": pr + "\n\n" + diff, "sections": input_sections(pr, diff, "", ""), "input_tokens": 20}
        same = {"parent": "H01_min", "text": None, "sections": parent["sections"], "input_tokens": 20}
        context, counts = codex.render_commands([command("bash -lc 'cat a.py'", "x\n")], lambda t: len(t.split()), 100)
        level = codex.harness_level(parent, context, counts, lambda t: len(t.split()), 1000, {"key": "k"})
        record = {"id": "p", "hunks": [codex.with_level({"id": "p__h0001", "levels": {"H01_min": parent, "H02_function": same}}, level)]}
        unit = hunk_units([record])[0]
        self.assertEqual(unit["levels"]["H02_function"]["text"], parent["text"])  # filled in from its parent
        self.assertTrue(unit["levels"][codex.LEVEL]["text"].endswith("```\nx\n```"))


class RunTests(unittest.TestCase):
    def test_the_prompt_allows_exploring_and_then_asks_for_the_review(self):
        prompt = codex.agent_prompt("REVIEW REQUEST", "aacr-base")
        self.assertTrue(prompt.endswith("REVIEW REQUEST"))
        self.assertIn("`aacr-base` and HEAD", prompt)
        self.assertIn("read-only", prompt)
        self.assertNotEqual(codex.settings_key({"prompt": "a"}), codex.settings_key({"prompt": "b"}))

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

    def test_the_recorded_level_has_a_profile_of_its_own(self):
        profile = read_notebook_profile("cloud-ccrb-hunk-codex")
        self.assertEqual((profile["contexts"], profile["levels"]), ("data/ccrb_hunk/context_levels.codex.jsonl", [codex.LEVEL]))
        self.assertNotEqual(profile["run_name"], read_notebook_profile("cloud-ccrb-hunk")["run_name"])


if __name__ == "__main__":
    unittest.main()
