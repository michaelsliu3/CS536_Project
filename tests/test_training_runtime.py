import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from src.training.runtime import atomic_json, complete_checkpoint, latest_checkpoint, freeze_contract, RunLogger

class RuntimeTests(unittest.TestCase):
    def test_checkpoint_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            for step in [10,20]:
                p=Path(d)/f'checkpoint-{step}';p.mkdir()
                for name in ['trainer_state.json','optimizer.pt','scheduler.pt','rng_state.pth','adapter_config.json','adapter_model.safetensors']:
                    (p/name).write_text('{}')
                complete_checkpoint(p)
            self.assertTrue(latest_checkpoint(d).endswith('20'))
            (Path(d)/'checkpoint-20/optimizer.pt').write_text('truncated')
            self.assertTrue(latest_checkpoint(d).endswith('10'))
            (Path(d)/'checkpoint-10/rng_state.pth').unlink()
            with self.assertRaises(RuntimeError):latest_checkpoint(d)
    def test_contract_and_append_logs(self):
        with tempfile.TemporaryDirectory() as d:
            freeze_contract(d,{'revision':'abc'})
            freeze_contract(d,{'revision':'abc'})
            with self.assertRaises(ValueError):freeze_contract(d,{'revision':'def'})
            for _ in range(2):
                logger=RunLogger(d)
                logger.on_log(None,SimpleNamespace(global_step=1,epoch=.1),None,logs={'epoch':.1,'loss':1.0})
            rows=[json.loads(x) for x in (Path(d)/'training_log.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows),2)
            self.assertNotEqual(rows[0]['session'],rows[1]['session'])

if __name__=='__main__':unittest.main()
