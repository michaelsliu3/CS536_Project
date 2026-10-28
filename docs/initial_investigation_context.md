# CS536 Project — Initial Investigation and Context Handoff

## 1. Purpose, provenance, and limits

This document transfers the available conversation context to another Codex agent. It records project decisions, methodological reasoning, hardware, the latest complete proposal text, references, document preferences, unresolved issues, and secondary discussion.

Prepared on October 28, 2026. The supplied history explicitly skips some earlier conversation and begins partway through a token-counting discussion. Therefore this is a detailed summary of all substantive project information visible in the supplied chat, not a claim to recover omitted messages. External paper facts and repository observations below are carried forward from the conversation, not independently reverified during this handoff. Distinguish user-confirmed facts, proposed work, and prior assistant reports.

The current request is to produce this single Markdown file, named exactly `initial_investigation_context.md`. It does not request new experiments, repository changes, or another DOCX.

## 2. Project identity and central direction

- Course/project: Machine Learning II / CS536; conversation belongs to the “536 Project.”
- User and project lead: **Michael Liu** (刘宇茗).
- Other team members: **Ruiding Feng** and **Li-hua Peng**. Preserve these full names and capitalization.
- Working proposal title: **Learning to Select Context per Code Diff: An Accuracy–Cost Trade-off for LLM Code Review**.
- Repository: https://github.com/michaelsliu3/CS536_Project
- Main question: Can a learned selector choose useful context separately for each code change while maintaining review accuracy and using fewer tokens than fixed context strategies?
- Deep-learning contribution: fine-tune a small transformer selector or reranker. The reviewer remains unchanged in the main study.
- This is an empirical study of learned context selection for code review against strong fixed strategies, not a claim to invent selective context generally.
- Robustness is outside the core project. Cross-model transfer is optional, only if time permits.
- CodeReviewer is no longer the dataset or a core prior-work reference. Do not reintroduce it.

### Success and negative-result criteria

A positive result is either:

1. Accuracy within **one percentage point** of the best fixed choice, with **at least 30% fewer extra context tokens**; or
2. Higher accuracy at the same token budget.

A negative result is no improvement over the best fixed accuracy–cost trade-off. A negative result is still useful if supported by careful analysis of when context helps or hurts.

## 3. Professor’s feedback — authoritative project direction

Michael explicitly asked that the entire proposal emphasize the professor’s feedback because it is the exact intended direction. The message was signed **Lesia**:

> Hi all,
>
> Here are my thoughts on the project proposal.
>
> The main question can be the accuracy-cost trade-off: Can a learned selector choose context separately for each code diff, maintaining review accuracy while using fewer tokens than fixed context strategies?
>
> Other items:
>
> 1. Remove the robustness study from the core project. Cross-model transfer can be a stretch goal.
> 2. CodeReviewer may not be a good fit because it seems not to provide the repository context. Please take a look at other benchmarks, maybe ContextCRBench?
> 3. Let’s make the deep-learning component more explicit. For example, you could fine-tune a small transformer selector or reranker. Greedy removal can provide training labels or serve as a baseline, but it should not be the final method.
> 4. You can compare against diff-only, all-context, the best fixed context configuration, random selection, and a similarity-based selector.
> 5. If the learned selector does not outperform the best fixed configuration, the fallback can be a careful analysis of when context helps or hurts.
>
> Let me know if you have any questions and I am looking towards your explorations this semester!
>
> Best,
> Lesia

## 4. Data and label interpretation

Selected benchmark: **ContextCRBench**.

The proposal carries forward the paper-reported count of **67,910 examples of code changes with related context**. Context discussed includes:

- Issue descriptions/text.
- Pull request (PR) titles and descriptions.
- Surrounding code before and after a change.

These fields help explain the intended behavior and how changed code fits into a project. The selected task is the benchmark’s **code-quality assessment task**, using **merge status** as its evaluation label.

