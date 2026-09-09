# GitHub-to-GitLab Model and API

## 1. Purpose

The GitHub-to-GitLab bridge connects a source repository and its GitHub Actions workflow to CI workloads hosted in GitLab. It lets a GitHub commit use an existing GitLab pipeline without duplicating that pipeline in GitHub.

The bridge transfers the commit, explicitly triggers the GitLab pipeline, follows its result, retrieves relevant jobs and logs, and reports the outcome in GitHub Actions. This bridge is one use case of the more general ClusterHop `run` API; it does not define the complete future ClusterHop or HPC feature set.

## 2. Repository Roles

### `clusterhop`

`clusterhop` is the reusable Python package. It contains configuration validation, Git operations, GitLab API access, orchestration, log processing, summary generation, and cancellation lifecycle handling. Production behavior and reusable APIs belong here.

### `github2lab-test`

`github2lab-test` is the integration-test and demonstration repository. Its GitHub Actions workflow installs `clusterhop` from a pinned Git commit and exercises the bridge against a test GitLab project.

It validates the complete GitHub-to-GitLab path, but it is not the reusable implementation and should not contain a second copy of the bridge logic.

## 3. High-Level Model

```text
GitHub push
    |
    v
GitHub Actions workflow
    |
    v
clusterhop runner (current GitHub-to-GitLab implementation)
    |
    +--> push commit to GitLab branch ci/<github-commit-sha> with ci.skip
    |
    +--> trigger the GitLab pipeline explicitly
    |
    +--> poll pipeline status and fetch jobs/logs
    |
    v
GitHub Actions summary and process exit status
```

The push uses `ci.skip` so it does not create a duplicate GitLab pipeline. The runner then starts the intended pipeline through the GitLab Trigger API and passes the relevant GitHub metadata.

## 4. Main Components and Responsibilities

### `runner.py`

Coordinates the complete lifecycle: push, trigger, polling, job and log collection, summary writing, cleanup, and the structured final result.

### `config.py`

Loads and validates runtime configuration before remote work begins. It defines reusable defaults and rejects unsafe token-bearing remote URLs.

### `git.py`

Creates the `ci/<sha>` branch name and safely pushes the current GitHub commit to GitLab with the `ci.skip` push option.

### `gitlab.py`

Provides the GitLab API client for triggering pipelines, reading status, retrieving paginated jobs and traces, and requesting cancellation.

### `logs.py`

Decodes traces safely, removes terminal control sequences, calculates sizes, and decides whether a cleaned log can be embedded in the summary.

### `summary.py`

Renders the user-facing GitHub Actions Markdown summary with pipeline status, links, jobs, and available log information or safe fallback messages.

### `cancellation.py`

Handles `SIGINT` and `SIGTERM`, records cancellation, invokes registered cleanup once, and restores previous signal handlers.

### `state.py`

Persists non-secret active-pipeline metadata in versioned JSON. Atomic writes support reliable, idempotent cleanup across process and workflow boundaries.

### `fallback.py`

Provides workflow-level fallback cancellation:

```bash
python -m clusterhop.github2lab.fallback
```

It validates the state file against current configuration and cancels an active pipeline if the main runner could not finish cleanup.

## 5. User-Facing API

After review, the general ClusterHop API direction is to expose a reusable public interface. The current working integration remains available while that public interface is implemented.

### 5.1 Current Working Python Integration

**Status:** Current and validated, but not the planned primary public API.

```python
from clusterhop.github2lab.runner import run_from_environment

result = run_from_environment(repository_path=".")
raise SystemExit(result.exit_code)
```

`github2lab-test` currently uses this function. It remains useful during migration, but its long-term public support status has not been decided.

### 5.2 Proposed Public Python API

**Status:** Agreed direction; not yet implemented.

```python
clusterhop.run(
    config=config,
    repo_path=repo_path,
    options=options,
    secrets=secrets,
)
```

This general API represents a run against a configured destination. It is not limited to the current GitHub-to-GitLab bridge and can describe destination types such as GitLab, GitHub, or Forgejo.

### 5.3 Proposed Public CLI

**Status:** Agreed direction; not yet implemented.

```bash
clusterhop run --config config.yaml --options options.yaml <repo-path> --secrets secrets.yaml
```

