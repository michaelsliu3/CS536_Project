import tempfile
import unittest
from pathlib import Path
from src.evaluation.comparison import parse_prediction, majority_label, evaluate_cached, summarize
from src.evaluation.prompts import build_user_prompt

class ComparisonTests(unittest.TestCase):
    def test_strict_parser(self):
        self.assertIs(parse_prediction('{"is_merged":false}'),False)
        for text in ['```json\n{"is_merged":true}\n```','{"is_merged":1}', '{"is_merged":true,"extra":1}', '{"is_merged":true,"is_merged":false}', 'blah']:
            self.assertIsNone(parse_prediction(text))
    def test_majority_ties(self):
        self.assertFalse(majority_label([True,False]))
        self.assertTrue(majority_label([True,True,False]))
    def test_resume_invalid_and_identity(self):
        calls=[]
        examples=[('a',{'full_name':'repo','pr_number':1,'merged':False}),('b',{'full_name':'repo','pr_number':2,'merged':True})]
        def predict(example,adapter):
            calls.append(adapter)
            return ('{"is_merged":true}' if adapter else 'invalid'),{}
        with tempfile.TemporaryDirectory() as d:
            rows=evaluate_cached(examples,False,predict,{'seed':42},d)
            self.assertEqual(len(calls),4)
            metrics={r['model']:r for r in summarize(rows)}
            self.assertEqual(metrics['base']['accuracy'],0)
            self.assertEqual(metrics['majority']['accuracy'],.5)
            self.assertEqual(metrics['adapter']['accuracy'],.5)
            evaluate_cached(examples,False,predict,{'seed':42},d)
            self.assertEqual(len(calls),4)
            with self.assertRaises(ValueError):evaluate_cached(examples,False,predict,{'seed':43},d)
    def test_labels_not_in_prompt(self):
        record=dict(full_name='repo',lang='py',diff_hunk='+x',issue_title='',issue_body='',pr_title='',pr_body='',snippet_before='',snippet_after='',merged=False)
        before=build_user_prompt(record)
        record.update(merged=True,review_comments='LEAK',verdict='LEAK')
        self.assertEqual(before,build_user_prompt(record))

if __name__=='__main__':unittest.main()
