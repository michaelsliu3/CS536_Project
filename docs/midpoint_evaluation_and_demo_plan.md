# Midpoint evaluation and demonstration plan

Date: October 28, 2026. Deadline: November 2, 2026, per the instructor's announcement supplied by Michael. Status: proposed work, not measured results. Use the manual Jupyter workflow; LLaMA-Factory is optional and not needed for this milestone.

## Recommendation and course fit

Use the existing Qwen notebook to evaluate the completed 3B training run against the untouched base model on the same held-out examples. Produce a compact result table, training curves, paired confusion matrices, and representative successes/failures. Add an interactive notebook example viewer if time allows. Do not spend the remaining midpoint time building a general coding agent.

This recommendation follows `MLII_fall2026_project_kickoff.pdf`: the midpoint is at most three pages and should include initial results, lessons learned, successes/challenges, changes of direction, and the repository link. The supplied email confirms that negative initial results are acceptable with discussion. The kickoff explicitly says “A negative result at the midpoint is fine. No baseline is not.” It also values baselines, ablations, multiple seeds, honest failure analysis, and reproducibility. It does not require an IDE integration or deployed demo.

Qwen fine-tuning results can therefore be useful initial evidence, but a decreasing training-loss curve alone is insufficient. Report the question answered by this pilot: **Does supervised adaptation improve Qwen3B's generated merge-status predictions on held-out repositories?** Do not promise that this alone satisfies every grading expectation.

The proposal and the newer investigation handoff both define the main contribution as a learned context selector with a frozen reviewer. Qwen adaptation remains a preliminary experiment unless the team explicitly changes the research direction. Explain how the pilot establishes data/model/evaluation infrastructure and what remains to connect it to the main accuracy–cost question. A small frozen-reviewer diff-only versus all-context comparison would provide a direct bridge. The newer handoff supersedes older proposal details about bibliography and cloud hardware; no research redesign is implied by this plan.

## What is known, and what needs verification

Michael reports completing Qwen3B training. This plan accepts that report but does not invent training results. No checkpoint JSON files were found under the local `checkpoints/` during this inspection; the trained adapter and logs may be on the cloud machine.

The current notebook source was inspected on October 28:

- It saves adapters, tokenizer files, `training_log.jsonl`, and `run_manifest.json` when the relevant cells finish.
- It includes base/adapted generation evaluation, accuracy, macro-F1, malformed-output counts, and teacher-forced completion metrics.
- Generated evaluation currently uses at most **32 examples**. That is a smoke test, not a precise estimate of benchmark performance.
- It has a 70/30 repository train/evaluation split. The evaluation partition is also passed to `Trainer` for evaluation each epoch. It is not automatically an untouched final test set.
- The parser searches for a Boolean field with a regex and maps malformed responses to `False`. This may give malformed answers credit on negative-label rows. Preserve/report this historical rule, and separately score strict valid-JSON correctness without silently changing a previously reported metric.
- The callback clears `training_log.jsonl` on initialization. Do not rerun the training cell just to obtain plots; load saved logs in a separate analysis section.
- The checked-in working notebook currently contains the earlier hardware-dependent defaults (MPS 3B LoRA versus NVIDIA 7B QLoRA), despite this conversation's requested common 3B QLoRA default. Its visible output also reports MPS LoRA. Verify the actual cloud run's adapter configuration, manifest, and source revision before calling the completed run “3B QLoRA.” Do not infer the run from the notebook filename or stale output. This planning task did not modify the notebook.

## Stage 1 — recover and freeze the experiment evidence

Collect from the completed run: adapter weights, `adapter_config.json`, tokenizer/chat template, training log, `trainer_state.json` if present, manifest, package versions, base-model identifier and revision, GPU, seed, quantization, sequence limit, epochs, optimizer settings, and exact train/evaluation membership.

Copy small logs/manifests to `results/midpoint/`; retain large weights under ignored `checkpoints/` or on the cloud machine. Keep an immutable copy of original logs before evaluating. Resolve 3B/7B and LoRA/QLoRA identity from actual artifacts. Record missing information as unknown. Recover exact row IDs, not only repository names, particularly if balanced sampling or caps were used.

Deliverable: an artifact inventory and a verified run summary. Existing data provenance and schema must be checked against the release actually used; distinguish paper-reported examples from released files.