The CLI should call the same general run API. It replaces the earlier `clusterhop github2lab` proposal. Compatibility with the existing legacy `clusterhop run SOURCE_FILE DESTINATION_NAME` behavior must be handled deliberately during implementation.

### 5.4 Current GitHub Actions Integration

**Status:** Current and validated in `github2lab-test`.

The workflow installs `clusterhop` from a pinned commit, supplies environment configuration and GitHub secrets, calls `run_from_environment(repository_path=".")`, and returns `result.exit_code`. A cancellation fallback step can use persisted pipeline state.

### 5.5 Future GitHub Action Wrapper

**Status:** Future; not implemented.

```yaml
- name: Run GitHub-to-GitLab bridge
  uses: exacb-org/clusterhop@v1
  env:
    GITLAB_TOKEN: ${{ secrets.GITLAB_TOKEN }}
    GITLAB_TRIGGER_TOKEN: ${{ secrets.GITLAB_TRIGGER_TOKEN }}
  with:
    config: <config-file>
    options: <options-file>
```

The wrapper should be a later, preferably separate layer that invokes the stable CLI internally. Its repository, name, release mechanism, and secret interface still require confirmation.

`python -m clusterhop.github2lab` is not planned as a public API. The existing fallback module remains an internal workflow utility where needed.

## 6. Configuration API

The current implementation loads environment variables. The proposed public API moves toward separate configuration, options, and secrets inputs without exposing sensitive values.

### 6.1 Proposed Input Separation

- `config` describes the destination and project, including destination type such as GitLab, GitHub, or Forgejo.
- `options` describes runtime behavior, including wait time, timeout/failure time, retry counts, target branch options, polling interval, log limit, and cleanup timeout.
- `secrets` contains secret values or references needed to authenticate with the destination.

The exact YAML schemas remain open design work. `seanvault` is the planned or possible mechanism for encrypted secret fields in readable configuration or credentials files, including CI use. Password/key delivery for decrypting those fields must be designed separately.

Secrets must never appear in summaries, pipeline state, logs, exceptions, or other persisted non-secret output.

### 6.2 Current Environment Contract

The validated GitHub-to-GitLab implementation currently uses the following environment variables during migration.

#### Required variables

| Variable | Purpose |
| --- | --- |
| `GITLAB_TOKEN` | Authenticates GitLab API reads/cancellation and the Git push. |
| `GITLAB_TRIGGER_TOKEN` | Authenticates the pipeline trigger request. |
| `GITLAB_URL` | Base URL of the target GitLab instance. |
| `GITLAB_REMOTE_URL` | Credential-free Git remote URL for the target project. |
| `GITLAB_PROJECT_ID` or `GITLAB_PROJECT_PATH` | Identifies the target project; exactly one is required. |
| `GITHUB_SHA` | Commit to push and use as pipeline metadata. |
| `GITHUB_REPOSITORY` | Source GitHub repository identifier. |
| `GITHUB_RUN_ID` | Source GitHub Actions run identifier. |

GitHub Actions normally supplies the three `GITHUB_*` values automatically.

#### Optional variables

| Variable | Default | Purpose |
| --- | ---: | --- |
| `CLUSTERHOP_POLL_INTERVAL_SECONDS` | `5` | Delay between pipeline status checks. |
| `CLUSTERHOP_PIPELINE_TIMEOUT_SECONDS` | `150` | Maximum wait for a terminal pipeline result. |
| `CLUSTERHOP_LOG_SUMMARY_LIMIT_BYTES` | `800000` | Largest cleaned log embedded in the summary. |
| `CLUSTERHOP_PIPELINE_STATE_FILE` | unset | Path for non-secret state used by fallback cleanup. |
| `CLUSTERHOP_CLEANUP_TIMEOUT_SECONDS` | `10` | Maximum duration for a cleanup request. |

When GitHub Actions supplies `GITHUB_STEP_SUMMARY`, it selects the summary output file. These defaults reflect the validated prototype and remain configurable. They should later map into the general `config`, `options`, and `secrets` model.

## 7. Runtime Behavior

