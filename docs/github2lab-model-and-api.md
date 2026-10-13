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

After review, ClusterHop exposes a reusable public API and CLI. The current working integration remains available during migration while users adopt these public interfaces.

### 5.1 Current Working Python Integration

**Status:** Current and validated, but not the planned primary public API.

```python
from clusterhop.github2lab.runner import run_from_environment

result = run_from_environment(repository_path=".")
raise SystemExit(result.exit_code)
```

`github2lab-test` currently uses this function. It remains useful during migration, but its long-term public support status has not been decided.

### 5.2 Current Public Python API

**Status:** Implemented and available as the current public Python API.

```python
clusterhop.run(
    config=config,
    repo_path=repo_path,
    options=options,
    secrets=secrets,
)
```

This general API represents a run against a configured destination. The current implementation supports GitLab; other destination types such as GitHub or Forgejo remain future work.

### 5.3 Current Public CLI

**Status:** Implemented and available as the current public CLI.

```bash
clusterhop run --config config.yaml --options options.yaml <repo-path> --secrets secrets.yaml
```

The CLI calls the same general run API. It replaces the earlier `clusterhop github2lab` proposal while preserving the existing legacy `clusterhop run SOURCE_FILE DESTINATION_NAME` behavior through deliberate command dispatch.

### 5.4 Current GitHub Actions Integration

**Status:** Current and validated in `github2lab-test`.

The workflow installs `clusterhop` from a pinned commit, supplies environment configuration and GitHub secrets, calls `run_from_environment(repository_path=".")`, and returns `result.exit_code`. A cancellation fallback step can use persisted pipeline state.

### 5.5 Future GitHub Action Wrapper

**Status:** Future; not implemented.

```yaml
- name: Run GitHub-to-GitLab bridge
  uses: exacb-org/clusterhop@v1
  env:
    SEANVAULT_PASSWORD: ${{ secrets.SEANVAULT_PASSWORD }}
  with:
    config: <config-file>
    options: <options-file>
    secrets: <secrets-file>
```

The wrapper should be a later, preferably separate layer that invokes the stable CLI internally. Its repository, name, release mechanism, and secret interface still require confirmation.

`python -m clusterhop.github2lab` is not planned as a public API. The existing fallback module remains an internal workflow utility where needed.

## 6. Configuration API

The public API and CLI support separate configuration, options, and secrets inputs without exposing sensitive values. The existing environment-based integration remains supported during migration.

### 6.1 Current Input Separation

- `config` describes the destination and project, including destination type such as GitLab, GitHub, or Forgejo.
- `options` describes the currently supported runtime behavior: polling interval, pipeline timeout, pipeline state file, and cleanup timeout.
- `secrets` contains secret values or references needed to authenticate with the destination.

The current YAML contract is intentionally flat. Nested organization may be added later, but it is not supported by the current public format. Secrets may be supplied as plain YAML values or as explicitly marked Seanvault values.

Secrets must never appear in summaries, pipeline state, logs, exceptions, or other persisted non-secret output.

### 6.2 Current Flat YAML Contract

```yaml
# config.yaml
destination_type: gitlab
gitlab_url: https://gitlab.example.com
gitlab_remote_url: https://gitlab.example.com/group/project.git
gitlab_project_id: 1234
github_repository: example-org/example-repository
github_commit_sha: 0123456789abcdef0123456789abcdef01234567
github_run_id: "987654"
```

Use `gitlab_project_path` instead of `gitlab_project_id` when the project path is preferred; do not provide both.

```yaml
# options.yaml (optional)
poll_interval_seconds: 5
pipeline_timeout_seconds: 150
pipeline_state_file: /tmp/github2lab-pipeline-state.json
cleanup_timeout_seconds: 10
```

`CLUSTERHOP_LOG_SUMMARY_LIMIT_BYTES` remains environment-variable based and is not an `options.yaml` field.

```yaml
# secrets.yaml
gitlab_token: <gitlab-token>
gitlab_trigger_token: <gitlab-trigger-token>
```

Plain secrets YAML remains supported and does not require a Seanvault password. A Seanvault-managed file uses explicit markers:

