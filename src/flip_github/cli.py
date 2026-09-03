"""Capture a GitHub issue or pull request through the public API.

The artifact deliberately keeps both a compact normalized view and the raw API
responses. The normalized view is easy to inspect; the raw responses prevent a
new GitHub field from being silently discarded before Flip takes custody.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from . import __version__


API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = f"flip-github/{__version__} (+https://github.com/lavallee/flip-github)"
DEFAULT_ACCEPT = "application/vnd.github+json"
TARGET_RE = re.compile(r"^/([^/]+)/([^/]+)/(issues|pull)/(\d+)(?:/.*)?$")


class CaptureError(RuntimeError):
    """A capture could not be completed without an evidence gap."""


@dataclass(frozen=True)
class Target:
    owner: str
    repo: str
    number: int
    route_kind: str
    input_url: str


def parse_target(value: str) -> Target:
    """Parse a public github.com issue or pull-request URL."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "github.com",
        "www.github.com",
    }:
        raise CaptureError("expected a https://github.com/OWNER/REPO/issues/N or /pull/N URL")
    match = TARGET_RE.match(parsed.path.rstrip("/"))
    if not match:
        raise CaptureError("expected a GitHub issue or pull-request URL with a numeric id")
    owner, repo, route_kind, number = match.groups()
    return Target(owner, repo, int(number), route_kind, value)


def _api_url(value: str) -> str:
    if value.startswith(f"{API_ROOT}/"):
        return value
    if not value.startswith("/"):
        value = f"/{value}"
    return f"{API_ROOT}{value}"


