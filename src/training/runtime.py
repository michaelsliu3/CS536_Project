"""Persistent run metadata, append-only telemetry, and verified Trainer recovery."""
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from transformers import TrainerCallback


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w') as f:
        json.dump(value, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):h.update(block)
    return h.hexdigest()


def packages():
    return {p: importlib.metadata.version(p) for p in
            ['torch','transformers','peft','accelerate','bitsandbytes','datasets','numpy','tokenizers','huggingface-hub','safetensors']}


# Provenance and the requested training horizon do not determine model identity.
PROVENANCE_KEYS = {"additional_epochs", "epochs", "packages", "python", "runtime_sha256", "training_code_hash",
                   "save_steps", "save_seconds", "local_model_dir", "requested_revision"}
LEGACY_DEFAULTS = {"optimizer": "paged_adamw_8bit", "weight_decay": 0.01,
                   "warmup_ratio": 0.03, "scheduler": "cosine",
                   "max_train_examples": None, "max_validation_examples": None}


def training_identity(config):
    return {k: v for k, v in config.items() if k not in PROVENANCE_KEYS}


def select_run(root, name, config):
    """Reuse a matching configuration in this run family; never overwrite a mismatch.

    Caller must hold the root selection lock until configuration is saved.
    Legacy contracts are read only for migration; historical provenance stays intact.
    """
    root = Path(root)
    identity = training_identity(config)
    candidates = [root / name] + sorted(root.glob(name + '--*'))
    for directory in candidates:
        path = directory / 'training_config.json'
        legacy = directory / 'run_contract.json'
        if path.exists():
            previous = json.loads(path.read_text())
        elif legacy.exists():
            previous = {**LEGACY_DEFAULTS, **json.loads(legacy.read_text())}
        else:
            continue
        if training_identity(previous) == identity:
            return directory
    directory = root / name
    if directory.exists() and any(directory.iterdir()):
        suffix = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
        directory = root / (name + '--' + suffix)
        if directory.exists() and any(directory.iterdir()):
            directory = root / (name + '--' + suffix + '-' + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def plan_epochs(directory, additional_epochs, checkpoint=None):
    """Persist an additional-epoch request so retries keep its original target.

    Called under the run lock. Once the verified checkpoint reaches the target,
    the next setup invocation starts a new request from that checkpoint's epoch.
    """
    import math
    if isinstance(additional_epochs, bool) or not isinstance(additional_epochs, (int, float)) or not math.isfinite(additional_epochs) or additional_epochs <= 0:
        raise ValueError("NUM_TRAIN_EPOCHS must be a positive number of additional epochs")
    path = Path(directory) / 'epoch_plan.json'
    epoch = 0.0
    if checkpoint:
        epoch = float(json.loads((Path(checkpoint) / 'trainer_state.json').read_text()).get('epoch') or 0)
    if path.exists():
        plan = json.loads(path.read_text())
        if epoch + 1e-8 < plan['total_epochs']:
            return plan
    plan = {'request_id': uuid.uuid4().hex, 'started_at': now(),
            'start_epoch': epoch, 'additional_epochs': additional_epochs,
            'total_epochs': epoch + additional_epochs}
    atomic_json(path, plan)
    return plan


class ResumeSchedule(TrainerCallback):
    """Recompute LambdaLR's current rate for an extended total training horizon.

    Trainer restores optimizer moments and scheduler step. Its newly constructed
    cosine lambda uses the new horizon; synchronize the optimizer before step one.
    This is a continuation, not equivalent to originally training with that horizon.
    """
    def on_train_begin(self, args, state, control, optimizer=None, lr_scheduler=None, **kwargs):
        # Trainer loads saved intervals before this callback. Honor the current
        # validation cadence without altering restored training progress.
        if args.eval_steps is not None:
            import math
            state.eval_steps = (math.ceil(state.max_steps * args.eval_steps)
                                if 0 < args.eval_steps < 1 else args.eval_steps)
        if state.global_step and hasattr(lr_scheduler, 'lr_lambdas'):
            rates = [base * fn(lr_scheduler.last_epoch)
                     for base, fn in zip(lr_scheduler.base_lrs, lr_scheduler.lr_lambdas)]
            for group, rate in zip(optimizer.param_groups, rates):
                group['lr'] = rate
            lr_scheduler._last_lr = rates


def complete_checkpoint(path):
    path=Path(path)
    required=['trainer_state.json','optimizer.pt','scheduler.pt','rng_state.pth','adapter_config.json']
    if not all((path/p).is_file() for p in required) or not (path/'adapter_model.safetensors').is_file():
        raise ValueError(f'Incomplete single-GPU checkpoint: {path}')
    checks={p.name:sha256(p) for p in path.iterdir() if p.is_file() and p.name!='complete.json'}
    atomic_json(path/'complete.json',checks)


def valid_checkpoint(path):
    try:
        checks=json.loads((path/'complete.json').read_text())
        return bool(checks) and all((path/name).is_file() and sha256(path/name)==value for name,value in checks.items())
    except (OSError,ValueError):return False


def latest_checkpoint(directory):
    candidates=sorted(Path(directory).glob('checkpoint-*'),key=lambda p:int(p.name.split('-')[-1]),reverse=True)
    for path in candidates:
        if valid_checkpoint(path):return str(path)
    if candidates:raise RuntimeError('No verified complete checkpoint. Inspect storage; refusing to restart training silently.')
    return None


class RunLogger(TrainerCallback):
    def __init__(self, directory, save_seconds=300, keep=3, session=None):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.save_seconds=save_seconds;self.keep=keep;self.session=session or uuid.uuid4().hex
        self.started=time.monotonic();self.last_save=self.started

    def event(self, kind, **data):
        record={'timestamp_utc':now(),'session':self.session,'event':kind,
                'session_elapsed_seconds':time.monotonic()-self.started,**data}
        with (self.directory/'training_log.jsonl').open('a') as f:
            f.write(json.dumps(record,default=str)+'\n');f.flush();os.fsync(f.fileno())

    def on_log(self,args,state,control,logs=None,**kwargs):
        import torch
        memory={}
        if torch.cuda.is_available():
            memory={'cuda_allocated_bytes':torch.cuda.memory_allocated(),
                    'cuda_reserved_bytes':torch.cuda.memory_reserved(),
                    'cuda_peak_allocated_bytes':torch.cuda.max_memory_allocated()}
        self.event('metrics', **{'step':state.global_step,'epoch':state.epoch,**(logs or {}),**memory})

    def on_step_end(self,args,state,control,**kwargs):
        if time.monotonic()-self.last_save>=self.save_seconds:control.should_save=True
        return control

    def on_save(self,args,state,control,**kwargs):
        path=Path(args.output_dir)/f'checkpoint-{state.global_step}'
        config = self.directory / "training_config.json"
        if config.exists():
            atomic_json(path / "training_config.json", json.loads(config.read_text()))
        complete_checkpoint(path)
        self.last_save=time.monotonic()
        self.event('checkpoint_complete',step=state.global_step,path=str(path))
        # Only prune after a new checkpoint has been fully written and verified.
        complete=sorted((p for p in Path(args.output_dir).glob('checkpoint-*') if (p/'complete.json').exists()),
                        key=lambda p:int(p.name.split('-')[-1]),reverse=True)
        for old in complete[self.keep:]:shutil.rmtree(old)

    def on_train_end(self,args,state,control,**kwargs):
        self.event('training_loop_finished',step=state.global_step,epoch=state.epoch)


def acquire_run_lock(directory):
    """Hold returned file for the lifetime of this Linux/macOS training kernel."""
    import fcntl
    path=Path(directory)/'.run.lock'
    handle=path.open('a+')
    try:
        fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError('Another kernel is using this run directory. Reconnect to it or stop it before resuming.')
    return handle
