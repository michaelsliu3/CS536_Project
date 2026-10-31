# Draft QA — October 31, 2026

## Original draft checks

- Three pages including references, verified from the final PDF.
- All three pages rendered and visually inspected; lessons section starts on page 3.
- Supplied style file is byte-identical to the reference; no margin/font overrides.
- All three citation keys resolve; primary source metadata checked online.
- No overfull boxes or undefined references in final compilation log.
- Tectonic emits only the expected UTF-8/inputenc compatibility warning.
- Empirical issues CSV passes the skill validator; results issue remains TODO.
- No cloud access, evaluation, training or fabricated quantitative results.
- Local Tectonic was installed through Homebrew to export and inspect the PDF.
- Results, dataset audit evidence and exact cloud run metadata still require filling.

## Dataset-example revision

Verified record 63859 against local JSON and training membership; four excerpt lines are verbatim; the issue description is paraphrased. The label is separate from inputs. Source hashes and
omissions are recorded in dataset-example.json. Recompiled within three pages
without changing the NeurIPS style.

## Template and wording revision

- Compared both user-pasted files with the repository draft. The supplied full
  template is byte-identical to `docs/report_format/neurips_2026/neurips_2026.tex`.
- Used the supplied full research title and member NetIDs. Rewrote the prose in
  direct first-person language, with specific implementation work, difficulties,
  and next steps; retained the course-required tool acknowledgment.
- Replaced `preprint` with `nonanonymous`. The template's line numbering, fonts,
  and margins remain; only the notice text is overridden locally in `main.tex`
  to identify the CS536 midpoint report. No conference submission or acceptance
  is implied, and neither reference style copy was edited.
- Compiled twice using pdfLaTeX (TeX Live 2025). Final log has no overfull boxes,
  undefined citations/references, or LaTeX warnings. Bibliography entries and keys
  remain unchanged.
- Rendered and inspected all three US Letter pages. PDF text extraction confirms
  the course notice and the absence of the Preprint label. Source and PDF have
  matching content; real evaluation entries remain explicitly pending.
- Included implemented context-policy and offline-analysis progress. Synthetic
  analysis tests are distinguished from the cloud predictions still needed for
  the results table; no artificial scores were inserted.

## Line-number removal

- Disabled margin line numbering in `main.tex` at the user's request; body text,
  author details, section numbering, and page numbering are unchanged.
- Recompiled twice and visually checked all three pages. PDFKit selection found
  no text in the left line-number margin on any page; the course notice remains.
- The compiled PDF remains three US Letter pages with no unresolved citations
  or overfull boxes. The reference style file is unchanged.

## First-page numbering

- Removed the first-page notice locally and applied the normal page style after
  the title. All three pages now have centered page numbers 1, 2, and 3.
- Preserved the user's existing author-affiliation edit and the disabled margin
  line numbers. The reference style file remains unchanged.
- Two pdfLaTeX passes succeeded. Rendered and visually checked all three pages;
  PDFKit footer selections verify 1/2/3 and empty line-number margins. The old
  course notice is absent, with no overfull boxes or unresolved references.

## Verified model and data revision

- Used the read-only server audit of `qwen3b-qlora-split-v1-run5-local` and
  `checkpoint-14289`. Recorded artifact names, model revision, adapter identity,
  split digest, exact counts, and limitations in `model-data-audit.json`.
- Added the actual decoder architecture, adapter targets and trainable parameter
  count, saved training settings, answer-only loss, and input/generation caps.
  The three-epoch training completion is not presented as a finalized evaluation
  or evidence of prediction improvement.
- Added the retained/excluded record counts, repository/class split table,
  language differences, fixed sample composition, and post-truncation cap
  frequencies. Split class totals and recorded language totals agree.
- Kept all evaluation scores and loss values unfilled at the user's request.
  Distinguished the completed adaptation training from the planned selector.
  No remote program was changed and no new training or inference was started.
- Compiled twice with pdfLaTeX; the report remains three US Letter pages with
  resolved citations/table references and no overfull boxes. One underfull
  vertical-box warning remains; all pages were rendered and inspected, with no
  clipping, overlap, or unreadable content.
- PDFKit checks confirm page numbers 1/2/3, empty line-number margins, and no
  old course notice or Preprint label. Both reference style/checklist copies
  remain byte-identical to the supplied originals. Source hash was checked
  before replacing the tracked PDF with the inspected build.

## Read-only evaluation audit and limitations revision

- Re-read existing server source/configuration and original saved prediction
  artifacts. Server HEAD remains `c1bcbb9`; its pre-existing dirty notebooks and
  comparison module were inspected as actual files. Server docs match the
  already-read local originals. No server program or dependency was changed.
- Confirmed 200 unique identical IDs per condition, matching labels/repositories/
  PRs and protocol digests, and raw-output/strict-prediction agreement. Independently
  recomputed metrics match the saved metrics. The existing offline paired-analysis
  CLI also accepts the genuine artifacts and reproduces the observed scores.
- CPU-only reconstruction with the saved chat template and checkpoint tokenizer
  matches all 200 input lengths/truncation flags and source labels. All 118
  truncated prompts lose the assistant suffix; all 82 untruncated base outputs
  contain code fences. No model was loaded for generation or training.
- Added `evaluation-audit.json` with checksums/counts and `evaluation-audit.md`
  with the architecture flow, findings, limitations, and untested remedies.
  Kept raw predictions in temporary local audit storage, outside Git.
- Filled the original-protocol table as diagnostics, separated class imbalance
  from format/truncation failure, and avoided a causal claim or quality-gain claim.
  Proposed training-only balancing, intact chat boundaries, and a new evaluation
  protocol; none was implemented in program code. Main reviewer remains frozen.
- Two final pdfLaTeX passes succeed; three pages including references. One
  underfull vertical-box warning remains, with no overfull boxes, undefined
  citations, or unresolved references. All final pages rendered and visually
  inspected. Footer/margin checks confirm 1/2/3, no line numbers or old notice.
- Final server recheck detected a concurrent notebook save adding two cells;
  all eleven existing code cells, six other inspected program files, and the
  three audited validation artifacts were preserved. Recorded this explicitly;
  no server writes or execution of those new cells occurred in this audit.
  Re-read the newly updated midpoint plan from the latest GitHub master.
