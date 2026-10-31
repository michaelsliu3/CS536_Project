"""Write auditable tables and a Markdown report from paired analysis."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from .analysis import CATEGORIES


def _json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def _csv(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row if key != "confusion_matrix"))
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _number(value):
    return "not recorded" if value is None else f"{value:.3f}"


def report_markdown(result, *, synthetic=False):
    title = "SYNTHETIC DEMO: saved prediction analysis" if synthetic else "Saved prediction analysis"
    metrics = result["metrics"]
    first = metrics[0]
    lines = [f"# {title}", "",
             f"Recorded split: **{_cell(result['split'])}**. Paired examples: **{first['examples']}**. "
             f"Repositories: **{first['repositories']}**. "
             f"Class support: False={first['support_false']}, True={first['support_true']}.", ""]
    if synthetic:
        lines += ["**These are artificial test data, not ContextCRBench results.**", ""]
    if not result["protocol_verified"]:
        lines += ["No saved protocol was supplied. Split membership and experiment identity are unverified; "
                  "do not describe this as an independent final-test result.", ""]
    lines += ["## Metrics", "",
              "Invalid answers are always incorrect. Macro-F1 averages the two Boolean classes; "
              "invalid answers contribute false negatives for their true class.", "",
              "| Method | Accuracy (%) | Macro-F1 | Invalid (%) | Mean input tokens | Mean extra tokens | Reviewer latency (s) | Selector latency (s) |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in metrics:
        lines.append(f"| {_cell(row['model'])} | {100 * row['accuracy']:.2f} | {row['macro_f1']:.3f} | "
                     f"{100 * row['malformed_rate']:.2f} | {_number(row['mean_input_tokens'])} | "
                     f"{_number(row['mean_extra_context_tokens'])} | {_number(row['mean_latency_seconds'])} | "
                     f"{_number(row['mean_selection_latency_seconds'])} |")
    lines += ["", "Missing/incomplete cost fields are not averaged over partial records. "
              "Reviewer latency follows the recorded `latency_seconds` field; it is not end-to-end "
              "latency when tokenization or selector time was omitted.", "", "## Paired changes", "",
              f"Reference: **{_cell(result['reference'])}**. Differences below are candidate minus reference, in percentage points.", "",
              "| Candidate | Both correct | Improved | Regressed | Both wrong | Accuracy difference | 95% paired interval |",
              "| --- | ---: | ---: | ---: | ---: | ---: | --- |"]
    bootstrap = result["bootstrap"]
    for pair in result["paired_comparisons"]:
        bounds = bootstrap["difference_intervals"].get(pair["model"], {}).get("accuracy")
        interval = "unavailable" if bounds is None else f"[{100 * bounds[0]:+.2f}, {100 * bounds[1]:+.2f}]"
        lines.append(f"| {_cell(pair['model'])} | {pair['both_correct']} | {pair['improved']} | "
                     f"{pair['regressed']} | {pair['both_wrong']} | {100 * pair['accuracy_difference']:+.2f} | {interval} |")
    lines += ["", f"Bootstrap: {bootstrap['samples']} draws, seed {bootstrap['seed']}, "
              "95% percentile intervals. Each draw samples whole repositories with replacement and "
              "uses the same draw for all methods. Scores remain example-weighted. Accuracy and macro-F1 "
              "intervals, including paired differences, are stored in `summary.json`.", ""]
    if bootstrap["status"] != "ok":
        lines += ["Fewer than two repositories: repository-bootstrap intervals are unavailable.", ""]
    lines += ["These intervals describe resampling uncertainty conditional on the saved predictions. "
              "They do not measure training-seed variation or establish independence between repositories. "
              "With few repositories or strong size imbalance, intervals can be unstable; report coverage "
              "and avoid treating an interval as proof of generalization.", "", "## Accuracy and token cost", "",
              f"Cost axis: **{result['cost_field']}**. The frontier uses observed point estimates only; "
              "it is not a statistical superiority test. The zero-call majority control is excluded.", ""]
    if result["cost_field"] == "input_tokens":
        lines += ["Total input-token comparisons do not establish savings in optional context tokens.", ""]
    if result["cost_points"]:
        lines += ["| Method | Mean tokens | Accuracy (%) | On empirical frontier |", "| --- | ---: | ---: | --- |"]
        for point in result["cost_points"]:
            lines.append(f"| {_cell(point['model'])} | {point['tokens']:.2f} | {100 * point['accuracy']:.2f} | {point['on_frontier']} |")
    else:
        lines += ["No complete measured cost data are available."]
    if result["cost_omissions"]:
        lines += ["", "Missing/incomplete costs: " + ", ".join(_cell(name) for name in result["cost_omissions"]) + "."]
    lines += ["", "## Illustrative examples", "",
              "Selection rule: the first up to three lexicographically sorted IDs in each paired category. "
              "These examples explain outcomes; they do not change the metrics. Full paired outcomes are in `paired_examples.csv`.", ""]
    for pair in result["paired_comparisons"]:
        lines += [f"### {_cell(pair['model'])} versus {_cell(result['reference'])}", ""]
        for category in CATEGORIES:
            ids = [row["id"] for row in result["paired_examples"]
                   if row["model"] == pair["model"] and row["category"] == category][:3]
            lines.append(f"- {category}: " + (", ".join(_cell(key) for key in ids) if ids else "no examples"))
        lines.append("")
    lines += ["## Interpretation limits", "",
              "This tool analyzes supplied artifacts; it does not run a model, audit dataset chronology, "
              "recover missing predictions, or verify training exposure. Merge status remains a proxy label. "
              "A base/adapter comparison measures adaptation; a context-policy comparison requires a frozen "
              "reviewer and identical generation settings. Use validation for development and keep final "
              "test decisions frozen. Do not select favorable seeds, budgets, or examples from this report.", ""]
    return "\n".join(lines)


def write_report(result, output_dir, *, provenance, plots=False):
    """Write only to a new/empty directory; preserve source artifacts and old reports."""
    output = Path(output_dir)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Report output must be a new or empty directory")
    if plots:
        import matplotlib  # Check optional dependency before writing any artifacts.
    output.mkdir(parents=True, exist_ok=True)
    _json(output / "summary.json", result)
    _csv(output / "metrics.csv", result["metrics"])
    _csv(output / "repository_metrics.csv", result["repository_metrics"])
    _csv(output / "paired_examples.csv", result["paired_examples"])
    pairs = []
    for pair in result["paired_comparisons"]:
        row = dict(pair)
        intervals = result["bootstrap"]["difference_intervals"].get(pair["model"], {})
        for metric in ("accuracy", "macro_f1"):
            bounds = intervals.get(metric, [None, None])
            row[f"{metric}_difference_low"] = bounds[0]
            row[f"{metric}_difference_high"] = bounds[1]
        pairs.append(row)
    _csv(output / "paired_comparisons.csv", pairs)
    synthetic = provenance.get("synthetic", False)
    (output / "report.md").write_text(report_markdown(result, synthetic=synthetic), encoding="utf-8")
    if plots:
        from .plots import create_plots
        create_plots(result, output, synthetic=synthetic)
    manifest = {"schema_version": 1, "source": provenance, "plots": plots,
                "bootstrap": {key: value for key, value in result["bootstrap"].items()
                              if key not in ("model_intervals", "difference_intervals")},
                "reference": result["reference"], "cost_field": result["cost_field"],
                "artifacts": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in sorted(output.iterdir()) if path.is_file()}}
    if plots:
        manifest["matplotlib_version"] = matplotlib.__version__
    # The manifest appears last, so its presence identifies a completed report.
    _json(output / "analysis_manifest.json", manifest)
    return output