The GitHub Actions summary should show the overall pipeline status and every pipeline job in a clear table, including successful and failed jobs. When multiple jobs exist, the public behavior should not select or prioritize one job as the "main" job. Detailed log presentation for multiple jobs still requires implementation design.

### Success

The runner collects available jobs and logs, writes a success summary with the full jobs table, marks active state inactive, and exits successfully.

### Failure

The runner still gathers useful job/log data, writes a failed summary showing all jobs, marks state inactive, and returns a non-zero exit code.

### Large log

If a cleaned trace exceeds the limit, the full trace is omitted. The summary provides a clear explanation, sizes, and available GitLab links.

### Timeout

The runner requests cancellation using the bounded cleanup timeout. Its final result remains `timeout`, even if cancellation succeeds. Successful cleanup marks state inactive; failed cleanup leaves it active for fallback retry.

### Manual cancellation

The runner handles `SIGINT` and `SIGTERM`. If a pipeline ID is known, its idempotent cleanup callback requests cancellation. The runner result remains `canceled`.

### Fallback cancellation

A workflow cancellation step can run `python -m clusterhop.github2lab.fallback`. Missing/inactive state is a successful no-op. Active matching state causes one bounded cancellation attempt. GitLab's intermediate `canceling` status confirms that cancellation was accepted.

## 8. Internal API vs Public API

The workers in `git.py`, `gitlab.py`, `logs.py`, `summary.py`, `cancellation.py`, `state.py`, and `fallback.py` are implementation details. They are tested, but users should not depend on their names or signatures as stable APIs.

The current working integration point during migration is:

```python
run_from_environment(repository_path=".")
```

For GitHub Actions, the current interface is the pinned-package workflow invoking this function and, where needed, the fallback module.

The planned stable public surface is `clusterhop.run(config, repo_path, options, secrets)`, the `clusterhop run ...` CLI, documented config/options/secrets behavior, exit semantics, and relevant fallback behavior. Transport, storage, parsing, and orchestration helpers should remain internal.

## 9. Proposed Public CLI API

The agreed CLI direction is:

```bash
clusterhop run --config config.yaml --options options.yaml <repo-path> --secrets secrets.yaml
```

The command should load the separated inputs, call the general `clusterhop.run(...)` API, write `GITHUB_STEP_SUMMARY` when running in GitHub Actions, and return the run result as a documented process exit code.

The `--secrets` argument identifies a secrets file; it must not accept secret values directly on the command line. `seanvault` may provide encrypted secret fields. Exact file schemas, validation, precedence, and CI password handling remain to be designed.

This general command replaces the earlier `clusterhop github2lab` proposal. Implementation must address compatibility with the existing legacy `clusterhop run SOURCE_FILE DESTINATION_NAME` command.

## 10. Open Design Questions

- What is the exact `config.yaml` structure?
- What is the exact `options.yaml` structure?
- What is the exact `secrets.yaml` structure, and how should the `seanvault` password or key be supplied safely in CI?
- What is the release path: pinned commits, Git tags, PyPI, or a staged combination?
- What should the future GitHub Action wrapper repository, name, and location be?

The package name remains `clusterhop`. The questions above require maintainer review before publishing a stable interface.

## 11. Current Validated Baseline

The implementation has been tested end to end through `github2lab-test` for:

- normal success;
- pipeline failure;
- oversized log handling;
- timeout detection and active-pipeline cancellation;
- manual GitHub Actions cancellation;
- state-file fallback cancellation; and
- final normal success after reverting temporary test changes.

Unit tests also cover configuration, Git/GitLab boundaries, logs, summaries, runner lifecycle, state persistence, and cancellation/fallback without real secrets or network calls.

This validates the current GitHub-to-GitLab workflow. The public API direction changed after review and is not implemented by this baseline.

## 12. Recommended Next Steps

1. Review the updated model and API if further clarification is needed.
2. Implement `clusterhop.run(config, repo_path, options, secrets)`.
3. Implement the `clusterhop run ...` CLI while addressing legacy CLI compatibility.
4. Add validated `config`, `options`, and `secrets` file loading.
5. Integrate `seanvault` for approved encrypted-secret handling.
6. Update summary behavior to show all pipeline jobs clearly without choosing a main job.
7. Later create a reusable GitHub Action wrapper as a separate layer or repository.
