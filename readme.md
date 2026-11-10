<!--
- SPDX-License-Identifier: Apache-2.0
- Copyright (C) 2026 Jayesh Badwaik <j.badwaik@fz-juelich.de>
-->

# clusterhop

ClusterHop is CI/CD connector for HPC clusters between GitLab and HPC Clusters. It allows users to
seamlessly submit jobs from GitLab CI/CD pipelines to HPC clusters, monitor their status, and
retrieve results back to GitLab.

## Reusable GitHub Action

The repository contains a Docker-based Action that wraps the existing ClusterHop CLI. Until a
reviewed release tag exists, test it by pinning an exact commit SHA rather than using `@v1`:

```yaml
steps:
  - name: Checkout repository
    uses: actions/checkout@v6
    with:
      fetch-depth: 0

  - name: Run ClusterHop
    uses: exacb-org/clusterhop@<full-commit-sha>
    with:
      gitlab-url: https://gitlab.example.com
      gitlab-project: group/project
      jsc-machine: jureca # Optional.
      secrets-file: .github/clusterhop/secrets.keyloom.yaml
    env:
      KEYLOOM_PASSWORD: ${{ secrets.KEYLOOM_PASSWORD }}
```

`gitlab-project` is the GitLab `path_with_namespace`. The secrets file must be a relative path
inside the checked-out repository and contain the existing Keyloom-encrypted `gitlab_token` and
`gitlab_trigger_token` fields. The Action resolves authoritative project metadata through GitLab,
executes the public `clusterhop run` CLI, and performs bounded best-effort fallback cancellation
from its Docker post-entrypoint.
