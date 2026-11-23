"""Local structural checks; actionlint remains a separate deeper validator."""

from pathlib import Path

import yaml

files = list(Path(".github/workflows").glob("*.yml"))
scheduled = []
for path in files:
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    triggers = workflow["on"]
    assert workflow.get("permissions", {}).get("contents") == "read", path
    if "schedule" in triggers:
        scheduled.append(path.name)
        assert "push" not in triggers and "pull_request" not in triggers
        assert workflow["concurrency"]["cancel-in-progress"] == "false"
        assert workflow["jobs"]["run"]["env"]["PAPER_ORDERS_ENABLED"].endswith("|| 'false' }}")
    assert all(
        step.get("run", "").find("git push") < 0
        for job in workflow["jobs"].values()
        for step in job["steps"]
    )
assert scheduled == ["swing-scan.yml"], scheduled
print(f"PASS: {len(files)} workflow YAML files; one scheduled executor; no order mode on push")
