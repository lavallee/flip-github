"""Capture a GitHub issue, pull request, or discussion through the public API.

The content artifact is a compact normalized view of the record and its full
conversation. Complete API responses live in a separate raw artifact so
unrelated repository metadata cannot create a false editorial change.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from . import __version__

API_ROOT = "https://api.github.com"
GRAPHQL_ROOT = f"{API_ROOT}/graphql"
API_VERSION = "2022-11-28"
USER_AGENT = f"flip-github/{__version__} (+https://github.com/lavallee/flip-github)"
DEFAULT_ACCEPT = "application/vnd.github+json"
TARGET_RE = re.compile(r"^/([^/]+)/([^/]+)/(issues|pull|discussions)/(\d+)(?:/.*)?$")


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
    """Parse a public github.com issue, pull-request, or discussion URL."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "github.com",
        "www.github.com",
    }:
        raise CaptureError(
            "expected a https://github.com/OWNER/REPO/issues/N, /pull/N, or /discussions/N URL"
        )
    match = TARGET_RE.match(parsed.path.rstrip("/"))
    if not match:
        raise CaptureError("expected a GitHub issue, pull-request, or discussion URL")
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

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Run one authenticated GraphQL query and surface schema errors."""
        if not self.token:
            raise CaptureError("GitHub Discussions capture requires GITHUB_TOKEN or GH_TOKEN")
        request = Request(
            GRAPHQL_ROOT,
            data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
            headers={
                "Accept": DEFAULT_ACCEPT,
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = response.read()
        except HTTPError as error:
            try:
                detail = json.loads(error.read().decode("utf-8")).get("message")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                detail = None
            raise CaptureError(
                f"GitHub GraphQL API HTTP {error.code}: {detail or error.reason}"
            ) from error
        except (URLError, OSError) as error:
            raise CaptureError(f"GitHub GraphQL API request failed: {error}") from error

        try:
            result = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CaptureError("GitHub GraphQL API returned invalid JSON") from error
        if not isinstance(result, dict):
            raise CaptureError("GitHub GraphQL API returned an unexpected response")
        if result.get("errors"):
            messages = "; ".join(
                str(item.get("message") or item)
                for item in result["errors"]
                if isinstance(item, dict)
            )
            raise CaptureError(f"GitHub GraphQL API error: {messages or result['errors']}")
        if not isinstance(result.get("data"), dict):
            raise CaptureError("GitHub GraphQL API response did not include data")
        return result


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


DISCUSSION_QUERY = """
query DiscussionCapture($owner: String!, $repo: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    nameWithOwner
    discussion(number: $number) {
      id
      number
      title
      url
      body
      createdAt
      updatedAt
      closed
      closedAt
      stateReason
      locked
      author { login url __typename }
      authorAssociation
      category { name slug isAnswerable }
      isAnswered
      answer { id url }
      answerChosenAt
      answerChosenBy { login url __typename }
      comments(first: 100, after: $after) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          databaseId
          url
          body
          createdAt
          updatedAt
          isAnswer
          author { login url __typename }
          authorAssociation
          replies(first: 100) {
            totalCount
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              databaseId
              url
              body
              createdAt
              updatedAt
              isAnswer
              author { login url __typename }
              authorAssociation
              replyTo { id }
            }
          }
        }
      }
    }
  }
}
"""


DISCUSSION_REPLIES_QUERY = """
query DiscussionReplies($id: ID!, $after: String) {
  node(id: $id) {
    ... on DiscussionComment {
      replies(first: 100, after: $after) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          databaseId
          url
          body
          createdAt
          updatedAt
          isAnswer
          author { login url __typename }
          authorAssociation
          replyTo { id }
        }
      }
    }
  }
}
"""


def _graphql_user(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "login": value.get("login"),
        "html_url": value.get("url"),
        "type": value.get("__typename"),
    }


def _discussion_reply(value: dict[str, Any]) -> dict[str, Any]:
    reply_to = value.get("replyTo") if isinstance(value.get("replyTo"), dict) else {}
    return {
        "id": value.get("id"),
        "database_id": value.get("databaseId"),
        "author": _graphql_user(value.get("author")),
        "author_association": value.get("authorAssociation"),
        "created_at": value.get("createdAt"),
        "updated_at": value.get("updatedAt"),
        "html_url": value.get("url"),
        "is_answer": bool(value.get("isAnswer")),
        "reply_to_id": reply_to.get("id"),
        "body": value.get("body") or "",
    }


def _discussion_comment(value: dict[str, Any], replies: list[dict[str, Any]]) -> dict[str, Any]:
    comment = _discussion_reply(value)
    comment["replies"] = [_discussion_reply(item) for item in replies]
    return comment


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
    discussion = artifact.get("discussion")
    if discussion:
        lines.extend(
            [
                f"- Category: {discussion.get('category') or 'unknown'}",
                f"- Answered: {'yes' if discussion.get('is_answered') else 'no'}",
                f"- Answer chosen at: {discussion.get('answer_chosen_at') or 'not answered'}",
                f"- Answer chosen by: {_author_label(discussion.get('answer_chosen_by'))}",
            ]
        )

    lines.extend(["", "## Original report", "", source.get("body") or "[No body]"])

    comments = artifact["comments"]
    comment_label = "Discussion comments" if discussion else "Issue comments"
    reply_count = sum(len(comment.get("replies") or []) for comment in comments)
    count_label = (
        f"{len(comments)} top-level, {reply_count} replies" if discussion else str(len(comments))
    )
    lines.extend(["", f"## {comment_label} ({count_label})", ""])
    if not comments:
        lines.append(f"[No {comment_label.lower()}]")
    for comment in comments:
        lines.extend(
            [
                (
                    f"### {_author_label(comment.get('author'))} — "
                    f"{comment.get('created_at') or 'unknown date'}"
                ),
                "",
                f"[Open comment]({comment.get('html_url')})" if comment.get("html_url") else "",
                "",
                comment.get("body") or "[No body]",
                "",
            ]
        )
        for reply in comment.get("replies") or []:
            lines.extend(
                [
                    (
                        f"#### Reply from {_author_label(reply.get('author'))} — "
                        f"{reply.get('created_at') or 'unknown date'}"
                    ),
                    "",
                    f"[Open reply]({reply.get('html_url')})" if reply.get("html_url") else "",
                    "",
                    reply.get("body") or "[No body]",
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
                    (
                        f"### {_author_label(review.get('author'))} — "
                        f"{review.get('state') or 'unknown state'} — "
                        f"{review.get('submitted_at') or 'unknown date'}"
                    ),
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
                    (
                        f"### {_author_label(comment.get('author'))} — "
                        f"{comment.get('created_at') or 'unknown date'}"
                    ),
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


def _capture_discussion(
    target: Target,
    dest: str | Path,
    *,
    client: GitHubClient,
    now: datetime | None,
) -> dict[str, Any]:
    """Capture every top-level discussion comment and nested reply."""
    after: str | None = None
    raw_pages: list[dict[str, Any]] = []
    raw_reply_pages: list[dict[str, Any]] = []
    raw_comments: list[dict[str, Any]] = []
    normalized_comments: list[dict[str, Any]] = []
    discussion_record: dict[str, Any] | None = None
    repository_name = f"{target.owner}/{target.repo}"
    expected_comments: int | None = None
    expected_replies = 0
    captured_replies = 0

    while True:
        response = client.graphql(
            DISCUSSION_QUERY,
            {
                "owner": target.owner,
                "repo": target.repo,
                "number": target.number,
                "after": after,
            },
        )
        raw_pages.append(response)
        repository = response["data"].get("repository")
        if not isinstance(repository, dict):
            raise CaptureError(f"GitHub repository {repository_name} was not found")
        repository_name = repository.get("nameWithOwner") or repository_name
        discussion = repository.get("discussion")
        if not isinstance(discussion, dict):
            raise CaptureError(f"GitHub discussion {repository_name}#{target.number} was not found")
        if discussion_record is None:
            discussion_record = discussion
        elif discussion.get("id") != discussion_record.get("id"):
            raise CaptureError("GitHub GraphQL pagination changed discussions mid-capture")

        connection = discussion.get("comments")
        if not isinstance(connection, dict):
            raise CaptureError("GitHub discussion did not include a comments connection")
        total_count = connection.get("totalCount")
        if not isinstance(total_count, int):
            raise CaptureError("GitHub discussion did not declare its comment count")
        if expected_comments is None:
            expected_comments = total_count
        elif total_count != expected_comments:
            raise CaptureError("GitHub discussion comment count changed during capture")

        nodes = connection.get("nodes")
        if not isinstance(nodes, list):
            raise CaptureError("GitHub discussion comments were not returned as a list")
        for raw_comment in nodes:
            if not isinstance(raw_comment, dict):
                raise CaptureError("GitHub discussion returned an invalid comment")
            reply_connection = raw_comment.get("replies")
            if not isinstance(reply_connection, dict):
                raise CaptureError("GitHub discussion comment omitted its replies")
            reply_total = reply_connection.get("totalCount")
            reply_nodes = reply_connection.get("nodes")
            if not isinstance(reply_total, int) or not isinstance(reply_nodes, list):
                raise CaptureError("GitHub discussion returned invalid reply pagination")
            replies = [item for item in reply_nodes if isinstance(item, dict)]
            if len(replies) != len(reply_nodes):
                raise CaptureError("GitHub discussion returned an invalid reply")

            reply_page_info = reply_connection.get("pageInfo") or {}
            reply_after = reply_page_info.get("endCursor")
            while reply_page_info.get("hasNextPage"):
                reply_response = client.graphql(
                    DISCUSSION_REPLIES_QUERY,
                    {"id": raw_comment.get("id"), "after": reply_after},
                )
                raw_reply_pages.append(reply_response)
                node = reply_response["data"].get("node")
                next_connection = node.get("replies") if isinstance(node, dict) else None
                if not isinstance(next_connection, dict):
                    raise CaptureError("GitHub discussion reply page was incomplete")
                if next_connection.get("totalCount") != reply_total:
                    raise CaptureError("GitHub discussion reply count changed during capture")
                next_nodes = next_connection.get("nodes")
                if not isinstance(next_nodes, list) or not all(
                    isinstance(item, dict) for item in next_nodes
                ):
                    raise CaptureError("GitHub discussion returned an invalid reply page")
                replies.extend(next_nodes)
                reply_page_info = next_connection.get("pageInfo") or {}
                reply_after = reply_page_info.get("endCursor")

            if len(replies) != reply_total:
                raise CaptureError(
                    "refusing a partial capture: GitHub reports "
                    f"{reply_total} replies to comment {raw_comment.get('id')} "
                    f"but the API returned {len(replies)}"
                )
            expected_replies += reply_total
            captured_replies += len(replies)
            raw_comments.append({"comment": raw_comment, "replies": replies})
            normalized_comments.append(_discussion_comment(raw_comment, replies))

        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not after:
            raise CaptureError("GitHub discussion pagination omitted its next cursor")

    if discussion_record is None or expected_comments is None:
        raise CaptureError("GitHub discussion capture returned no record")
    if len(raw_comments) != expected_comments:
        raise CaptureError(
            "refusing a partial capture: GitHub reports "
            f"{expected_comments} discussion comments but the API returned {len(raw_comments)}"
        )

    retrieved_at = (now or datetime.now(UTC)).astimezone(UTC)
    retrieved_text = retrieved_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    state = "closed" if discussion_record.get("closed") else "open"
    category = (
        discussion_record.get("category")
        if isinstance(discussion_record.get("category"), dict)
        else {}
    )
    answer = (
        discussion_record.get("answer") if isinstance(discussion_record.get("answer"), dict) else {}
    )
    source = {
        "kind": "discussion",
        "repository": repository_name,
        "number": discussion_record.get("number") or target.number,
        "title": discussion_record.get("title") or f"{repository_name} #{target.number}",
        "html_url": discussion_record.get("url") or target.input_url,
        "author": _graphql_user(discussion_record.get("author")),
        "author_association": discussion_record.get("authorAssociation"),
        "state": state,
        "state_reason": discussion_record.get("stateReason"),
        "locked": bool(discussion_record.get("locked")),
        "created_at": discussion_record.get("createdAt"),
        "updated_at": discussion_record.get("updatedAt"),
        "closed_at": discussion_record.get("closedAt"),
        "body": discussion_record.get("body") or "",
        "labels": [],
    }
    discussion_details = {
        "category": category.get("name"),
        "category_slug": category.get("slug"),
        "category_is_answerable": bool(category.get("isAnswerable")),
        "is_answered": bool(discussion_record.get("isAnswered")),
        "answer_id": answer.get("id"),
        "answer_url": answer.get("url"),
        "answer_chosen_at": discussion_record.get("answerChosenAt"),
        "answer_chosen_by": _graphql_user(discussion_record.get("answerChosenBy")),
    }
    artifact: dict[str, Any] = {
        "schema_version": "flip.github-capture/2",
        "input_url": target.input_url,
        "canonical_url": source["html_url"],
        "source": source,
        "comments_complete": True,
        "comments_expected": expected_comments,
        "replies_complete": captured_replies == expected_replies,
        "replies_expected": expected_replies,
        "comments": normalized_comments,
        "discussion": discussion_details,
        "pull_request": None,
        "reviews": [],
        "review_comments": [],
        "timeline": [],
        "pagination": {
            "comment_pages": len(raw_pages),
            "reply_pages": len(raw_reply_pages),
            "timeline_pages": 0,
            "review_pages": 0,
            "review_comment_pages": 0,
        },
        "raw_file": "raw.json.gz",
    }
    artifact["text"] = _render_markdown(artifact)

    output = Path(dest)
    output.mkdir(parents=True, exist_ok=True)
    (output / "capture.json").write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    raw_payload = {
        "discussion_pages": raw_pages,
        "reply_pages": raw_reply_pages,
        "comments": raw_comments,
    }
    (output / "raw.json.gz").write_bytes(
        gzip.compress(
            (json.dumps(raw_payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            mtime=0,
        )
    )
    envelope = {
        "flip": {
            "title": source["title"],
            "canonical_url": source["html_url"],
            "retrieved_at": retrieved_text,
            "strategy": "publisher-api",
            "status": "success",
            "mime": "application/json",
            "user_agent": USER_AGENT,
            "backend_ref": f"github:{repository_name}#discussion-{source['number']}",
        }
    }
    (output / "flip.json").write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    return artifact


def capture(
    value: str,
    dest: str | Path,
    *,
    client: GitHubClient | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Capture one issue, pull request, or discussion without truncating its thread."""
    target = parse_target(value)
    client = client or GitHubClient(
        token=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    )
    if target.route_kind == "discussions":
        return _capture_discussion(target, dest, client=client, now=now)
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
        raw_pull_value, _ = client.get(f"/repos/{target.owner}/{target.repo}/pulls/{target.number}")
        if not isinstance(raw_pull_value, dict):
            raise CaptureError("GitHub API returned an unexpected pull-request record")
        raw_pull = raw_pull_value
        raw_reviews, review_pages = client.all_pages(
            f"/repos/{target.owner}/{target.repo}/pulls/{target.number}/reviews"
        )
        raw_review_comments, review_comment_pages = client.all_pages(
            f"/repos/{target.owner}/{target.repo}/pulls/{target.number}/comments"
        )

    retrieved_at = (now or datetime.now(UTC)).astimezone(UTC)
    retrieved_text = retrieved_at.strftime("%Y-%m-%dT%H:%M:%SZ")
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
        "schema_version": "flip.github-capture/2",
        "input_url": value,
        "canonical_url": source["html_url"],
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
        "raw_file": "raw.json.gz",
    }
    artifact["text"] = _render_markdown(artifact)

    output = Path(dest)
    output.mkdir(parents=True, exist_ok=True)
    capture_path = output / "capture.json"
    sidecar_path = output / "flip.json"
    capture_path.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    raw_payload = {
        "issue": issue,
        "pull_request": raw_pull,
        "comments": raw_comments,
        "reviews": raw_reviews,
        "review_comments": raw_review_comments,
        "timeline": raw_timeline,
    }
    (output / "raw.json.gz").write_bytes(
        gzip.compress(
            (json.dumps(raw_payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            mtime=0,
        )
    )
    envelope = {
        "flip": {
            "title": source["title"],
            "canonical_url": source["html_url"],
            "retrieved_at": retrieved_text,
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
        description="Capture a complete GitHub issue, pull request, or discussion for Flip.",
    )
    parser.add_argument("url", nargs="?", help="GitHub issue, pull-request, or discussion URL")
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
    reply_count = sum(len(comment.get("replies") or []) for comment in artifact["comments"])
    reply_label = (
        f" and {reply_count} repl{('y' if reply_count == 1 else 'ies')}" if reply_count else ""
    )
    sys.stderr.write(
        f"flip-github: captured {source['kind'].replace('_', ' ')} "
        f"{source['repository']}#{source['number']} at state {source['state']} "
        f"with {len(artifact['comments'])} comment(s){reply_label}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
