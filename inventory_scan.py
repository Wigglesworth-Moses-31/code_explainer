#!/usr/bin/env python3
"""
Phase 0 inventory scanner.

Walks every repo in a GitHub org and classifies each as API-service,
event-driven-service, or both — based on framework/SDK signatures found
in source files. Produces a CSV registry to seed the microservice inventory.

Usage:
    export GITHUB_TOKEN=ghp_xxx
    python inventory_scan.py --org YOUR_ORG_NAME --out inventory.csv

Requires: pip install requests
"""

import argparse
import base64
import csv
import os
import re
import sys
import time

import requests

GITHUB_API = "https://api.github.com"

# --- Detection signatures -----------------------------------------------
# File-name globs (via GitHub code search) and content regexes used to
# decide whether a repo exposes APIs, consumes/produces events, or both.

API_SIGNATURES = {
    "java_spring": [
        r"@RestController", r"@RequestMapping", r"@GetMapping",
        r"@PostMapping", r"@PutMapping", r"@DeleteMapping",
    ],
    "node_express": [
        r"app\.get\(", r"app\.post\(", r"app\.put\(", r"app\.delete\(",
        r"router\.get\(", r"router\.post\(",
    ],
    "node_nest": [
        r"@Controller\(", r"@Get\(", r"@Post\(",
    ],
    "openapi": [
        r"openapi:\s*['\"]?3", r"swagger:\s*['\"]?2",
    ],
}

EVENT_SIGNATURES = {
    "java_sqs_sns": [
        r"AmazonSQS", r"SqsClient", r"AmazonSNS", r"SnsClient",
        r"@SqsListener", r"software\.amazon\.awssdk\.services\.sqs",
    ],
    "java_kafka": [
        r"@KafkaListener", r"KafkaTemplate", r"org\.apache\.kafka",
    ],
    "node_aws_sdk": [
        r"new SQS\(", r"SQSClient", r"new SNS\(", r"SNSClient",
        r"@aws-sdk/client-sqs", r"@aws-sdk/client-sns",
    ],
    "node_kafka": [
        r"kafkajs", r"kafka-node",
    ],
}

LANGUAGE_HINT_FILES = {
    "java": ["pom.xml", "build.gradle", "build.gradle.kts"],
    "node": ["package.json"],
}


def gh_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def list_org_repos(org, token):
    repos = []
    page = 1
    while True:
        resp = requests.get(
            f"{GITHUB_API}/orgs/{org}/repos",
            headers=gh_headers(token),
            params={"per_page": 100, "page": page, "type": "sources"},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        repos.extend(batch)
        page += 1
    return repos


def get_default_branch_tree(owner, repo, branch, token):
    resp = requests.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{branch}",
        headers=gh_headers(token),
        params={"recursive": "1"},
    )
    if resp.status_code != 200:
        return []
    return resp.json().get("tree", [])


def detect_language(tree_paths):
    langs = set()
    for hint, files in LANGUAGE_HINT_FILES.items():
        if any(any(p.endswith(f) for f in files) for p in tree_paths):
            langs.add(hint)
    return langs


def candidate_source_files(tree_paths, langs, limit=40):
    """Pick a bounded sample of likely-relevant files to content-scan,
    so we don't fetch every file in large repos."""
    exts = []
    if "java" in langs:
        exts.append(".java")
    if "node" in langs:
        exts.append(".js")
        exts.append(".ts")
    if not exts:
        exts = [".java", ".js", ".ts"]

    candidates = [
        p for p in tree_paths
        if any(p.endswith(e) for e in exts)
        and "/test/" not in p and "/tests/" not in p
        and "node_modules/" not in p
    ]
    # Prioritize files whose names suggest controllers/consumers/listeners
    priority_kw = ["controller", "route", "listener", "consumer", "producer",
                   "handler", "subscriber", "publisher", "sqs", "sns", "kafka"]
    candidates.sort(key=lambda p: (
        0 if any(k in p.lower() for k in priority_kw) else 1
    ))
    return candidates[:limit]


def fetch_file_content(owner, repo, path, token):
    resp = requests.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
        headers=gh_headers(token),
    )
    if resp.status_code != 200:
        return ""
    data = resp.json()
    if data.get("encoding") == "base64":
        try:
            return base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
        except Exception:
            return ""
    return ""


def scan_repo(owner, repo_name, default_branch, token):
    tree = get_default_branch_tree(owner, repo_name, default_branch, token)
    tree_paths = [t["path"] for t in tree if t.get("type") == "blob"]

    if not tree_paths:
        return {
            "is_api": False, "is_event": False, "languages": "",
            "sample_files_scanned": 0, "notes": "empty or inaccessible tree",
        }

    langs = detect_language(tree_paths)
    files_to_scan = candidate_source_files(tree_paths, langs)

    all_api_patterns = [p for pats in API_SIGNATURES.values() for p in pats]
    all_event_patterns = [p for pats in EVENT_SIGNATURES.values() for p in pats]

    is_api, is_event = False, False
    for path in files_to_scan:
        content = fetch_file_content(owner, repo_name, path, token)
        if not is_api and any(re.search(p, content) for p in all_api_patterns):
            is_api = True
        if not is_event and any(re.search(p, content) for p in all_event_patterns):
            is_event = True
        if is_api and is_event:
            break
        time.sleep(0.05)  # be gentle on rate limits

    return {
        "is_api": is_api,
        "is_event": is_event,
        "languages": "/".join(sorted(langs)) if langs else "unknown",
        "sample_files_scanned": len(files_to_scan),
        "notes": "",
    }


def main():
    parser = argparse.ArgumentParser(description="Phase 0 microservice inventory scanner")
    parser.add_argument("--org", required=True, help="GitHub org name")
    parser.add_argument("--out", default="inventory.csv", help="Output CSV path")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of repos scanned (0 = no limit)")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: set GITHUB_TOKEN env var with a PAT that has repo read access.", file=sys.stderr)
        sys.exit(1)

    print(f"Listing repos for org '{args.org}'...")
    repos = list_org_repos(args.org, token)
    if args.limit:
        repos = repos[: args.limit]
    print(f"Found {len(repos)} repos. Scanning...")

    rows = []
    for i, repo in enumerate(repos, 1):
        name = repo["name"]
        owner = repo["owner"]["login"]
        default_branch = repo.get("default_branch", "main")
        print(f"[{i}/{len(repos)}] {name} (branch: {default_branch})")
        try:
            result = scan_repo(owner, name, default_branch, token)
        except requests.HTTPError as e:
            result = {"is_api": False, "is_event": False, "languages": "",
                      "sample_files_scanned": 0, "notes": f"error: {e}"}

        flow_type = []
        if result["is_api"]:
            flow_type.append("API")
        if result["is_event"]:
            flow_type.append("Event")
        if not flow_type:
            flow_type.append("Unclassified")

        rows.append({
            "repo_name": name,
            "repo_url": repo["html_url"],
            "default_branch": default_branch,
            "languages": result["languages"],
            "flow_type": "+".join(flow_type),
            "sample_files_scanned": result["sample_files_scanned"],
            "notes": result["notes"],
        })

    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [
            "repo_name", "repo_url", "default_branch", "languages",
            "flow_type", "sample_files_scanned", "notes",
        ])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nDone. Wrote {len(rows)} rows to {args.out}")
    unclassified = [r for r in rows if r["flow_type"] == "Unclassified"]
    if unclassified:
        print(f"NOTE: {len(unclassified)} repos were unclassified — review these manually "
              f"(may need more sample files scanned, or aren't microservices at all).")


if __name__ == "__main__":
    main()
