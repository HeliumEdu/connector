#!/usr/bin/env bash
# Verifies a connector release's Lambda zip is in S3 before deploying it.
#
# Usage: bin/verify-release.sh <version>

set -euo pipefail

VERSION="${1:?Usage: bin/verify-release.sh <version>}"
KEY="helium/connector/connector-$VERSION.zip"

echo "Verifying s3://heliumedu/$KEY exists ..."
if ! error="$(aws s3api head-object --bucket heliumedu --key "$KEY" 2>&1 >/dev/null)"; then
  echo "✗ s3://heliumedu/$KEY not found: $error" >&2
  exit 1
fi
echo "✓ Release zip found"