## Stage 2 — decide what “test dataset” means

Yes, use held-out data in Jupyter. Name the split honestly:

1. If the existing 30% evaluation data influenced checkpoint choice, hyperparameters, prompts, or decisions, report **validation results** or a **held-out pilot evaluation**. Do not relabel it as an untouched test set.
2. If a genuinely untouched repository partition was reserved before training and remains unused, evaluate there once after freezing the protocol.
3. If no untouched partition exists, report validation evidence for this midpoint and establish repository-grouped train/validation/test manifests before the next run. The proposal's intended 80/10/10 split is a future target, not a description of the existing 70/30 run. A subset retrospectively taken from training data cannot become a valid test set.

Check repository overlap, PR grouping, duplicate changes, and label distribution. Ensure labels and post-decision review information never enter prompts. Issue/PR text and code chronology still need audit: field names alone do not prove absence of leakage. Merge status is a proxy, not verified defect ground truth.

Start with a deterministic 100–200 example pilot from the eligible held-out partition, following the project milestone. Save its IDs before inspecting predictions. Cover multiple repositories and report class balance; do not choose convenient successes. After the protocol and runtime pass inspection, expand evaluation if justified. If sampling is class-balanced, state that results refer to that sampled distribution. Never describe a balanced small sample as full-benchmark accuracy.

## Stage 3 — paired base-versus-adapter evaluation

Load the exact original base model and the saved adapter with Transformers/PEFT. PEFT checkpoints contain adapter parameters rather than the entire base model [1]. No retraining or adapter merging is needed for notebook evaluation. Use a fresh base instance or a correctly disabled adapter context; verify that the baseline really excludes learned adapter weights.

Evaluate both models on identical IDs, input context, prompt, tokenizer/chat template, truncation, quantization, device, and deterministic generation settings. If hardware or precision differs, document it and avoid attributing the whole difference to training. Match the notebook's prompt format and output schema; do not switch to generic “review this code” instructions for the main comparison.

Required comparisons:

- Training-majority constant classifier, fixed from training labels.
- Untouched Qwen2.5-Coder-3B-Instruct.
- The same base plus the trained adapter.

Optional bridge to the main project: untouched Qwen3B with diff-only versus all allowed context on the same held-out sample. Count reviewer input tokens to show the quality/cost trade-off. Keep this distinct from the adaptation comparison.

For each prediction save: example/repository/PR ID, model and adapter identity, context policy, true label, raw output, parsed label or invalid status, strict correctness, input/output token counts, truncation indicator, latency, and run configuration. Cache completed outputs so plotting never triggers model generation. Keep the same fixed policy for invalid outputs across models.

Primary reporting: accuracy, macro-F1 with both Boolean classes explicitly specified, per-class precision/recall, class support, malformed rate, sample/repository count, and paired improvement/regression counts. Also report strict success (valid schema plus correct label) so malformed outputs cannot receive accidental credit. Scikit-learn documents these classification metrics and confusion matrices [2]. Do not call teacher-forced token accuracy “classification accuracy”; common JSON punctuation can make it look excellent while labels are wrong.

If feasible, show uncertainty from paired bootstrap resampling by repository, using the same resampled groups for both models. With few repositories, intervals are unstable: report that limitation. One completed training seed can support preliminary findings, but evaluation bootstrapping is not a substitute for multiple training seeds.

## Stage 4 — notebook figures and interactive evidence

Create an evaluation/analysis section that can load existing artifacts without executing installation, downloads, or training. Run inference on cloud NVIDIA; transfer small prediction files to the M4 Max for plotting.

| Output | What it shows | Guardrail |
|---|---|---|
| Training and validation loss versus optimizer step | Optimization behavior and possible overfitting | Plot validation only where logged; one epoch may yield one validation point. Do not invent a curve. |
| Base/adapted/majority score table or grouped bars | Whether adaptation improves held-out decisions | Label N, split, class balance, and invalid-output rule; avoid truncated axes that exaggerate differences. |
| Two confusion matrices with shared labels/scales | False-positive and false-negative behavior | Include raw counts; account for invalid responses explicitly rather than dropping them. |
| Paired outcome counts | Both correct, improved, regressed, both wrong | Report regressions alongside improvements on identical examples. |
| Optional accuracy-versus-input-tokens plot | Diff-only/all-context quality and cost | Each point must have measured quality and tokens; do not invent a learned-selector point. |

