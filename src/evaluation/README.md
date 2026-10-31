# Analyze saved predictions

`scripts/analyze_predictions.py` turns the comparison notebook's saved
`predictions.jsonl` into paired evidence for the midpoint report. It works
offline and never loads weights, generates predictions, downloads data, or
trains a model. It also accepts context-policy conditions stored in the same
schema, so a frozen-reviewer experiment can compare policies later.

## Run on existing artifacts

Copy the completed cloud comparison directory, including `protocol.json` and
`predictions.jsonl`, to the analysis machine. In the project environment:

```bash
python scripts/analyze_predictions.py \
  --predictions results/qwen3b-comparison/predictions.jsonl \
  --models majority base adapter --reference base \
  --bootstrap-samples 2000 --seed 42 \
  --output results/qwen3b-analysis-v1 --plots
```

Replace the input path with the actual output directory configured in the
comparison notebook. The adjacent `protocol.json` is detected automatically;
use `--protocol /path/to/protocol.json` for a separate location. Its exact IDs
and SHA-256 identity must match every prediction. Without a protocol, output is
explicitly marked unverified and its split is unspecified. This check does not
prove training/test independence or validate the dataset's chronology.

Use a new or empty output directory. Existing reports are preserved; choose a
new version after changing data or analysis settings. Numeric analysis and CSV/
Markdown exports use Python's standard library. `--plots` additionally needs
Matplotlib, already listed in the project requirements. No CUDA is needed.

## Files produced

| File | Evidence |
| --- | --- |
| `report.md` | Split, sample/repository counts, class support, metrics, paired changes, intervals, deterministic illustrative IDs, limitations |
| `summary.json` | Full metrics, confusion counts, accuracy/macro-F1 intervals, and empirical cost frontier |
| `metrics.csv` | Per-method accuracy, macro-F1, per-class precision/recall/F1, invalid rate, recorded costs, and truncation rate |
| `paired_comparisons.csv` | Both correct / improved / regressed / both wrong; differences and intervals versus the reference |
| `paired_examples.csv` | Every paired example and its outcome category |
| `repository_metrics.csv` | The same metrics broken down by repository |
| `analysis_manifest.json` | Source-file and analysis-code hashes, saved evaluation protocol, settings, output checksums; written after successful completion |

With `--plots`, PNG and PDF copies are saved for scores, confusion matrices
(including an invalid column and shared color scale), paired outcomes, paired
accuracy intervals, and measured accuracy/token cost. Interval figures are
omitted when fewer than two repositories exist; cost figures require measured
costs. Figures label the recorded split and sample size. Select the relevant
figures for the course page limit rather than inserting every plot.

## Input schema and validation

Required per row: `id` (stable string), `model` (condition name), `repository`,
`label` (Boolean), and `prediction` (Boolean or `null` for invalid output).
Optional notebook fields include `pr_number`, `raw_output`, `valid`, `correct`,
`protocol_hash`, `input_tokens`, `output_tokens`, `truncated`, and
`latency_seconds`. Future policy exports can also record `extra_context_tokens`
and `selection_latency_seconds`.

The tool rejects duplicate `(model, id)` pairs, partial pairing, mismatched
labels/repository/PR identities, mixed protocol hashes, contradictory raw
answers or correctness flags, and invalid numeric costs. `--models` additionally
detects a wholly missing condition. Complete paired runs are required: the tool
does not silently keep only the intersection of available IDs. Raw outputs, when
present, are checked using the existing strict parser.

Invalid answers always count as wrong. Macro-F1 explicitly averages False and
True; invalid answers count as false negatives for the correct class. Missing
costs are never treated as zeros or averaged over a partial subset. A missing
class receives F1=0, matching the existing comparison notebook's fixed labels.

## Paired uncertainty and honest cost accounting

The bootstrap draws R whole repositories with replacement in each replicate,
where R is the observed repository count. All examples within each drawn
repository are retained, including repeat draws. Every method receives the
**same** draw. Metrics are recomputed on the pooled examples, preserving the
example-weighted estimand. The 2.5th and 97.5th percentiles use linear
interpolation. Both absolute metrics and candidate-minus-reference differences
are exported; these are percentile confidence intervals, not standard errors.

This follows the cluster-bootstrap principle discussed by
[Field and Welsh (2007)](https://doi.org/10.1111/j.1467-9868.2007.00593.x).
Resampling assumes repositories are suitable independent clusters; grouping
alone does not prove that assumption. Few repositories or large size imbalance
can make intervals unstable. A single repository yields no interval. The
bootstrap describes sampling variation conditional on the supplied predictions;
it is not a replacement for repeated selector-training seeds or split audits.

By default the cost axis is **total input tokens**, matching current notebook
exports. Use `--cost-field extra_context_tokens` only when all compared methods
record measured optional-context costs. Total-token measurements are never
relabeled as extra-context savings. The majority control is excluded from the
cost frontier because it makes no reviewer call. The frontier is descriptive,
uses observed means, and does not establish significant superiority or match
methods' average budgets automatically. Report selector and reviewer runtime
separately unless complete end-to-end timing was actually measured.

Illustrative examples use the first three sorted IDs in each paired category,
including regressions and shared failures. They do not change headline metrics.
The tool never chooses a prompt, checkpoint, fixed mask, budget, or seed. Tune
those on validation; reserve final test for the frozen design. Base/adapter
comparisons remain separate from the main frozen-reviewer context study.

## Verification and contribution scope

```bash
python3 -m unittest discover -s tests -p 'test_analysis.py' -v
```

Tests use artificial predictions with known improvements, regressions, invalid
answers, and costs. They cover alignment, protocol checks, whole-repository
paired resampling, unavailable intervals, missing costs, and report provenance.
When installed, scikit-learn independently checks metric parity and Matplotlib
checks all figure exports. Mark demonstration runs with `--synthetic`; a row
containing `"synthetic": true` also marks every report and figure automatically.

This contribution supplies a reproducible analysis path. Real model scores and
context-token savings require genuine audited prediction artifacts; none are
created or claimed by implementing the tool. Explain the paired design and its
limits in your own presentation, and acknowledge LLM assistance as required by
the course.
