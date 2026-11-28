"""Local structural checks; actionlint remains a separate deeper validator."""

from pathlib import Path

import yaml

files = list(Path(".github/workflows").glob("*.yml"))
scheduled = set()
probes = {
    "scheduler-heartbeat.yml": "1,16,31,46 * * * *",
    "scheduler-offset-probe.yml": "7,22,37,52 * * * *",
}
for path in files:
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    triggers = workflow["on"]
    assert workflow.get("permissions", {}).get("contents") == "read", path
    if "schedule" in triggers:
        scheduled.add(path.name)
        if path.name in probes:
            assert triggers["schedule"] == [{"cron": probes[path.name]}]
            assert set(triggers) == {"schedule", "workflow_dispatch", "push"}
            assert triggers["push"] == {
                "branches": ["main"],
                "paths": [str(path)],
            }
            assert workflow["permissions"] == {"contents": "read", "actions": "read"}
            assert "concurrency" not in workflow and "env" not in workflow
            assert set(workflow["jobs"]) == {"heartbeat"}
            job = workflow["jobs"]["heartbeat"]
            assert "environment" not in job and "env" not in job
            assert job["timeout-minutes"] == "2"
            assert all("uses" not in step for step in job["steps"])
            assert all(
                step.get("env", {}) == {"GH_TOKEN": "${{ github.token }}"} for step in job["steps"]
            )
            assert not any(
                forbidden in path.read_text()
                for forbidden in ("secrets.", "trading_bot", "engine.py", "pip install")
            )
        else:
            assert path.name == "swing-scan.yml", path
            assert "push" not in triggers and "pull_request" not in triggers
            assert workflow["concurrency"]["cancel-in-progress"] == "false"
            assert workflow["jobs"]["run"]["env"]["PAPER_ORDERS_ENABLED"].endswith("|| 'false' }}")
    assert all(
        step.get("run", "").find("git push") < 0
        for job in workflow["jobs"].values()
        for step in job["steps"]
    )
assert scheduled == {"swing-scan.yml", *probes}, scheduled
print(
    f"PASS: {len(files)} workflow YAML files; one scheduled executor plus {len(probes)} read-only probes; "
    "no order mode on push"
)