Important qualification: merge status is an indirect measure of code quality, not proof that a change is bug-free or contains a bug. Merged changes can contain bugs, and unmerged changes can be rejected for reasons unrelated to quality. Inspect a sample of labels and fields, and describe results as performance on the benchmark task.

Leakage controls planned:

- Exclude merge status, review answers, and information added after the review decision from inputs.
- Keep examples from a repository in a single data split.
- Keep related PRs together.
- Remove duplicate changes.
- Use training data to generate selector targets; validation for settings and fixed-context selection; final test only after decisions are fixed.

The conversation does not establish that dataset schema inspection, temporal-field filtering, deduplication, or final split preparation has actually been completed. Verify these before claiming implementation.

## 5. Compute, models, and repository observations

### User-confirmed hardware and preliminary tests

| Resource | Hardware | Intended use |
|---|---|---|
| Local machine | NVIDIA RTX 5070 Ti, **16 GB VRAM** | Smaller experimental/preliminary training, e.g. **3B QLoRA** |
| Cloud machine | NVIDIA RTX 5090, **32 GB VRAM**, **92 GB system RAM** | Larger datasets and **7B** experiments, using QLoRA or ordinary LoRA |

Michael corrected the generic wording “cloud vGPU with 32 GB VRAM” to the specific RTX 5090 configuration. Do not revert to the generic description. The 5070 Ti has substantially less compute and is intended for experimentation; the 5090 handles larger runs.

Michael stated that local model test runs had succeeded. Avoid language such as “accessible online but not yet downloaded or checked,” which contradicts those tests. Successful test runs do not establish completed main-study results.

Python tooling described: **PyTorch** and **Hugging Face Transformers**.

Main reviewer planned: **Qwen2.5-Coder-7B-Instruct**. The **3B version** is for smaller experiments.

### Prior assistant’s repository inspection (historical; recheck current source)

Notebook URL:
https://github.com/michaelsliu3/CS536_Project/blob/master/notebooks/qwen3b_qlora_contextcrbench.ipynb

The prior assistant reported:

- The notebook supports **4-bit QLoRA** and **ordinary LoRA**.
- **7B QLoRA** was selected at inspection time, despite “qwen3b” in the notebook filename.
- The notebook used a **70%/30% repository split**.
- Reviewer LoRA/QLoRA fine-tuning was a **separate preliminary experiment**.
- The main selector study keeps Qwen unchanged.
- The learned selector was still planned.
- “Without QLoRA” in the existing setup means **ordinary LoRA**, not full fine-tuning of every model weight.

No commit SHA, training log, completed selector code, or measured result is present in the visible conversation. Treat these as historical observations, not verified current repository state.

### Split wording correction

Michael initially wrote “80% test data” while describing larger training. The assistant interpreted this as **80% training, 10% validation, 10% test** to avoid leakage. That split subsequently appears in Michael’s supplied final draft. Keep the distinction between the proposed 80/10/10 split and the historically observed notebook’s 70/30 split. Repository grouping may require approximate percentages.

## 6. Method and token accounting

### Planned training and inference pipeline

1. Prepare repository-separated datasets and allowable context candidates.
2. Keep a reviewer model fixed for the main study.
3. On **training examples**, run the reviewer with different context combinations.
4. Compare outputs against training labels and record correctness and token use.
5. Use these outcomes to construct selector training targets, such as the least expensive observed correct context combination.
6. Fine-tune a small transformer selector/reranker.
7. At inference time, the selector chooses context per code change within a token budget **before** the reviewer runs and without access to the answer.
8. Assemble the prompt, verify its token count, run the reviewer, and record performance and cost.

Greedy removal may generate training targets or serve as a baseline. It must not replace the learned selector as the final method. Exact selector architecture, target construction, candidate partitioning, objective, and budget-search strategy remain unspecified in the visible chat.

### Token accounting decisions

