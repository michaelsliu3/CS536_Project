"""Read the run profiles of notebooks/outcome_table.ipynb without running the notebook, so a
script and the notebook cannot drift apart."""
from __future__ import annotations

import ast
import json
from pathlib import Path

NOTEBOOK = Path(__file__).resolve().parents[2] / "notebooks" / "outcome_table.ipynb"


def read_notebook_profile(profile: str, notebook: Path = NOTEBOOK) -> dict:
    """The notebook's PROFILES[profile]."""
    for cell in json.loads(Path(notebook).read_text(encoding="utf-8"))["cells"]:
        source = "".join(cell["source"]) if isinstance(cell["source"], list) else cell["source"]
        if cell["cell_type"] != "code" or "PROFILES = {" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PROFILES" for t in node.targets):
                profiles = ast.literal_eval(node.value)
                if profile not in profiles:
                    raise ValueError(f"profile {profile!r} is not in {Path(notebook).name} (has {sorted(profiles)})")
                return profiles[profile]
    raise ValueError(f"no `PROFILES = {{...}}` assignment found in {notebook}")
