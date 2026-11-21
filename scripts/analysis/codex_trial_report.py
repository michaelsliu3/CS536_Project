#!/usr/bin/env python3
"""The numbers and figures of a Codex trial's write-up (docs/codex_first_trial.md), from the
trial's saved sessions and scores. Calls no model.

    .venv/bin/python scripts/analysis/codex_trial_report.py
    .venv/bin/python scripts/analysis/codex_trial_report.py --no-figures

It reads, for a per-hunk profile (default cloud-hunk-kept):

- <out_root>/<run_name>_codex/summary.csv and <run_name>_codex_nothink/summary.csv, which
  `scripts/harness/record_codex.py score` writes (one row per level, Codex and the profile's own);
- the Codex session files under ROOT/harness/codex/ and codex_nothink/.

It prints where each run's input tokens come from (the harness's own instructions, the request,
the conversation resent) and one session request by request, and draws three figures into
docs/figures/codex_first_trial/: quality and cost of every level, input tokens by source, and one
session's requests. The model's tokenizer must be available offline (it counts the request).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from src.harness_context import codex as harness  # noqa: E402
from src.review_outcomes.profiles import read_notebook_profile  # noqa: E402
from src.tracer_context.runtime import make_counter  # noqa: E402

RUNS = {"Codex, thinking on": ("codex", "_codex"), "Codex, thinking off": ("codex_nothink", "_codex_nothink")}
# The chart colours and ink of the project's figures: three categorical hues that pass the
# colour-blind checks next to each other, on a near-white surface.
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
DPI = 200


def read_summary(path: Path) -> dict[str, dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["level"]: row for row in csv.DictReader(handle)}


def session_requests(folder: Path) -> tuple[list[dict], str]:
    """(a session's parsed session files, the request it was given)."""
    sessions, request = [], ""
    for path in sorted(folder.glob("session_*.jsonl")):
        lines = path.read_text(encoding="utf-8").splitlines()
        sessions.append(harness.parse_session(lines))
        for line in lines:
            record = json.loads(line) if line.startswith("{") else {}
            payload = record.get("payload") or {}
            if record.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
                text = "".join(part.get("text") or "" for part in payload.get("content") or () if isinstance(part, dict))
                if "<environment_context>" not in text:
                    request = text
    return sessions, request


def run_sources(folder: Path, count) -> dict:
    """A run's input tokens by source, summed over its sessions, and what was read once."""
    total = {"sessions": 0, "requests": 0, "harness": 0, "request": 0, "conversation": 0, "total": 0, "command_output": 0}
    for session_dir in sorted(folder.glob("*__h*")):
        sessions, request = session_requests(session_dir)
        sources = harness.input_sources(sessions, count(request))
        total["sessions"] += 1
        for name in ("requests", "harness", "request", "conversation", "total"):
            total[name] += sources[name]
        total["command_output"] += sum(command["output_tokens"] or 0 for session in sessions for command in session["commands"])
    return total


def session_trace(folder: Path, count) -> list[dict]:
    """One session request by request: its size, the cached part, and what the model did."""
    sessions, request = session_requests(folder)
    lines = [json.loads(line) for line in (folder / "session_exec.jsonl").read_text(encoding="utf-8").splitlines() if line.startswith("{")]
    rows, did, printed, calls = [], [], {}, {}
    for record in lines:
        payload = record.get("payload") or {}
        if record.get("type") == "response_item" and payload.get("type") == "function_call":
            arguments = json.loads(payload.get("arguments") or "{}")
            calls[payload["call_id"]] = arguments.get("cmd") or "(waits on a running command)"
            did.append(payload["call_id"])
        elif record.get("type") == "response_item" and payload.get("type") == "function_call_output":
            output = payload.get("output") if isinstance(payload.get("output"), str) else json.dumps(payload.get("output"))
            found = re.search(r"Original token count: (\d+)", output)
            printed[payload["call_id"]] = int(found.group(1)) if found else 0
        elif record.get("type") == "token_usage_record":
            rows.append({"usage": payload["usage"], "calls": did})
            did = []
    first, own_request = rows[0]["usage"]["input_tokens"], count(request)
    for row in rows:
        size = row["usage"]["input_tokens"]
        row.update(input=size, cached=row["usage"].get("cached_input_tokens", 0), output=row["usage"]["output_tokens"],
                   harness=first - own_request, request=own_request, conversation=size - first,
                   commands=[calls[c] for c in row["calls"]], printed=sum(printed.get(c, 0) for c in row["calls"]))
    return rows


# ---------- figures ----------

def _rounded(ax, x0, y0, x1, y1, color, end: str | None, radius_px: float = 4.0):
    """A bar from (x0, y0) to (x1, y1) in data units, with its data end rounded by about
    `radius_px` screen pixels: "right" for a horizontal bar, "top" for a column, None for an inner
    segment of a stack. The axis limits and the figure's size must be final when this is called."""
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MPath

    box, fig = ax.get_position(), ax.figure
    (xlo, xhi), (ylo, yhi) = ax.get_xlim(), ax.get_ylim()
    rx = radius_px * (xhi - xlo) / (box.width * fig.get_figwidth() * 100)   # 100 screen pixels to the inch
    ry = radius_px * (yhi - ylo) / (box.height * fig.get_figheight() * 100)
    rx, ry = min(rx, abs(x1 - x0) / 2), min(ry, abs(y1 - y0) / 2)
    M, L, C = MPath.MOVETO, MPath.LINETO, MPath.CURVE3
    if end == "right":
        verts = [(x0, y0), (x1 - rx, y0), (x1, y0), (x1, y0 + ry), (x1, y1 - ry), (x1, y1), (x1 - rx, y1), (x0, y1), (x0, y0)]
        codes = [M, L, C, C, L, C, C, L, L]
    elif end == "top":
        verts = [(x0, y0), (x0, y1 - ry), (x0, y1), (x0 + rx, y1), (x1 - rx, y1), (x1, y1), (x1, y1 - ry), (x1, y0), (x0, y0)]
        codes = [M, L, C, C, L, C, C, L, L]
    else:
        verts, codes = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], [M, L, L, L, L]
    ax.add_patch(PathPatch(MPath(verts, codes), facecolor=color, edgecolor="none", zorder=3))


