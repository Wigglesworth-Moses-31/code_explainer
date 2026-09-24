# Code Explainer

Documents the code flow of every API and event flow across ~50 middleware microservices (Java/Spring Boot and Node.js; SQS/SNS event-driven), so a prod support engineer can understand a flow without reading the code cold during an incident.

## Pipeline

| Stage | Purpose | Status |
|---|---|---|
| **Phase 0: Inventory** | List every service and classify it as API, Event, or both | Built (`inventory_scan.py`) |
| **Context Visualizer** | Trace one flow end to end: entry point, downstream calls, events, payload transformations | Manual POC (done for one API flow) |
| **Document Builder** | Render the trace into a consistent deep-dive doc plus a "surface manifest" of the files that define the flow | Manual POC (template in `poc/docs/`) |
| **Diff Analyzer** | Detect whether a documented flow is stale: `git diff <baseline SHA>..HEAD` scoped to the flow's surface files | Designed, not built |

New service: skip Diff Analyzer, run Context Visualizer then Document Builder.
Already-documented service: run Diff Analyzer first (e.g. each sprint), and only re-document flows whose surface files changed.

## Runtime model: local directories, no cloning

The tool **never clones or fetches at runtime**. You keep real git clones of the services in one local directory and point the tool at it.

- Clones must be real git repos (they need a `.git` folder and history). ZIP downloads won't work.
- Syncing (`git fetch` / `git pull`) is a separate step you run yourself. Analysis never triggers it.
- Every generated doc is stamped with the commit SHA it was built from, so staleness is visible.
- No GitHub token is needed in local mode.

```
~/workspace/repos/
   service-a/   (git clone)
   service-b/   (git clone)
   ...
```

## Setup (office machine)

Requirements: `git` and Python 3. `pip install requests` is only needed for `--org` mode.

1. Get this repo onto the machine (clone it, or copy the folder).
2. Put your service clones under one directory, e.g. `~/workspace/repos`.
3. Check the clones are on the branch you want documented (`git branch --show-current`).

## Phase 0: inventory scan

```bash
# Local mode (recommended): reads clones from disk, no network
python3 inventory_scan.py --repos-root ~/workspace/repos --out inventory.csv

# Try a few first
python3 inventory_scan.py --repos-root ~/workspace/repos --limit 5 --out test_inventory.csv

# GitHub API mode (needs a PAT with repo read access, SSO-authorized for the org)
export GITHUB_TOKEN=...
python3 inventory_scan.py --org YOUR_ORG --out inventory.csv
```

Exactly one of `--repos-root` or `--org` is required.

Output columns: `repo_name, repo_url, default_branch, head_sha, languages, flow_type, sample_files_scanned, notes`.

- `flow_type` is `API`, `Event`, `API+Event`, or `Unclassified`.
- `head_sha` is the commit scanned. It becomes the baseline for the Diff Analyzer.
- In local mode, `default_branch` is the branch currently checked out, not the remote default.

### How classification works
Each repo's tracked files are sampled (up to 40 files, prioritising names like controller, listener, consumer, processor, handler, template) and matched against signatures:

- **API:** Spring (`@RestController`, `@GetMapping`...), Express/Nest routes, OpenAPI files.
- **Event:** AWS SQS/SNS SDKs (v2 and v3), `@SqsListener`, Kafka, Lambda handlers, and SAM/CloudFormation resources (`AWS::SQS::Queue`, `AWS::SNS::Topic`, `AWS::Serverless::Function`) in `template*.yaml`.

### Known limits
- Sampling is bounded, so unusual naming can land in `Unclassified`. Review those manually.
- A service that is both API and event needs both signatures within the sampled files.
- Signatures are patterns, not proof. Treat the CSV as a first-pass inventory to review.

## POC

`poc/` holds a worked example. See [poc/README.md](poc/README.md).

- The POC inputs are third-party open-source repos (a Spring Boot API service and an AWS SNS/SQS Lambda sample). They are **not committed**; place copies under `poc/api-service` and `poc/event-lambda-service` to make the doc links resolve.
- Sample output: [poc/docs/organization-service_with-departments-and-employees_flow.md](poc/docs/organization-service_with-departments-and-employees_flow.md). It found a hidden 3-service fan-out, no timeouts or circuit breakers, and a config repo outside the traced code.

Scan the POC clones locally:

```bash
python3 inventory_scan.py --repos-root poc --out poc_inventory.csv
```

## Flow doc template

Each flow doc contains: overview, entry point, hop-by-hop call flow, payload contracts, error handling and resilience, risk points, and a surface manifest. It must state explicitly when something could not be traced from the repos available (for example config that lives in a separate config repo).

## Working with real services

Generated docs will contain internal hostnames, queue names, and endpoints. Keep them in a **private** repo.

## Roadmap

1. Run `--repos-root` inventory on the real 50 services and review `Unclassified` rows.
2. Pilot Context Visualizer and Document Builder on one API service and one event service.
3. Document all flows and record surface manifests.
4. Build the Diff Analyzer to flag stale docs each sprint.
