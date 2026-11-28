# Codex CLI per hunk against our four levels: 1,000 random hunks (November 28, 2026)

**Status: run on November 27 and 28, 2026, and stopped on purpose at 1,000 of the 1,811 hunks.** Codex CLI reviewed 1,000 hunks of AACR-Bench on the same model as our reviewer (Qwen3.8-27B, thinking off), one session per hunk, and its reviews were scored exactly like our four context levels on the same hunks. Codex covers 415 reference comments and our levels cover 568 to 578: 27 to 28% fewer for Codex, with an interval that excludes zero against every level. It makes 16.3 model requests per hunk and costs 19.4 times our no-context review. The gap is not the same everywhere: Codex is behind on comments that can be answered from the diff or the file, and level with our reviews on comments that need the repository.

This follows the first trial on 12 hunks ([codex_first_trial.md](codex_first_trial.md)). The target it serves, and why Codex is run per hunk, are in [project_target.md](project_target.md). The four levels on all 185 pull requests are in [aacr_confirmation_run.md](aacr_confirmation_run.md).

## 1. What was run

| Part | Choice |
|---|---|
| Hunks | The 1,811 hunks reviewed at all four levels, shuffled once (seed 536) and run in 19 blocks of 100. The first 10 blocks were run: 1,000 hunks from 170 pull requests and 47 repositories |
| Why a random order | The harness works repository by repository, so a run stopped early would otherwise leave whole repositories done and the rest untouched. Finished blocks together are a random sample of all hunks |
| Codex | Codex CLI 0.171.0, one session per hunk, given our reviewer's own request for that hunk, in a checkout of the repository at the reviewed commit. It may run commands and read files there. Its own settings folder is separate from any user's, and reading outside the checkout is blocked ([codex_first_trial.md](codex_first_trial.md)) |
| Model | Qwen3.8-27B with thinking off, the same server as our reviewer (vLLM on one RTX PRO 6000, eight requests at a time). Codex ran on a laptop and called the server through SSH tunnels |
| Time limit | 900 seconds per session. A session without a review gets one more try |
| Scoring | As for our levels: only findings that quote the reviewed hunk are counted, and the same judge matches them to the reference comments. A session that ends without a review counts as covering nothing, and its cost is kept |
| Cost | Tokens as the server counted them, at $2.00 per million new input tokens, $0.10 per million cached input tokens, $2.50 per million cache writes and $10.00 per million output tokens. Nothing was billed |
| Intervals | The middle 95% of 10,000 bootstrap draws of repositories (seed 536) |

Comments covered are counted per hunk: a hunk's review can cover the reference comments of its file, so the counts below are sums over the sampled hunks' reviews.

## 2. Result

| Reviewer | Comments covered | Findings on the hunk | Findings off the hunk | Requests per hunk | Input tokens per hunk | Cost | Times no context |
|---|---|---|---|---|---|---|---|
| Codex CLI | 415 | 1,225 | 2,197 | 16.3 | 348,720 | $114.21 | 19.4 |
| `H01_min` (no context) | 568 | 1,532 | 25 | 1 | 958 | $5.87 | 1.0 |
| `H02_function` | 578 | 1,497 | 128 | 1 | 1,547 | $7.56 | 1.3 |
| `H03_file` | 572 | 1,504 | 257 | 1 | 2,425 | $9.98 | 1.7 |
| `H04_callees1` | 569 | 1,514 | 276 | 1 | 3,601 | $12.47 | 2.1 |

Codex minus each level, in comments covered:

| Level | Difference [95% interval] | As a share of the level's count | Hunks where Codex covers more, fewer |
|---|---|---|---|
| `H01_min` | −153 [−226, −86] | −27% [−34%, −19%] | 115, 230 |
| `H02_function` | −163 [−238, −100] | −28% [−34%, −21%] | 102, 231 |
| `H03_file` | −157 [−231, −88] | −27% [−34%, −19%] | 108, 233 |
| `H04_callees1` | −154 [−219, −93] | −27% [−34%, −20%] | 101, 221 |

