"""Paired analysis of saved predictions. Standard library only; no inference."""
from __future__ import annotations

import math
import random
from collections import defaultdict

from .comparison import parse_prediction
from .splits import digest

COST_FIELDS = ("input_tokens", "extra_context_tokens", "output_tokens",
               "latency_seconds", "selection_latency_seconds")
CATEGORIES = ("both_correct", "improved", "regressed", "both_wrong")


def validate_predictions(rows, *, protocol=None, required_models=None):
    """Reject incomplete pairing, ambiguous types, and inconsistent cached evidence."""
    if protocol is not None and not isinstance(protocol, dict):
        raise ValueError("Saved protocol must be a JSON object")
    grouped = defaultdict(dict)
    identities = {}
    hashes = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each prediction must be a JSON object")
        for key in ("id", "model", "repository", "label", "prediction"):
            if key not in row:
                raise ValueError(f"Missing prediction field: {key}")
        for key in ("id", "model", "repository"):
            if not isinstance(row[key], str) or not row[key].strip():
                raise ValueError(f"{key} must be a nonempty string")
        if type(row["label"]) is not bool:
            raise ValueError("label must be a Boolean")
        if row["prediction"] is not None and type(row["prediction"]) is not bool:
            raise ValueError("prediction must be a Boolean or null (invalid output)")
        valid = row["prediction"] is not None
        correct = valid and row["prediction"] == row["label"]
        for key, expected in (("valid", valid), ("correct", correct)):
            if key in row and (type(row[key]) is not bool or row[key] != expected):
                raise ValueError(f"Inconsistent cached {key}: {row['id']}")
        if "raw_output" in row:
            if not isinstance(row["raw_output"], str) or parse_prediction(row["raw_output"]) != row["prediction"]:
                raise ValueError(f"raw_output disagrees with prediction: {row['id']}")
        for key in COST_FIELDS:
            value = row.get(key)
            if value is not None:
                if (type(value) not in (int, float) or not math.isfinite(value) or value < 0
                        or (key.endswith("tokens") and type(value) is not int)):
                    raise ValueError(f"Invalid {key}: {row['id']}")
        if "truncated" in row and type(row["truncated"]) is not bool:
            raise ValueError("truncated must be a Boolean when recorded")
        key = row["id"]
        if key in grouped[row["model"]]:
            raise ValueError(f"Duplicate (model, id): {row['model']}, {key}")
        identity = (row["repository"], row["label"], row.get("pr_number"))
        if key in identities and identities[key] != identity:
            raise ValueError(f"Label/repository/PR mismatch across models: {key}")
        identities[key] = identity
        grouped[row["model"]][key] = dict(row)
        protocol_hash = row.get("protocol_hash")
        if protocol_hash is not None and (not isinstance(protocol_hash, str) or not protocol_hash):
            raise ValueError("protocol_hash must be a nonempty string when recorded")
        hashes.add(protocol_hash)
    if not grouped:
        raise ValueError("No predictions to analyze")
    if len(hashes) != 1:
        raise ValueError("Mixed or partially missing evaluation protocol hashes")
    if required_models is not None and set(grouped) != set(required_models):
        raise ValueError("Observed model conditions do not match required_models")
    expected = set(identities)
    if any(set(values) != expected for values in grouped.values()):
        raise ValueError("Every model must have exactly the same example IDs; partial runs are rejected")
    if protocol is not None:
        ids = protocol.get("ids")
        if (not isinstance(ids, list) or not ids
                or any(not isinstance(value, str) for value in ids)
                or len(ids) != len(set(ids)) or set(ids) != expected):
            raise ValueError("Prediction IDs do not match the saved protocol")
        if hashes != {digest(protocol)}:
            raise ValueError("Prediction protocol hash does not match protocol.json")
    return {name: [grouped[name][key] for key in sorted(expected)] for name in sorted(grouped)}


def _confusion(rows):
    # Rows: true False/True. Columns: predicted False/True/invalid.
    matrix = [[0, 0, 0], [0, 0, 0]]
    for row in rows:
        column = 2 if row["prediction"] is None else int(row["prediction"])
        matrix[int(row["label"])][column] += 1
    return matrix


def _scores(matrix):
    total = sum(map(sum, matrix))
    result = {"examples": total, "accuracy": (matrix[0][0] + matrix[1][1]) / total,
              "malformed_rate": (matrix[0][2] + matrix[1][2]) / total}
    f1s = []
    for label, name in ((0, "false"), (1, "true")):
        tp = matrix[label][label]
        fp = matrix[1 - label][label]
        fn = sum(matrix[label]) - tp  # Includes invalid answers for this true class.
        result[f"precision_{name}"] = tp / (tp + fp) if tp + fp else 0.0
        result[f"recall_{name}"] = tp / (tp + fn) if tp + fn else 0.0
        result[f"support_{name}"] = sum(matrix[label])
        f1 = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0
        result[f"f1_{name}"] = f1
        f1s.append(f1)
    result["macro_f1"] = sum(f1s) / 2
    return result


def _summary(name, rows):
    matrix = _confusion(rows)
    result = {"model": name, "repositories": len({row["repository"] for row in rows}),
              **_scores(matrix), "confusion_matrix": matrix}
    for key in COST_FIELDS:
        values = [row[key] for row in rows if row.get(key) is not None]
        # Never silently report the mean of just a convenient subset.
        result[f"recorded_{key}"] = len(values)
        result[f"mean_{key}"] = sum(values) / len(values) if len(values) == len(rows) else None
    values = [row["truncated"] for row in rows if "truncated" in row]
    result["truncation_rate"] = sum(values) / len(values) if len(values) == len(rows) else None
    return result