Use Matplotlib/Seaborn-style static figures, saved as PNG and PDF. For the report, prioritize one compact metrics table and two figures; keep extra plots in the notebook. Do not generate ROC/calibration plots from Boolean predictions alone. Those require a defined, comparable score (for example full candidate-answer log-likelihoods), which would be additional work.

Add an `ipywidgets` example selector or a simple displayed dataframe: show the code change, supplied context, dataset label, base raw output, adapted raw output, and correctness. Include at least one improvement, regression, and shared failure if those categories exist. If no examples exist in a category, say so. Select examples by a documented rule, with a small random sample alongside illustrative cases. This is already a usable demonstration of the customized model.

## Can the model run in a Cursor-like harness?

Yes, in principle. A harness needs an inference endpoint and a compatible client. It does not itself train the model or prove its quality. The notebook's target is `{"is_merged":true/false}`; training on that task does not establish tool calling, editing, repository navigation, or general agent competence. A specialized “review this diff” interface fits the trained behavior.

| Route | Work required | Recommendation |
|---|---|---|
| Jupyter plus PEFT and an example viewer | Reload adapter, run or display cached predictions | First choice for November 2: minimal deployment changes and strongest connection to reproducible evaluation. |
| Small Gradio review form over the same Transformers model | Thin form with diff/context fields and strict JSON result | Optional demonstration after evaluation; label arbitrary user-entered examples as qualitative. |
| Continue in VS Code plus cloud inference endpoint | Serve model, configure OpenAI-compatible provider, constrain prompt/context | Best researched IDE-style extension path, but defer until report evidence is complete. |
| Exported model plus llama.cpp/Ollama locally | Compatible export/conversion, optional quantization, parity checks | Useful later for portable demonstrations; more moving parts than needed for midpoint. |
| Direct integration into Cursor | Confirm current custom-model/endpoint and feature restrictions | Not established by this research; do not assume an arbitrary private LoRA checkpoint can replace every Cursor model. |

Continue's official documentation supports OpenAI-compatible providers with a configurable `apiBase`, and lists vLLM and llama-cpp-python [3]. vLLM documents serving a base model with named LoRA modules through its OpenAI-compatible server [4]. Thus a plausible architecture is **VS Code/Continue → SSH-forwarded API → cloud Qwen base + adapter**. Exact Qwen model, adapter rank/targets, dtype, quantization, and installed vLLM version still require a compatibility smoke test. Use a separate serving environment, not an untested replacement of the training stack.

A future integration test should: load the exact adapter; query `/v1/models`; send the same frozen prompt through notebook and endpoint; compare outputs on a fixed sample; configure Continue's provider/model name; and verify that extra IDE instructions or automatic context collection do not alter the task. Bind the cloud service locally and use an SSH tunnel. Start with chat/review behavior; do not claim agent support without testing tool calls and editing separately.

For a standalone export, PEFT documents `merge_and_unload()` but warns that some quantization/settings do not support merging [1]. A practical route is loading the original base at a supported precision, loading the adapter, merging, saving, then converting to a supported serving format. Export can change numerical behavior; evaluate the exported model again. llama.cpp's server documents OpenAI-compatible chat endpoints [5]. These are deployment options, not reasons to migrate before the midpoint.

## Three-page report outline

1. **Page 1:** research question and proposed selector direction; why this Qwen pilot was run; dataset, split, model/method, training setup; evidence provenance and limitations.
2. **Page 2:** base/adapted/majority results table, loss figure, confusion matrices or paired outcomes; explain improvements and regressions. Use actual values only.
3. **Page 3:** lessons, successes, challenges, leakage/label limitations, what remains unimplemented, next steps toward frozen-reviewer context selection, repository link, references and tool acknowledgments.

A successful training run is an engineering success. Improved held-out task performance is a separate empirical claim. If quality does not improve, discuss class imbalance, truncation, small evaluation size, overfitting, prompt/output mismatch, and label noise as hypotheses; investigate before asserting causes.

## Work sequence through November 2

