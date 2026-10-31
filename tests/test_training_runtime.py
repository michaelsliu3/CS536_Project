import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from src.training.runtime import atomic_json, complete_checkpoint, latest_checkpoint, select_run, training_identity, RunLogger

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
    def test_config_selection_and_append_logs(self):
        with tempfile.TemporaryDirectory() as d:
            config = {'revision': 'abc', 'epochs': 1}
            first = select_run(d, 'pilot', config)
            atomic_json(first/'training_config.json', config)
            self.assertEqual(select_run(d, 'pilot', {**config, 'epochs': 4}), first)
            changed = select_run(d, 'pilot', {**config, 'revision': 'def'})
            self.assertNotEqual(first, changed)
            atomic_json(changed/'training_config.json', {**config, 'revision': 'def'})
            self.assertEqual(select_run(d, 'pilot', {**config, 'revision': 'def'}), changed)
            for _ in range(2):
                logger=RunLogger(d)
                logger.on_log(None,SimpleNamespace(global_step=1,epoch=.1),None,logs={'epoch':.1,'loss':1.0})
            rows=[json.loads(x) for x in (Path(d)/'training_log.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows),2)
            self.assertNotEqual(rows[0]['session'],rows[1]['session'])

if __name__=='__main__':unittest.main()

class EpochPlanTests(unittest.TestCase):
    def test_additional_epochs_survive_retries_and_extend_after_completion(self):
        from src.training.runtime import plan_epochs
        with tempfile.TemporaryDirectory() as d:
            checkpoint = Path(d)/'checkpoint-10'
            checkpoint.mkdir()
            atomic_json(checkpoint/'trainer_state.json', {'epoch': 1})
            first = plan_epochs(d, 3, checkpoint)
            self.assertEqual(first['total_epochs'], 4)
            atomic_json(checkpoint/'trainer_state.json', {'epoch': 2.5})
            self.assertEqual(plan_epochs(d, 3, checkpoint), first)
            self.assertEqual(plan_epochs(d, 9, checkpoint), first)
            atomic_json(checkpoint/'trainer_state.json', {'epoch': 4})
            second = plan_epochs(d, 3, checkpoint)
            self.assertEqual(second['total_epochs'], 7)
            self.assertNotEqual(first['request_id'], second['request_id'])

    def test_fresh_request_and_invalid_counts(self):
        from src.training.runtime import plan_epochs
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(plan_epochs(d, 1)['total_epochs'], 1)
            self.assertEqual(plan_epochs(d, 1)['total_epochs'], 1)
            for value in [0, -1, float('nan'), float('inf'), True]:
                with self.assertRaises(ValueError):
                    plan_epochs(d, value)
