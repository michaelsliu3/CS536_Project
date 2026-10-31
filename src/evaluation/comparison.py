"""Evaluation-only comparison and resumable per-example evidence."""
from __future__ import annotations
import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from .splits import as_bool, build_manifest, digest
from .prompts import SYSTEM_PROMPT, build_user_prompt

# The frozen 3B base that the evaluated adapter must have been trained on.
MODEL_NAME = 'Qwen/Qwen2.5-Coder-3B-Instruct'


# Streaming SHA-256 used to fingerprint adapter weights and the evaluation code itself.
def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


# Strict parser: exactly one JSON object with a single Boolean is_merged key. Anything else is invalid.
def parse_prediction(text):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError('Duplicate JSON key')
            result[k] = v
        return result
    try:
        value = json.loads(text, object_pairs_hook=unique)
        # type(...) is bool rejects 0/1, which json.loads would otherwise hand back as ints.
        if isinstance(value, dict) and set(value) == {'is_merged'} and type(value['is_merged']) is bool:
            return value['is_merged']
    except (ValueError, TypeError):
        pass
    return None


# Constant-classifier baseline, fixed from training labels only.
def majority_label(labels):
    labels = [as_bool(v) for v in labels]
    if not labels:
        raise ValueError('No training labels')
    return sum(labels) > len(labels) / 2  # Ties always resolve to False.


def prepare_data(extract_root, manifest_path, run_manifest_path, split='validation', final_test=False):
    if split not in {'validation', 'test'}:
        raise ValueError('Choose validation or test')
    # The test partition is touched once, after the protocol is frozen; require an explicit flag.
    if split == 'test' and not final_test:
        raise ValueError('Final test requires explicit protocol freeze acknowledgment')
    root = Path(extract_root)
    manifest = json.loads(Path(manifest_path).read_text())
    run = json.loads(Path(run_manifest_path).read_text())
    # Refuse to evaluate an adapter whose training run did not embed this exact split manifest.
    if run.get('model') != MODEL_NAME or run.get('split_manifest') != manifest:
        raise ValueError('Adapter run must identify Qwen3B and contain this exact split manifest. Historical runs need their original protocol; do not retrofit a new test split.')
    rows, records = [], {}
    # Rebuild split rows from the extracted dataset; IDs are paths relative to the extract root.
    for path in sorted(root.rglob('*.json')):
        record = json.loads(path.read_text())
        rid = path.relative_to(root).as_posix()
        diff = str(record.get('diff_hunk') or '').replace('\r\n','\n').strip()
        if not diff:
            raise ValueError(f'Empty diff: {rid}')
        rows.append({'id': rid, 'full_name': record['full_name'], 'pr_number': record['pr_number'],
                     'lang': record.get('lang'), 'merged': record['merged'],
                     'content_hash': hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest(),
                     'diff_hash': hashlib.sha256(diff.encode()).hexdigest()})
        records[rid] = path
    # Recomputing the manifest proves the on-disk data still matches the frozen split.
    reconstructed = build_manifest(rows, seed=manifest['seed'], validation_limit=manifest['validation_limit'], demo_limit=manifest['demo_limit'])
    if reconstructed != manifest:
        raise ValueError('Dataset or split fingerprint mismatch')
    ids = manifest['validation_generation_ids' if split == 'validation' else 'test_generation_ids']
    labels = {r['id']: r['merged'] for r in rows}
    # The majority baseline uses training labels only, never validation or test.
    constant = majority_label(labels[rid] for rid in manifest['ids']['train'])
    return manifest, run, [(rid, json.loads(records[rid].read_text())) for rid in ids], constant


def summarize(rows):
    from sklearn.metrics import accuracy_score, precision_recall_fscore_support
    output = []
    # One metrics row per model condition; macro-F1 is averaged over the two real classes only.
    for name in ('majority', 'base', 'adapter'):
        selected = [r for r in rows if r['model'] == name]
        if not selected:
            continue
        y = [int(r['label']) for r in selected]
        # Invalid is a third prediction category: always incorrect, never silently False.
        p = [-1 if r['prediction'] is None else int(r['prediction']) for r in selected]
        precision, recall, f1, support = precision_recall_fscore_support(y, p, labels=[0,1], zero_division=0)
        output.append({'model':name, 'examples':len(y), 'accuracy':accuracy_score(y,p),
                       'macro_f1':float(f1.mean()), 'malformed_rate':p.count(-1)/len(p),
                       'precision_false':float(precision[0]), 'precision_true':float(precision[1]),
                       'recall_false':float(recall[0]), 'recall_true':float(recall[1]),
                       'support_false':int(support[0]), 'support_true':int(support[1])})
    return output


