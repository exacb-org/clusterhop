# GitHub-to-GitLab Model and API

## 1. Purpose

The GitHub-to-GitLab bridge connects a source repository and its GitHub Actions workflow to CI workloads hosted in GitLab. It lets a GitHub commit use an existing GitLab pipeline without duplicating that pipeline in GitHub.

The bridge transfers the commit, explicitly triggers the GitLab pipeline, follows its result, retrieves relevant jobs and logs, and reports the outcome in GitHub Actions. This bridge is one part of ClusterHop; it does not define the complete future ClusterHop or HPC feature set.

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
clusterhop github2lab runner
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

Coordinates the complete lifecycle: push, trigger, polling, job selection, log processing, summary writing, cleanup, and the structured final result.

### `config.py`

Loads and validates runtime configuration before remote work begins. It defines reusable defaults and rejects unsafe token-bearing remote URLs.

### `git.py`

Creates the `ci/<sha>` branch name and safely pushes the current GitHub commit to GitLab with the `ci.skip` push option.

### `gitlab.py`

Provides the GitLab API client for triggering pipelines, reading status, retrieving paginated jobs and traces, and requesting cancellation.

### `logs.py`

Decodes traces safely, removes terminal control sequences, calculates sizes, and decides whether a cleaned log can be embedded in the summary.

### `summary.py`

Renders the user-facing GitHub Actions Markdown summary with pipeline status, links, jobs, and the selected log or a safe fallback message.

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

## 5. User-Facing API Options

The final public interface has not been selected. These options should be reviewed together.

### 5.1 Current Python API: `run_from_environment`

**Status:** Current and working.

**Users:** Python callers and the current integration workflow.

```python
from clusterhop.github2lab.runner import run_from_environment

result = run_from_environment(repository_path=".")
raise SystemExit(result.exit_code)
```

**Pros:** Direct structured result, straightforward testing, and no extra command layer.

**Cons:** Workflow authors need a Python snippet and must know the module path. Making this exact import permanently stable requires an explicit public-API commitment.

### 5.2 Current GitHub Actions Workflow Interface

**Status:** Current and working in `github2lab-test`.

**Users:** Repository maintainers connecting GitHub Actions to GitLab CI.

The workflow installs `clusterhop` from a pinned commit, supplies secrets and configuration through the environment, calls `run_from_environment(repository_path=".")`, and returns `result.exit_code`. A cancellation-only fallback step can call the fallback module using persisted state.

**Pros:** Validated end to end, reproducible through commit pinning, and integrated with GitHub secrets and summaries.

**Cons:** Each consumer must maintain installation and Python invocation YAML until a reusable wrapper exists.

### 5.3 Proposed CLI API: `clusterhop github2lab`

**Status:** Proposed; not implemented.

**Users:** Local operators, CI workflows, and users who prefer a command-line interface.

```bash
clusterhop github2lab
```

**Pros:** Simple and discoverable, removes the Python heredoc, and provides a natural place for future non-secret CLI options.

**Cons:** The existing `clusterhop run SOURCE_FILE DESTINATION_NAME` behavior must remain compatible. Commands, options, exit codes, and configuration precedence need approval.

### 5.4 Possible Module Execution

**Status:** Proposed for the main runner; the fallback module is already executable.

**Users:** CI or Python environments where the package is installed but a console-script command is undesirable.

```bash
python -m clusterhop.github2lab
```

**Pros:** Requires no additional executable entry point and is convenient in Python-based CI.

**Cons:** Less discoverable than a dedicated CLI and still requires a stable `__main__` contract.

### 5.5 Possible Future GitHub Action Wrapper

**Status:** Future; not implemented.

**Users:** GitHub repository maintainers who want a concise reusable action.

```yaml
- name: Run GitHub-to-GitLab bridge
  uses: exacb-org/clusterhop-github2lab@v1
  env:
    GITLAB_TOKEN: ${{ secrets.GITLAB_TOKEN }}
    GITLAB_TRIGGER_TOKEN: ${{ secrets.GITLAB_TRIGGER_TOKEN }}
```

**Pros:** Best GitHub-native experience, centralized setup, and consistent fallback lifecycle.

**Cons:** Requires release/versioning decisions, wrapper maintenance, security documentation, and an installation strategy.

| Option | Status | Primary audience | Main trade-off |
| --- | --- | --- | --- |
| `run_from_environment` | Current | Python callers | Powerful but exposes a Python integration surface |
| GitHub Actions workflow | Current | Repository maintainers | Validated but currently verbose and repository-owned |
| `clusterhop github2lab` | Proposed | CLI and CI users | Discoverable, but needs compatibility design |
| `python -m clusterhop.github2lab` | Proposed | Python/CI environments | Easy packaging, less discoverable |
| GitHub Action wrapper | Future | GitHub workflow authors | Best GitHub UX, highest release cost |

