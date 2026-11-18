"""Force recipe pip environments to consume the exact locally built SDK wheel."""
import os
from pathlib import Path
root = Path(os.environ["RUNNER_TEMP"])
wheel, = (root / "wheels").glob("*.whl")
constraint = root / "candidate.txt"
constraint.write_text("openeight @ " + wheel.resolve().as_uri() + "\n")
with Path(os.environ["GITHUB_ENV"]).open("a") as output:
    output.write("PIP_CONSTRAINT=" + str(constraint) + "\n")
