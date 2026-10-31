import tempfile
from pathlib import Path
import torch
from transformers import Qwen2Config,Qwen2ForCausalLM,TrainingArguments,Trainer,TrainerCallback,default_data_collator
from peft import LoraConfig,get_peft_model
from src.training.runtime import RunLogger,latest_checkpoint,ResumeSchedule

def model():
    torch.manual_seed(42)
    base=Qwen2ForCausalLM(Qwen2Config(vocab_size=20,hidden_size=16,intermediate_size=32,num_hidden_layers=1,num_attention_heads=2,num_key_value_heads=2))
    return get_peft_model(base,LoraConfig(r=2,lora_alpha=4,lora_dropout=0,bias='none',task_type='CAUSAL_LM',target_modules=['q_proj','v_proj']))

data=[{'input_ids':[1,2,3,4], 'attention_mask':[1]*4,'labels':[-100,-100,3,4]} for _ in range(8)]
class Interrupt(TrainerCallback):
    def on_save(self,args,state,control,**kwargs):
        if state.global_step==2:raise RuntimeError('simulated interruption')

def args(d):
    return TrainingArguments(output_dir=d,use_cpu=True,max_steps=4,per_device_train_batch_size=1,learning_rate=.001,lr_scheduler_type="cosine",
        save_strategy='steps',save_steps=1,save_total_limit=None,logging_steps=1,report_to='none',seed=42,disable_tqdm=True)

import unittest

class TrainerResumeTests(unittest.TestCase):
    def test_optimizer_and_rng_resume_matches_uninterrupted(self):
        with tempfile.TemporaryDirectory() as d:
            ref=model();Trainer(model=ref,args=args(d+'/reference'),train_dataset=data,data_collator=default_data_collator).train()
            interrupted=model()
            try:
                Trainer(model=interrupted,args=args(d+'/resumed'),train_dataset=data,data_collator=default_data_collator,
                    callbacks=[RunLogger(d+'/resumed'),Interrupt()]).train()
            except RuntimeError as e:
                assert str(e)=='simulated interruption'
            checkpoint=latest_checkpoint(d+'/resumed');assert checkpoint.endswith('checkpoint-2')
            resumed=model();trainer=Trainer(model=resumed,args=args(d+'/resumed'),train_dataset=data,data_collator=default_data_collator,callbacks=[RunLogger(d+'/resumed')])
            trainer.train(resume_from_checkpoint=checkpoint)
            assert trainer.state.global_step==4
            for (n,a),(m,b) in zip(ref.named_parameters(),resumed.named_parameters()):
                if a.requires_grad:torch.testing.assert_close(a,b,rtol=1e-5,atol=1e-7)
            trainer._save_checkpoint(resumed,trial=None)
            print('PASS: interrupted at step 2, resumed to step 4; adapter weights match uninterrupted training; final save API works')

    def test_extend_completed_run(self):
        with tempfile.TemporaryDirectory() as d:
            first_args = args(d)
            first_args.max_steps = 2
            trainer = Trainer(model=model(), args=first_args, train_dataset=data,
                data_collator=default_data_collator, callbacks=[RunLogger(d), ResumeSchedule()])
            trainer.train()
            checkpoint = latest_checkpoint(d)
            resumed = model()
            class Capture(TrainerCallback):
                def on_train_begin(self, args, state, control, optimizer=None, **kwargs):
                    self.lr = optimizer.param_groups[0]['lr']
            capture = Capture()
            trainer = Trainer(model=resumed, args=args(d), train_dataset=data,
                data_collator=default_data_collator, callbacks=[RunLogger(d), ResumeSchedule(), capture])
            trainer.train(resume_from_checkpoint=checkpoint)
            self.assertEqual(trainer.state.global_step, 4)
            self.assertGreater(capture.lr, 0)

    def test_resume_uses_new_validation_interval(self):
        with tempfile.TemporaryDirectory() as d:
            first_args = args(d)
            first_args.max_steps = 2
            first_args.eval_strategy = "steps"
            first_args.eval_steps = 1
            Trainer(model=model(), args=first_args, train_dataset=data, eval_dataset=data,
                data_collator=default_data_collator, callbacks=[RunLogger(d), ResumeSchedule()]).train()
            next_args = args(d)
            next_args.eval_strategy = "steps"
            next_args.max_steps = 6
            next_args.eval_steps = 4
            class Capture(TrainerCallback):
                def __init__(self): self.steps = []
                def on_evaluate(self, args, state, control, **kwargs):
                    self.steps.append(state.global_step)
            capture = Capture()
            trainer = Trainer(model=model(), args=next_args, train_dataset=data, eval_dataset=data,
                data_collator=default_data_collator, callbacks=[RunLogger(d), ResumeSchedule(), capture])
            trainer.train(resume_from_checkpoint=latest_checkpoint(d))
            self.assertEqual(trainer.state.eval_steps, 4)
            self.assertIn(4, capture.steps)
            self.assertTrue(set(capture.steps).issubset({4, 6}))  # Some versions also evaluate at completion.
