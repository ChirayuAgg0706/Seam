"""Check that Seam's adapter and extension versions are declared consistently.

The Python package (src/seam/__init__.py) is the source; pyproject.toml reads it. The VS
Code extension can release listing-only updates separately, with seamAdapterVersion
declaring the included adapter. Both changelogs must have their respective version.
Usage: python tools/check_versions.py [v<adapter-version>|extension-v<extension-version>]
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as fh:
        return fh.read()


def main():
    package = re.search(
        r'^__version__ = "([^"]+)"', read("src", "seam", "__init__.py"), re.MULTILINE
    ).group(1)
    manifest = json.loads(read("vscode", "package.json"))
    extension = manifest["version"]
    adapter = manifest.get("seamAdapterVersion", extension)
    problems = []
    if adapter != package:
        problems.append(
            "the extension expects adapter %s, the package says %s" % (adapter, package)
        )
    if not re.search(r"^## .*%s" % re.escape(package), read("CHANGELOG.md"), re.MULTILINE):
        problems.append("CHANGELOG.md has no section for %s" % package)
    if not re.search(
        r"^## .*%s" % re.escape(extension), read("vscode", "CHANGELOG.md"), re.MULTILINE
    ):
        problems.append("vscode/CHANGELOG.md has no section for %s" % extension)
    if len(sys.argv) > 1:
        tag = sys.argv[1]
        actual = extension if tag.startswith("extension-v") else package
        expected = tag.removeprefix("extension-").removeprefix("v")
        if expected != actual:
            problems.append("expected version %s, the release says %s" % (tag, actual))
    for problem in problems:
        print("version check: " + problem)
    if not problems:
        print("version check: adapter %s, extension %s" % (package, extension))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
