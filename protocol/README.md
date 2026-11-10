# Protocol figure

Paper-level protocol figure for the final report: repository-aware code review with a
learned context selector.

| File | What |
|---|---|
| `protocol.tex` | Standalone TikZ source (edit this) |
| `protocol.pdf` | Vector figure for LaTeX |
| `protocol.png` | 220 dpi preview |

Build:

```bash
cd protocol && latexmk -pdf protocol.tex && latexmk -c
pdftoppm -png -r 220 -singlefile protocol.pdf protocol
```

Include in the NeurIPS report:

```latex
\begin{figure}[t]
  \centering
  \includegraphics[width=\linewidth]{protocol/protocol.pdf}
  \caption{Protocol. Two inputs: the review prompt and the project folder.}
  \label{fig:protocol}
\end{figure}
```

Layout follows LLMLingua (Jiang et al., 2023, Figure 1): four bordered columns,
Input -> Context Selector (ours, trained) -> Frozen LLM -> Review Findings, each showing
real content from one AACR-Bench PR, FreeCAD/FreeCAD#21257 ("PartDesign: Fix hole centered
on point edge case"; data/aacr_sample/context_levels.json):

- the diff lines, changed-file paths, symbol and graph counts, and the ten per-level input
  token counts (13k ... 256k) are real values for that PR;
- the highlighted level (L04) is an illustrative choice, not a selector result;
- the findings column shows the Codex `/review` JSON schema with placeholder values.

Content follows `docs/context_selector_roadmap.md` (Sections 2, 5.5, 8.5, 9.3) and
`configs/levels/`.

## Motivation figure

| File | What |
|---|---|
| `motivation.tex` | Standalone TikZ source |
| `motivation.pdf` | Vector figure for LaTeX |
| `motivation.png` | 200 dpi preview |

Top row: Input (prompt, project folder) -> agentic review, where an LLM agent and the
project folder exchange tool calls and tool results for n turns (for example Codex
`/review` or Claude Code) -> Output (review findings). Bottom: the input tokens of each
LLM call. Call k re-sends the prompt plus the tool results of turns 1..k-1, so the same
tokens are sent again and again and the total grows roughly with n^2. The tool names are
examples and the block sizes are schematic, not measurements.

## Selector-training figure (how labels are made)

| File | What |
|---|---|
| `gen_selector_training.py` | Generates `selector_training.tex` (edit this, then run `python3 gen_selector_training.py selector_training.tex`) |
| `selector_training.tex` / `.pdf` / `.png` | The figure |

One PR, read left to right as a table: (1) its ten contexts and their input tokens,
(2) the frozen LLM reviews each level once, (3) the AACR judge checks which human reference
comments each review found, (4) each level's score = quality - lambda x tokens / max tokens,
and the best level is that PR's training label. Repeated for all 196 PRs this is the outcome
table (1,960 reviews); the selector learns input-side features -> label by table lookup.
Token counts are real (FreeCAD/FreeCAD#21257); findings, coverage and scores are made up.
The label rule shown (best trade-off, lambda = 0.5) is one of the candidate rules in
`docs/context_selector_roadmap.md` 8.5.7; the rule is not decided yet.

## Teacher-labelled training figure (exam analogy)

`gen_selector_training_teacher.py` generates `selector_training_teacher.tex` (`.pdf`, `.png`),
in the same one-PR table style as `selector_training`. Drawn from the team's description;
this labelling scheme is **not yet written in `docs/context_selector_roadmap.md`** (which
labels levels by reviewing every level with the frozen LLM, section 8.5.7).

Top: ten real traced items of FreeCAD/FreeCAD#21257 with their real token sizes and the
level each first appears in. (1) Candidate code -> (2) a teacher LLM that knows the solution
(the PR's 6 human review comments) marks which comment each item supports -> (3) items used by
some comment form the exam scope -> (4) item labels, 1 if in scope.
Bottom: (5) the label level is the smallest nested level that contains every in-scope item
(here L05, 87k tokens instead of 256k for L10); (6) the training set has one row per training
PR, input = the PR and its input-side features (no solution), target = the label level, and
the selector learns x -> level. Which comments each item supports, and so the label, is
illustrative.
