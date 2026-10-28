"""Frozen repository splits for future ContextCRBench runs (no model dependencies)."""
from __future__ import annotations
import hashlib
import json
from collections import Counter
from pathlib import Path


def as_bool(value):
    if isinstance(value, bool):
        return value
    if str(value).strip().lower() in {'true', '1'}:
        return True
    if str(value).strip().lower() in {'false', '0'}:
        return False
    raise ValueError(f'Invalid merged label: {value!r}')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def build_manifest(rows, seed=42, validation_limit=200, demo_limit=12):
    if validation_limit < 1 or demo_limit < 1:
        raise ValueError('Sample limits must be positive')
    rows = sorted(rows, key=lambda r: r['id'])
    if len({r['id'] for r in rows}) != len(rows):
        raise ValueError('Duplicate record IDs')
    for r in rows:
        if not r['full_name'] or r['pr_number'] is None or not r['diff_hash']:
            raise ValueError(f'Missing repository/PR/diff identity: {r["id"]}')
        as_bool(r['merged'])
    # Exact normalized diff duplicates: retain the smallest stable ID, without using labels to choose it.
    seen, retained, excluded = {}, [], []
    for r in rows:
        if r['diff_hash'] in seen:
            original = seen[r['diff_hash']]
            if as_bool(original['merged']) != as_bool(r['merged']):
                raise ValueError(f'Conflicting labels for duplicate diff: {original["id"]}, {r["id"]}')
            excluded.append({'id': r['id'], 'duplicate_of': original['id']})
        else:
            seen[r['diff_hash']] = r
            retained.append(r)
    rank = lambda value: digest([seed, value])
    repos = sorted({r['full_name'] for r in retained}, key=rank)
    if len(repos) < 10:
        raise ValueError('Need at least 10 repositories for this 80/10/10 protocol; review grouped CV instead.')
    n_test = max(1, round(len(repos) * .1))
    n_val = max(1, round(len(repos) * .1))
    groups = {'test': repos[:n_test], 'validation': repos[n_test:n_test+n_val], 'train': repos[n_test+n_val:]}
    assignments, summary = {}, {}
    for name, group in groups.items():
        group_set = set(group)
        selected = [r for r in retained if r['full_name'] in group_set]
        assignments[name] = sorted((r['id'] for r in selected), key=rank)
        counts = Counter(as_bool(r['merged']) for r in selected)
        if len(counts) != 2:
            raise ValueError(f'{name} lacks both classes; review protocol, do not search seeds for favorable scores.')
        summary[name] = {'repositories': len(group), 'rows': len(selected), 'labels': {str(k): v for k,v in counts.items()}}
    val_ids = assignments['validation'][:validation_limit]
    return {'version': 1, 'seed': seed, 'target_repository_fractions': [0.8,0.1,0.1],
            'validation_limit': validation_limit, 'demo_limit': demo_limit,
            'dataset_fingerprint': digest([{k:v for k,v in r.items() if k != 'path'} for r in rows]),
            'repositories': groups, 'ids': assignments, 'validation_generation_ids': val_ids,
            'demo_ids': val_ids[:demo_limit], 'test_generation_ids': assignments['test'],
            'excluded_exact_duplicates': excluded, 'summary': summary}


def freeze_manifest(rows, path, **kwargs):
    """Reuse only identical manifests; never silently overwrite changed data or settings."""
    path = Path(path)
    manifest = build_manifest(rows, **kwargs)
    if path.exists():
        if json.loads(path.read_text()) != manifest:
            raise ValueError(f'Manifest mismatch: {path}. Use a new version and retrain; do not overwrite historical splits.')
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x') as f:
            json.dump(manifest, f, indent=2)
    return manifest