- **Codex covers fewer comments than every level,** by about a quarter, and each interval excludes zero.
- **It costs 19.4 times the no-context review and 9.2 times the callees level.** Of its 348.7 million input tokens, 22.9 million were new and the rest were cached repeats, which is why about 360 times the tokens costs about 19 times as much.
- **Its precision on the counted findings is a little lower:** 0.314 against 0.333 to 0.355.
- **It writes more about other code than about the hunk:** 2,197 findings quote code outside the hunk it was asked to review, against 1,225 that quote the hunk. Those findings are not scored (Section 8).

## 3. As the sample grew

The same comparison each time a block was scored, against `H01_min`:

| Hunks | Codex | `H01_min` | Difference [95% interval] | Share |
|---|---|---|---|---|
| 100 | 45 | 50 | −5 [−19, +9] | −10% |
| 200 | 93 | 120 | −27 [−53, −2] | −23% |
| 300 | 129 | 167 | −38 [−70, −8] | −23% |
| 400 | 177 | 241 | −64 [−99, −33] | −27% |
| 500 | 210 | 278 | −68 [−102, −38] | −24% |
| 600 | 253 | 331 | −78 [−115, −43] | −24% |
| 700 | 304 | 401 | −97 [−142, −55] | −24% |
| 800 | 343 | 455 | −112 [−163, −64] | −25% |
| 900 | 379 | 512 | −133 [−190, −80] | −26% |
| 1,000 | 415 | 568 | −153 [−226, −86] | −27% |

The share settled at about a quarter from 200 hunks on, and the cost ratio stayed at about 19 to 20 throughout. The run was stopped at 1,000 because more blocks were narrowing the interval without moving the result.

## 4. Where Codex covers more and where fewer

This split was looked at after the totals were known, so it is a description and not a test fixed in advance. It counts distinct reference comments covered by the sampled hunks' reviews: 698 are covered by at least one of the five reviewers.

| Reference comments | Covered by some reviewer | Codex | `H01_min` | `H02_function` | `H03_file` | `H04_callees1` | Codex minus `H01_min` [95% interval] |
|---|---|---|---|---|---|---|---|
| Context label `Diff Level` | 345 | 182 | 242 | 246 | 231 | 232 | −60 [−92, −31] |
| Context label `File Level` | 237 | 109 | 152 | 160 | 160 | 158 | −43 [−68, −19] |
| Context label `Repo Level` | 116 | 64 | 65 | 63 | 74 | 74 | −1 [−17, +15] |
| Generated by an AI tool | 545 | 270 | 369 | 376 | 374 | 373 | −99 [−134, −63] |
| Written by a person | 153 | 85 | 90 | 93 | 91 | 91 | −5 [−25, +15] |
| Blocking | 53 | 29 | 36 | 40 | 42 | 40 | −7 [−17, +2] |
| Should fix | 200 | 115 | 133 | 141 | 135 | 136 | −18 [−36, +1] |
| Nit | 441 | 209 | 288 | 286 | 286 | 286 | −79 [−110, −49] |

Per-hunk counts by the size of the hunk (thirds of the sample by changed lines):

| Hunk size | Hunks | Codex | `H01_min` | Codex minus `H01_min` [95% interval] |
|---|---|---|---|---|
| Up to 2 changed lines | 408 | 103 | 147 | −44 [−92, −6] |
| 3 to 7 changed lines | 290 | 98 | 168 | −70 [−114, −35] |
| Over 7 changed lines | 302 | 214 | 253 | −39 [−61, −17] |

- **On comments that need the repository, Codex is level with our reviews:** 64 against 63 to 74. Against the file and callees levels it is 10 behind, with intervals that include zero.
- **It is behind on comments answerable from the diff or the file:** 182 against 242 and 109 against 152 for no context.
- **Most of the gap is nits and AI-generated comments.** On comments written by people it is 5 behind, within chance.
- **It is behind at every hunk size,** least in relative terms on the largest hunks.

So exploring the repository buys parity on the comments that need it, at 19 times the cost, and loses on the rest.

## 5. What a session looks like

