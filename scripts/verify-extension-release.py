"""Check a public release VSIX before uploading it to an extension registry."""

import hashlib
import json
from pathlib import Path
import re
import struct
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
        bundled = archive.read("extension/bundled/seam/__init__.py").decode()
        adapter_version = re.search(r'^__version__ = "([^"]+)"', bundled, re.MULTILINE)
        if not adapter_version or adapter_version.group(1) != manifest.get(
            "seamAdapterVersion", version
        ):
            raise ValueError("Bundled debugger version does not match the declared adapter version")
        if manifest.get("icon"):
            icon = archive.read("extension/" + manifest["icon"])
            if icon[:8] != b"\x89PNG\r\n\x1a\n":
                raise ValueError("Extension icon must be a PNG")
            width, height = struct.unpack(">II", icon[16:24])
            if width != height or width < 128:
                raise ValueError("Extension icon must be square and at least 128 pixels")
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
        if "python-rust-demo.gif" in readme:
            url = (
                "https://raw.githubusercontent.com/ChirayuAgg0706/Seam/main/"
                "vscode/images/python-rust-demo.gif"
            )
            if url not in readme:
                raise ValueError("Missing public demo GIF URL")
            demo = archive.read("extension/images/python-rust-demo.gif")
            if demo[:6] not in (b"GIF87a", b"GIF89a"):
                raise ValueError("Demo must be a GIF")
            width, height = struct.unpack("<HH", demo[6:10])
            if width < 640 or height < 400 or b"NETSCAPE2.0" not in demo:
                raise ValueError("Demo must be readable and loop")
    print(f"Verified {publisher}.seam-debugger {version}, Linux x64, SHA-256 {digest}")


if __name__ == "__main__":
    try:
        verify(*sys.argv[1:])
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as error:
        sys.exit(str(error))
