# Final report draft

Main source: `main.tex`, with `references.bib` and the figures under `figures/`. This is the course's
final report (kickoff: 5 to 8 pages in the NeurIPS-style template, unlimited appendix, link to the
repository; content: motivation, methods, main results, experiments, observations, lessons learned).
First draft written on November 28, 2026.

The main text is 7 pages; references and three appendices follow (11 pages in all).

## Build

```bash
.venv/bin/python paper/final/make_figures.py     # figures/levels.pdf, figures/frontier.pdf, figures/numbers.json
cd paper/final && tectonic main.tex               # main.pdf; runs BibTeX and the extra passes itself
```

`make_figures.py` calls no model. It reads the confirmation run's report
(`data/aacr_hunk/outcomes/qwen3.8-27b_aacr-hunk_kept4/confirmation_report.json`, local), the selectors'
budget table and the loop's report (`results/selector185_llm/`), and the saved reviews. The last build had
no overfull-box warnings.

`neurips_2026.sty` is copied unchanged from `docs/report_format/neurips_2026/`. As in the midpoint report,
the style runs in `nonanonymous` mode with line numbers off and the conference notice suppressed; margins
and fonts are untouched. The conference checklist is not included.

## Where each number comes from

| Part of the report | Source |
|---|---|
| Table 1, Figure 2, groups, wording (levels on the first 93, the new 92 and all 185) | `docs/aacr_confirmation_run.md`; `scripts/analysis/confirmation_run_report.py` |
| Table 2 (selectors on all 185) and the must-fix paragraph | `results/selector185_llm/llm_auc.csv`, `llm_budget_table.csv`; `docs/selector185_must_fix.md` |
| The 21 selectors on the first 93 (text and Table 7) | `docs/selector50_run.md` |
| Figure 3 and the loop, with both gates (text, Tables 8 and 9) | `results/selector185_llm/selector_loop.json`, `selector_loop_gate_llm_scores.json`; `docs/selector_loop_185.md` |
| Table 3 (Codex) and the split in Section 4.4 | `results/codex_blocks/report_1000_hunks.json`; `docs/codex_blocks_run.md` |
| Per pull request against per hunk; the ten-level pilot (Table 6) | `docs/outcome_table_first_run.md`, `docs/per_hunk_review_test.md` (Sections 1 and 19) |
| The midpoint's numbers | `paper/midpoint/main.tex` |
| The proposal's plan (Table 4) | `docs/context_selection_proposal_detailed.docx` |

## Still provisional

Nothing is waiting on a run. The Codex comparison was stopped on purpose at 1,000 of the 1,811 hunks, a
random sample.

## References

Every entry of `references.bib` was checked against its source page on November 28, 2026: the arXiv
abstract pages for 2601.19494, 2511.07017, 2310.04408, 2203.09095, 2603.27277, 2409.12186, 2309.06180,
2106.09685; the PMLR pages for Repoformer (v235) and Shi et al. (v202); the model card of
`Qwen/Qwen3.8-27B`; and the `openai/codex` repository. Papers are cited by what those pages show. A venue
is given only where the page names one.

The acknowledgments and the team-contribution lines were confirmed by Michael on November 28, 2026.

## Not claimed

No result is new to this draft: every number is from a saved result or one of the documents above. The
report does not claim that a selector saves tokens at equal quality, that context improves coverage, or
that the loop's gain comes from the added context.
