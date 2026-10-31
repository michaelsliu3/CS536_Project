# CS536 integration: read before SKILL.md

This is the shared LaTeX skill for all agents in this repository. The canonical
copy is `.cursor/skills/latex-document/`; `.agents/skills/latex-document` is a
relative symlink for Codex. Both paths resolve scripts and references identically.
If a checkout cannot use symlinks, read the canonical path directly.

The user selected `docs/report_format/neurips_2026/` as the project report format.
Preserve its layout and follow the course page limits in `docs/`. Generic skill
preambles, IEEE templates, global list compaction and font/layout changes do not
override that selection. Preserve the reference originals; edit a separate draft.
Template examples and upstream test reports are not our research evidence.

The upstream skill describes Claude-specific tools and Linux installation steps.
Use the equivalent available tools on the current platform; do not assume
AskUserQuestion, a sibling generate-image skill, apt, TeX Live or other dependencies
are installed. Prefer the available built-in LaTeX compiler when applicable.
Read helper scripts before executing them: some install system packages. Installing
this skill did not install a TeX distribution or execute upstream setup scripts.
Use existing user choices for document elements rather than asking again.

Consult AGENTS.md for the mandatory entry-point sequence. Compilation must be
verified from diagnostics and page inspection, not merely from a PDF existing.