def _interval(values):
    """95% percentile interval with linear interpolation, not a normal SE bar."""
    ordered = sorted(values)

    def quantile(q):
        position = (len(ordered) - 1) * q
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (position - lower) * (ordered[upper] - ordered[lower])

    return [quantile(0.025), quantile(0.975)]


def repository_bootstrap(grouped, reference, *, samples=2000, seed=42):
    """Resample whole repositories; use each draw for every model condition.

    Each replicate draws R repository IDs with replacement, retains every row
    in each drawn repository (including multiplicity), then scores all rows.
    Estimates are example-weighted, not an unweighted mean of repository scores.
    """
    if type(samples) is not int or samples < 1 or type(seed) is not int:
        raise ValueError("Bootstrap samples must be a positive integer; seed must be an integer")
    repositories = sorted({row["repository"] for row in grouped[reference]})
    result = {"unit": "repository", "samples": samples, "seed": seed, "confidence_level": 0.95,
              "repositories": len(repositories), "model_intervals": {}, "difference_intervals": {}}
    if len(repositories) < 2:
        result["status"] = "insufficient_repositories"
        return result
    by_repo = {name: {repo: _confusion([row for row in rows if row["repository"] == repo])
                      for repo in repositories} for name, rows in grouped.items()}
    scores = {name: {key: [] for key in ("accuracy", "macro_f1")} for name in grouped}
    differences = {name: {key: [] for key in ("accuracy", "macro_f1")}
                   for name in grouped if name != reference}
    rng = random.Random(seed)
    for _ in range(samples):
        draw = [rng.choice(repositories) for _ in repositories]
        current = {}
        for name in grouped:
            matrix = [[sum(by_repo[name][repo][label][prediction] for repo in draw)
                       for prediction in range(3)] for label in range(2)]
            current[name] = _scores(matrix)
            for key in scores[name]:
                scores[name][key].append(current[name][key])
        for name in differences:
            for key in differences[name]:
                differences[name][key].append(current[name][key] - current[reference][key])
    result.update(status="ok",
                  model_intervals={name: {key: _interval(values) for key, values in metrics.items()}
                                   for name, metrics in scores.items()},
                  difference_intervals={name: {key: _interval(values) for key, values in metrics.items()}
                                        for name, metrics in differences.items()})
    return result


def analyze_predictions(rows, *, reference="base", protocol=None, required_models=None,
                        bootstrap_samples=2000, seed=42, cost_field="input_tokens"):
    if cost_field not in ("input_tokens", "extra_context_tokens"):
        raise ValueError("cost_field must be input_tokens or extra_context_tokens")
    grouped = validate_predictions(rows, protocol=protocol, required_models=required_models)
    if reference not in grouped:
        raise ValueError(f"Reference model is absent: {reference}")
    metrics = [_summary(name, values) for name, values in grouped.items()]
    scores = {row["model"]: row for row in metrics}
    pairs, examples = [], []
    for name, values in grouped.items():
        if name == reference:
            continue
        counts = dict.fromkeys(CATEGORIES, 0)
        for base, candidate in zip(grouped[reference], values):
            before = base["prediction"] is not None and base["prediction"] == base["label"]
            after = candidate["prediction"] is not None and candidate["prediction"] == candidate["label"]
            category = ("both_correct" if before and after else "regressed" if before
                        else "improved" if after else "both_wrong")
            counts[category] += 1
            examples.append({"id": base["id"], "repository": base["repository"], "label": base["label"],
                             "reference": reference, "model": name, "category": category,
                             "reference_prediction": base["prediction"], "prediction": candidate["prediction"]})
        pairs.append({"reference": reference, "model": name, **counts,
                      "accuracy_difference": scores[name]["accuracy"] - scores[reference]["accuracy"],
                      "macro_f1_difference": scores[name]["macro_f1"] - scores[reference]["macro_f1"]})
    repositories = sorted({row["repository"] for row in grouped[reference]})
    repository_metrics = [{"repository": repo, **_summary(name, [r for r in values if r["repository"] == repo])}
                          for repo in repositories for name, values in grouped.items()]
    # Majority is a zero-call statistical control, not a token-efficiency policy.
    cost_points = [{"model": m["model"], "tokens": m[f"mean_{cost_field}"], "accuracy": m["accuracy"]}
                   for m in metrics if m["model"] != "majority" and m[f"mean_{cost_field}"] is not None]
    for point in cost_points:
        point["on_frontier"] = not any(
            other["tokens"] <= point["tokens"] and other["accuracy"] >= point["accuracy"]
            and (other["tokens"] < point["tokens"] or other["accuracy"] > point["accuracy"])
            for other in cost_points)
    return {"reference": reference, "split": (protocol or {}).get("split", "unspecified"),
            "protocol_verified": protocol is not None, "metrics": metrics,
            "paired_comparisons": pairs, "paired_examples": examples,
            "repository_metrics": repository_metrics,
            "bootstrap": repository_bootstrap(grouped, reference, samples=bootstrap_samples, seed=seed),
            "cost_field": cost_field, "cost_points": cost_points,
            "cost_omissions": [m["model"] for m in metrics if m["model"] != "majority"
                               and m[f"mean_{cost_field}"] is None]}
