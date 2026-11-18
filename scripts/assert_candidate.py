"""Verify the installed wheel's PEP 610 origin and digest."""
import hashlib
import json
from importlib.metadata import distribution
from pathlib import Path
import sys
wheel = Path(sys.argv[1]).resolve(strict=True)
origin = json.loads(distribution("openeight").read_text("direct_url.json"))
assert origin["url"] == wheel.as_uri(), origin["url"]
assert origin["archive_info"]["hashes"]["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
print("Installed candidate origin and SHA256 match")
