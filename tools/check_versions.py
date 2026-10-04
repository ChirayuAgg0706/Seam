"""Check that every place that states Seam's version agrees.

The Python package (src/seam/__init__.py) is the source; pyproject.toml reads it. The VS
Code extension has its own manifest, and the changelog must have a section for the
version. Usage: python tools/check_versions.py [expected-version]
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
    package = re.search(r'^__version__ = "([^"]+)"', read("src", "seam", "__init__.py"),
                        re.MULTILINE).group(1)
    extension = json.loads(read("vscode", "package.json"))["version"]
    problems = []
    if extension != package:
        problems.append("vscode/package.json says %s, the package says %s" % (extension, package))
    if not re.search(r"^## .*%s" % re.escape(package), read("CHANGELOG.md"), re.MULTILINE):
        problems.append("CHANGELOG.md has no section for %s" % package)
    if len(sys.argv) > 1 and sys.argv[1].lstrip("v") != package:
        problems.append("expected version %s, the package says %s" % (sys.argv[1], package))
    for problem in problems:
        print("version check: " + problem)
    if not problems:
        print("version check: %s everywhere" % package)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