- Count candidate context lengths using the **reviewer’s tokenizer**.
- Use those measured lengths when selecting candidates within a budget.
- Count the entire assembled prompt again to confirm it fits.
- Log **extra context tokens**, **total prompt/input tokens**, and **generated answer/output tokens** separately.
- Evaluate **runtime including the selector**; selector overhead may offset savings from shorter reviewer inputs.
- Main plot: **accuracy versus average extra context tokens** for the learned selector and every baseline.
- Also report total tokens and time.
- If using llama.cpp, the earlier discussion noted its server’s `/tokenize` endpoint as an option for token counting with the loaded model. This is conditional, not a settled runtime choice.
- Reference mentioned: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md

Illustrative record from the conversation (hypothetical, **not experimental results**):

| Diff | Selected context | Extra context tokens | Total input tokens | Qwen correct? |
|---|---|---:|---:|---|
| 1042 | None | 0 | 420 | No |
| 1042 | Issue | 183 | 603 | Yes |
| 1042 | Issue + code before | 617 | 1,037 | Yes |

In this example, issue-only is the cheapest **observed** correct choice. The word “observed” matters: it does not prove global optimality over all possible prompts.

## 7. Evaluation, baselines, validation, and fallback

### Required baselines

1. Diff-only / code change alone.
2. All available allowed context.
3. Best fixed context configuration, chosen using validation data.
4. Random context selection.
5. Similarity-based context selection.

Optional additional baseline/support: greedy removal.

Metrics and tests:

- Accuracy.
- F1 averaged across classes (macro-F1).
- Extra context token cost.
- Total prompt tokens and generated answer tokens, recorded separately.
- Runtime including selector overhead.
- Accuracy–token trade-off across budgets.
- Tests of context types, token budgets, and training-target choices.
- Analysis by context type, code-change size, programming language, and missing information where feasible.

### Cross-validation discussion

Michael asked whether to use cross-validation, what it means, and whether its absence is “just 1 fold.” The advice was:

- Use a fixed **repository-based 80%/10%/10% training/validation/test split** for the main project, because training the selector and running the reviewer across combinations are expensive.
- Training: generate targets and train selector.
- Validation: select settings, budgets, and best fixed configuration.
- Test: compare final methods after decisions are fixed.
- If resources permit, repeat selector training with **three random seeds**. This measures training randomness, **not sensitivity to dataset splits**.
- If there are too few repositories for a reliable validation set, consider **grouped cross-validation within the training/validation portion**, keeping final test repositories untouched.
- In 5-fold cross-validation, divide data into five folds, train fresh five times on four folds, validate on the remaining fold, rotate validation folds, and summarize average and variability.
- For this project, group folds by repository to avoid leakage.
- A single fixed training/validation split is called **holdout validation**, not usually “1-fold cross-validation.”
- “Without cross-validation” does not mean “without a validation set.” No validation set would leave no separate tuning data before final testing.

Suggested optional proposal wording from that discussion:

> We will use a repository-based training, validation, and test split. Model settings and the best fixed context configuration will be selected using validation data. If resources permit, we will repeat selector training with three random seeds to assess consistency.

This was advice after the final DOCX; the visible chat does not show that the document was updated to include it.

### Risks and fallback details

- Learned selection might not beat the best fixed configuration because many changes benefit from similar context.
- Analyze when context helps or hurts even if the main hypothesis is not supported.
- Inspect sample labels and acknowledge merge-status limitations.
- Generating targets over many context combinations may be expensive.
- First measure runtime on a small sample; reuse saved reviewer outputs; reduce training sample; use greedy removal for target generation if needed.
- Preserve the learned selector as the final method.
- Check whether selector runtime outweighs reviewer token savings.
- Robustness is not core; transfer is optional.

Michael requested the proposal’s risk section be **one reasonably short paragraph**, rather than multiple long paragraphs. The canonical version is retained below.

## 8. Prior-work selection, meaning, and references

Assignment requirement explicitly quoted in the chat: discuss motivation, cite **3–5 papers actually read**, and explain what the study adds. The final chosen set contains five papers. Michael said “these are the 5 i will use.” This is not direct confirmation that every team member has read every paper; do not claim it without confirmation.

