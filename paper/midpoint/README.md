# Midpoint report draft

Main source: `main.tex`. This is the <=3-page November 2 midpoint draft,
not the older final-report plan under `paper/plan/`.

The original `neurips_2026.sty` and `checklist.tex` are copied unchanged from
`docs/report_format/neurips_2026/`. The report uses `preprint` mode so the team
names are visible without implying acceptance. The conference checklist is kept
as a reference but is not included in the three-page course check-in.

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

Open this folder in Codex to use the built-in LaTeX editor/compiler. A local
standalone PDF can also be generated with `tectonic --keep-logs main.tex`.
