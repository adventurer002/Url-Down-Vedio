"""Run current positive F1/F1.1 regressions. No paid model/API calls.

Run from backend: uv run python reviews/run_clips_f1_audit.py
The original defect assertions and clips-f1-audit.xml remain historical evidence.
"""
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
REVIEW = Path(__file__).resolve().parent
pnpm = shutil.which("pnpm")
if not pnpm:
    raise RuntimeError("pnpm is required for frontend regressions")
result = subprocess.run(
    [pnpm, "exec", "vitest", "run", "src/tests/clips.test.tsx",
     "--reporter=junit", "--outputFile=" + str(REVIEW / "clips-f11-focused.xml")],
    cwd=FRONTEND, capture_output=True, text=True, timeout=90,
)
(REVIEW / "clips-f11-focused.log").write_text(result.stdout + result.stderr)
if result.returncode:
    print((result.stdout + result.stderr)[-12000:])
    raise SystemExit(result.returncode)
xml = ET.parse(REVIEW / "clips-f11-focused.xml").getroot()
suites = list(xml) if xml.tag == "testsuites" else [xml]
for suite in suites:
    print({key: suite.attrib.get(key) for key in ("tests", "failures", "errors", "skipped")})
print("Positive F1/F1.1 regressions passed; no temporary test file was created.")
