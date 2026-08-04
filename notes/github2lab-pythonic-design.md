# GitHub-to-GitLab Pythonic Version Design

## 1. Goal

Move the existing working GitHub Actions + Bash prototype into clean reusable Python logic.

The current prototype already solved:

- Issue #4: GitLab log handling and large-log protection
- Issue #7: GitLab Trigger API and GitHub metadata variables
- Issue #6: cancellation handling from GitHub to GitLab

The Pythonic version should not be a line-by-line Bash rewrite. It should redesign the logic cleanly for future use from GitHub Actions, servers, and possibly broader clusterhop/exaCB workflows.

## 2. Current Status

The current `clusterhop` repository is already a Python package.

Known current structure:

```text
src/
└── clusterhop/
    ├── __init__.py
    ├── metadata.py
    └── cli/
        └── __init__.py
```

## 3. Scope

This design covers the Pythonic version of the existing GitHub-to-GitLab bridge from Issue #5.

The current goal is to move the already working GitHub Actions + Bash prototype into clean, reusable Python code. This includes the bridge logic for pushing GitHub commits to GitLab, triggering GitLab pipelines through the GitLab API, passing variables, waiting for results, collecting logs, writing GitHub Actions summaries, and handling cancellation.

This work is one part of the larger ClusterHop/exaCB ecosystem. It does not define or implement the entire future ClusterHop system, including broader HPC job submission, HPC job monitoring, benchmark execution, or result retrieval from HPC systems.

The Pythonic version should be designed cleanly enough to support future GitHub Actions, server-based usage, and possible HPC/exaCB extensions later, but those future features are outside the scope of this first design.

The final package and publishing identity is not decided here. The current repository uses the package name `clusterhop`, while Issue #5 also mentions the existing `exacb` PyPI project. Publishing, package-renaming, or release decisions require confirmation before implementation/release.

## 4. Existing Prototype Behavior

The existing prototype is implemented using GitHub Actions YAML and Bash. It currently lives in the `exacb-org/github2lab-test` repository, primarily in `.github/workflows/github-to-gitlab.yml`, with the corresponding GitLab pipeline defined in `.gitlab-ci.yml`. This logic has not yet been implemented inside the `clusterhop` Python package.

The prototype flow is:

1. A commit is pushed to the `main` branch of the GitHub repository.

2. GitHub Actions starts the GitHub-to-GitLab workflow and checks out the repository with its full Git history (`fetch-depth: 0`).

3. The workflow derives a GitLab branch name from the GitHub commit SHA:

   ```text
   ci/<github-commit-sha>
   ```

4. The workflow configures an authenticated GitLab Git remote and pushes the current commit to the derived branch:

   ```text
   HEAD:refs/heads/ci/<github-commit-sha>
   ```

   The push uses the GitLab `ci.skip` push option. This transfers the commit to GitLab without starting an automatic pipeline from the push.

5. After the push succeeds, the workflow calls the GitLab Trigger API to start the intended pipeline explicitly. The request includes:

   - The GitLab trigger token
   - The `ci/<github-commit-sha>` ref
   - `GITHUB_COMMIT_SHA`
   - `GITHUB_REPOSITORY`
   - `GITHUB_RUN_ID`

6. The trigger step expects HTTP status `201`. It reads the pipeline ID and web URL from the JSON response and exposes them as GitHub Actions step outputs. A non-`201` response or a missing pipeline ID stops the workflow with an error.

7. The workflow polls the GitLab pipeline API every five seconds, for up to 30 attempts. The following statuses are treated as terminal:

   - `success`
   - `failed`
   - `canceled`
   - `skipped`

   If no terminal status is reached within the polling limit, the recorded result is `timeout`.

8. While polling, the Bash step installs handlers for `SIGINT` and `SIGTERM`. If GitHub cancels the workflow, the handler disables repeated signal handling, sends a GitLab pipeline cancellation request, checks the GitLab status up to three times, and exits with status `130`.

9. After the pipeline finishes, the workflow requests the pipeline's jobs from the GitLab API. It records the total job count and currently treats the first returned job as the main job. For that job, it records:

   - Job ID
   - Name
   - Status
   - Stage
   - Duration
   - GitLab web URL

   If the pipeline has no jobs, the workflow creates explicit `not_available` outputs instead of failing.

