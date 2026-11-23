"""Conservative local content scan; reports paths only, never matched values."""

import re
import subprocess
from pathlib import Path

paths = (
    subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    .decode()
    .split("\0")
)
patterns = [
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
    re.compile(r"AKIA[A-Z0-9]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r'"private_key"\s*:\s*"[^"\n]{20,}'),
]
violations = []
count = 0
for name in paths:
    if not name or not Path(name).is_file():
        continue
    raw = Path(name).read_text(errors="replace")
    count += 1
    if any(pattern.search(raw) for pattern in patterns):
        violations.append(name)
for path in [".env", "state/trading.sqlite3", ".venv312/bin/python", "service-account.json"]:
    result = subprocess.run(["git", "check-ignore", "-q", path], check=False)
    if result.returncode != 0:
        violations.append("missing ignore rule: " + path)
if violations:
    print("FAIL: inspect these paths privately:", ", ".join(violations))
    raise SystemExit(1)
print(
    f"PASS: scanned {count} source/documentation/fixture files; no detected credential patterns; secret/state ignores present"
)
