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


def freeze_contract(directory, contract):
    path = Path(directory)/'run_contract.json'
    if path.exists():
        previous = json.loads(path.read_text())
        if previous != contract:
            changed = sorted(k for k in set(previous)|set(contract) if previous.get(k)!=contract.get(k))
            raise ValueError(f'Run contract changed: {changed}. Restore settings/environment or choose a new RUN_NAME.')
    else:
        if list(Path(directory).glob('checkpoint-*')):
            raise ValueError('Existing checkpoints without a run contract: use a new run directory.')
        atomic_json(path, contract)


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
