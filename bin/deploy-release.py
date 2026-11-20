#!/usr/bin/env python3

"""
Deploy a release of a Helium component (platform, connector) to an environment.

This script handles the full deployment workflow:
1. Update Terraform variables in deploy repo
2. Commit and push changes
3. Find and apply the Terraform plan
4. Wait for deployment to complete
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from git import Repo


def update_terraform_variables(deploy_repo_path, environment, version, version_variable):
    """Update the version variable in Terraform."""
    print(f"Updating Terraform variables for {environment} to version {version} ...")

    variables_file = Path(deploy_repo_path) / "terraform" / "environments" / environment / "variables.tf"

    if not variables_file.exists():
        print(f"✗ Variables file not found: {variables_file}")
        return False

    # Read the file
    with open(variables_file, 'r') as f:
        content = f.read()

    # Update only the version variable's default value using awk-like logic
    lines = content.split('\n')
    new_lines = []
    in_version_block = False

    for line in lines:
        if line.strip().startswith(f'variable "{version_variable}"'):
            in_version_block = True
            new_lines.append(line)
        elif in_version_block and 'default' in line:
            # Replace the default value
            new_line = re.sub(
                r'default\s*=\s*"[^"]*"',
                f'default     = "{version}"',
                line
            )
            new_lines.append(new_line)
            in_version_block = False
        else:
            new_lines.append(line)

    # Write back
    with open(variables_file, 'w') as f:
        f.write('\n'.join(new_lines))

    print(f"✓ Updated {variables_file} with version {version}")

    # Show the updated variable
    print(f"\n{version_variable} variable:")
    subprocess.run(["grep", "-A", "2", f'variable "{version_variable}"', str(variables_file)])

    return True


def commit_and_push_changes(deploy_repo_path, version, environment, component):
    """
    Commit and push Terraform changes.

    Returns:
        tuple: (success: bool, changes_pushed: bool)
            - (True, True) if changes were pushed successfully
            - (True, False) if no changes needed (already up to date)
            - (False, False) if an error occurred
    """
    print(f"\nCommitting and pushing changes ...")

    repo = Repo(deploy_repo_path)
    origin = repo.remote('origin')

    print(f"Remote 'origin' URL: {origin.url}")
    print("Pulling latest changes from origin ...")

    # Use subprocess to run git pull directly
    result = subprocess.run(
        ["git", "pull", "origin", "main"],
        cwd=deploy_repo_path,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        print(f"Warning: Pull had issues but continuing ...")
        print(f"  stdout: {result.stdout}")
        print(f"  stderr: {result.stderr}")

    # Check if there are changes
    if not repo.is_dirty(untracked_files=False):
        print("✓ No changes to commit - version already deployed")
        return (True, False)  # Success, but no changes

    # Stage the variables file
    variables_file = f"terraform/environments/{environment}/variables.tf"
    repo.index.add([variables_file])

    # Commit with [component] prefix so Terraform run is identifiable
    commit_message = f"[{component}] Deploy {version} to {environment}"
    repo.index.commit(commit_message)

    # Push using git command directly for better error handling
    try:
        print("Pushing to origin ...")

        # Use subprocess to run git push directly
        result = subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=deploy_repo_path,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            print(f"✗ Push failed:")
            print(f"  stdout: {result.stdout}")
            print(f"  stderr: {result.stderr}")
            return (False, False)

        print(f"✓ Committed and pushed: {commit_message}")
    except Exception as e:
        print(f"✗ Push failed with exception: {e}")
        return (False, False)

    return (True, True)  # Success with changes pushed


TERRAFORM_API = "https://app.terraform.io/api/v2"

TERRAFORM_DONE_STATUSES = {"apply_queued", "applying", "applied", "confirmed", "planned_and_finished"}
TERRAFORM_FAILED_STATUSES = {"errored", "discarded", "canceled", "force_canceled"}


class TerraformCloudUnavailableError(Exception):
    """Terraform Cloud kept failing transiently (5xx, 429, or a connection error) through every retry."""


def _tfc_request(method, url, headers, attempts=6, delay_seconds=10, **kwargs):
    """
    Make a Terraform Cloud API request, retrying transient failures.

    :raises TerraformCloudUnavailableError: when every attempt failed transiently.
    """
    for attempt in range(1, attempts + 1):
        try:
            response = requests.request(method, url, headers=headers, timeout=30, **kwargs)
            if response.status_code != 429 and response.status_code < 500:
                return response
            reason = f"HTTP {response.status_code}"
        except requests.RequestException as e:
            reason = type(e).__name__
        if attempt < attempts:
            print(f"  Terraform Cloud {reason}, retrying in {delay_seconds}s ({attempt}/{attempts}) ...")
            time.sleep(delay_seconds)
    raise TerraformCloudUnavailableError(f"{method} {url} failed after {attempts} attempts: {reason}")


def _is_release_run(message, environment, version, component):
    return message.startswith(f"[{component}] Deploy {version} to {environment}")


def find_and_apply_terraform_run(environment, version, terraform_token, component, other_release_prefix,
                                 pushed_at, expect_new_run=True, timeout_minutes=10):
    """
    Find the Terraform run for this release and apply it, discarding competing runs. A new run is one created after
    ``pushed_at``, so an older run of the same version (a rollback or redeploy) isn't mistaken for it. A re-run that had
    nothing to commit (``expect_new_run`` False) applies the release's run if it's still waiting, starts a new one if
    it failed, and otherwise has nothing to do.
    """
    print(f"\nFinding and applying Terraform run ...")

    headers = {
        "Authorization": f"Bearer {terraform_token}",
        "Content-Type": "application/vnd.api+json"
    }

    # Get workspace details
    workspace_resp = _tfc_request("GET", f"{TERRAFORM_API}/organizations/HeliumEdu/workspaces/{environment}",
                                  headers)
    workspace_resp.raise_for_status()
    workspace_id = workspace_resp.json()['data']['id']

    print(f"Workspace ID: {workspace_id}")

    # Wait for and find the [component] run, discard competing runs
    print(f"Waiting for [{component}] Terraform plan for version {version} ...")

    iterations = (timeout_minutes * 60) // 10
    run_id = None

    for i in range(iterations):
        time.sleep(10)

        # Get runs
        try:
            runs_resp = _tfc_request("GET", f"{TERRAFORM_API}/workspaces/{workspace_id}/runs", headers, attempts=1)
        except TerraformCloudUnavailableError as e:
            print(f"  {e}; checking again ...")
            continue
        runs_resp.raise_for_status()
        runs = runs_resp.json().get('data', [])

        # Discard competing runs
        for run in runs:
            run_id_iter = run['id']
            status = run['attributes']['status']
            message = run['attributes'].get('message', '')

            # Discard non-[component] planned/pending runs or wrong version; the other release applies its own
            if status in ['planned', 'pending'] and not message.startswith(other_release_prefix) and \
               not _is_release_run(message, environment, version, component):

                actions = run['attributes'].get('actions', {})
                reject_endpoint = 'cancel' if actions.get('is-cancelable') else 'discard'

                print(f"  Discarding run {run_id_iter}: {message}")
                try:
                    _tfc_request("POST", f"{TERRAFORM_API}/runs/{run_id_iter}/actions/{reject_endpoint}", headers,
                                 json={"comment": f"Discarding in favor of official [{component}] release"})
                except TerraformCloudUnavailableError as e:
                    print(f"  {e}")

        # Find our [component] run (runs are listed newest first)
        release_run = next((run for run in runs
                            if _is_release_run(run['attributes'].get('message', ''), environment, version, component)
                            and (not expect_new_run or
                                 datetime.fromisoformat(run['attributes']['created-at'].replace('Z', '+00:00'))
                                 >= pushed_at)),
                           None)

        if release_run is None:
            if not expect_new_run:
                print(f"✓ No [{component}] run is waiting for version {version}, nothing to apply")
                return True
            continue

        status = release_run['attributes']['status']
        if release_run['attributes'].get('actions', {}).get('is-confirmable'):
            run_id = release_run['id']
            print(f"✓ Found [{component}] run {run_id} for version {version}")
            break
        if status in TERRAFORM_DONE_STATUSES:
            print(f"✓ [{component}] run {release_run['id']} for version {version} is already {status}, "
                  f"nothing to apply")
            return True
        if status in TERRAFORM_FAILED_STATUSES:
            if expect_new_run:
                print(f"✗ [{component}] run {release_run['id']} for version {version} is {status}")
                return False
            print(f"  [{component}] run {release_run['id']} for version {version} is {status}, starting a new run ...")
            new_run_resp = _tfc_request("POST", f"{TERRAFORM_API}/runs", headers, json={
                "data": {
                    "type": "runs",
                    "attributes": {"message": f"[{component}] Deploy {version} to {environment}"},
                    "relationships": {"workspace": {"data": {"type": "workspaces", "id": workspace_id}}},
                }
            })
            new_run_resp.raise_for_status()
            pushed_at = datetime.fromisoformat(
                new_run_resp.json()['data']['attributes']['created-at'].replace('Z', '+00:00'))
            expect_new_run = True

    if not run_id:
        print(f"✗ No [{component}] planned run found within {timeout_minutes} minutes")
        return False

    # Apply the plan
    print(f"Applying Terraform plan {run_id} ...")
    apply_resp = _tfc_request("POST", f"{TERRAFORM_API}/runs/{run_id}/actions/apply", headers,
                              json={"comment": f"[{component}] Apply {version}"})

    if apply_resp.status_code not in [200, 202]:
        print(f"✗ Apply failed with HTTP {apply_resp.status_code}")
        print(f"Response: {apply_resp.text}")
        return False

    print("✓ Apply triggered successfully")
    return True


def wait_for_deployment(environment, version, status_url, timeout_minutes=20):
    """Wait for the release's status URL to report the new version."""
    print(f"\nWaiting for deployment to be live ...")

    info_uri = status_url

    print(f"Checking {info_uri} for version {version} ...")

    iterations = (timeout_minutes * 60) // 20

    for i in range(iterations):
        time.sleep(20)

        try:
            response = requests.get(info_uri, timeout=10)
            if response.status_code == 200:
                data = response.json()
                current_version = data.get('version', 'unknown')

                print(f"  Current version: {current_version} (waiting for {version})")

                if current_version == version:
                    print(f"✓ Version {version} is now live in {environment}!")
                    return True
            else:
                print(f"  API returned HTTP {response.status_code}")
        except Exception as e:
            print(f"  API error: {e}")

    print(f"✗ Deployment did not complete within {timeout_minutes} minutes")
    return False


