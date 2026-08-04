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

1. A commit is pushed to the GitHub repository.

2. GitHub Actions starts the GitHub-to-GitLab workflow.

3. The workflow derives a GitLab branch name from the GitHub commit SHA:

   ```text
   ci/<github-commit-sha>
   ```

## 5. Inputs and Secrets

## 6. Proposed Python Architecture

## 7. Error Handling and Cancellation

## 8. Testing Plan

## 9. Unresolved Decisions
