import copy
import json
import unittest

from src.context_selection.baselines import review_messages, select_context
from src.context_selection.prompts import build_user_prompt, context_chunks
from src.evaluation.prompts import build_user_prompt as original_prompt


def example():
    return dict(full_name="team/repo", lang="Python", diff_hunk="+ validate_account(account)",
                issue_title="Validate account", issue_body="Reject invalid accounts",
                pr_title="Account validation", pr_body="Check account before saving",
                snippet_before="save(account)", snippet_after="validate_account(account); save(account)",
                merged=True, review_comments="OUTCOME_SECRET", verdict="OUTCOME_SECRET")


def fake_tokens(messages):
    # Character counts are a deterministic test double, never benchmark tokens.
    return len(json.dumps(messages, sort_keys=True))


class ContextSelectionTests(unittest.TestCase):
    def test_default_prompt_preserves_historical_all_context_format(self):
        self.assertEqual(build_user_prompt(example()), original_prompt(example()))
        self.assertEqual(build_user_prompt(example()),
            "Repository: team/repo\n\nLanguage: Python\n\nDiff hunk:\n+ validate_account(account)"
            "\n\nIssue title:\nValidate account\n\nIssue body:\nReject invalid accounts"
            "\n\nPR title:\nAccount validation\n\nPR body:\nCheck account before saving"
            "\n\nCode before:\nsave(account)\n\nCode after:\nvalidate_account(account); save(account)")

    def test_outcome_fields_cannot_change_policy_or_prompt(self):
        record = example()
        changed = copy.deepcopy(record)
        changed.update(merged=False, review_comments="OTHER_SECRET", verdict="OTHER_SECRET",
                       is_merged=False, label=False, prediction=True)
        for policy in ("diff_only", "all_context", "fixed", "random", "similarity"):
            first = select_context(record, policy, fake_tokens, fixed_context=("issue",), budget=1000)
            second = select_context(changed, policy, fake_tokens, fixed_context=("issue",), budget=1000)
            self.assertEqual(first, second)
            self.assertEqual(review_messages(record, first.context_types), review_messages(changed, second.context_types))
            self.assertNotIn("OUTCOME_SECRET", str(review_messages(record, first.context_types)))

    def test_fixed_masks_and_missing_chunks(self):
        record = example()
        record.update(issue_title="", issue_body=None, snippet_after="\x00 \n")
        self.assertEqual(tuple(context_chunks(record)), ("pr", "code_before"))
        self.assertEqual(select_context(record, "all_context", fake_tokens).context_types, ("pr", "code_before"))
        self.assertEqual(select_context(record, "fixed", fake_tokens, fixed_context=("issue", "pr")).context_types, ("pr",))
        record.pop("issue_title")
        record["issue_tilte"] = "Historical spelling"
        self.assertIn("Historical spelling", build_user_prompt(record, context_types=("issue",)))

    def test_diff_only_and_zero_budget(self):
        record = example()
        for policy in ("diff_only", "random", "similarity"):
            result = select_context(record, policy, fake_tokens, budget=0)
            self.assertEqual(result.context_types, ())
            self.assertEqual(result.extra_context_tokens, 0)
            self.assertEqual(result.input_tokens, fake_tokens(review_messages(record, ())))
        self.assertNotIn("Code after:", build_user_prompt(record, context_types=()))
        self.assertIn("Diff hunk:", build_user_prompt(record, context_types=()))

    def test_assembled_chat_budget_includes_joint_tokenization_effects(self):
        record = example()
        record.update(snippet_before="", snippet_after="")

        def joint_tokens(messages):
            text = messages[1]["content"]
            return fake_tokens(messages) + (1000 if "Issue body:" in text and "PR body:" in text else 0)

        for policy in ("random", "similarity"):
            for seed in range(20):
                result = select_context(record, policy, joint_tokens, budget=500, seed=seed)
                self.assertLessEqual(result.extra_context_tokens, 500)
                self.assertLessEqual(len(result.context_types), 1)
                self.assertEqual(result.input_tokens, joint_tokens(review_messages(record, result.context_types)))

    def test_random_is_stable_per_id_and_independent_of_batch_order(self):
        record = example()
        forward = {rid: select_context(record, "random", fake_tokens, budget=300, seed=13, example_id=rid)
                   for rid in ("a", "b", "c")}
        reverse = {rid: select_context(record, "random", fake_tokens, budget=300, seed=13, example_id=rid)
                   for rid in ("c", "b", "a")}
        self.assertEqual(forward, reverse)
        outputs = {select_context(record, "random", fake_tokens, seed=seed, example_id="a").context_types
                   for seed in range(20)}
        self.assertGreater(len(outputs), 1)

    def test_similarity_prefers_relevant_text_and_can_select_nothing(self):
        record = dict(full_name="repo", lang="py", diff_hunk="+ validate_account(account)",
                      issue_body="validate_account account", pr_body="documentation typography",
                      snippet_before="", snippet_after="")
        result = select_context(record, "similarity", fake_tokens)
        self.assertEqual(result.context_types, ("issue",))
        record["diff_hunk"] = "+ unrelated_identifier"
        self.assertEqual(select_context(record, "similarity", fake_tokens).context_types, ())

    def test_all_context_and_fixed_never_silently_change_policy(self):
        for policy in ("all_context", "fixed"):
            with self.assertRaisesRegex(ValueError, "never silently trimmed"):
                select_context(example(), policy, fake_tokens, budget=0, fixed_context=("issue",))

    def test_whole_input_limit_and_invalid_arguments(self):
        record = example()
        base = fake_tokens(review_messages(record, ()))
        for policy in ("random", "similarity"):
            result = select_context(record, policy, fake_tokens, max_input_tokens=base + 100)
            self.assertLessEqual(result.input_tokens, base + 100)
        with self.assertRaisesRegex(ValueError, "Diff-only"):
            select_context(record, "diff_only", fake_tokens, max_input_tokens=base - 1)
        for bad in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                select_context(record, "random", fake_tokens, budget=bad)
        with self.assertRaises(ValueError):
            select_context(record, "oracle", fake_tokens)
        with self.assertRaises(ValueError):
            select_context(record, "fixed", fake_tokens, fixed_context=("merged",))
        with self.assertRaises(ValueError):
            select_context(record, "diff_only", lambda messages: -1)
        with self.assertRaises(ValueError):
            build_user_prompt(record, context_types=("verdict",))

    def test_empty_optional_context(self):
        record = dict(full_name="repo", lang="py", diff_hunk="+x")
        for policy in ("diff_only", "all_context", "fixed", "random", "similarity"):
            result = select_context(record, policy, fake_tokens)
            self.assertEqual(result.available_context_types, ())
            self.assertEqual(result.context_types, ())


if __name__ == "__main__":
    unittest.main()
