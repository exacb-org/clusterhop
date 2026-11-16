<!--
- SPDX-License-Identifier: Apache-2.0
- Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
-->

# clusterhop

ClusterHop is CI/CD connector for HPC clusters between GitLab and HPC Clusters. It allows users to
seamlessly submit jobs from GitLab CI/CD pipelines to HPC clusters, monitor their status, and
retrieve results back to GitLab.

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
    uses: exacb-org/clusterhop@<full-commit-sha>
    with:
      gitlab-url: https://gitlab.example.com
      gitlab-project: group/project
      jsc-machine: jureca # Optional
      secrets-file: .github/clusterhop/secrets.keyloom.yaml
    env:
      KEYLOOM_PASSWORD: ${{ secrets.KEYLOOM_PASSWORD }}
```

The Action uses Keyloom-encrypted GitLab credentials and supports optional target-machine selection.
Pin the Action to a full commit SHA until a reviewed release tag is available.

## Documentation

For the complete workflow, architecture, configuration, monitoring, and cancellation behavior, see
[ClusterHop Workflow and Architecture](docs/clusterhop-workflow.md).
