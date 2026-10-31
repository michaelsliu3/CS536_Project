"""Static report figures from saved analysis; Matplotlib is imported only on use."""
from __future__ import annotations

import math
from pathlib import Path
from textwrap import fill


def create_plots(result, output_dir, *, synthetic=False):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output = Path(output_dir)
    metrics = result["metrics"]
    subtitle = ("SYNTHETIC DEMO | " if synthetic else "") + str(result["split"])
    subtitle += f" | N={metrics[0]['examples']}, repositories={metrics[0]['repositories']}"

    def save(fig, name):
        fig.savefig(output / f"{name}.png", dpi=180, bbox_inches="tight")
        fig.savefig(output / f"{name}.pdf", bbox_inches="tight")
        plt.close(fig)

    with plt.rc_context({"font.size": 10, "pdf.fonttype": 42}):
        positions = list(range(len(metrics)))
        fig, ax = plt.subplots(figsize=(max(6, len(metrics) * 1.4), 4.5), layout="constrained")
        ax.bar([x - .18 for x in positions], [m["accuracy"] for m in metrics], .36, label="Accuracy")
        ax.bar([x + .18 for x in positions], [m["macro_f1"] for m in metrics], .36, label="Macro-F1")
        ax.set(xticks=positions, xticklabels=[fill(m["model"], 18) for m in metrics], ylim=(0, 1),
               ylabel="Score", title=f"Generated-answer scores\n{subtitle}")
        ax.legend()
        save(fig, "scores")

        columns = min(3, len(metrics))
        rows = math.ceil(len(metrics) / columns)
        fig, axes = plt.subplots(rows, columns, figsize=(3.5 * columns, 3.2 * rows),
                                 squeeze=False, layout="constrained")
        maximum = max(max(map(max, m["confusion_matrix"])) for m in metrics)
        used = []
        for ax, metric in zip(axes.flat, metrics):
            matrix = metric["confusion_matrix"]
            chart = ax.imshow(matrix, cmap="Blues", vmin=0, vmax=max(1, maximum))
            for y in range(2):
                for x in range(3):
                    ax.text(x, y, str(matrix[y][x]), ha="center", va="center",
                            color="white" if matrix[y][x] > maximum / 2 else "black")
            ax.set(xticks=[0, 1, 2], xticklabels=["False", "True", "Invalid"],
                   yticks=[0, 1], yticklabels=["False", "True"], xlabel="Predicted", ylabel="True label",
                   title=fill(metric["model"], 25))
            used.append(ax)
        for ax in list(axes.flat)[len(metrics):]:
            ax.set_visible(False)
        fig.colorbar(chart, ax=used, label="Examples", shrink=.7)
        fig.suptitle(f"Confusion matrices (shared color scale)\n{subtitle}")
        save(fig, "confusion_matrices")

        pairs = result["paired_comparisons"]
        if pairs:
            fig, ax = plt.subplots(figsize=(7, max(3.5, .65 * len(pairs) + 2)), layout="constrained")
            left = [0] * len(pairs)
            for category, color in (("both_correct", "#4c78a8"), ("improved", "#59a14f"),
                                    ("regressed", "#e15759"), ("both_wrong", "#bab0ac")):
                values = [pair[category] for pair in pairs]
                ax.barh(range(len(pairs)), values, left=left, label=category.replace("_", " "), color=color)
                left = [a + b for a, b in zip(left, values)]
            ax.set(yticks=list(range(len(pairs))), yticklabels=[p["model"] for p in pairs],
                   xlabel="Paired examples", title=f"Outcomes versus {result['reference']}\n{subtitle}")
            ax.legend(loc="upper center", bbox_to_anchor=(.5, -.2), ncol=2)
            save(fig, "paired_outcomes")
            if result["bootstrap"]["status"] == "ok":
                fig, ax = plt.subplots(figsize=(7, max(3.5, .65 * len(pairs) + 2)), layout="constrained")
                for y, pair in enumerate(pairs):
                    low, high = result["bootstrap"]["difference_intervals"][pair["model"]]["accuracy"]
                    ax.plot([100 * low, 100 * high], [y, y], color="#4c78a8", linewidth=2)
                    ax.plot(100 * pair["accuracy_difference"], y, "o", color="black")
                ax.axvline(0, color="gray", linestyle="--")
                ax.set(yticks=list(range(len(pairs))), yticklabels=[p["model"] for p in pairs],
                       xlabel="Accuracy difference (percentage points); 95% paired repository-bootstrap interval",
                       title=f"Differences versus {result['reference']}\n{subtitle}")
                save(fig, "paired_intervals")

        points = result["cost_points"]
        if points:
            fig, ax = plt.subplots(figsize=(7, 4.5), layout="constrained")
            legend_labels = set()
            for index, point in enumerate(points):
                label = "On empirical frontier" if point["on_frontier"] else "Dominated point estimate"
                ax.scatter(point["tokens"], point["accuracy"], marker="o" if point["on_frontier"] else "x", s=65,
                           label=label if label not in legend_labels else None)
                legend_labels.add(label)
                ax.annotate(point["model"], (point["tokens"], point["accuracy"]), xytext=(5, 7 + 14 * (index % 3)),
                            textcoords="offset points", fontsize=9)
            ax.set(xlabel="Mean " + result["cost_field"].replace("_", " "), ylabel="Accuracy", ylim=(0, 1),
                   title=f"Measured accuracy and cost (point estimates)\n{subtitle}")
            ax.margins(x=.2)
            ax.legend(loc="lower right", fontsize=9)
            save(fig, "accuracy_cost")
