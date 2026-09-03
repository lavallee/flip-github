# GitHub issue capture example

Configure the adapter as a named Flip web lane:

```toml
[fetchers.web]
github = "flip-github {url} {dest}"
```

Then capture an issue or pull request:

```bash
flip add-source https://github.com/owner/repository/issues/123 \
  --kind web --via github
```

A successful `capture.json` contains the current state, complete issue-comment
set, timeline events, and raw API responses. Pull requests also contain their
merge state, reviews, and inline review comments. The adapter exits nonzero if
GitHub's reported issue-comment count does not match the paginated result.

