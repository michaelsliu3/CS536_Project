"""The look shared by the project's report figures (matplotlib): colours, axes, rounded bars.

Used by the report scripts in this folder, which draw into docs/figures/.
"""
from __future__ import annotations

# Three categorical hues that pass the colour-blind checks next to each other, on a near-white
# surface, and the ink of text, grid and axes.
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
DPI = 200


def rounded(ax, x0, y0, x1, y1, color, end: str | None, radius_px: float = 4.0):
    """A bar from (x0, y0) to (x1, y1) in data units, with its data end rounded by about
    `radius_px` screen pixels: "right" for a horizontal bar, "left" for one that runs leftwards
    (x1 below x0), "top" for a column, None for an inner segment of a stack. The axis limits and
    the figure's size must be final when this is called."""
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
    elif end == "left":  # a bar that runs leftwards from its baseline x0 to its data end x1
        verts = [(x0, y0), (x1 + rx, y0), (x1, y0), (x1, y0 + ry), (x1, y1 - ry), (x1, y1), (x1 + rx, y1), (x0, y1), (x0, y0)]
        codes = [M, L, C, C, L, C, C, L, L]
    elif end == "top":
        verts = [(x0, y0), (x0, y1 - ry), (x0, y1), (x0 + rx, y1), (x1 - rx, y1), (x1, y1), (x1, y1 - ry), (x1, y0), (x0, y0)]
        codes = [M, L, C, C, L, C, C, L, L]
    else:
        verts, codes = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], [M, L, L, L, L]
    ax.add_patch(PathPatch(MPath(verts, codes), facecolor=color, edgecolor="none", zorder=3))


def gap(ax, axis: str, pixels: float = 2.0) -> float:
    """`pixels` screen pixels along an axis, in data units: the surface gap between stacked segments."""
    box, fig = ax.get_position(), ax.figure
    if axis == "x":
        low, high = ax.get_xlim()
        return pixels * (high - low) / (box.width * fig.get_figwidth() * 100)
    low, high = ax.get_ylim()
    return pixels * (high - low) / (box.height * fig.get_figheight() * 100)


def style(ax, grid_axis: str):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.spines["left" if grid_axis == "x" else "bottom"].set_visible(True)
    ax.spines["left" if grid_axis == "x" else "bottom"].set_color(AXIS)
    ax.grid(axis=grid_axis, color=GRID, linewidth=0.6, zorder=0)
    ax.tick_params(colors=MUTED, length=0, labelsize=8)
    ax.set_axisbelow(True)


def compact(value: float) -> str:
    return f"{value / 1e6:.2f}M" if value >= 1e6 else f"{value / 1e3:.0f}K" if value >= 1e4 else f"{value:,.0f}"


def legend(fig, entries, y=0.02):
    from matplotlib.patches import Patch
    fig.legend(handles=[Patch(facecolor=color, edgecolor="none", label=label) for label, color in entries], loc="lower center",
               ncol=len(entries), frameon=False, fontsize=8, labelcolor=INK2, handlelength=1.0, handleheight=1.0, bbox_to_anchor=(0.5, y))