- **Requests and time:** 16,265 model requests for 1,000 hunks. A session took 237 seconds at the median and 274 on average, with up to 20 sessions running at once.
- **Tokens:** each request resends Codex's instructions and the conversation so far, so a hunk reads 348,720 input tokens on average, against 958 to 3,601 for our single request.
- **Drift:** besides the findings on other code, 383 commands named a path outside the checkout; the read block refuses those.

## 6. What happened during the run

| When (EST) | What |
|---|---|
| Nov 27, 16:13 to 16:23 | First start, stopped by hand after ten minutes; the cut-off sessions were redone |
| Nov 27, 19:22 | Resumed; block 1 scored at 20:06 |
| Nov 27, 20:06 to 23:35 | **A lossy route to the server during block 2.** Packet loss of 15 to 20% held the single SSH tunnel to about 17 to 30 KB/s, and 47 sessions ran out of time. They were moved aside and run again; they count in no result. From then on a pool of six compressed tunnels was used (`scripts/harness/tunnel_pool.py`) |
| Nov 28, from block 3 | 20 sessions at a time in place of 12 |
| Nov 28, block 3 | One hunk's checkout failed twice (`nextcloud/server` 52846); its commit was fetched by hand and the session run separately |
| Nov 28, block 6 | **One real timeout.** `timescale/timescaledb` 8138, hunk 14, hit the 900-second limit on both tries at normal speed (36 requests). It stays in the sample as a session that covers nothing; its hunk has no reference comment |
| Nov 28, 09:00 | Stopped after block 10 was scored. Block 11 had just started; its few cut-off sessions are not counted |

**A caveat on block 2.** 52 of its sessions finished during the slow period, and only short sessions could beat the time limit then (9.9 requests on average, against 17.1 in block 1). They were kept. Codex's coverage relative to the levels is the same in those sessions and in the 47 redone ones (0.67 and 0.64, permutation p = 0.70).

## 7. What can and cannot be claimed

1. **Can be claimed:** on this benchmark and model, one request per hunk covers over a third more reference comments than Codex per hunk, at about a twentieth of the cost.
2. **Can be claimed:** the result is stable from 200 hunks on and does not depend on which of our levels Codex is set against.
3. **Cannot be claimed:** that Codex is the worse reviewer overall. Its findings on code outside the hunk are not scored, and it was given one hunk at a time, which is not how it is normally used.
4. **Cannot be claimed as a test:** the split of Section 4. It was chosen after the totals were seen.

## 8. Limits

- **Per hunk only.** Codex once per pull request with its own review command, as a developer would run it, was not run.
- **Findings off the hunk are not scored.** Some of the 2,197 may be real problems elsewhere in the pull request. The comparison is on the hunk each session was asked to review.
- **Thinking off, one model, one judge.** The first trial with thinking on covered more at a higher cost on 12 hunks.
- **1,000 of 1,811 hunks.** They are a random sample, so the result should carry to the rest, with the interval above.
- **Cost uses a price list.** With cached input at a twentieth of new input, Codex's cost is 19 times ours; without that discount it would be more than 100 times.
- **The time limit ended one session.** A longer limit would add its review and its cost.

## 9. How to reproduce

```bash
.venv/bin/python -u scripts/harness/tunnel_pool.py -p <ssh port> root@<server> --tunnels 6      # local port 8000 to the model server
.venv/bin/python -u scripts/harness/run_codex_blocks.py --profile cloud-hunk-kept --thinking off \
    --parallel 10 --concurrency 2 --max-blocks 10                                               # runs and scores block after block
.venv/bin/python scripts/analysis/codex_blocks_report.py --breakdown \
    --json results/codex_blocks/report_1000_hunks.json                                          # this document's numbers
.venv/bin/python scripts/analysis/codex_blocks_report.py --blocks 5                             # any earlier point of Section 3
```

- The driver skips finished sessions and keeps the saved order, so a stopped run continues where it was.
- The report calls no model. `results/codex_blocks/report_1000_hunks.json` holds every number of Sections 2, 4 and 5.
- Tests: `tests/test_run_codex_blocks.py`, `tests/test_tunnel_pool.py`, `tests/test_codex_blocks_report.py`.