def _gap(ax, axis: str, pixels: float = 2.0) -> float:
    """`pixels` screen pixels along an axis, in data units: the surface gap between stacked segments."""
    box, fig = ax.get_position(), ax.figure
    if axis == "x":
        low, high = ax.get_xlim()
        return pixels * (high - low) / (box.width * fig.get_figwidth() * 100)
    low, high = ax.get_ylim()
    return pixels * (high - low) / (box.height * fig.get_figheight() * 100)


def _style(ax, grid_axis: str):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.spines["left" if grid_axis == "x" else "bottom"].set_visible(True)
    ax.spines["left" if grid_axis == "x" else "bottom"].set_color(AXIS)
    ax.grid(axis=grid_axis, color=GRID, linewidth=0.6, zorder=0)
    ax.tick_params(colors=MUTED, length=0, labelsize=8)
    ax.set_axisbelow(True)


def _compact(value: float) -> str:
    return f"{value / 1e6:.2f}M" if value >= 1e6 else f"{value / 1e3:.0f}K" if value >= 1e4 else f"{value:,.0f}"


def _legend(fig, entries, y=0.02):
    from matplotlib.patches import Patch
    fig.legend(handles=[Patch(facecolor=color, edgecolor="none", label=label) for label, color in entries], loc="lower center",
               ncol=len(entries), frameon=False, fontsize=8, labelcolor=INK2, handlelength=1.0, handleheight=1.0, bbox_to_anchor=(0.5, y))


