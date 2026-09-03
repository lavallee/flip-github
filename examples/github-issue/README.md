# GitHub issue capture example

Configure the adapter as a named Flip web lane:

```toml
[fetchers.web]
github = "flip-github {url} {dest}"
```

Then capture an issue, pull request or Discussion:

```bash
flip add-source https://github.com/owner/repository/issues/123 \
  --kind web --via github

flip add-source https://github.com/owner/repository/discussions/456 \
  --kind web --via github
```

A successful `capture.json` contains the current state, complete issue-comment
set and normalized timeline events. Pull requests also contain their merge
state, reviews and inline review comments. Complete publisher responses are
retained separately in deterministic `raw.json.gz`, leaving `capture.json` as
Flip's primary comparable artifact. The adapter exits nonzero if GitHub's
reported issue-comment count does not match the paginated result. Discussion
captures include all top-level comments, their replies and answer state; they
require `GITHUB_TOKEN` or `GH_TOKEN` because GitHub exposes them through its
authenticated GraphQL API.
