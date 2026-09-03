# flip-github

`flip-github` captures a GitHub issue or pull request as an evolving source,
not just an opening post. It is a zero-runtime-dependency fetcher for
[Flip](https://github.com/lavallee/flip) notebooks and a small Flip plugin with
a repeatable source-refresh workflow.

GitHub issue bodies are snapshots. The current answer may live in a later owner
comment, a maintainer reply, a linked change, a reopened issue, or a merged pull
request. A body-only capture can therefore publish a problem as unresolved long
after the record says otherwise.

## What it captures

- the issue or pull request's state at retrieval time;
- every paginated issue comment;
- pull-request merge state, reviews, and inline review comments;
- lifecycle events, including closures, reopenings, and cross-references;
- the complete raw API responses alongside a readable normalized view; and
- a Flip return envelope with the `publisher-api` capture method.

The command fails if GitHub's declared issue-comment count does not match what
pagination returned. That is a narrow completeness check for the source
artifact, not a claim that the discussion is correct.

## Install

Python 3.12 or newer is required.

```bash
uv tool install git+https://github.com/lavallee/flip-github
```

Public repositories work without authentication at GitHub's lower anonymous
rate limit. Set `GITHUB_TOKEN` or `GH_TOKEN` in the environment for a higher
limit or repositories the token may access. Tokens are sent only in the API
authorization header and are never written to captures.

## Configure Flip

Add a named web lane to `$FLIP_HOME/config.toml`:

```toml
[fetchers.web]
github = "flip-github {url} {dest}"
```

Capture a source:

```bash
flip add-source https://github.com/omacom/omarchy/issues/6380 \
  --kind web --via github
```

Refresh it before publishing a claim that depends on current state:

```bash
flip source recheck A12 --via github
```

Open `sources/raw/<source-id>/<capture>/capture.json` and read the `source`,
`comments`, `reviews`, `review_comments`, and `timeline` sections. The `text`
field presents the same evidence as readable Markdown for structured-text
extractors.

## Use the Flip plugin

The repository also transports an agent skill and a source-refresh workflow.
Linking only registers the materialized directory; it does not enable an
executable hook.

```bash
flip plugin doctor /path/to/flip-github
flip plugin link /path/to/flip-github
flip extension list
flip workflow show lavallee/github-evidence/github-source-refresh
```

The adapter itself remains an explicit subprocess in Flip's integration
configuration. Flip plugins do not silently install or activate networked
fetchers.

## Interpret the result

Repository state and real-world outcome are different fields:

- closed does not necessarily mean fixed;
- merged means code entered the pull request's base branch, not necessarily a
  release;
- an owner saying a fix worked is stronger than an unexplained closure, but is
  still one report; and
- later contrary reports may indicate a regression, configuration difference,
  or different failure.

The capture is current as of `fetched_at`. It cannot recover deleted comments
or earlier versions of edited text. Rechecking creates a new provenance event
so change over time remains visible in Flip rather than overwriting the old
record.

## Development

The tests cover only the failure modes this tool exists to prevent: missing a
later paginated fix comment, losing pull-request review/merge state, or
accepting an incomplete comment set.

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

## License

MIT
