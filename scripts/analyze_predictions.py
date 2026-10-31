"""Analyze saved paired predictions; no model, dataset download, or training."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.evaluation.analysis import analyze_predictions
from src.evaluation.reporting import write_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", required=True, type=Path, help="Saved predictions.jsonl")
    parser.add_argument("--protocol", type=Path, help="Defaults to protocol.json beside predictions, when present")
    parser.add_argument("--output", required=True, type=Path, help="New or empty report directory")
    parser.add_argument("--reference", default="base", help="Reference condition, e.g. base or diff_only")
    parser.add_argument("--models", nargs="+", help="Require exactly these conditions to detect missing whole methods")
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--cost-field", choices=("input_tokens", "extra_context_tokens"), default="input_tokens")
    parser.add_argument("--plots", action="store_true", help="Also export PNG/PDF figures; requires Matplotlib")
    parser.add_argument("--synthetic", action="store_true", help="Mark every report/figure as an artificial demonstration")
    args = parser.parse_args()
    try:
        source = args.predictions.read_bytes()
        rows = [json.loads(line) for line in source.decode("utf-8").splitlines() if line.strip()]
        protocol_path = args.protocol or args.predictions.with_name("protocol.json")
        if args.protocol is not None or protocol_path.exists():
            protocol_source = protocol_path.read_bytes()
            protocol = json.loads(protocol_source)
        else:
            protocol_source, protocol = None, None
        result = analyze_predictions(rows, reference=args.reference, protocol=protocol,
                                     required_models=args.models, bootstrap_samples=args.bootstrap_samples,
                                     seed=args.seed, cost_field=args.cost_field)
        provenance = {"predictions_file": args.predictions.name,
                      "predictions_sha256": hashlib.sha256(source).hexdigest(),
                      "protocol_sha256": hashlib.sha256(protocol_source).hexdigest() if protocol_source else None,
                      "evaluation_protocol": protocol,
                      "synthetic": args.synthetic or any(row.get("synthetic") is True for row in rows),
                      "source_code_sha256": {str(path.relative_to(PROJECT_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                             for path in [Path(__file__), *(PROJECT_ROOT / "src/evaluation").glob("*.py")]}}
        write_report(result, args.output, provenance=provenance, plots=args.plots)
    except (ValueError, OSError, ImportError) as error:
        parser.exit(2, f"Analysis failed: {error}\n")
    print(f"Wrote paired analysis to {args.output / 'report.md'}")


if __name__ == "__main__":
    main()
