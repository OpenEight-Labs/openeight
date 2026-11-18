"""Select independent package checks; unknown/shared paths run all lanes."""
import os
import subprocess
from pathlib import Path


def select_checks(paths: list[str]) -> dict[str, bool]:
    shared = any(not path.startswith(("sdk/python/", "sdk/typescript/", "cookbook/")) for path in paths)
    python = shared or any(path.startswith("sdk/python/") for path in paths)
    typescript = shared or any(path.startswith("sdk/typescript/") for path in paths)
    return {
        "python": python,
        "typescript": typescript,
        "cookbook": python or any(path.startswith("cookbook/") for path in paths),
    }


def main() -> None:
    base = os.environ.get("CHECK_BASE", "")
    if base and set(base) != {"0"}:
        paths = subprocess.check_output(
            ["git", "diff", "--name-only", base, "HEAD"], text=True
        ).splitlines()
    else:
        paths = ["shared"]
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for name, enabled in select_checks(paths).items():
            output.write(f"{name}={str(enabled).lower()}\n")


if __name__ == "__main__":
    main()
