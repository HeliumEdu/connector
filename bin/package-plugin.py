#!/usr/bin/env python3
"""
Package `plugin/` as the Agent Plugins ZIP that OpenAI's plugin directory takes, with its manifest's `version` set to
the connector's.
"""

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "plugin"
VERSION_FILE = ROOT / "helium_connector" / "__init__.py"


def read_version():
    match = re.search(r'^__version__ = "([^"]+)"$', VERSION_FILE.read_text(), re.MULTILINE)
    if not match:
        sys.exit(f"✗ __version__ not found in {VERSION_FILE}")
    return match.group(1)


def package(output, version):
    source = json.loads((PLUGIN_DIR / "plugin.json").read_text())
    manifest = {key: source[key] for key in ("$schema", "name")} | {"version": version} | source
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plugin.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        for path in sorted(PLUGIN_DIR.rglob("*")):
            relative = path.relative_to(PLUGIN_DIR).as_posix()
            if path.is_file() and relative != "plugin.json" and not path.name.startswith("."):
                archive.write(path, relative)
    print(f"Built {output} ({version})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Where to write the ZIP")
    args = parser.parse_args()
    package(args.output.resolve(), read_version())


if __name__ == "__main__":
    main()
