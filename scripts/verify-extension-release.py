"""Check a public release VSIX before uploading it to an extension registry."""

import hashlib
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
import zipfile


def verify(package, publisher, version):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]*", publisher):
        raise ValueError("Invalid publisher ID")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Expected a release version such as 0.1.0")
    package = Path(package)
    digest, filename = Path(str(package) + ".sha256").read_text().strip().split()
    if filename != package.name or hashlib.sha256(package.read_bytes()).hexdigest() != digest:
        raise ValueError("VSIX checksum or filename mismatch")
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read("extension/package.json"))
        for key, value in {
            "publisher": publisher,
            "name": "seam-debugger",
            "version": version,
        }.items():
            if manifest.get(key) != value:
                raise ValueError("Unexpected extension " + key)
        xml = ET.fromstring(archive.read("extension.vsixmanifest"))
        identity = xml.find("{*}Metadata/{*}Identity")
        expected = {
            "Publisher": publisher,
            "Id": "seam-debugger",
            "Version": version,
            "TargetPlatform": "linux-x64",
        }
        if identity is None or any(identity.get(key) != value for key, value in expected.items()):
            raise ValueError("VSIX identity or platform mismatch")
        for name in (
            "extension/bundled/__main__.py",
            "extension/bundled/seam/cli.py",
            "extension/bundled/seam/_target/_seam_trap.abi3.so",
            "extension/lib/adapter.js",
            "extension/images/python-rust-stack.png",
            "extension/images/python-variables-at-native-stop.png",
            "extension/images/native-disassembly.png",
        ):
            if not archive.read(name):
                raise ValueError("Missing or empty bundled file: " + name)
        readme = archive.read("extension/readme.md").decode()
        for image in (
            "python-rust-stack.png",
            "python-variables-at-native-stop.png",
            "native-disassembly.png",
        ):
            url = (
                "https://raw.githubusercontent.com/ChirayuAgg0706/Seam/main/vscode/images/" + image
            )
            if url not in readme:
                raise ValueError("Missing public screenshot URL: " + image)
    print(f"Verified {publisher}.seam-debugger {version}, Linux x64, SHA-256 {digest}")


if __name__ == "__main__":
    try:
        verify(*sys.argv[1:])
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as error:
        sys.exit(str(error))