def evaluate_cached(examples, constant, predict, protocol, output_dir):
    """predict(record, use_adapter) returns raw output and timing/token metadata."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # The protocol fingerprint includes the example IDs and majority label so caches cannot be mixed.
    protocol = {**protocol, 'ids':[rid for rid,_ in examples], 'majority_label':constant}
    path = output_dir / 'protocol.json'
    if path.exists():
        if json.loads(path.read_text()) != protocol:
            raise ValueError('Output directory contains a different evaluation protocol')
    else:
        # 'x' mode creates the file exactly once; a concurrent writer gets an error instead of a silent overwrite.
        with path.open('x') as f:
            json.dump(protocol, f, indent=2)
    records = []
    for rid, example in examples:
        for name in ('majority','base','adapter'):
            # One file per (example, model) pair so an interrupted run resumes without recomputation.
            item = output_dir / f'{digest([rid,name])}.json'
            if item.exists():
                row = json.loads(item.read_text())
                if row['id'] != rid or row['model'] != name or row['protocol_hash'] != digest(protocol):
                    raise ValueError('Corrupted cache identity')
            else:
                # Majority predictions cost nothing; the real conditions call the supplied predict() function.
                if name == 'majority':
                    raw, metadata = json.dumps({'is_merged':constant}), {'latency_seconds':0, 'input_tokens':0, 'output_tokens':0}
                else:
                    raw, metadata = predict(example, name == 'adapter')
                prediction = parse_prediction(raw)
                row = {'id':rid, 'repository':example['full_name'], 'pr_number':example['pr_number'],
                       'model':name, 'label':as_bool(example['merged']), 'raw_output':raw,
                       'prediction':prediction, 'valid':prediction is not None,
                       'correct':prediction is not None and prediction == as_bool(example['merged']),
                       'protocol_hash':digest(protocol), **metadata}
                # Write-then-rename keeps the cache consistent if the process dies mid-write.
                temporary = item.with_suffix('.tmp')
                temporary.write_text(json.dumps(row, indent=2))
                temporary.replace(item)
            records.append(row)
    # Flat exports for plotting notebooks that should not need to load a model.
    (output_dir/'predictions.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
    (output_dir/'metrics.json').write_text(json.dumps(summarize(records),indent=2))
    return records


def resolve_base_source(adapter_config, revision, local_model_dir=None, run=None):
    """Validate local provenance without rewriting the adapter's original metadata."""
    recorded = adapter_config.get('base_model_name_or_path')
    if local_model_dir is None:
        if recorded != MODEL_NAME:
            raise ValueError('Adapter was trained from a local path; supply local_model_dir and its run manifest.')
        return MODEL_NAME, {}
    root = Path(local_model_dir).expanduser().resolve()
    run = run or {}
    training = run.get('training_config', {})
    if run.get('model') != MODEL_NAME or run.get('base_revision') != revision:
        raise ValueError('Local base requires matching model and revision in the training run manifest')
    if recorded not in (MODEL_NAME, training.get('local_model_dir')):
        raise ValueError('Adapter base path does not match training provenance')
    index = root / 'model.safetensors.index.json'
    expected = training.get('local_model_index_sha256')
    if not expected or not index.is_file() or file_hash(index) != expected:
        raise ValueError('Local base weight index does not match the training fingerprint')
    shards = sorted(set(json.loads(index.read_text())['weight_map'].values()))
    for name in shards:
        shard = (root / name).resolve()
        if not shard.is_relative_to(root) or not shard.is_file():
            raise ValueError(f'Missing or invalid local base shard: {name}')
    # The index fingerprint is training evidence, not a checksum of weight contents.
    # Hash actual local weights into the evaluation protocol to prevent cache mixing.
    return str(root), {'base_source': 'local', 'base_revision_source': 'training_manifest',
                       'base_files': {name: file_hash(root / name)
                                      for name in ['config.json', 'model.safetensors.index.json', *shards]}}


