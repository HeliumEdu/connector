#!/usr/bin/env python3
"""
Verify a connector release's Lambda zip is in S3 before deploying it.
"""

import argparse
import subprocess
import sys

BUCKET = "heliumedu"


def verify_zip_exists(version):
    key = f"helium/connector/connector-{version}.zip"
    print(f"Verifying s3://{BUCKET}/{key} exists ...")
    result = subprocess.run(["aws", "s3api", "head-object", "--bucket", BUCKET, "--key", key],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f"✗ s3://{BUCKET}/{key} not found")
        return False
    print("✓ Release zip found")
    return True


def main():
    parser = argparse.ArgumentParser(description="Verify a connector release's zip")
    parser.add_argument("version", help="Version to verify (e.g., 0.1.4)")
    args = parser.parse_args()

    if not verify_zip_exists(args.version):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