```yaml
# secrets.yaml
gitlab_token:
  __seanvault__: secret
  value: "$SEAN_VAULT;1;ARGON2ID;AES256_GCM;..."
gitlab_trigger_token:
  __seanvault__: secret
  value: "$SEAN_VAULT;1;ARGON2ID;AES256_GCM;..."
```

When a secrets file contains a `__seanvault__` marker, the CLI requires `SEANVAULT_PASSWORD` from the environment and resolves the file with Seanvault. In GitHub Actions, this password should be stored as a GitHub Actions secret and exposed only to the ClusterHop step. Decrypted values are held in memory and passed as an ordinary mapping to `clusterhop.run(...)`; ClusterHop does not write decrypted plaintext back to disk.

The CLI loads these files as mappings and passes them to `clusterhop.run(...)`. Secret values must remain out of logs, summaries, errors, and pipeline state. Step 8 will update the GitHub Actions workflow to use these files safely.

### 6.3 Current Environment Contract

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

When GitHub Actions supplies `GITHUB_STEP_SUMMARY`, it selects the summary output file. Most values are now supplied through the public `config`, `options`, and `secrets` inputs when using the CLI. `CLUSTERHOP_LOG_SUMMARY_LIMIT_BYTES` remains environment-variable based.

## 7. Runtime Behavior

The GitHub Actions summary shows the overall pipeline status and every pipeline job in a clear table, including successful and failed jobs. When multiple jobs exist, it does not select or prioritize one job as the "main" job; users follow the per-job GitLab links for detailed logs.

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

## 9. Current Public CLI API

The current CLI is:

```bash
clusterhop run --config config.yaml --options options.yaml <repo-path> --secrets secrets.yaml
```

The command loads the separated inputs, calls the general `clusterhop.run(...)` API, writes `GITHUB_STEP_SUMMARY` through the existing runner when running in GitHub Actions, and returns the run result as a documented process exit code.

The `--secrets` argument identifies a plain or Seanvault-marked secrets file; it does not accept secret values or the Seanvault password directly on the command line. Seanvault-marked files require `SEANVAULT_PASSWORD` in the environment.

This general command replaces the earlier `clusterhop github2lab` proposal while preserving compatibility with the existing legacy `clusterhop run SOURCE_FILE DESTINATION_NAME` command.

## 10. Open Design Questions

- When, if ever, should the current flat `config.yaml` and `options.yaml` structures gain nested organization?
- How should future secret backends coexist with the current Seanvault integration?
- When should ClusterHop replace its private GitHub SSH Seanvault dependency with the normal PyPI dependency?
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

This validates the current GitHub-to-GitLab workflow. The public Python API, CLI, flat YAML loading, and all-jobs summary behavior are now implemented.

## 12. Remaining Work

The design direction, all-jobs summary behavior, public Python API, public CLI, flat YAML loading, and Seanvault secret loading are implemented. The remaining work is:

### Seanvault PyPI Dependency Cleanup

Seanvault support currently uses the private GitHub SSH dependency:

```text
seanvault @ git+ssh://git@github.com/exacb-org/seanvault.git
```

After Seanvault is published to PyPI, replace it with a normal versioned dependency, for example:

```text
seanvault>=0.1.0
```

This is a small dependency cleanup, not a redesign of the existing Seanvault integration.

### Step 8: Update GitHub Actions Usage

- Update the GitHub Actions workflow to invoke `clusterhop run --config ... --options ... <repo-path> --secrets ...`.
- Supply `SEANVAULT_PASSWORD` through GitHub Actions Secrets.
- Keep real secret values out of the repository and workflow logs.
- Connect the implemented CLI, flat YAML, and Seanvault behavior to the real CI workflow.

### Step 9: Future Reusable GitHub Action Wrapper

- Later create a reusable wrapper, preferably as a separate layer or repository.
- Have the wrapper call the stable ClusterHop CLI internally.
- Decide its repository, name, and release mechanism separately.

### Step 10: End-to-End Testing

- Test the complete path: GitHub Actions → ClusterHop CLI → config/options/secrets YAML → Seanvault decryption → GitLab pipeline → GitHub summary and result.
- Include multi-job GitLab pipeline testing and verify the all-jobs GitHub summary behavior.
- Verify success, failure, cancellation, timeout, and secret-safety behavior.