def _with_per_page(value: str) -> str:
    parts = urlsplit(_api_url(value))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("per_page", "100")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _next_link(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(","):
        section = part.strip()
        match = re.match(r"<([^>]+)>\s*;(.*)$", section)
        if match and re.search(r'\brel="?next"?\b', match.group(2)):
            return match.group(1)
    return None


class GitHubClient:
    """Small injectable GitHub REST client with complete pagination."""

    def __init__(
        self,
        token: str | None = None,
        timeout: float = 30,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.token = token
        self.timeout = timeout
        self.opener = opener

    def get(self, value: str, *, accept: str = DEFAULT_ACCEPT) -> tuple[Any, Any]:
        url = _api_url(value)
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(url, headers=headers)
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = response.read()
                response_headers = response.headers
        except HTTPError as error:
            try:
                payload = error.read()
                detail = json.loads(payload.decode("utf-8")).get("message")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                detail = None
            limit = ""
            remaining = error.headers.get("X-RateLimit-Remaining") if error.headers else None
            reset = error.headers.get("X-RateLimit-Reset") if error.headers else None
            if remaining == "0":
                limit = f"; API rate limit exhausted (reset epoch {reset or 'unknown'})"
            raise CaptureError(
                f"GitHub API HTTP {error.code} for {url}: {detail or error.reason}{limit}"
            ) from error
        except (URLError, OSError) as error:
            raise CaptureError(f"GitHub API request failed for {url}: {error}") from error

        try:
            return json.loads(payload.decode("utf-8")), response_headers
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CaptureError(f"GitHub API returned invalid JSON for {url}") from error

    def all_pages(self, value: str, *, accept: str = DEFAULT_ACCEPT) -> tuple[list[Any], int]:
        url: str | None = _with_per_page(value)
        seen: set[str] = set()
        items: list[Any] = []
        pages = 0
        while url:
            if url in seen:
                raise CaptureError(f"GitHub API returned a pagination loop at {url}")
            seen.add(url)
            payload, headers = self.get(url, accept=accept)
            if not isinstance(payload, list):
                raise CaptureError(f"GitHub API expected a list while paginating {url}")
            items.extend(payload)
            pages += 1
            url = _next_link(headers.get("Link"))
        return items, pages


def _user(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "login": value.get("login"),
        "html_url": value.get("html_url"),
        "type": value.get("type"),
    }


def _comment(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": value.get("id"),
        "author": _user(value.get("user")),
        "author_association": value.get("author_association"),
        "created_at": value.get("created_at"),
        "updated_at": value.get("updated_at"),
        "html_url": value.get("html_url"),
        "body": value.get("body") or "",
    }


def _review(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": value.get("id"),
        "author": _user(value.get("user")),
        "author_association": value.get("author_association"),
        "state": value.get("state"),
        "submitted_at": value.get("submitted_at"),
        "commit_id": value.get("commit_id"),
        "html_url": value.get("html_url"),
        "body": value.get("body") or "",
    }


def _timeline_event(value: dict[str, Any]) -> dict[str, Any]:
    source = value.get("source") if isinstance(value.get("source"), dict) else {}
    source_issue = source.get("issue") if isinstance(source.get("issue"), dict) else {}
    label = value.get("label") if isinstance(value.get("label"), dict) else {}
    return {
        "id": value.get("id"),
        "event": value.get("event"),
        "created_at": value.get("created_at"),
        "actor": _user(value.get("actor")),
        "commit_id": value.get("commit_id"),
        "commit_url": value.get("commit_url"),
        "label": label.get("name"),
        "source_issue": {
            "number": source_issue.get("number"),
            "title": source_issue.get("title"),
            "state": source_issue.get("state"),
            "html_url": source_issue.get("html_url"),
            "is_pull_request": bool(source_issue.get("pull_request")),
        }
        if source_issue
        else None,
    }


def _display_state(issue: dict[str, Any], pull: dict[str, Any] | None) -> str:
    if pull and pull.get("merged"):
        return "merged"
    return str(issue.get("state") or "unknown")


def _author_label(value: dict[str, Any] | None) -> str:
    if not value:
        return "unknown author"
    login = value.get("login")
    return f"@{login}" if login else "unknown author"


def _render_markdown(artifact: dict[str, Any]) -> str:
    source = artifact["source"]
    lines = [
        f"# {source['title']}",
        "",
        f"- URL: {source['html_url']}",
        f"- Repository: {source['repository']}",
        f"- Type: {source['kind'].replace('_', ' ')}",
        f"- State at capture: {source['state']}",
        f"- Author: {_author_label(source.get('author'))}",
        f"- Created: {source.get('created_at') or 'unknown'}",
        f"- Updated: {source.get('updated_at') or 'unknown'}",
        f"- Closed: {source.get('closed_at') or 'not closed'}",
        f"- Captured: {artifact['fetched_at']}",
    ]
    if source.get("state_reason"):
        lines.append(f"- State reason: {source['state_reason']}")
    pull = artifact.get("pull_request")
    if pull:
        lines.extend(
            [
                f"- Draft: {'yes' if pull.get('draft') else 'no'}",
                f"- Merged: {'yes' if pull.get('merged') else 'no'}",
                f"- Merged at: {pull.get('merged_at') or 'not merged'}",
                f"- Base: {pull.get('base') or 'unknown'}",
                f"- Head: {pull.get('head') or 'unknown'}",
            ]
        )

    lines.extend(["", "## Original report", "", source.get("body") or "[No body]"])

    comments = artifact["comments"]
    lines.extend(["", f"## Issue comments ({len(comments)})", ""])
    if not comments:
        lines.append("[No issue comments]")
    for comment in comments:
        lines.extend(
            [
                f"### {_author_label(comment.get('author'))} — "
                f"{comment.get('created_at') or 'unknown date'}",
                "",
                f"[Open comment]({comment.get('html_url')})" if comment.get("html_url") else "",
                "",
                comment.get("body") or "[No body]",
                "",
            ]
        )

    reviews = artifact.get("reviews") or []
    if pull:
        lines.extend(["", f"## Reviews ({len(reviews)})", ""])
        if not reviews:
            lines.append("[No reviews]")
        for review in reviews:
            lines.extend(
                [
                    f"### {_author_label(review.get('author'))} — "
                    f"{review.get('state') or 'unknown state'} — "
                    f"{review.get('submitted_at') or 'unknown date'}",
                    "",
                    review.get("body") or "[No body]",
                    "",
                ]
            )

        review_comments = artifact.get("review_comments") or []
        lines.extend(["", f"## Inline review comments ({len(review_comments)})", ""])
        if not review_comments:
            lines.append("[No inline review comments]")
        for comment in review_comments:
            lines.extend(
                [
                    f"### {_author_label(comment.get('author'))} — "
                    f"{comment.get('created_at') or 'unknown date'}",
                    "",
                    comment.get("body") or "[No body]",
                    "",
                ]
            )

    timeline = artifact["timeline"]
    lines.extend(["", f"## Lifecycle events ({len(timeline)})", ""])
    if not timeline:
        lines.append("[No timeline events returned]")
    for event in timeline:
        detail = event.get("event") or "unknown event"
        if event.get("label"):
            detail += f": {event['label']}"
        if event.get("source_issue"):
            linked = event["source_issue"]
            detail += f": {linked.get('title') or linked.get('html_url') or 'linked issue'}"
        lines.append(
            f"- {event.get('created_at') or 'unknown date'} — {detail} — "
            f"{_author_label(event.get('actor'))}"
        )
    return "\n".join(line for line in lines if line is not None).rstrip() + "\n"


def capture(
    value: str,
    dest: str | Path,
    *,
    client: GitHubClient | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Capture one issue or pull request, refusing an incomplete comment set."""
    target = parse_target(value)
    client = client or GitHubClient(token=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    issue_path = f"/repos/{target.owner}/{target.repo}/issues/{target.number}"
    issue, _ = client.get(issue_path)
    if not isinstance(issue, dict):
        raise CaptureError("GitHub API returned an unexpected issue record")

    comments_url = issue.get("comments_url") or f"{issue_path}/comments"
    raw_comments, comment_pages = client.all_pages(comments_url)
    expected_comments = issue.get("comments")
    if not isinstance(expected_comments, int):
        raise CaptureError("GitHub API issue record did not declare its comment count")
    if len(raw_comments) != expected_comments:
        raise CaptureError(
            "refusing a partial capture: GitHub reports "
            f"{expected_comments} issue comments but the API returned {len(raw_comments)}"
        )

    raw_timeline, timeline_pages = client.all_pages(f"{issue_path}/timeline")
    is_pull_request = bool(issue.get("pull_request"))
    raw_pull: dict[str, Any] | None = None
    raw_reviews: list[dict[str, Any]] = []
    raw_review_comments: list[dict[str, Any]] = []
    review_pages = 0
    review_comment_pages = 0
    if is_pull_request:
        raw_pull_value, _ = client.get(
            f"/repos/{target.owner}/{target.repo}/pulls/{target.number}"
        )
        if not isinstance(raw_pull_value, dict):
            raise CaptureError("GitHub API returned an unexpected pull-request record")
        raw_pull = raw_pull_value
        raw_reviews, review_pages = client.all_pages(
            f"/repos/{target.owner}/{target.repo}/pulls/{target.number}/reviews"
        )
        raw_review_comments, review_comment_pages = client.all_pages(
            f"/repos/{target.owner}/{target.repo}/pulls/{target.number}/comments"
        )

    fetched_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    fetched_text = fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    repository_url = issue.get("repository_url") or ""
    repository = repository_url.removeprefix(f"{API_ROOT}/repos/") or (
        f"{target.owner}/{target.repo}"
    )
    source = {
        "kind": "pull_request" if is_pull_request else "issue",
        "repository": repository,
        "number": issue.get("number") or target.number,
        "title": issue.get("title") or f"{repository} #{target.number}",
        "html_url": issue.get("html_url") or value,
        "author": _user(issue.get("user")),
        "author_association": issue.get("author_association"),
        "state": _display_state(issue, raw_pull),
        "state_reason": issue.get("state_reason"),
        "locked": bool(issue.get("locked")),
        "created_at": issue.get("created_at"),
        "updated_at": issue.get("updated_at"),
        "closed_at": issue.get("closed_at"),
        "body": issue.get("body") or "",
        "labels": [
            label.get("name")
            for label in issue.get("labels") or []
            if isinstance(label, dict) and label.get("name")
        ],
    }
    pull = None
    if raw_pull:
        base = raw_pull.get("base") if isinstance(raw_pull.get("base"), dict) else {}
        head = raw_pull.get("head") if isinstance(raw_pull.get("head"), dict) else {}
        pull = {
            "draft": bool(raw_pull.get("draft")),
            "merged": bool(raw_pull.get("merged")),
            "merged_at": raw_pull.get("merged_at"),
            "merged_by": _user(raw_pull.get("merged_by")),
            "merge_commit_sha": raw_pull.get("merge_commit_sha"),
            "base": base.get("label") or base.get("ref"),
            "head": head.get("label") or head.get("ref"),
            "additions": raw_pull.get("additions"),
            "deletions": raw_pull.get("deletions"),
            "changed_files": raw_pull.get("changed_files"),
        }

    artifact: dict[str, Any] = {
        "schema_version": "flip.github-capture/1",
        "input_url": value,
        "canonical_url": source["html_url"],
        "fetched_at": fetched_text,
        "source": source,
        "comments_complete": True,
        "comments_expected": expected_comments,
        "comments": [_comment(item) for item in raw_comments],
        "pull_request": pull,
        "reviews": [_review(item) for item in raw_reviews],
        "review_comments": [_comment(item) for item in raw_review_comments],
        "timeline": [_timeline_event(item) for item in raw_timeline],
        "pagination": {
            "comment_pages": comment_pages,
            "timeline_pages": timeline_pages,
            "review_pages": review_pages,
            "review_comment_pages": review_comment_pages,
        },
        "raw": {
            "issue": issue,
            "pull_request": raw_pull,
            "comments": raw_comments,
            "reviews": raw_reviews,
            "review_comments": raw_review_comments,
            "timeline": raw_timeline,
        },
    }
    artifact["text"] = _render_markdown(artifact)

    output = Path(dest)
    output.mkdir(parents=True, exist_ok=True)
    capture_path = output / "capture.json"
    sidecar_path = output / "flip.json"
    capture_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    envelope = {
        "flip": {
            "title": source["title"],
            "canonical_url": source["html_url"],
            "retrieved_at": fetched_text,
            "strategy": "publisher-api",
            "status": "success",
            "mime": "application/json",
            "user_agent": USER_AGENT,
            "backend_ref": f"github:{repository}#{source['number']}",
        }
    }
    sidecar_path.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    return artifact


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flip-github",
        description="Capture a complete GitHub issue or pull request for Flip.",
    )
    parser.add_argument("url", nargs="?", help="GitHub issue or pull-request URL")
    parser.add_argument("dest", nargs="?", help="Flip capture destination directory")
    parser.add_argument("--timeout", type=float, default=30, help="HTTP timeout in seconds")
    parser.add_argument("--version", action="version", version=f"flip-github {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.url or not args.dest:
        parser.error("URL and DEST are required")
    client = GitHubClient(
        token=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
        timeout=args.timeout,
    )
    try:
        artifact = capture(args.url, args.dest, client=client)
    except CaptureError as error:
        sys.stderr.write(f"flip-github: {error}\n")
        return 1
    source = artifact["source"]
    sys.stderr.write(
        f"flip-github: captured {source['kind'].replace('_', ' ')} "
        f"{source['repository']}#{source['number']} at state {source['state']} "
        f"with {len(artifact['comments'])} issue comment(s)\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