| Final reference | Relevance | Boundary to preserve |
|---|---|---|
| ContextCRBench [1] | Dataset with enriched code-review context; compares fixed context choices | Verify exact metadata, fields, and task labels before implementation |
| ElliCE [2] | Conceptual inspiration: seeking small input changes / little extra useful information | Does not supply this project’s selector method or robustness guarantees |
| RECOMP [3] | Learns selection/compression of retrieved information, including selective augmentation | Language modeling and QA, not code review; project selects existing context sections rather than necessarily generating summaries |
| Repoformer [4] | Learns when extra retrieved code context helps | Code completion, not review; uses a code model for retrieval decisions and generation, whereas project plans a separate small selector and unchanged reviewer |
| Shi et al. [5] | Irrelevant context can lower accuracy | Evidence is from arithmetic/math reasoning; do not present it as direct code-review evidence |

### Final bibliography carried forward

[1] Hu, R., et al. “Benchmarking LLMs for Fine-Grained Code Review with Enriched Context in Practice.” FSE Industry Papers, 2026. arXiv:2511.07017.

[2] Turbal, B., Voitsitska, I., and Semenova, L. “ElliCE: Efficient and Provably Robust Algorithmic Recourse via the Rashomon Sets.” NeurIPS, 2025.

[3] Xu, F., Shi, W., and Choi, E. “RECOMP: Improving Retrieval-Augmented LMs with Context Compression and Selective Augmentation.” ICLR, 2024.

[4] Wu, D., Ahmad, W. U., Zhang, D., Ramanathan, M. K., and Ma, X. “Repoformer: Selective Retrieval for Repository-Level Code Completion.” ICML, 2024.

[5] Shi, F., et al. “Large Language Models Can Be Easily Distracted by Irrelevant Context.” ICML, 2023.

Source links present in the earlier conversation:

- ContextCRBench: https://arxiv.org/abs/2511.07017 and https://doi.org/10.48550/arXiv.2511.07017
- ElliCE: https://papers.nips.cc/paper_files/paper/2025/hash/b34efa1182a3f96b2bc4cd844238ee31-Abstract-Conference.html
- RECOMP: https://proceedings.iclr.cc/paper_files/paper/2024/hash/bda88ed2892f5e61c9a9bf215c566913-Abstract-Conference.html
- Repoformer: https://proceedings.mlr.press/v235/wu24a.html
- Shi et al.: https://proceedings.mlr.press/v202/shi23a

### Earlier references considered, then superseded

An earlier revision used ContextCRBench, ElliCE, LAURA, and SWE-PRBench. The later five-paper set above supersedes this selection.

- **LAURA** was described as adding PR and code context for review-comment generation. Link: https://arxiv.org/abs/2512.01356
- **SWE-PRBench** was described as showing broader fixed context can hurt issue detection on its own benchmark. Link: https://arxiv.org/abs/2603.26130
- Neither was described as using ContextCRBench. They were related evidence, not direct predecessors.
- **CodeReviewer** was explicitly removed because it was no longer used and might not provide repository context.
- At one earlier stage ElliCE was numbered [4]; final numbering is [2]. Use the final numbering consistently.

## 9. Writing and formatting preferences

Michael repeatedly requested:

- Simple, familiar wording while remaining formal.
- Avoid uncommon terms such as **“hunk”**. Use **“code change.”**
- Replace “gold labels” with “correct answers from the dataset” or similarly clear wording.
- Introduce LLM-assisted programming and code review for a reader relatively new to the field before discussing context selection.
- Do not begin abruptly with “A code change may require issue text…” or assume the reader knows the problem.
- Final accepted introduction begins “It has become increasingly common for software engineers to use large language models (LLMs), such as ChatGPT, to help with programming.”
- Black text, no color theme; bold when helpful.
- Formal wording for plans and roles, with full names.
- A **10-week / approximately 2.5-month** plan, with each week explicitly listed as `Week 1: ...`, `Week 2: ...`, etc.
- Roles as separate named entries, not a chart/table.
- **Single-column** proposal layout. Two columns were explicitly rejected.
- Final DOCX request: **one page**, preserving the full text as much as possible through spacing/formatting rather than rewriting.