class QwenComparison:
    """One frozen base, unmerged adapter; disable_adapter provides the matched baseline."""
    def __init__(self, adapter_dir, max_seq_length=2048, revision='main', local_model_dir=None, run=None):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
        from peft import PeftModel
        # bitsandbytes NF4 requires CUDA; the Mac only consumes the saved metrics.
        if not torch.cuda.is_available():
            raise RuntimeError('Run inference on cloud NVIDIA; use saved metrics on the Mac.')
        adapter_dir = Path(adapter_dir)
        config = json.loads((adapter_dir/'adapter_config.json').read_text())
        source, source_identity = resolve_base_source(config, revision, local_model_dir, run)
        # disable_adapter() only removes the LoRA deltas; trained biases or saved modules would leak into the "base".
        if config.get('bias', 'none') != 'none' or config.get('modules_to_save'):
            raise ValueError('Matched disabled-adapter baseline requires bias=none and no modules_to_save; load a separate untouched base for other adapters.')
        weights = list(adapter_dir.glob('adapter_model*.safetensors')) + list(adapter_dir.glob('adapter_model*.bin'))
        if not weights:
            raise FileNotFoundError('Adapter weights missing')
        self.tokenizer = AutoTokenizer.from_pretrained(adapter_dir, local_files_only=True)
        self.tokenizer.truncation_side = 'right'
        self.max_seq_length = max_seq_length
        if max_seq_length <= 32:
            raise ValueError('Sequence length must exceed generation reserve')
        # Both conditions share one NF4-quantized base, so the adapter is the only difference between them.
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        base = AutoModelForCausalLM.from_pretrained(source, revision=revision, local_files_only=local_model_dir is not None,
            device_map={'':0}, dtype=dtype, quantization_config=BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type='nf4', bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=dtype))
        # Record the exact Hub commit so the evaluation can be reproduced later.
        resolved_revision = revision if local_model_dir is not None else getattr(base.config, '_commit_hash', None)
        if not resolved_revision:
            raise ValueError('Cannot identify resolved base-model revision')
        self.model = PeftModel.from_pretrained(base, adapter_dir, is_trainable=False).eval()
        self.model.config.use_cache = True
        # Everything that could change outputs is hashed into the identity stored with the results.
        self.identity = {**source_identity, 'model': MODEL_NAME, 'base_revision':resolved_revision,
            'adapter_files':{p.name:file_hash(p) for p in [adapter_dir/'adapter_config.json',*weights]},
            'tokenizer_hash':digest(self.tokenizer.backend_tokenizer.to_str()),
            'chat_template':self.tokenizer.chat_template, 'max_seq_length':max_seq_length,
            'quantization':'nf4-double', 'dtype':str(dtype), 'gpu':torch.cuda.get_device_name(0),
            'torch':torch.__version__, 'generation':{'max_new_tokens':32,'do_sample':False},
            'prompt_hash':file_hash(Path(__file__).with_name('prompts.py')),
            'evaluation_code_hash':file_hash(__file__)}

    def predict(self, example, use_adapter):
        import torch
        from contextlib import nullcontext
        prompt = self.tokenizer.apply_chat_template([
            {'role':'system','content':SYSTEM_PROMPT},
            {'role':'user','content':build_user_prompt(example)}], tokenize=False, add_generation_prompt=True)
        # Tokenize once without truncation to detect whether the prompt exceeded the window.
        full = self.tokenizer(prompt)['input_ids']
        # Reserve 32 tokens for the JSON answer when truncating the prompt.
        batch = self.tokenizer(prompt,return_tensors='pt',truncation=True,max_length=self.max_seq_length-32).to('cuda')
        # Synchronize so latency covers the whole generate() call, not just the kernel launch.
        torch.cuda.synchronize()
        started=time.perf_counter()
        # Same model object for both conditions; disable_adapter() yields the matched base-model baseline.
        with torch.inference_mode(), (nullcontext() if use_adapter else self.model.disable_adapter()):
            result=self.model.generate(**batch,max_new_tokens=32,do_sample=False,
                pad_token_id=self.tokenizer.pad_token_id if self.tokenizer.pad_token_id is not None else self.tokenizer.eos_token_id,
                eos_token_id=self.tokenizer.eos_token_id)
        torch.cuda.synchronize()
        elapsed=time.perf_counter()-started
        generated=result[0,batch['input_ids'].shape[1]:]
        return self.tokenizer.decode(generated,skip_special_tokens=True), {
            'input_tokens':int(batch['input_ids'].shape[1]), 'output_tokens':len(generated),
            'truncated':len(full)>self.max_seq_length-32, 'latency_seconds':elapsed}
