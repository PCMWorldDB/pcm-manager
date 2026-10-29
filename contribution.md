# Contributing to WorldDB

## Branches and environments

Use this workflow for application changes:

`feature branch` → pull request into `release/dev` → test on the dev site → pull request into `main` → deploy to prod

| Environment | Git branch | Container image |
| --- | --- | --- |
| Dev test site | `release/dev` | `ghcr.io/pcmworlddb/pcm-manager-webapp:dev` |
| Prod | `main` | `ghcr.io/pcmworlddb/pcm-manager-webapp:prod` |

These mappings are defined in `.github/workflows/build-webapp-images.yml`. A push to either branch builds and publishes its image. That workflow does not deploy the image to a running site; deployment is a separate step.

## Making a change

1. Start from the latest `release/dev` and create a short-lived branch:

   ```bash
   git fetch origin
   git switch -c feature/describe-your-change origin/release/dev
   ```

2. Make a focused change and validate it locally. Follow the setup instructions in [README.md](README.md).
3. Commit and push your feature branch, then open a pull request with **base: `release/dev`**. Explain the change, how you verified it, and any remaining limitations.
4. Resolve review conversations and satisfy any required checks before merging.
5. After the dev image builds successfully, deploy it to the dev test site and verify the affected behavior there. Fix any issues through another feature branch and pull request into `release/dev`.

Do not commit `.env`, credentials, database dumps containing private data, or other secrets.

## Promoting to prod

1. Confirm that all changes being promoted have been tested on the dev site. A promotion includes every change in `release/dev` that is missing from `main`.
2. Open a pull request with **base: `main`** and **compare: `release/dev`**.
3. Describe the included changes, dev-site verification, and any manual schema or configuration steps needed for deployment.
4. Satisfy the prod ruleset requirements and merge. Prefer a merge commit for this promotion so the shared history between the long-lived branches is preserved.
5. Confirm the prod image builds successfully, deploy it, and verify the affected behavior on the production site.

If a fix must go directly into `main`, use a pull request and promptly merge `main` back into `release/dev` through another pull request so dev retains the fix.

## Recommended GitHub rulesets

Create separate active branch rulesets under **Settings → Rules → Rulesets**. Target the exact branch names below. Leave bypass lists empty to apply the rules to maintainers too.

| Protection | Prod: `main` | Dev: `release/dev` |
| --- | --- | --- |
| Restrict deletions | Enable | Enable |
| Block force pushes | Enable | Enable |
| Require a pull request before merging | Enable | Enable |
| Required approvals | 1 when another maintainer can review; otherwise 0 | 0 initially |
| Require conversation resolution before merging | Enable | Enable |
| Dismiss stale approvals on new commits | Enable when requiring approvals | Enable if approvals become required |
| Require status checks to pass | Add reliable PR checks when available | Add reliable PR checks when available |

Leave **Restrict updates** disabled so normal pull request merges are allowed. Leave linear history, signed commits, and required deployments disabled unless the project deliberately adopts those requirements. Requiring linear history would conflict with the merge-commit promotion described above.

The image publishing workflow runs after pushes. Use the checks from `.github/workflows/pr-checks.yml` as required pre-merge checks on both branches: **Python checks**, **PostgreSQL regression tests**, **Container build**, **Secret scan**, and **Dependency audit**. Run the workflow once so GitHub can offer these names in the ruleset settings. With zero required approvals, a solo maintainer can still merge their own pull requests.

## Automated checks

PR checks run on GitHub-hosted Ubuntu runners using `pull_request`, read-only repository permissions, and checkout without persisted credentials. They use disposable PostgreSQL 15 databases and synthetic fixtures, with no production secrets, external API calls, image publishing, or home lab runners. Keep contributor code out of privileged `pull_request_target` workflows. See [GitHub's security guidance](https://docs.github.com/en/actions/reference/security/secure-use).

The checks include Python compilation, selected Ruff correctness rules, PostgreSQL regression tests, a build of the production Dockerfile, a redacted Gitleaks scan of Git history, and pip-audit of resolved application requirements. Findings fail their respective jobs; there is no vulnerability suppression baseline. Existing dependency vulnerabilities must be addressed before making Dependency audit a required passing check. Dependabot opens weekly dependency and Actions updates.

To run the Python checks locally with Python 3.11:

```bash
python -m pip install -r requirements-dev.txt
python -m compileall -q webapp wsgi.py tests
ruff check webapp wsgi.py tests
python -m pip_audit -r requirements.txt --strict --progress-spinner off
```

For regression tests, start a **disposable** PostgreSQL server with a user allowed to create databases and install `pg_trgm`. Do not use an existing development or production server. The suite initializes the `main` schema in `worlddb_test`, creates temporary namespace databases, and writes release files under a temporary directory. It deliberately does not read `.env`. Set the dedicated test variable before running:

```bash
export WORLDDB_TEST_DATABASE_URL=postgresql://worlddb_test:ci-only-password@localhost:5432/worlddb_test
python -m pytest
```

In PowerShell, use `$env:WORLDDB_TEST_DATABASE_URL = 'postgresql://worlddb_test:ci-only-password@localhost:5432/worlddb_test'`. The database name must be `worlddb_test`; missing configuration fails rather than silently skipping integration tests. Destroy the disposable server when finished.

The initial suite protects startup/schema seeding, anonymous and authenticated direct-action permissions, editor approval restrictions, reads from separate namespace databases containing overlapping rider IDs, rejection of the wrong namespace connection, release artifact contents, preservation of untouched SQLite data, and rollback on invalid CDB updates. Release tests mock database selection and artifact metadata persistence while generating real files. They do not yet cover the full CDB import round trip, release status transitions, or all mutation routes. Extend these tests when changing those paths.

The current rider SQL artifact is a MySQL `dyn_cyclist` table dump, including table replacement statements; it is not yet standalone rider-stat update SQL. The tests preserve the existing format. Changing that behavior needs a separate domain change.

The active CDB mappings are in `webapp/integrations/cdb/cdb_mappings.yaml` (the older `webapp/models/ns/cdb_mappings.yaml` path no longer exists).

## Project-specific review

- Keep changes surgical and follow existing Python and Flask patterns.
- Verify role permissions and namespace boundaries when affected.
- For CDB changes, check `webapp/models/ns/cdb_mappings.yaml` and preserve compatibility.
- Document manual schema changes and which database they affect (`main` or `namespace`). Do not introduce automatic migrations without agreement.
- Report the checks performed and anything that remains unverified. For CDB release changes, verify the CDB SQLite file, rider stats update SQL, and release overview HTML as applicable.