Evolution of preferences: initially Michael permitted bullets/tables to improve readability and said a version was too short and tight. A weekly plan/roles table was then tried. Michael later explicitly rejected the table for plans and roles. Later, after supplying full text, Michael prioritized preserving that text on one page. Two columns were tried and rejected; the last reported artifact was single-column, compact, with **8.5-point body text**. Do not assume two columns are acceptable because they were used temporarily.

## 10. Latest complete proposal text

The following is the latest full proposal text shown in the chat, before final formatting-only DOCX iterations. It preserves the wording of the final formatted text response. Later discussion about three seeds/cross-validation is not silently incorporated.

---

**Machine Learning II Project Proposal**

**Learning to Select Context per Code Diff: An Accuracy–Cost Trade-off for LLM Code Review**

### 1. Research Question

Can a trained selector choose context for each code change to maintain review accuracy while using fewer tokens than fixed context choices? **A positive result** is accuracy within one percentage point of the best fixed choice with at least 30% fewer extra context tokens, or higher accuracy at the same token budget. **A negative result** is no improvement over the best fixed accuracy–cost trade-off.

### 2. Motivation and Prior Work

It has become increasingly common for software engineers to use large language models (LLMs), such as ChatGPT, to help with programming. One use is code review: checking proposed code changes for bugs and other problems before accepting them. To review a change accurately, an LLM may need information about its purpose and the surrounding code. However, including all available information uses more tokens and may distract the model.

- **ContextCRBench [1]** provides code changes with related information and compares fixed context choices for code review.
- **ElliCE [2]** studies small input changes that improve model decisions. This inspired our focus on how little extra information a code reviewer needs.
- **RECOMP [3]** trains models to select or summarize useful information from retrieved text, reducing input length while maintaining performance.
- **Repoformer [4]** trains a code model to decide when retrieving additional context will help with code completion.
- **Shi et al. [5]** show that irrelevant information can reduce LLM accuracy on arithmetic reasoning tasks, supporting the motivation that more context is not always better.

Building on these ideas, our project will train a small transformer selector to choose context separately for each code change. We will evaluate whether it maintains review accuracy while using fewer tokens than fixed context strategies.

### 3. Data, Compute, and Method

**Data.** We will use the publicly available ContextCRBench dataset [1], whose paper reports 67,910 examples of code changes with related context. Examples include issue descriptions, pull request (PR) titles and descriptions, and surrounding code before and after a change. These fields provide information about the intended behavior and how the changed code fits into the project. We will focus on the benchmark’s code-quality assessment task, using merge status as its evaluation label. This label is an indirect measure of quality, rather than proof that a change contains a bug. Merge status and review answers will be excluded from model inputs.

**Compute.** We have a local RTX 5070 Ti with 16 GB VRAM for preliminary experiments, including 3B QLoRA training, and a cloud RTX 5090 with 32 GB VRAM and 92 GB RAM for larger datasets and 7B experiments using QLoRA or ordinary LoRA. Our Python setup uses PyTorch and Hugging Face Transformers, and preliminary local model tests have succeeded.

**Method.** We will use Qwen2.5-Coder-7B-Instruct as the main reviewer, with the 3B version for smaller experiments. The core method will fine-tune a small transformer selector or reranker to choose useful context separately for each code change while keeping the reviewer unchanged. We will run the reviewer with different context combinations on training examples and record accuracy and token use to create selector training targets. The selector will then choose context within a token budget. Greedy removal may provide training targets or serve as a baseline; the learned selector will be the final method.

### 4. Evaluation Plan

We will measure accuracy, F1 averaged across classes, context tokens, and runtime including the selector. Baselines will include diff-only, all-context, the best fixed configuration selected on validation data, random selection, and a similarity-based selector. Planned component tests will examine context types, token budgets, and training targets. Tokens will be counted with the reviewer’s tokenizer.

