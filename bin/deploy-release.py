#!/usr/bin/env python3
"""
Deploy a connector release to prod:

1. Verify the release's Lambda zip is in S3
2. Bump `connector_version` in the infra repo
3. Commit and push the change
4. Find and apply the Terraform plan
5. Wait for the connector to report the new version
"""

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

ORGANIZATION = "HeliumEdu"
WORKSPACE = "prod"
BUCKET = "heliumedu"
STATUS_URL = "https://mcp.heliumedu.com/status"
COMMIT_PREFIX = "[connector]"
#: Runs another release workflow applies itself, so they're never discarded here
OWNED_PREFIXES = ("[connector]", "[platform]")


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


def update_terraform_variables(deploy_repo_path, version):
    variables_file = Path(deploy_repo_path) / "terraform" / "environments" / WORKSPACE / "variables.tf"
    content = variables_file.read_text()
    updated = re.sub(r'(variable "connector_version" \{[^}]*?default\s*=\s*)"[^"]*"', rf'\g<1>"{version}"', content,
                     count=1)
    if updated == content and f'"{version}"' not in content:
        print(f"✗ connector_version not found in {variables_file}")
        return False
    variables_file.write_text(updated)
    print(f"✓ connector_version set to {version} in {variables_file}")
    return True


def commit_and_push_changes(deploy_repo_path, version):
    """
    :return: (success, whether a change was pushed)
    """
    def git(*args):
        return subprocess.run(["git", *args], cwd=deploy_repo_path, capture_output=True, text=True)

    git("pull", "origin", "main")
    if not git("status", "--porcelain").stdout.strip():
        print("✓ No changes to commit - version already deployed")
        return True, False

    git("add", f"terraform/environments/{WORKSPACE}/variables.tf")
    git("commit", "-m", f"{COMMIT_PREFIX} Deploy {version} to {WORKSPACE}")
    result = git("push", "origin", "main")
    if result.returncode != 0:
        print(f"✗ Push failed: {result.stderr}")
        return False, False
    print("✓ Pushed")
    return True, True


def find_and_apply_terraform_run(version, token, timeout_minutes=10):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/vnd.api+json"}
    workspace = requests.get(f"https://app.terraform.io/api/v2/organizations/{ORGANIZATION}/workspaces/{WORKSPACE}",
                             headers=headers)
    workspace.raise_for_status()
    workspace_id = workspace.json()["data"]["id"]

    print(f"Waiting for the {COMMIT_PREFIX} run for {version} to plan ...")
    run_id = None
    for _ in range((timeout_minutes * 60) // 10):
        time.sleep(10)
        runs = requests.get(f"https://app.terraform.io/api/v2/workspaces/{workspace_id}/runs", headers=headers)
        runs.raise_for_status()

        for run in runs.json().get("data", []):
            status = run["attributes"]["status"]
            message = run["attributes"].get("message", "")
            if status in ("planned", "pending") and not message.startswith(OWNED_PREFIXES):
                actions = run["attributes"].get("actions", {})
                endpoint = "cancel" if actions.get("is-cancelable") else "discard"
                print(f"  Discarding run {run['id']}: {message}")
                requests.post(f"https://app.terraform.io/api/v2/runs/{run['id']}/actions/{endpoint}",
                              headers=headers, json={"comment": f"Discarding in favor of {COMMIT_PREFIX} release"})
            elif message.startswith(COMMIT_PREFIX) and version in message:
                if status == "planned":
                    run_id = run["id"]
                elif status in ("discarded", "canceled", "errored", "force_canceled"):
                    print(f"✗ Run {run['id']} ended {status}")
                    return False
        if run_id:
            break

    if not run_id:
        print(f"✗ No planned {COMMIT_PREFIX} run within {timeout_minutes} minutes")
        return False

    print(f"Applying {run_id} ...")
    applied = requests.post(f"https://app.terraform.io/api/v2/runs/{run_id}/actions/apply", headers=headers,
                            json={"comment": f"{COMMIT_PREFIX} Apply {version}"})
    if applied.status_code not in (200, 202):
        print(f"✗ Apply failed with HTTP {applied.status_code}: {applied.text}")
        return False
    print("✓ Apply triggered")
    return True


def wait_for_deployment(version, timeout_minutes=20):
    print(f"Checking {STATUS_URL} for version {version} ...")
    for _ in range((timeout_minutes * 60) // 20):
        time.sleep(20)
        try:
            current = requests.get(STATUS_URL, timeout=10).json().get("version")
        except (requests.RequestException, ValueError) as e:
            print(f"  Status check failed: {e}")
            continue
        print(f"  Current version: {current} (waiting for {version})")
        if current == version:
            print(f"✓ Version {version} is live")
            return True
    print(f"✗ Deployment did not complete within {timeout_minutes} minutes")
    return False


def main():
    parser = argparse.ArgumentParser(description="Deploy a connector release to prod")
    parser.add_argument("version", help="Version to deploy (e.g., 0.1.4)")
    parser.add_argument("--deploy-repo", required=True, help="Path to the infra repository")
    args = parser.parse_args()

    token = os.environ.get("TERRAFORM_API_TOKEN")
    if not token:
        print("✗ TERRAFORM_API_TOKEN environment variable required")
        return 1

    if not verify_zip_exists(args.version):
        return 1
    if not update_terraform_variables(args.deploy_repo, args.version):
        return 1
    success, changes_pushed = commit_and_push_changes(args.deploy_repo, args.version)
    if not success:
        return 1
    if changes_pushed and not find_and_apply_terraform_run(args.version, token):
        return 1
    if not wait_for_deployment(args.version):
        return 1

    print("✓ Deployment complete!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
