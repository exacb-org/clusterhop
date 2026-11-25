<!--
- SPDX-License-Identifier: Apache-2.0
- Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
-->

# clusterhop

ClusterHop connects GitHub Actions with GitLab CI/CD. It securely transfers the corresponding
GitHub commit, triggers and monitors the configured GitLab pipeline, and reports the results back
to GitHub Actions. GitLab pipelines can optionally execute HPC workloads through their configured
runners.

## GitHub-to-GitLab Integration

ClusterHop also provides a reusable GitHub Action, Python API, and CLI to connect GitHub workflows
with GitLab CI/CD pipelines.

When code is pushed to GitHub, ClusterHop securely transfers the corresponding commit to GitLab,
triggers the configured pipeline, monitors its execution, and reports results back to GitHub
Actions.

## Reusable GitHub Action

```yaml
steps:
  - uses: actions/checkout@v6
    with:
      fetch-depth: 0
  - name: Run ClusterHop
    uses: exacb-org/clusterhop@v0.3.0
    with:
      gitlab-url: https://gitlab.example.com
      gitlab-project: group/project
      preset-machine: jureca # Optional
      secrets-file: .github/clusterhop/secrets.keyloom.yaml
    env:
      CLUSTERHOP_SECRETS_PASSWORD: ${{ secrets.CLUSTERHOP_SECRETS_PASSWORD }}
```

The Action uses Keyloom-encrypted GitLab credentials and supports optional machine-preset selection.
Pinning the Action to a full commit SHA remains available for maximum immutability.

## Documentation

For the complete workflow, architecture, configuration, monitoring, and cancellation behavior, see
[ClusterHop Workflow and Architecture](https://github.com/exacb-org/clusterhop/blob/main/docs/clusterhop-workflow.md).