To avoid data leakage, we will target an 80% training, 10% validation, and 10% test split by repository, keeping related PRs together and removing duplicate changes. Review answers, merge status, and information added after the review decision will be excluded from model inputs. We will tune settings on validation data and evaluate the final method on the test set once.

### 5. Risks and Fallback

The main risk is that the learned selector may not outperform the best fixed context configuration. If this happens, we will analyze when context helps or hurts across context types, code-change sizes, and programming languages. Benchmark labels may also contain errors, so we will inspect a sample and acknowledge their limitations. If generating training targets is too expensive, we will use a smaller training sample, reuse saved reviewer outputs, or apply greedy removal to produce targets while keeping the learned selector as the final method.

### 6. Plan and Roles

**Timeline — 10 weeks, approximately 2.5 months**

**Week 1:** Review prior work and verify dataset fields and labels.

**Week 2:** Prepare repository-based splits and evaluate the reviewer on a small sample.

**Week 3:** Evaluate fixed context configurations and measure token use.

**Week 4:** Evaluate random and similarity baselines; generate selector training targets.

**Week 5:** Fine-tune the small transformer context selector.

**Week 6:** Tune token budgets and conduct tests of context types and training targets.

**Week 7:** Finalize the method and evaluate on the reserved test set.

**Week 8:** Analyze accuracy, token savings, runtime, and when context helps or hurts.

**Week 9:** Complete the analysis and prepare the research report.

**Week 10:** Finalize the report and prepare the presentation; test cross-model transfer if time permits.

**Roles**

**Michael Liu:** Project lead; research design and software implementation.

**Ruiding Feng:** Model training, evaluation, experiments, report preparation, and presentation.

**Li-hua Peng:** Model training, evaluation, experiments, report preparation, and presentation.

**Anticipated LLM Use**

We will use LLMs for coding assistance, debugging, and editing. We will check generated code and interpret results ourselves.

### References

[1] Hu, R., et al. “Benchmarking LLMs for Fine-Grained Code Review with Enriched Context in Practice.” FSE Industry Papers, 2026. arXiv:2511.07017.

[2] Turbal, B., Voitsitska, I., and Semenova, L. “ElliCE: Efficient and Provably Robust Algorithmic Recourse via the Rashomon Sets.” NeurIPS, 2025.

[3] Xu, F., Shi, W., and Choi, E. “RECOMP: Improving Retrieval-Augmented LMs with Context Compression and Selective Augmentation.” ICLR, 2024.

[4] Wu, D., Ahmad, W. U., Zhang, D., Ramanathan, M. K., and Ma, X. “Repoformer: Selective Retrieval for Repository-Level Code Completion.” ICML, 2024.

[5] Shi, F., et al. “Large Language Models Can Be Easily Distracted by Irrelevant Context.” ICML, 2023.

---

## 11. Document history and artifact status

Earlier turns repeatedly edited two artifacts:

- `/workspace/scratch/caa4776d7a91/output/docx/context_selection_proposal_one_page.docx`
- `/workspace/scratch/caa4776d7a91/output/docx/context_selection_proposal_detailed.docx`

The detailed version was reported as six pages during several earlier revisions. Both versions reportedly received black formatting, the 10-week plan, hardware updates, professor-aligned methodology, and full team names. Later work focused on the one-page document and a newly supplied full text with the five final references. Do not assume the detailed version was synchronized to every later revision.

Last reported one-page artifact: **single column**, full text retained, compact spacing, **8.5-point body font**, verified by the previous assistant as one page. This is a historical report, not a fresh verification of surviving bytes.

The current project source list is empty. Workspace maintenance may have pruned old files. A local scan during this handoff found no `.docx`, `.md`, `.py`, or `.ipynb` project artifacts before creating this handoff. Recover prior documents from available persistent storage if required; otherwise reconstruct from the canonical text here. Do not tell the user they deleted the documents or claim the old sandbox links still work without checking.

