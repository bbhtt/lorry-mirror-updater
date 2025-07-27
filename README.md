## Lorry Mirror Updater

A wrapper for [bst-to-lorry](https://gitlab.com/CodethinkLabs/lorry/bst-to-lorry)
to automate updating lorry mirror definitions and sending Gitlab
merge requests to the mirroring-config repository.

This is expected to be run from a clean checkout of the
mirroring-config repository.

### Install

```sh
pip install --user git+https://github.com/bbhtt/lorry-mirror-updater.git@v0.1.0#egg=lorry_mirror_updater
```

[python-gitlab](https://python-gitlab.readthedocs.io/en/stable/) is
optionally used to send Gitlab merge requests. `GITLAB_API_KEY`
environment variable is used for the API key to send MRs and delete
old branches.

### Usage

This is expected to be run inside a Gitlab CI environment with the
following variables available: `GITLAB_API_KEY` and
`CI_PROJECT_ID, CI_SERVER_URL` (Gitlab predefined).

Note, that it is best to run it inside a docker image coming with
`bst, bst-to-lorry` and all other dependencies, such as something based
on `registry.gitlab.com/freedesktop-sdk/infrastructure/freedesktop-sdk-docker-images/bst2:latest`

```
Lorry mirror updater

A wrapper for bst-to-lorry to automate updating lorry files and sending Gitlab
merge requests to the mirroring-config repository. This is expected to be run
from a clean checkout of the mirroring-config repository.

options:
  -h, --help            Show this help message and exit
  --version             Show the version and exit
  --mirror-config       Path to the lorry-mirror-updater config file (default: mirrors.json)
  --base-branch         Base branch of mirroring-config repository (default: main)
  --git-directory       Path to the git mirror directory (default: gits)
  --raw-files-directory
                        Path to the raw files mirror directory (default: files)
  --exclude-alias [ ...]
                        List of aliases to exclude in bst-to-lorry (default: fdsdk_git, fdsdk_mirror)
  --push                Push the branch to remote repository
  --create-mr           Push the branch to remote repository
  --lorry2              Use lorry2 format in bst-to-lorry
```

An example usage in a mirroring-config repository. The repository
layout is as follows:

```
tree -L 1
.
├── files/                <- Raw file mirrors
├── gits/                 <- Git mirrors
├── mirrors.json          <- lorry-mirror-updater config
├──.gitlab-ci.yml
```

```yml
default:
  image: "<input docker image>"
  interruptible: false

stages:
  - schedule

update-mirrors:
  stage: schedule
  needs: []
  timeout: 1h
  rules:
  - if: '$CI_COMMIT_BRANCH == "main" && $CI_COMMIT_REF_PROTECTED == "true" && $CI_PIPELINE_SOURCE == "schedule"'
  script:
    - git remote set-url origin "https://gitlab-ci-token:${GITLAB_API_TOKEN}@gitlab.com/example/mirroring-config.git"
    - git config user.name "mirror_updater_bot"
    - git config user.email "mirror_updater_bot@localhost"
    - git branch -f "${CI_COMMIT_REF_NAME}" "origin/${CI_COMMIT_REF_NAME}"
    - lorry-mirror-updater --push --create-mr
```

### Development

```sh
uv run ruff format
uv run ruff check --fix --exit-non-zero-on-fix
uv run mypy .
```
