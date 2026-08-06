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

The Pythonic version must handle errors and cancellation more carefully than the current Bash prototype.

### Error handling

The tool should fail clearly when required inputs are missing, such as:

- GitLab token
- GitLab trigger token
- GitLab project ID or project path
- GitHub commit SHA
- GitLab remote URL

The tool should also handle GitLab API problems, such as:

- authentication failure
- project not found
- pipeline trigger failure
- pipeline status request failure
- job list request failure
- job log request failure
- timeout while waiting for pipeline result

Errors should produce clear messages, but secrets must never be printed.

### Cancellation handling

The current Bash prototype already handles GitHub workflow cancellation by catching `SIGINT` and `SIGTERM`.

The Pythonic version should keep this behavior.

If the GitHub Actions workflow is manually cancelled while the Python command is running, the Python process should:

1. catch the cancellation signal
2. check whether a GitLab pipeline has already been started
3. send a GitLab pipeline cancellation request if possible
4. avoid sending duplicate cancellation requests
5. exit with a non-success status

Cancellation edge cases to consider:

- cancellation before the GitLab pipeline is created
- cancellation while triggering the GitLab pipeline
- cancellation while waiting for the GitLab pipeline result
- cancellation after the GitLab pipeline already finished
- cancellation while fetching jobs or logs
- receiving both `SIGINT` and `SIGTERM`

The tool should keep enough runtime state to know whether there is an active GitLab pipeline that needs to be cancelled.

### Final status behavior

The final exit behavior should be clear:

- GitLab pipeline `success` → Python command exits with code `0`
- GitLab pipeline `failed` → Python command exits with non-zero code
- GitLab pipeline `canceled` → Python command exits with non-zero code
- timeout → Python command exits with non-zero code
- API error → Python command exits with non-zero code
- GitHub workflow cancellation → Python command tries to cancel GitLab pipeline and exits with non-zero code

## 8. Testing Plan

The Pythonic version should be tested step by step after the design is approved.

Initial tests should focus on small units first:

- configuration validation
- GitLab API request construction
- pipeline status mapping
- variable forwarding
- log size handling
- missing log handling
- summary generation
- cancellation state handling

After unit-level tests, the full workflow should be tested with the existing GitHub-to-GitLab test repository.

Integration tests should cover:

- successful GitLab pipeline
- failed GitLab pipeline
- large GitLab job log
- missing GitLab log
- manual GitHub workflow cancellation
- timeout while waiting for GitLab pipeline

No PyPI publishing or final release should happen until the Pythonic version passes the same scenarios that the Bash/YAML prototype already passed.

## 9. Unresolved Decisions

The following decisions still need confirmation before implementation or release:

- Should the final package be published as `clusterhop` or under the existing `exacb` PyPI project?

- What should the final CLI command be?
  For example: `clusterhop github2lab`

- Should the GitHub-to-GitLab bridge logic live in `src/clusterhop/github2lab/`?

- Should configuration come from environment variables, CLI arguments, or both?

- Should the GitLab project be configured by project ID, project path, or project URL?

- What format should be used for custom GitLab pipeline variables?

- Where is the boundary between this GitHub-to-GitLab bridge task and future broader ClusterHop/HPC functionality?

## 10. Implementation Plan

The Pythonic version should be implemented in small steps, not all at once. Each step should be reviewed before moving to the next one.

1. Review and preserve the existing `clusterhop` CLI behavior.
   First understand the current command entry point: `src/clusterhop/cli/__init__.py`.
   We should not break the existing `clusterhop` command while adding the new GitHub-to-GitLab logic.

2. Create the proposed `src/clusterhop/github2lab/` package structure.
   This will be the new place for the GitHub-to-GitLab bridge logic.
   The CLI should stay small and call this internal package.

3. Add configuration loading and validation.
   The Python tool needs to read required inputs such as GitLab tokens, project information, GitHub metadata, timeout, and log limit.
   Secrets must never be printed or hardcoded.

4. Add GitLab API helper logic.
   Replace the current Bash `curl` logic with Python functions.
   These functions should trigger pipelines, check status, cancel pipelines, fetch jobs, and fetch logs.

5. Add Git branch and push logic.
   Move the current Git push behavior into Python.
   This includes deriving the `ci/<commit-sha>` remote branch name and pushing the current commit to GitLab with `ci.skip`.

6. Add the main runner flow.
   This is the coordinator that runs the full process from start to end.
   It should push the commit, trigger GitLab, wait for the result, collect jobs and logs, write the summary, and return the final status.

7. Add log handling and GitHub Summary generation.
   Move the completed Issue #4 behavior into Python.
   Small logs should be shown in the summary, while large logs should be replaced with a clear message and GitLab job link.

8. Add cancellation handling.
   Move the completed Issue #6 behavior into Python.
   If the GitHub workflow is cancelled, Python should catch `SIGINT` and `SIGTERM` and cancel the related GitLab pipeline.

9. Add tests for each small part.
   Tests should cover configuration, GitLab API logic, status mapping, variables, logs, summary generation, and cancellation state.
   Tests should be added gradually alongside the implementation, not all at once at the end.