def figure_quality_cost(rows: list[tuple[str, dict]], references: int, path: Path):
    """Two panels on one list of rows: references covered, and cost."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MultipleLocator

    fig, axes = plt.subplots(1, 2, figsize=(9.2, 3.3), dpi=DPI, sharey=True, facecolor=SURFACE)
    fig.subplots_adjust(left=0.2, right=0.95, top=0.84, bottom=0.2, wspace=0.16)
    names = [name for name, _ in rows][::-1]
    panels = [("covered", f"Reference comments covered (of {references})", references, lambda v: f"{v:.0f}", 5),
              ("cost_usd", "Cost on a paid API, in dollars", None, lambda v: f"${v:.2f}", 0.5)]
    for ax, (field, title, limit, show, step) in zip(axes, panels):
        values = [float(row[field]) for _, row in rows][::-1]
        ax.set_xlim(0, limit or max(values) * 1.18)
        ax.set_ylim(-0.6, len(names) - 0.4)
        ax.set_yticks(range(len(names)), names)
        _style(ax, "x")
        ax.tick_params(axis="y", labelsize=9, labelcolor=INK2)
        ax.set_title(title, fontsize=9.5, color=INK, loc="left", pad=8)
        ax.xaxis.set_major_locator(MultipleLocator(step))
        ax.xaxis.set_major_formatter(lambda value, _, show=show: show(value) if field == "cost_usd" else f"{value:.0f}")
        for index, (name, value) in enumerate(zip(names, values)):
            _rounded(ax, 0, index - 0.21, value, index + 0.21, ORANGE if name.startswith("Codex") else BLUE, "right")
            ax.annotate(show(value), (value, index), xytext=(5, 0), textcoords="offset points", va="center", fontsize=8.5, color=INK)
    _legend(fig, [("Our reviewer, one call per hunk at a fixed level", BLUE), ("Codex CLI, one session per hunk", ORANGE)])
    fig.savefig(path, dpi=DPI, facecolor=SURFACE)
    plt.close(fig)


def figure_sources(bars: list[tuple[str, dict]], path: Path):
    """One stacked bar per run: input tokens by what is resent."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    parts = [("harness", "Harness's own instructions and tools", BLUE),
             ("request", "Review request (PR text and hunk)", ORANGE),
             ("conversation", "Context: level's code, or conversation so far", AQUA)]
    fig, ax = plt.subplots(figsize=(9.2, 2.6), dpi=DPI, facecolor=SURFACE)
    fig.subplots_adjust(left=0.26, right=0.94, top=0.86, bottom=0.34)
    names = [name for name, _ in bars][::-1]
    longest = max(sum(values[key] for key, _, _ in parts) for _, values in bars)
    ax.set_xlim(0, longest * 1.13)
    ax.set_ylim(-0.6, len(names) - 0.4)
    ax.set_yticks(range(len(names)), names)
    ax.xaxis.set_major_formatter(lambda value, _: "0" if value == 0 else f"{value / 1e6:.1f}M")
    _style(ax, "x")
    ax.tick_params(axis="y", labelsize=9, labelcolor=INK2)
    ax.set_title("Input tokens sent to the model, 12 hunks, by what they are", fontsize=9.5, color=INK, loc="left", pad=8)
    gap = _gap(ax, "x")
    for index, (name, values) in enumerate(bars[::-1]):
        left, total = 0.0, sum(values[key] for key, _, _ in parts)
        present = [(key, color) for key, _, color in parts if values[key] > 0]
        for position, (key, color) in enumerate(present):
            last = position == len(present) - 1
            right = left + values[key]
            _rounded(ax, left + (gap / 2 if position else 0), index - 0.21, right - (0 if last else gap / 2), index + 0.21, color,
                     "right" if last else None)
            share = values[key] / total
            if share >= 0.12 and total >= longest * 0.2:  # print a share only where it fits inside the segment
                ax.text((left + right) / 2, index, f"{share:.0%}", ha="center", va="center", fontsize=8, color="#ffffff", zorder=4)
            left = right
        ax.annotate(_compact(total), (total, index), xytext=(5, 0), textcoords="offset points", va="center", fontsize=8.5, color=INK)
    _legend(fig, [(label, color) for _, label, color in parts], y=0.01)
    fig.savefig(path, dpi=DPI, facecolor=SURFACE)
    plt.close(fig)