def main():
    parser = argparse.ArgumentParser(description="Deploy a component release")
    parser.add_argument("version", help="Version to deploy (e.g., 1.18.10)")
    parser.add_argument("environment", choices=["dev", "prod"], help="Environment to deploy to")
    parser.add_argument("--deploy-repo", required=True, help="Path to deploy repository")
    parser.add_argument("--component", required=True, help="Component to deploy, the commit prefix (e.g., platform)")
    parser.add_argument("--version-variable", required=True, help="Terraform version variable (e.g., helium_version)")
    parser.add_argument("--status-url", required=True, help="URL that reports the live version as JSON `version`")
    parser.add_argument("--other-release-prefix", required=True,
                        help="Commit prefix of the other release's runs, which it applies itself (e.g., [connector])")
    parser.add_argument("--skip-wait", action="store_true", help="Skip waiting for deployment")

    args = parser.parse_args()

    print(f"Starting deployment of version {args.version} to {args.environment} ...")
    print(f"Deploy repo: {args.deploy_repo}")
    print(f"Skip wait: {args.skip_wait}\n")

    # Get required environment variables
    terraform_token = os.environ.get('TERRAFORM_API_TOKEN')
    if not terraform_token:
        print("✗ TERRAFORM_API_TOKEN environment variable required")
        return 1

    # Update Terraform variables
    if not update_terraform_variables(args.deploy_repo, args.environment, args.version, args.version_variable):
        return 1

    # Commit and push
    pushed_at = datetime.now(timezone.utc)
    success, changes_pushed = commit_and_push_changes(args.deploy_repo, args.version, args.environment,
                                                      args.component)
    if not success:
        return 1

    # Find and apply Terraform run (a re-run with nothing to commit still applies a run left waiting)
    try:
        if not find_and_apply_terraform_run(args.environment, args.version, terraform_token, args.component,
                                            args.other_release_prefix, pushed_at, expect_new_run=changes_pushed):
            return 1
    except TerraformCloudUnavailableError as e:
        print(f"✗ {e}")
        return 1

    # Wait for deployment
    if not args.skip_wait:
        if not wait_for_deployment(args.environment, args.version, args.status_url):
            return 1

    print(f"\n✓ Deployment complete!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
