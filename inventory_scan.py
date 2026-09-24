#!/usr/bin/env python3
"""
Phase 0 inventory scanner.

Walks every repo (local git clones, or a GitHub org via the API) and classifies
each as API-service, event-driven-service, or both — based on framework/SDK signatures found
in source files. Produces a CSV registry to seed the microservice inventory.

Two sources (pick exactly one):
    --repos-root PATH   scan git clones already on local disk (no network, no token)
    --org NAME          scan a GitHub org via the API (needs GITHUB_TOKEN)

Usage:
    python inventory_scan.py --repos-root ~/workspace/repos --out inventory.csv

    export GITHUB_TOKEN=ghp_xxx
    python inventory_scan.py --org YOUR_ORG_NAME --out inventory.csv

Requires: pip install requests   (only needed for --org mode)
"""

import argparse
import base64
import csv
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

try:
    import requests
except ImportError:  # local mode works without it
    requests = None

GITHUB_API = "https://api.github.com"

# --- Detection signatures -----------------------------------------------
# Content regexes matched against sampled source files to
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
    "node_aws_sdk_v2": [
        r"new AWS\.(SQS|SNS)\(", r"require\(['\"]aws-sdk['\"]\)",
    ],
    "lambda_handler": [
        r"exports\.handler\s*=", r"export\s+(const|async function|function)\s+handler",
        r"def lambda_handler\(", r"implements RequestHandler<",
    ],
    "iac_event_resources": [
        r"AWS::SQS::Queue", r"AWS::SNS::Topic", r"AWS::Serverless::Function",
        r"AWS::Lambda::EventSourceMapping",
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
    # Infrastructure templates (SAM/CloudFormation) declare queues, topics and Lambdas
    candidates += [
        p for p in tree_paths
        if p.endswith((".yaml", ".yml")) and "template" in p.lower()
    ]
    # Prioritize files whose names suggest controllers/consumers/listeners
    priority_kw = ["controller", "route", "listener", "consumer", "producer",
                   "handler", "subscriber", "publisher", "sqs", "sns", "kafka",
                   "processor", "template"]
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


def classify_paths(tree_paths, read_file, throttle=0.0):
    """Shared classification logic. `read_file(path)` returns file text, so the
    same detection runs against the GitHub API or a local checkout."""
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
        content = read_file(path)
        if not is_api and any(re.search(p, content) for p in all_api_patterns):
            is_api = True
        if not is_event and any(re.search(p, content) for p in all_event_patterns):
            is_event = True
        if is_api and is_event:
            break
        if throttle:
            time.sleep(throttle)  # be gentle on rate limits

    return {
        "is_api": is_api,
        "is_event": is_event,
        "languages": "/".join(sorted(langs)) if langs else "unknown",
        "sample_files_scanned": len(files_to_scan),
        "notes": "",
    }


def scan_repo(owner, repo_name, default_branch, token):
    tree = get_default_branch_tree(owner, repo_name, default_branch, token)
    tree_paths = [t["path"] for t in tree if t.get("type") == "blob"]
    return classify_paths(
        tree_paths,
        lambda p: fetch_file_content(owner, repo_name, p, token),
        throttle=0.05,
    )


# --- Local (system directory) mode ---------------------------------------

def run_git(repo_path, *args):
    # fsmonitor disabled so a cloned repo's config cannot make git run a helper command
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(repo_path), *args],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def strip_url_credentials(url):
    """Remove user:token@ from http(s) remote URLs so secrets never reach the CSV."""
    parts = urlsplit(url)
    if parts.scheme in ("http", "https") and "@" in parts.netloc:
        return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))
    return url


def csv_safe(value):
    """Neutralise spreadsheet formula injection (cells starting with = + - @)."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def list_local_repos(root):
    """Immediate subdirectories of `root` that are real git clones."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"ERROR: --repos-root '{root}' is not a directory.")
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / ".git").exists())


def scan_local_repo(repo_path):
    tree_paths = run_git(repo_path, "ls-files").splitlines()

    def read_file(rel):
        try:
            return (repo_path / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""

    result = classify_paths(tree_paths, read_file)
    result["head_sha"] = run_git(repo_path, "rev-parse", "HEAD")
    result["default_branch"] = run_git(repo_path, "rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    result["repo_url"] = strip_url_credentials(run_git(repo_path, "remote", "get-url", "origin"))
    return result


FIELDNAMES = [
    "repo_name", "repo_url", "default_branch", "head_sha", "languages",
    "flow_type", "sample_files_scanned", "notes",
]


def collect_local(root, limit):
    repos = list_local_repos(root)
    if limit:
        repos = repos[:limit]
    print(f"Found {len(repos)} git clones under {Path(root).expanduser()}. Scanning...")
    for i, path in enumerate(repos, 1):
        print(f"[{i}/{len(repos)}] {path.name}")
        result = scan_local_repo(path)
        yield path.name, result["repo_url"], result["default_branch"], result


def collect_github(org, limit):
    if requests is None:
        raise SystemExit("ERROR: --org mode needs 'pip install requests'.")
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("ERROR: set GITHUB_TOKEN env var with a PAT that has repo read access.")
    print(f"Listing repos for org '{org}'...")
    repos = list_org_repos(org, token)
    if limit:
        repos = repos[:limit]
    print(f"Found {len(repos)} repos. Scanning...")
    for i, repo in enumerate(repos, 1):
        name = repo["name"]
        default_branch = repo.get("default_branch", "main")
        print(f"[{i}/{len(repos)}] {name} (branch: {default_branch})")
        try:
            result = scan_repo(repo["owner"]["login"], name, default_branch, token)
        except requests.HTTPError as e:
            result = {"is_api": False, "is_event": False, "languages": "",
                      "sample_files_scanned": 0, "notes": f"error: {e}"}
        yield name, repo["html_url"], default_branch, result


def main():
    parser = argparse.ArgumentParser(description="Phase 0 microservice inventory scanner")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--repos-root", help="Directory containing local git clones (one per service)")
    source.add_argument("--org", help="GitHub org name (uses the API; needs GITHUB_TOKEN)")
    parser.add_argument("--out", default="inventory.csv", help="Output CSV path")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of repos scanned (0 = no limit)")
    args = parser.parse_args()

    if args.repos_root:
        source_iter = collect_local(args.repos_root, args.limit)
    else:
        source_iter = collect_github(args.org, args.limit)

    rows = []
    for name, url, branch, result in source_iter:
        flow_type = []
        if result["is_api"]:
            flow_type.append("API")
        if result["is_event"]:
            flow_type.append("Event")
        if not flow_type:
            flow_type.append("Unclassified")

        rows.append({
            "repo_name": name,
            "repo_url": url,
            "default_branch": branch,  # local mode: the branch currently checked out
            "head_sha": result.get("head_sha", ""),
            "languages": result["languages"],
            "flow_type": "+".join(flow_type),
            "sample_files_scanned": result["sample_files_scanned"],
            "notes": result["notes"],
        })

    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows({k: csv_safe(v) for k, v in row.items()} for row in rows)

    print(f"\nDone. Wrote {len(rows)} rows to {args.out}")
    unclassified = [r for r in rows if r["flow_type"] == "Unclassified"]
    if unclassified:
        print(f"NOTE: {len(unclassified)} repos were unclassified - review these manually "
              f"(may need more sample files scanned, or aren't microservices at all).")


if __name__ == "__main__":
    main()
