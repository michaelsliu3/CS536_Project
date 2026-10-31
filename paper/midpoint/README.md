# Midpoint report draft

Main source: `main.tex`. This is the <=3-page November 2 midpoint draft,
not the older final-report plan under `paper/plan/`.

The original `neurips_2026.sty` and `checklist.tex` are copied unchanged from
`docs/report_format/neurips_2026/`. The report uses `nonanonymous` mode to retain
the supplied template's layout while showing the team names and NetIDs. Line
numbering is disabled in `main.tex` at the user's request. A local notice
override identifies this as the CS536
midpoint report rather than a preprint or conference submission. Margins, fonts,
and the reference style file are unchanged. The conference checklist is kept as
a reference but is not included in the three-page course check-in.

## Fill before submission

- Confirm the exact cloud run/checkpoint, completed epochs, actual hyperparameters,
  model revision, and any scheduler extensions from saved run artifacts.
- Fill the three-way validation table from paired predictions, including the
  training-majority baseline. Do not promote teacher-forced token accuracy to
  generated-answer accuracy.
- Add sample/class/repository counts, retained/excluded dataset counts, a compact
  loss plot, and a short explanation of successes and failures.
- Verify chronology/leakage checks and describe remaining limitations honestly.
- Keep test evaluation separate; do not imply the selector has been trained.
- Recompile after edits and keep the entire submitted PDF <=3 pages.

The text contains explicit pending markers. It is not ready for submission until
initial results and baseline evidence replace them. No results were fabricated.

## Sources and drafting decisions

Primary bibliography pages checked October 31, 2026:
- https://arxiv.org/abs/2511.07017
- https://proceedings.mlr.press/v235/wu24a.html
- https://arxiv.org/abs/2305.14314

Citations are kept directly in `thebibliography` for a self-contained draft.
Project claims use the kickoff PDF, current midpoint plan, later investigation
handoff, local notebook/runtime/split/prompt code, and user-reported workflow
issues. Source-code settings are not automatically verified cloud-run settings.
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