No final API choice is made in this document.

## 6. Current Configuration API

Configuration currently comes from environment variables. Secrets must be supplied through a secure runtime such as GitHub Actions secrets and must never be stored in pipeline state or summaries.

### Required variables

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

### Optional variables

| Variable | Default | Purpose |
| --- | ---: | --- |
| `CLUSTERHOP_POLL_INTERVAL_SECONDS` | `5` | Delay between pipeline status checks. |
| `CLUSTERHOP_PIPELINE_TIMEOUT_SECONDS` | `150` | Maximum wait for a terminal pipeline result. |
| `CLUSTERHOP_LOG_SUMMARY_LIMIT_BYTES` | `800000` | Largest cleaned log embedded in the summary. |
| `CLUSTERHOP_PIPELINE_STATE_FILE` | unset | Path for non-secret state used by fallback cleanup. |
| `CLUSTERHOP_CLEANUP_TIMEOUT_SECONDS` | `10` | Maximum duration for a cleanup request. |

When GitHub Actions supplies `GITHUB_STEP_SUMMARY`, it selects the summary output file. Defaults reflect the validated prototype and remain configurable.

## 7. Runtime Behavior

### Success

The runner collects available jobs and logs, writes a success summary, marks active state inactive, and exits successfully.

### Failure

The runner still gathers useful job/log data, writes a failed summary, marks state inactive, and returns a non-zero exit code.

### Large log

If a cleaned trace exceeds the limit, the full trace is omitted. The summary provides a clear explanation, sizes, and available GitLab links.

### Timeout

The runner requests cancellation using the bounded cleanup timeout. Its final result remains `timeout`, even if cancellation succeeds. Successful cleanup marks state inactive; failed cleanup leaves it active for fallback retry.

### Manual cancellation

The runner handles `SIGINT` and `SIGTERM`. If a pipeline ID is known, its idempotent cleanup callback requests cancellation. The runner result remains `canceled`.

### Fallback cancellation

A workflow cancellation step can run `python -m clusterhop.github2lab.fallback`. Missing/inactive state is a successful no-op. Active matching state causes one bounded cancellation attempt. GitLab's intermediate `canceling` status confirms that cancellation was accepted.

## 8. Internal API vs Public API

The workers in `git.py`, `gitlab.py`, `logs.py`, `summary.py`, `cancellation.py`, and `state.py` are currently implementation units. They are tested, but users should not assume every name and signature is permanently stable.

The current supported Python integration point is:

```python
run_from_environment(repository_path=".")
```

For GitHub Actions, the current interface is the pinned-package workflow invoking this function and optionally the fallback module.

After review, a small stable public surface should be documented. It may cover a CLI, a supported Python function/result, environment variables, exit semantics, and fallback. Transport, storage, parsing, and orchestration helpers should remain internal.

## 9. Proposed Future CLI API

The likely command is:

```bash
clusterhop github2lab
```

It would load the validated environment, run the same orchestration, write `GITHUB_STEP_SUMMARY` when available, and return the runner's exit code. It would replace the Python heredoc while preserving `clusterhop run SOURCE_FILE DESTINATION_NAME`.

Selected non-secret settings might later accept CLI overrides, but arguments and precedence need confirmation. Secret CLI arguments should be avoided because process listings and shell history can expose them.

The CLI name and contract remain proposals.

## 10. Open Design Questions

- Should the final command be `clusterhop github2lab`?
- Should `python -m clusterhop.github2lab` also be supported publicly?
- Should configuration remain environment-only or also support a file and non-secret CLI options?
- How should users select the main GitLab job?
- Should releases use pinned Git commits, Git tags, or PyPI?
- Should the package remain `clusterhop`, publish through `exacb`, or use another confirmed identity?
- Which Python functions and models should receive compatibility guarantees?
- Should a reusable GitHub Action live here or in a separate repository?

These decisions require review with Jayesh and the maintainers before publishing.

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

This validates the current workflow but does not freeze the final public API.

## 12. Recommended Next Steps

1. Review this model/API document with Jayesh.
2. Create a draw.io big-picture diagram from the agreed model.
3. Select the stable Python, CLI, module, and/or GitHub Action interfaces.
4. Confirm configuration precedence, job selection, package identity, versioning, and distribution.
5. Document compatibility guarantees.
6. Then implement the selected wrappers and productization/release work.
