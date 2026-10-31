# Midpoint report draft

Main source: `main.tex`. This is the <=3-page November 2 midpoint draft,
not the older final-report plan under `paper/plan/`.

The original `neurips_2026.sty` and `checklist.tex` are copied unchanged from
`docs/report_format/neurips_2026/`. The report uses `nonanonymous` mode to retain
the supplied template's layout while showing the team names and NetIDs. Line
numbering is disabled in `main.tex` at the user's request. The conference notice
is suppressed, and the first page uses a centered page number, like the later
pages. Margins, fonts,
and the reference style file are unchanged. The conference checklist is kept as
a reference but is not included in the three-page course check-in.

## Remaining experiment work

- Repeat the three-way comparison under a newly versioned prompt protocol after
  repairing truncation. Keep the original strict scores as diagnostics; do not
  promote format compliance or teacher-forced accuracy to review-quality gains.
- Test training-only balancing remedies separately and report minority recall
  and macro-F1. Keep validation/test distributions unchanged.
- A loss figure is not included; it would not by itself establish better verdicts.
- Verify chronology/leakage checks and describe remaining limitations honestly.
- Keep test evaluation separate; do not imply the selector has been trained.
- Recompile after edits and keep the entire submitted PDF <=3 pages.

The report includes initial saved-output diagnostics and explicit limitations.
Corrected-prompt or balancing results remain future work; no improvement from
those remedies is claimed. No results were fabricated.

## Sources and drafting decisions

Primary bibliography pages checked October 31, 2026:
- https://arxiv.org/abs/2511.07017
- https://proceedings.mlr.press/v235/wu24a.html
- https://arxiv.org/abs/2305.14314

Citations are kept directly in `thebibliography` for a self-contained draft.
Project claims use the kickoff PDF, current midpoint plan, later investigation
handoff, local notebook/runtime/split/prompt code, and saved artifacts inspected
in a read-only cloud audit. Source-code settings alone are not evidence of the
settings used by a completed run.
The older robustness/CodeReviewer outline in `paper/plan/` was not used.
The current user request explicitly authorizes a short draft with placeholders;
full-paper IEEE, literature-count and experiment quotas from the empirical skill
are inapplicable to this three-page course deliverable.

Compile from this directory using the installed LaTeX distribution. Run twice
to resolve citations and the table reference:

```bash
pdflatex -interaction=nonstopmode -halt-on-error main.tex
pdflatex -interaction=nonstopmode -halt-on-error main.tex
```

The revised draft uses the full research title and the member NetIDs supplied
with the template. Its prose distinguishes implemented code, reported cloud
workflow issues, pending measurements, and the planned learned selector. The
tool acknowledgment remains included.

## Dataset illustration

The report includes four verbatim diff lines from
`data/contextcrbench/extracted/final_json/63859.json` (pandas PR #53773): a Python
`None` guard. The issue description is paraphrased, and omitted lines are explicit.
The target is `merged=False`, not a claim that the proposed fix is incorrect.
Training membership and source bytes were checked locally. Source/manifest hashes
and selection details are in `../midpoint-plan/dataset-example.json`.
This is a data illustration, not a performance result. The full dataset stays ignored.

## Verified model and data setup

The model/data revision uses the saved artifacts from
`checkpoints/qwen3b-qlora-split-v1-run5-local`, final `checkpoint-14289`, on the
audited server. Its three-epoch training completion is distinct from finalizing
the evaluation workflow. The architecture comes from the base model's actual
`config.json`; training settings, parameter count, data cleaning, split counts,
language counts, and post-truncation lengths come from saved run artifacts.
The small [audit summary](../midpoint-plan/model-data-audit.json) records the
artifact names, run identity, model revision, and split-manifest digest. It is
a transcription of inspected evidence, not a copy of the original artifacts.
No cloud code was changed or new experiment run for this revision.

The report includes the split/class distribution table, language differences,
and fixed validation-sample composition. The learned context selector is still
a planned component, separate from the completed QLoRA training pilot.

## Saved-output diagnostic revision

A follow-up read-only audit recomputed the original 200-example comparison and
checked prompt integrity using existing tokenizer files on CPU. See the
[architecture, findings, and proposed remedies](../midpoint-plan/evaluation-audit.md)
and its [machine-readable evidence](../midpoint-plan/evaluation-audit.json).
The original base score is zero because all outputs fail strict parsing;
118 truncated inputs lose the assistant suffix, while 82 untruncated outputs
contain code fences. The adapter predicts True on every example, matching
the majority baseline and missing all 25 False examples. The paper separates
these observations from unproven explanations about their causes.

The table is now populated as **original-pipeline diagnostics**, not a finalized
quality comparison. The limitations section proposes repaired prompt budgeting,
a newly versioned comparison, and training-only resampling or example-weighted
answer loss. Server programs, dependencies, checkpoints, and outputs were not
modified, and no model inference or training was run for the audit.