10. If a main job exists, the workflow downloads its GitLab trace. It stores the original trace in `gitlab-job.log` and removes ANSI terminal escape sequences to create `gitlab-job-clean.log`. If no job exists or the trace request fails, it creates a clear fallback log message.

11. A summary step runs with `if: always()` and writes a GitHub Actions job summary containing:

    - GitLab pipeline status
    - GitHub commit SHA
    - GitLab branch
    - GitLab pipeline ID and link
    - Job count and main-job table
    - Main-job log information

    The cleaned job log is measured in bytes. The current summary limit is:

    ```text
    800000 bytes
    ```

    Logs at or below the limit are embedded in the summary. Larger logs are omitted and replaced with a message directing the user to the GitLab job link. If cancellation prevented creation of the cleaned log file, the summary creates a fallback explanation rather than crashing.

12. A final step also runs with `if: always()` and maps the GitLab result to the GitHub workflow result. Only GitLab `success` exits successfully. Failed, canceled, skipped, timed-out, or missing results cause the GitHub workflow to fail.

The current GitLab test pipeline runs `python3 main.py` and prints the three GitHub metadata variables received from the trigger request. This verifies that the transferred commit is executable and that the trigger variables reach GitLab CI.

## 5. Inputs and Secrets

The Pythonic version will need the same information that the current GitHub Actions + Bash prototype already uses.

### GitLab authentication

The tool needs GitLab credentials to push code, trigger pipelines, read pipeline status, fetch jobs and logs, and cancel pipelines.

The current prototype uses:

- `GITLAB_TOKEN`
- `GITLAB_TRIGGER_TOKEN`

`GITLAB_TOKEN` is used by the current prototype to authenticate the Git push and for GitLab API operations such as reading pipeline status, fetching jobs and logs, and cancelling pipelines.

`GITLAB_TRIGGER_TOKEN` is used to start a GitLab pipeline through the GitLab Trigger API.

These values are secrets. They must be supplied securely through GitHub Actions secrets or another secure runtime environment. They must never be hardcoded in Python code, written to logs, included in summaries, or committed to the repository.

### GitLab project information

The tool needs to know which GitLab project should receive the pushed branch and pipeline trigger.

The current prototype uses:

- GitLab project ID
- GitLab project path or URL
- Authenticated GitLab remote URL

The current test project is:

```text
Project ID: 8894
Project path: exacb/fahmin-experiments/githubci/github2lab-test
```

These project values are configuration, not secrets. The Pythonic version should make them configurable rather than hardcoding the current test project.

## 6. Proposed Python Architecture

The Pythonic version should keep the existing `clusterhop` CLI entry point, but move the GitHub-to-GitLab bridge logic into a separate internal subpackage.

Possible structure:

```text
src/
└── clusterhop/
    ├── __init__.py
    ├── metadata.py
    ├── cli/
    │   └── __init__.py          # Existing command-line entry point
    └── github2lab/
        ├── __init__.py
        ├── config.py            # Inputs, secrets, URLs, timeouts, limits
        ├── git.py               # Branch creation and GitLab push
        ├── gitlab.py            # GitLab API operations
        ├── models.py            # Pipeline and job data structures
        ├── logs.py              # Job log cleaning and size handling
        ├── summary.py           # GitHub Actions summary generation
        ├── cancellation.py      # SIGINT/SIGTERM handling
        └── runner.py            # Coordinates the complete bridge flow
```

The `cli` module should stay small. It should only parse command-line arguments and call the correct internal logic.

The main GitHub-to-GitLab bridge logic should live inside `clusterhop/github2lab/`, not directly inside the CLI file.

This keeps the package easier to test, maintain, and extend later.

The existing `clusterhop` command should remain the main CLI command.

A possible future command could be:

```text
clusterhop github2lab
```

This command would run the complete GitHub-to-GitLab bridge flow. The command name is provisional and should be confirmed before implementation.

## 7. Error Handling and Cancellation

## 8. Testing Plan

## 9. Unresolved Decisions