The exact assignment rubric/source document is not present. Known requirements include a one-page proposal, motivation with 3–5 papers actually read, an explanation of the contribution, and the six sections preserved above. Do not invent missing rubric requirements such as specific margins or minimum font size.

## 12. Team-name brainstorming and latest discussion

Michael requested fun project names, then specifically meme-style names.

Initial suggestions:

- Context Optional
- Diff Happens
- Token Misers
- Less Is Merge
- The Context Cutters
- Selective Attention
- Merge Wisely
- Small Context Energy
- Review on a Budget
- The Missing Context

The assistant favored **Less Is Merge** or **Context Optional**.

Meme suggestions:

- Context? In This Economy?
- What the Diff
- Skill Issue, Not GitHub Issue
- Bro Forgot the Context
- Let Him Merge
- Trust Me, It Compiles
- No Context, Just Vibes
- Token Budget Is Cooked
- 404 Context Not Found
- All You Need Is Less Attention
- Gaslight, Gatekeep, Git Push
- Three People, One GPU
- It Works on My Dataset
- We Have GPT at Home
- Who Needs Context Anyway

The assistant favored **Context? In This Economy?**, **Let Him Merge**, and **It Works on My Dataset**.

Michael specifically asked for the meaning of **“All You Need Is Less Attention.”** Explanation: it plays on **“Attention Is All You Need,”** the Transformer paper. The project supplies less, more useful context rather than everything, ideally preserving accuracy with fewer tokens. Technically it reduces **input context**, not the attention mechanism. Michael showed interest but did not explicitly finalize this as the team name.

## 13. Open questions and next-agent guardrails

### Matters not resolved in the visible chat

- Exact small transformer selector/reranker model and size.
- Candidate-context granularity and truncation rules.
- Exact selector learning objective and target format.
- How to label examples where no context combination produces a correct reviewer answer.
- Number of combinations evaluated and practical target-generation budget.
- Numerical token budgets and similarity-baseline implementation.
- Exact prompts, reviewer decoding settings, and output parsing.
- Dataset field names, repository counts, class balance, and missing-context rates.
- How field timestamps will support excluding post-decision information.
- Final split implementation and reconciliation of notebook 70/30 with proposed 80/10/10.
- Actual completed training/evaluation results and current repository state.
- Whether the optional three-seed consistency check will be adopted.
- Whether all five papers have been read by the team.
- Final team name.

These are open implementation details, not reasons to redesign the agreed project without discussion.

### Practical continuation priorities when implementation is requested

1. Inspect the repository’s current branch, files, instructions, and notebooks before editing; historical paths/configurations may have changed.
2. Verify benchmark fields and label semantics against the dataset/paper.
3. Keep reviewer fine-tuning experiments clearly separate from the fixed-reviewer selector study.
4. Implement leakage-safe repository grouping and cached reviewer runs.
5. Establish fixed baselines and token/runtime accounting before expensive target generation.
6. Make learned selection the actual final method, with greedy removal only as support/baseline.
7. Tune using validation only and preserve final test repositories.
8. Report negative results honestly and analyze context effects if selection does not improve the trade-off.
9. Preserve the user’s clear, formal writing style and single-column document preference.

### Do not accidentally carry forward superseded or unsupported claims

- Do not use CodeReviewer as the main dataset.
- Do not restore LAURA/SWE-PRBench to the final five references without a new request.
- Do not claim full-model fine-tuning merely because QLoRA is disabled.
- Do not train on the test labels or use them for inference-time selection.
- Do not equate merge status with verified bug correctness.
- Do not claim Shi et al. directly tested code review.
- Do not imply ElliCE’s robustness guarantees apply to the selector.
- Do not report the illustrative token table as real data.
- Do not treat a proposal, a historical repository inspection, or successful local smoke tests as completed selector experiments.
- Do not describe optional cross-model transfer or repeated seeds as completed work.
- Do not describe the full earlier chat as recovered; omitted messages were unavailable.
