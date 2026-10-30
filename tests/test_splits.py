import json
import tempfile
import unittest
from pathlib import Path
from src.evaluation.splits import build_manifest, freeze_manifest

class SplitTests(unittest.TestCase):
    def setUp(self):
        self.rows = [dict(id=f'{i}-{j}', full_name=f'repo/{i}', pr_number=j,
                          merged=bool(j), diff_hash=f'diff-{i}-{j}', content_hash=f'content-{i}-{j}')
                     for i in range(30) for j in range(2)]

    def test_disjoint_complete_and_order_independent(self):
        m=build_manifest(self.rows)
        self.assertEqual(m,build_manifest(self.rows[::-1]))
        sets=[set(m['ids'][k]) for k in ['train','validation','test']]
        self.assertEqual(len(set.union(*sets)),60)
        for a,b in [(0,1),(0,2),(1,2)]:self.assertFalse(sets[a]&sets[b])
        self.assertEqual([m['summary'][k]['repositories'] for k in ['train','validation','test']],[24,3,3])
        self.assertTrue(set(m['demo_ids'])<=sets[1])
        self.assertEqual(set(m['test_generation_ids']),sets[2])

    def test_duplicate_and_conflict(self):
        duplicate=dict(self.rows[0],id='zzz',full_name='other/repo')
        m=build_manifest(self.rows+[duplicate])
        self.assertEqual(m['excluded_exact_duplicates'],[{'id':'zzz','duplicate_of':'0-0'}])
        duplicate['merged']=True
        m=build_manifest(self.rows+[duplicate])
        self.assertEqual(m['excluded_conflicting_labels'],[{'ids':['0-0','zzz'],'diff_hash':'diff-0-0'}])
        self.assertFalse({'0-0','zzz'} & set().union(*(set(ids) for ids in m['ids'].values())))

    def test_frozen_change_rejected_and_paths_portable(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'split.json'
            original=freeze_manifest(self.rows,p)
            self.assertEqual(original,freeze_manifest([dict(r,path='/other/'+r['id']) for r in self.rows],p))
            changed=[dict(r) for r in self.rows];changed[0]['content_hash']='changed'
            with self.assertRaisesRegex(ValueError,'mismatch'):freeze_manifest(changed,p)
            self.assertEqual(json.loads(p.read_text()),original)

    def test_insufficient_groups_and_labels(self):
        with self.assertRaises(ValueError):build_manifest(self.rows[:4])
        with self.assertRaises(ValueError):build_manifest([dict(r,merged=True) for r in self.rows])

if __name__=='__main__':unittest.main()
