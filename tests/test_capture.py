from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from flip_github.cli import CaptureError, GitHubClient, capture


class Response:
    def __init__(self, payload, headers=None):
        self.payload = json.dumps(payload).encode("utf-8")
        self.headers = headers or {}

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FixtureOpener:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request.full_url, timeout))
        try:
            return self.routes[request.full_url]
        except KeyError as error:
            raise AssertionError(f"unexpected request: {request.full_url}") from error


def issue_record(*, comments, state="open", pull=False):
    record = {
        "number": 7,
        "title": "Suspend drains the battery",
        "html_url": "https://github.com/acme/widget/issues/7",
        "repository_url": "https://api.github.com/repos/acme/widget",
        "comments_url": "https://api.github.com/repos/acme/widget/issues/7/comments",
        "comments": comments,
        "state": state,
        "state_reason": "completed" if state == "closed" else None,
        "user": {"login": "owner", "html_url": "https://github.com/owner", "type": "User"},
        "body": "Suspend is broken in the original report.",
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-26T00:00:00Z",
        "closed_at": "2026-08-26T00:00:00Z" if state == "closed" else None,
        "labels": [],
    }
    if pull:
        record["pull_request"] = {"url": "https://api.github.com/repos/acme/widget/pulls/7"}
    return record


class CaptureTests(unittest.TestCase):
    def test_closed_issue_keeps_paginated_owner_fix_comment(self):
        first_page = "https://api.github.com/repos/acme/widget/issues/7/comments?per_page=100"
        second_page = (
            "https://api.github.com/repos/acme/widget/issues/7/comments?per_page=100&page=2"
        )
        opener = FixtureOpener(
            {
                "https://api.github.com/repos/acme/widget/issues/7": Response(
                    issue_record(comments=2, state="closed")
                ),
                first_page: Response(
                    [
                        {
                            "id": 1,
                            "user": {"login": "helper"},
                            "created_at": "2026-08-10T00:00:00Z",
                            "body": "I can reproduce this.",
                        }
                    ],
                    {"Link": f'<{second_page}>; rel="next"'},
                ),
                second_page: Response(
                    [
                        {
                            "id": 2,
                            "user": {"login": "owner"},
                            "created_at": "2026-08-26T00:00:00Z",
                            "body": "Fixed for me after the Quattro update.",
                        }
                    ]
                ),
                "https://api.github.com/repos/acme/widget/issues/7/timeline?per_page=100": Response(
                    [
                        {
                            "id": 3,
                            "event": "closed",
                            "created_at": "2026-08-26T00:00:00Z",
                            "actor": {"login": "owner"},
                        }
                    ]
                ),
            }
        )
        client = GitHubClient(opener=opener)
        with tempfile.TemporaryDirectory() as tmp:
            artifact = capture(
                "https://github.com/acme/widget/issues/7",
                tmp,
                client=client,
                now=datetime(2026, 9, 2, tzinfo=timezone.utc),
            )
            sidecar = json.loads((Path(tmp) / "flip.json").read_text())

        self.assertEqual(artifact["source"]["state"], "closed")
        self.assertTrue(artifact["comments_complete"])
        self.assertIn("Fixed for me after the Quattro update.", artifact["text"])
        self.assertEqual(sidecar["flip"]["strategy"], "publisher-api")

    def test_merged_pull_request_keeps_reviews_and_inline_comments(self):
        base = "https://api.github.com/repos/acme/widget"
        issue = issue_record(comments=0, state="closed", pull=True)
        pull = {
            "merged": True,
            "merged_at": "2026-08-30T00:00:00Z",
            "draft": False,
            "base": {"label": "acme:main"},
            "head": {"label": "owner:fix-suspend"},
            "changed_files": 1,
        }
        opener = FixtureOpener(
            {
                f"{base}/issues/7": Response(issue),
                f"{base}/issues/7/comments?per_page=100": Response([]),
                f"{base}/issues/7/timeline?per_page=100": Response(
                    [{"event": "merged", "actor": {"login": "maintainer"}}]
                ),
                f"{base}/pulls/7": Response(pull),
                f"{base}/pulls/7/reviews?per_page=100": Response(
                    [{"id": 10, "user": {"login": "reviewer"}, "state": "APPROVED"}]
                ),
                f"{base}/pulls/7/comments?per_page=100": Response(
                    [{"id": 11, "user": {"login": "reviewer"}, "body": "Looks good."}]
                ),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            artifact = capture(
                "https://github.com/acme/widget/pull/7",
                tmp,
                client=GitHubClient(opener=opener),
                now=datetime(2026, 9, 2, tzinfo=timezone.utc),
            )

        self.assertEqual(artifact["source"]["state"], "merged")
        self.assertEqual(artifact["reviews"][0]["state"], "APPROVED")
        self.assertEqual(artifact["review_comments"][0]["body"], "Looks good.")

    def test_comment_count_mismatch_refuses_partial_capture(self):
        base = "https://api.github.com/repos/acme/widget"
        opener = FixtureOpener(
            {
                f"{base}/issues/7": Response(issue_record(comments=2)),
                f"{base}/issues/7/comments?per_page=100": Response(
                    [{"id": 1, "user": {"login": "owner"}, "body": "Only one page landed."}]
                ),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(CaptureError, "refusing a partial capture"):
                capture(
                    "https://github.com/acme/widget/issues/7",
                    tmp,
                    client=GitHubClient(opener=opener),
                )
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()