def figure_session(trace: list[dict], title: str, path: Path):
    """One session: a stacked column per model request."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    parts = [("harness", "Codex's own instructions and tool definitions", BLUE), ("request", "Our request", ORANGE),
             ("conversation", "The conversation so far", AQUA)]
    fig, ax = plt.subplots(figsize=(9.2, 3.4), dpi=DPI, facecolor=SURFACE)
    fig.subplots_adjust(left=0.08, right=0.97, top=0.86, bottom=0.25)
    top = max(row["input"] for row in trace)
    ax.set_xlim(0.4, len(trace) + 0.6)
    ax.set_ylim(0, top * 1.12)
    ax.set_xticks(range(1, len(trace) + 1), [f"Request {i}" for i in range(1, len(trace) + 1)])
    ax.yaxis.set_major_formatter(lambda value, _: f"{value:,.0f}")
    _style(ax, "y")
    ax.tick_params(axis="x", labelsize=8.5, labelcolor=INK2)
    ax.set_title(title, fontsize=9.5, color=INK, loc="left", pad=8)
    gap = _gap(ax, "y")
    for index, row in enumerate(trace, 1):
        bottom = 0.0
        present = [(key, color) for key, _, color in parts if row[key] > 0]
        for position, (key, color) in enumerate(present):
            last = position == len(present) - 1
            _rounded(ax, index - 0.16, bottom + (gap / 2 if position else 0), index + 0.16, bottom + row[key] - (0 if last else gap / 2),
                     color, "top" if last else None)
            bottom += row[key]
        ax.annotate(f"{row['input']:,}", (index, row["input"]), xytext=(0, 4), textcoords="offset points", ha="center", fontsize=8.5, color=INK)
    _legend(fig, [(label, color) for _, label, color in parts], y=0.01)
    fig.savefig(path, dpi=DPI, facecolor=SURFACE)
    plt.close(fig)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default="cloud-hunk-kept", help="the per-hunk notebook profile of the trial")
    parser.add_argument("--session", default="ppy__osu__33423__h0002", help="the hunk whose thinking-off session is traced")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "docs" / "figures" / "codex_first_trial")
    parser.add_argument("--no-figures", action="store_true", help="print the numbers only")
    args = parser.parse_args(argv)

    config = read_notebook_profile(args.profile)
    root = (PROJECT_ROOT / config["contexts"]).parent
    scores = PROJECT_ROOT / config["out_root"]
    count, label = make_counter(config["reviewer"]["model"])
    print(f"profile {args.profile}; tokenizer {label}")

    summaries = {name: read_summary(scores / (config["run_name"] + suffix) / "summary.csv") for name, (_, suffix) in RUNS.items()}
    ours = next(iter(summaries.values()))
    rows = [(level, ours[level]) for level in config["levels"]] + [(name, summary[harness.HUNK_LEVEL]) for name, summary in summaries.items()]
    print("\nlevel, covered, recall, precision, severity score, all input, new input, output, cost")
    for name, row in rows:
        print(f"  {name}: {row['covered']}, {row['recall']}, {row['precision']}, {row.get('score_agreed')}, {int(row['prompt_tokens']):,}, "
              f"{int(row['new_input_tokens']):,}, {int(row['output_tokens']):,}, ${float(row['cost_usd']):.3f}")

    print("\ninput tokens by source")
    sources = {}
    for name, (folder, _) in RUNS.items():
        sources[name] = s = run_sources(root / "harness" / folder, count)
        print(f"  {name}: {s['sessions']} sessions, {s['requests']} requests, {s['total']:,} input tokens = harness {s['harness']:,} "
              f"({s['harness'] / s['total']:.1%}) + request {s['request']:,} ({s['request'] / s['total']:.1%}) + conversation "
              f"{s['conversation']:,} ({s['conversation'] / s['total']:.1%}); commands printed {s['command_output']:,} tokens once")
    widest, narrowest = ours[config["levels"][-1]], ours[config["levels"][0]]
    text = int(widest["level_text_tokens"])
    request = int(narrowest["level_text_tokens"])  # the narrowest level is the PR text and the hunk alone
    ours_sources = {"harness": int(widest["prompt_tokens"]) - text, "request": request, "conversation": text - request}
    print(f"  {config['levels'][-1]}: {int(widest['prompt_tokens']):,} = instructions {ours_sources['harness']:,} + request {request:,} "
          f"+ context {ours_sources['conversation']:,}")

    trace = session_trace(root / "harness" / "codex_nothink" / args.session, count)
    print(f"\nsession {args.session}, thinking off: request, input, cached, output, commands (tokens printed)")
    for index, row in enumerate(trace, 1):
        print(f"  {index}: {row['input']:,}, {row['cached']:,}, {row['output']:,}, {len(row['commands'])} ({row['printed']:,})")
    print(f"  total input {sum(row['input'] for row in trace):,}; printed once {sum(row['printed'] for row in trace):,}")

    if not args.no_figures:
        args.out.mkdir(parents=True, exist_ok=True)
        figure_quality_cost(rows, int(ours[config["levels"][0]]["references"]), args.out / "quality_and_cost.png")
        figure_sources([(f"Our reviewer at {config['levels'][-1]}", ours_sources)] + [(name, sources[name]) for name in reversed(RUNS)],
                       args.out / "input_tokens_by_source.png")
        figure_session(trace, f"One Codex session, thinking off ({args.session}): input tokens of each model request",
                       args.out / "one_session_requests.png")
        print(f"\nwrote {args.out}/quality_and_cost.png, input_tokens_by_source.png, one_session_requests.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
