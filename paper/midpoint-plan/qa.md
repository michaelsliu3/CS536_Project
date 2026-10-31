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