10. Test the full Pythonic workflow using the existing GitHub-to-GitLab test repository.
    The Python version should pass the same scenarios as the Bash/YAML prototype: success, failure, large logs, missing logs, cancellation, and timeout.
    Only after this should it be considered complete.

11. Review unresolved decisions before release.
    Before publishing, confirm the package name, final CLI command, configuration style, GitLab project input style, custom variable format, and release process.
    No PyPI publishing should happen before these decisions are confirmed.

### Step 1 Notes

The current CLI supports `clusterhop run SOURCE_FILE DESTINATION_NAME`. It copies `SOURCE_FILE` to `/home/work/xlab/2601-025-runner/DESTINATION_NAME.sh`. Adding the new `github2lab` functionality must not break this existing command or its file-copy behavior.

The current CLI also prints `sys.argv`. Future secrets must not be passed through or printed by this unredacted argument output.

### Step 3 Configuration Contract

The future `config.py` should expose a configuration model that is independent of GitHub Actions. Environment variables will be the initial input source, while CLI arguments or other sources can populate the same model later without changing its validation rules.

#### Required values

| Configuration field | Proposed environment variable | Secret | Purpose |
|---|---|---|---|
| GitLab token | `GITLAB_TOKEN` | Yes | Authenticates the Git push and GitLab API operations. |
| GitLab trigger token | `GITLAB_TRIGGER_TOKEN` | Yes | Triggers the GitLab pipeline. |
| GitLab URL | `GITLAB_URL` | No | Base URL of the GitLab instance. |
| GitLab project ID or project path | `GITLAB_PROJECT_ID` or `GITLAB_PROJECT_PATH` | No | Identifies the GitLab project. At least one must be provided. |
| GitLab remote URL | `GITLAB_REMOTE_URL` | No | Token-free Git remote URL used to push the commit. |
| GitHub commit SHA | `GITHUB_SHA` | No | Identifies the GitHub commit to transfer. |
| GitHub repository | `GITHUB_REPOSITORY` | No | Identifies the source GitHub repository. |
| GitHub run ID | `GITHUB_RUN_ID` | No | Identifies the source GitHub Actions run. |

The behavior when both `GITLAB_PROJECT_ID` and `GITLAB_PROJECT_PATH` are provided must be defined before implementation. Configuration should not silently select one without a documented precedence rule.

#### Optional values and defaults

The following initial defaults come from the working Bash/YAML prototype. They preserve the prototype's current behavior, but they are not permanent hardcoded limits for the reusable Python implementation.

| Configuration field | Proposed environment variable | Default |
|---|---|---|
| Poll interval | `CLUSTERHOP_POLL_INTERVAL_SECONDS` | `5` seconds |
| Pipeline timeout | `CLUSTERHOP_PIPELINE_TIMEOUT_SECONDS` | `150` seconds |
| Log summary limit | `CLUSTERHOP_LOG_SUMMARY_LIMIT_BYTES` | `800000` bytes |
| Extra pipeline variables | `CLUSTERHOP_GITLAB_VARIABLES` | Empty mapping (`{}`) |

The Pythonic version should allow these defaults to be overridden later through environment variables, CLI arguments, or configuration. This flexibility is required because the Pythonic version is intended to be the reusable implementation, not a direct copy of the Bash/YAML prototype.

The serialization format for `CLUSTERHOP_GITLAB_VARIABLES` remains an unresolved decision. The configuration model should ultimately represent extra variables as key-value pairs regardless of whether they come from an environment variable, CLI arguments, or another source.

#### Secret safety rules

- Tokens must never be printed.
- Tokens must never appear in logs, GitHub summaries, representation output such as `repr()`, or validation errors.
- A token-containing GitLab remote URL must not be permanently stored in configuration, written to disk, or exposed in process output.
- Authentication material needed for a Git operation should be applied only for that operation and handled separately from the token-free `GITLAB_REMOTE_URL` value.

#### Validation rules

- Validate the complete configuration before any Git push or GitLab API call.
- Fail clearly when a required value is missing or empty.
- Require at least one of `GITLAB_PROJECT_ID` or `GITLAB_PROJECT_PATH`.
- Require poll interval, pipeline timeout, and log summary limit values to be positive numbers.
- Error messages should identify the missing or invalid field without including its value when the field is secret.
- Configuration errors should stop execution before any external operation begins.

#### Input-source independence

Environment-variable loading should be separate from the configuration model and its validation. GitHub Actions can provide the proposed environment variables initially, while future CLI arguments or server-side configuration can construct the same model later. The core model should not import or depend on GitHub Actions-specific APIs.

## 11. Future Whole-Project Placeholders

These are outside the current Issue #5 scope, but may become future ClusterHop/exaCB tasks:

- Server-based execution
- Broader GitLab CI/CD to HPC integration
- HPC job submission
- HPC job monitoring
- Benchmark execution
- Result collection from HPC systems
- Deeper exaCB integration
- Production documentation and release workflow