- **October 28:** locate actual cloud artifacts; freeze run identity and split membership; resolve the notebook/config mismatch.
- **October 29:** implement evaluation-only loading and per-example exports; run the paired 100–200 example pilot and data checks.
- **October 30:** inspect errors, measure runtime, expand evaluation if justified; generate static plots and the notebook example viewer.
- **October 31:** add the small frozen diff-only/all-context comparison if feasible; draft the report around verified findings.
- **November 1:** reproduce plots from saved predictions, review all claims and split labels, compress to at most three pages.
- **November 2:** final check and submission. An IDE harness is optional follow-up, not a dependency.

Completion criteria: verified run identity; baseline and adapted predictions on identical eligible examples; saved per-example results; reportable metrics and plots; honest split/seed/label limitations; a runnable analysis path; and a report of at most three pages. No additional training is required merely to visualize the completed run.

## Sources and research limits

Local sources reviewed: `MLII_fall2026_project_kickoff.pdf`, `context_selection_proposal_detailed.docx`, and `initial_investigation_context.md`, plus the instructor email supplied in this chat and current notebook source. Documents already read in this session were reused. External primary documentation checked October 28, 2026:

1. [Hugging Face PEFT checkpoint format and merged models](https://huggingface.co/docs/peft/developer_guides/checkpoint): adapter contents, original base requirement, merge caveats.
2. [Scikit-learn model evaluation](https://scikit-learn.org/stable/modules/model_evaluation.html): classification metrics and evaluation definitions.
3. [Continue: OpenAI-compatible providers](https://docs.continue.dev/customize/model-providers/top-level/openai): endpoint/provider configuration.
4. [vLLM LoRA serving documentation](https://github.com/vllm-project/vllm/blob/main/docs/features/lora.md): named adapters, model requests, serving configuration.
5. [llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md): compatible inference endpoints.

Recommendations about time allocation, plots, sampling, and the report are planning judgments based on these sources. No cloud checkpoint was evaluated, no new benchmark results were generated, and no IDE integration was tested in producing this plan.

## Stage 2 decision and implemented protocol (October 28 update)

**Decision: use approximately 80% training / 10% validation / 10% final test, by repository, for future training runs.** This supersedes the old two-way 70/30 notebook split. It matches the main proposal's intended split. This is a design choice, not a claim that 80/10/10 is universally optimal.

### Is 80/20 okay?

Yes, an 80/20 train/test holdout is reasonable if every modeling decision is fixed independently of the test results. Once we inspect results to choose prompts, checkpoints, sequence lengths, or hyperparameters, that inspected portion is serving as validation. Scikit-learn explicitly distinguishes validation for model selection from a held-out final test and warns that repeated selection on test results leaks information [6]. Its leakage guidance recommends splitting before fitting preprocessing or models [7].

Alternatives: retain 20% as test and carve 10% of the full dataset from the remaining 80% for validation (70/10/20), or use grouped cross-validation within the 80% development portion. Neither is necessary for this first protocol. We choose 80/10/10 to preserve more training data while having both validation and final testing, consistent with the proposal. If the actual repository count is small, reconsider grouped CV; do not silently substitute a row-level split.

The code partitions **repositories**, so 80/10/10 is a target for repository counts, not an exact promise about example counts. Unequal repository sizes can produce very different row fractions. Inspect the saved audit before training. The splitter requires at least ten repositories and both labels in each full partition; these are minimum sanity checks, not guarantees of representative coverage. Report repository count, row count, class support, language coverage, and truncation/context missingness. Do not search seeds until model scores look favorable.

### Exact roles and examples

| Partition/output | Fixed policy | Allowed use |
|---|---|---|
| Train | All retained examples in the training repositories | Gradient updates; fit any learned preprocessing; determine majority baseline. |
| Validation | All retained examples in validation repositories | Epoch evaluation, checkpoint/prompt/hyperparameter development. |
| Validation generation sample | Up to 200 deterministic hash-ranked validation IDs, selected without looking at labels/predictions | Repeated paired base/adapted generated-answer comparison for midpoint graphs. Report sample class/repository counts. |
| Demonstration examples | First up to 12 IDs from that frozen validation sample | Honest fixed example viewer; no cherry-picking for the headline metric. |
| Final test | Every retained example in test repositories | One final paired evaluation after all decisions are frozen. Never supplied to Trainer or used for checkpoint selection. |

If 200 examples are too expensive, change the sample size in a **new protocol version before generating predictions**, not after seeing errors. If validation has fewer than 200, use all and report the actual N. The fixed sample preserves natural sampling rather than forcing class balance; its prevalence can differ from the full partition. Optional improvement/regression/error examples can be selected after evaluation for explanation, but must be marked as illustrative, with a documented selection rule and no effect on metrics.

For the midpoint, graphs should explicitly say **validation generation sample**, not “test accuracy,” unless final-test evaluation has actually been performed after selection. Loss curves use training/validation logs; confusion matrices and score bars must use paired predictions on the frozen generation sample. Compare base, adapted, and training-majority baseline. The example viewer should show the same preselected IDs for both models.

### Data integrity and persistence

`src/evaluation/splits.py` deterministically orders repositories and examples using SHA-256 with seed 42. The notebook saves `data/contextcrbench/splits/repository_80_10_10_v1.json` before model loading/training. It records exact IDs, repository assignments, validation/demo/test IDs, dataset fingerprint, excluded duplicates, and class/count summaries. It rejects changed data/settings instead of silently overwriting an existing manifest. IDs are relative to the extracted dataset, so copying the same release to another machine does not change membership.

Exact normalized-diff duplicates are removed globally, retaining the lexicographically first stable ID; identical normalized diffs with conflicting labels are excluded as a whole group and recorded in `excluded_conflicting_labels`. Review these conflicts before training; their labels cannot be resolved from the diff alone. This is a conservative explicit policy: repeated diffs with different surrounding context are also removed, which changes the evaluated population and must be reported. It does not detect near-duplicates, copied repositories, or temporal leakage. Complete those audits before setting `DATA_AUDIT_COMPLETE=True`; the flag records a human workflow decision, not automatic proof. No actual ContextCRBench audit or split counts were produced in this implementation because its payload was not downloaded for this task.

Training defaults to enabled (`RUN_TRAINING=True`, `DATA_AUDIT_COMPLETE=True`) per the current run workflow. Review the manifest and dataset audit before reporting results; these flags do not verify chronology, leakage, or near-duplicates. Trainer receives validation only. `run_final_test(protocol_frozen=True)` is opt-in and not called by Run All. Its flag is a procedural safeguard, not an access-control boundary; users must verify the model was trained with the matching manifest. Preserve the manifest in the run metadata.

The completed earlier model must keep its original split identity. Do not evaluate it on the new test partition and claim independence unless exact prior exposure has been ruled out. This new protocol applies to a future training run; implementing it does not retrain or invalidate the historical run, whose results can still be described as pilot validation evidence.

### Verification and remaining work

Synthetic tests cover disjoint and complete membership, deterministic behavior under input reordering, exact duplicate removal/conflicting labels, immutable manifest checks, machine-independent paths, and insufficient repository/class coverage. Notebook syntax is checked separately. Real-data class distributions, repository imbalance, near-duplicates, chronology, and final sample counts remain to be audited after data is available. Per-example prediction exports, strict JSON scoring, plots, and bootstrap uncertainty remain Stage 3–4 work; this update fixes which data they must use.

[6] [Scikit-learn: cross-validation and grouped data](https://scikit-learn.org/stable/modules/cross_validation.html), reviewed October 28, 2026. Supports separating selection from final testing and grouping dependent examples.

[7] [Scikit-learn: common pitfalls and data leakage](https://scikit-learn.org/stable/common_pitfalls.html), reviewed October 28, 2026. Supports splitting before learning transformations and avoiding test exposure during fitting/selection.

## Stage 3 implementation

Use `notebooks/qwen3b_evaluation_comparison.ipynb` for evaluation only. Set the adapter directory, completed run manifest, dataset root, and saved split manifest. Enable `RUN_INFERENCE` on cloud NVIDIA; default mode only reads cached results. The completed run must identify Qwen3B and embed the same Stage 2 split manifest. Historical runs without that provenance require recovery of their original split protocol; do not manually attach the new split to old weights.

The notebook computes the constant label from training rows only (ties resolve to False), compares the base with its adapter disabled against the same base with the adapter enabled, and uses identical validation IDs and generation settings. Adapters with trained bias or saved extra modules are rejected for this baseline route. Both model conditions use the same NF4 inference quantization. Choose the training base revision when known; the resolved inference commit and adapter hashes are recorded. This does not establish that an unknown historical training revision matches.

Per-example records include raw outputs, strict parsed labels, validity, correctness, repository/PR identity, token counts, truncation, and generation latency. Invalid JSON is always incorrect; macro-F1 covers the two real classes while invalid predictions count as misses for their true class. Resume is supported using individual cached records with protocol checks. The notebook provides the three-way metric table, a score figure, paired improvement/regression counts, and the fixed demo examples. More detailed Stage 4 figures can consume the exported JSONL without loading a model.

No actual trained adapter was available for inference verification during implementation. Unit tests verify strict parsing, majority ties, invalid scoring, cache reuse/protocol mismatch, and exclusion of outcome fields from prompts. Cloud model loading and real-data metrics still require the completed run artifacts. The original training notebook now imports the shared prompt module to prevent prompt drift.

## Cloud training reliability and report logging update

The training notebook now fixes Qwen2.5-Coder-3B-Instruct and NF4 QLoRA on every hardware profile. Apple Silicon is preparation-only; CUDA is required to execute it. There is no silent switch to 7B or FP32 LoRA.

Before model loading, the notebook resolves a base-model commit and freezes `run_contract.json`, including model revision, package/Python versions, dataset/split fingerprint, training configuration, prompt/runtime/training-code hashes, quantization and LoRA parameters. Resuming reuses the exact commit and rejects changed contracts. The evaluation notebook reads this recorded revision automatically. Full package inventory, GPU/CUDA information, tokenizer identity, trainable parameter count, and Git HEAD are written in per-session environment snapshots. Data diagnostics record class/repository/language counts, token-length percentiles and fraction reaching the cap. Cap frequency is a diagnostic, not an exact measure of discarded context.

`training_log.jsonl` is append-only with UTC timestamps, session IDs and optimizer steps. It records every-step loss/learning-rate/gradient-norm logs provided by Trainer, epoch, GPU allocated/reserved/peak memory, periodic validation loss and teacher-forced completion metrics, checkpoint events, resume events, interruptions and completion. Validation runs every 100 optimizer steps and after training; checkpoint saving is independent of expensive validation. Trainer runtime/throughput values after resuming may describe the resumed invocation, so use session timestamps and step records to describe total work and do not blindly sum cumulative counters or duplicate replayed steps.

Before/after generated validation evaluation uses strict JSON scoring and persists each example immediately: stable ID, repo/PR, true/predicted label, raw answer, validity, correctness, input/output tokens, truncation and generation latency. Interrupted generation resumes cached examples with identical evaluation identity. Accuracy, macro-F1, per-class precision/recall/support and malformed rate are saved. Majority comparison and report graphs remain in the independent comparison notebook. Numeric parsing semantics now match that notebook.

Full resumable checkpoints are requested every **10 optimizer steps** and approximately **300 seconds at the next optimizer boundary**, whichever triggers first; these are not hard wall-clock deadlines. Optimizer steps include eight accumulated microbatches. Three complete checkpoints are retained. Adapter weights, optimizer state, scheduler, Trainer state and RNG state are saved. A checksum completion marker is written only after saving, and retention pruning happens afterward. On restart, the latest intact checkpoint is selected; partial/corrupt newer saves fall back to an earlier intact checkpoint. If checkpoints exist but none verify, the notebook stops instead of silently starting over. A final full checkpoint and completion marker let a rerun skip finished training and retry export/evaluation. A run-directory lock prevents concurrent kernels from writing the same run on filesystems that support advisory locks.

Resume procedure: reconnect to an existing live kernel first. If it died, start one worker with the same code/environment, persistent `CHECKPOINT_ROOT`, `RUN_NAME`, dataset and manifest; rerun the setup/data/model/training cells, skipping dependency installation. Trainer restores optimizer, scheduler, RNG and data position. Work after the latest complete checkpoint may be repeated. Use a fresh name for intentionally new settings. Do not delete the cloud volume on instance termination; local checkpoints do not provide remote replication. Logs/checkpoints survive kernel failure only while their storage survives. Keep a volume snapshot or backup before destroying an instance.

Verification includes synthetic checkpoint corruption/fallback and append-only logging tests, plus a real tiny PEFT/Qwen CPU training experiment interrupted at step 2 and resumed through step 4. Resumed adapter weights match uninterrupted training within tolerance. This verifies the installed Trainer resume path, not actual CUDA/bitsandbytes recovery on the cloud GPU; run a short cloud recovery check before relying on a long run.
