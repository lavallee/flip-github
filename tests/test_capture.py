from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from datetime import UTC, datetime
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


class DiscussionFixtureClient:
    def __init__(self):
        self.calls = []

    def graphql(self, query, variables):
        self.calls.append((query, variables))
        if "DiscussionReplies" in query:
            return {
                "data": {
                    "node": {
                        "replies": {
                            "totalCount": 2,
                            "pageInfo": {"hasNextPage": False, "endCursor": "reply-2"},
                            "nodes": [
                                {
                                    "id": "reply-2",
                                    "databaseId": 12,
                                    "url": "https://github.com/acme/widget/discussions/9#discussioncomment-12",
                                    "body": "The later owner confirms the workaround.",
                                    "createdAt": "2026-09-03T00:00:00Z",
                                    "updatedAt": "2026-09-03T00:00:00Z",
                                    "isAnswer": True,
                                    "author": {
                                        "login": "second-owner",
                                        "url": "https://github.com/second-owner",
                                        "__typename": "User",
                                    },
                                    "authorAssociation": "NONE",
                                    "replyTo": {"id": "comment-1"},
                                }
                            ],
                        }
                    }
                }
            }
        return {
            "data": {
                "repository": {
                    "nameWithOwner": "acme/widget",
                    "discussion": {
                        "id": "discussion-9",
                        "number": 9,
                        "title": "Does suspend work?",
                        "url": "https://github.com/acme/widget/discussions/9",
                        "body": "The opening post says no.",
                        "createdAt": "2026-08-01T00:00:00Z",
                        "updatedAt": "2026-09-03T00:00:00Z",
                        "closed": True,
                        "closedAt": "2026-09-03T00:00:00Z",
                        "stateReason": "RESOLVED",
                        "locked": False,
                        "author": {
                            "login": "owner",
                            "url": "https://github.com/owner",
                            "__typename": "User",
                        },
                        "authorAssociation": "NONE",
                        "category": {
                            "name": "Q&A",
                            "slug": "q-a",
                            "isAnswerable": True,
                        },
                        "isAnswered": True,
                        "answer": {
                            "id": "reply-2",
                            "url": "https://github.com/acme/widget/discussions/9#discussioncomment-12",
                        },
                        "answerChosenAt": "2026-09-03T00:00:00Z",
                        "answerChosenBy": {
                            "login": "owner",
                            "url": "https://github.com/owner",
                            "__typename": "User",
                        },
                        "comments": {
                            "totalCount": 1,
                            "pageInfo": {"hasNextPage": False, "endCursor": "comment-1"},
                            "nodes": [
                                {
                                    "id": "comment-1",
                                    "databaseId": 11,
                                    "url": "https://github.com/acme/widget/discussions/9#discussioncomment-11",
                                    "body": "Try the new release.",
                                    "createdAt": "2026-09-02T00:00:00Z",
                                    "updatedAt": "2026-09-02T00:00:00Z",
                                    "isAnswer": False,
                                    "author": {
                                        "login": "helper",
                                        "url": "https://github.com/helper",
                                        "__typename": "User",
                                    },
                                    "authorAssociation": "CONTRIBUTOR",
                                    "replies": {
                                        "totalCount": 2,
                                        "pageInfo": {
                                            "hasNextPage": True,
                                            "endCursor": "reply-1",
                                        },
                                        "nodes": [
                                            {
                                                "id": "reply-1",
                                                "databaseId": 10,
                                                "url": "https://github.com/acme/widget/discussions/9#discussioncomment-10",
                                                "body": "The first reply is inconclusive.",
                                                "createdAt": "2026-09-02T12:00:00Z",
                                                "updatedAt": "2026-09-02T12:00:00Z",
                                                "isAnswer": False,
                                                "author": {
                                                    "login": "owner",
                                                    "url": "https://github.com/owner",
                                                    "__typename": "User",
                                                },
                                                "authorAssociation": "NONE",
                                                "replyTo": {"id": "comment-1"},
                                            }
                                        ],
                                    },
                                }
                            ],
                        },
                    },
                }
            }
        }


def issue_record(*, comments, state="open", pull=False):
    record = {
        "number": 7,
        "title": "Suspend drains the battery",
        "html_url": (
            "https://github.com/acme/widget/pull/7"
            if pull
            else "https://github.com/acme/widget/issues/7"
        ),
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


def rest_fixture_client(*, pull=False):
    base = "https://api.github.com/repos/acme/widget"
    routes = {
        f"{base}/issues/7": Response(issue_record(comments=0, pull=pull)),
        f"{base}/issues/7/comments?per_page=100": Response([]),
        f"{base}/issues/7/timeline?per_page=100": Response([]),
    }
    if pull:
        routes.update(
            {
                f"{base}/pulls/7": Response(
                    {
                        "merged": False,
                        "draft": False,
                        "base": {"label": "acme:main"},
                        "head": {"label": "owner:fix-suspend"},
                    }
                ),
                f"{base}/pulls/7/reviews?per_page=100": Response([]),
                f"{base}/pulls/7/comments?per_page=100": Response([]),
            }
        )
    return GitHubClient(opener=FixtureOpener(routes))


class CaptureTests(unittest.TestCase):
    def test_repeated_rest_capture_is_byte_identical_but_retrieval_time_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for route_kind in ("issues", "pull"):
                with self.subTest(route_kind=route_kind):
                    first = root / f"{route_kind}-first"
                    second = root / f"{route_kind}-second"
                    first_artifact = capture(
                        f"https://github.com/acme/widget/{route_kind}/7",
                        first,
                        client=rest_fixture_client(pull=route_kind == "pull"),
                        now=datetime(2026, 9, 2, tzinfo=UTC),
                    )
                    second_artifact = capture(
                        f"https://github.com/acme/widget/{route_kind}/7",
                        second,
                        client=rest_fixture_client(pull=route_kind == "pull"),
                        now=datetime(2026, 9, 3, tzinfo=UTC),
                    )

                    self.assertEqual(first_artifact, second_artifact)
                    self.assertEqual(
                        (first / "capture.json").read_bytes(),
                        (second / "capture.json").read_bytes(),
                    )
                    self.assertNotIn("fetched_at", first_artifact)
                    self.assertNotIn("Captured:", first_artifact["text"])
                    self.assertEqual(first_artifact["raw_file"], "raw.json.gz")
                    self.assertTrue((first / "raw.json.gz").is_file())
                    self.assertIn(
                        "Suspend is broken",
                        gzip.decompress((first / "raw.json.gz").read_bytes()).decode(),
                    )
                    self.assertNotEqual(
                        (first / "flip.json").read_bytes(),
                        (second / "flip.json").read_bytes(),
                    )
                    first_sidecar = json.loads((first / "flip.json").read_text())
                    second_sidecar = json.loads((second / "flip.json").read_text())
                    self.assertEqual(
                        first_sidecar["flip"]["retrieved_at"], "2026-09-02T00:00:00Z"
                    )
                    self.assertEqual(
                        second_sidecar["flip"]["retrieved_at"], "2026-09-03T00:00:00Z"
                    )

    def test_repeated_discussion_capture_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first_artifact = capture(
                "https://github.com/acme/widget/discussions/9",
                first,
                client=DiscussionFixtureClient(),
                now=datetime(2026, 9, 2, tzinfo=UTC),
            )
            second_artifact = capture(
                "https://github.com/acme/widget/discussions/9",
                second,
                client=DiscussionFixtureClient(),
                now=datetime(2026, 9, 3, tzinfo=UTC),
            )

            self.assertEqual(first_artifact, second_artifact)
            self.assertEqual(
                (first / "capture.json").read_bytes(),
                (second / "capture.json").read_bytes(),
            )
            self.assertNotEqual(
                (first / "flip.json").read_bytes(),
                (second / "flip.json").read_bytes(),
            )

    def test_discussion_keeps_paginated_replies_and_chosen_answer(self):
        client = DiscussionFixtureClient()
        with tempfile.TemporaryDirectory() as tmp:
            artifact = capture(
                "https://github.com/acme/widget/discussions/9",
                tmp,
                client=client,
                now=datetime(2026, 9, 3, tzinfo=UTC),
            )

        self.assertEqual(artifact["source"]["state"], "closed")
        self.assertTrue(artifact["discussion"]["is_answered"])
        self.assertEqual(artifact["replies_expected"], 2)
        self.assertEqual(len(artifact["comments"][0]["replies"]), 2)
        self.assertIn("later owner confirms", artifact["text"])

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
                now=datetime(2026, 9, 2, tzinfo=UTC),
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
                now=datetime(2026, 9, 2, tzinfo=UTC),
            )

        self.assertEqual(artifact["source"]["state"], "merged")
        self.assertEqual(artifact["reviews"][0]["state"], "APPROVED")
        self.assertEqual(artifact["review_comments"][0]["body"], "Looks good.")

    def test_unrelated_nested_repository_metadata_does_not_change_pull_capture(self):
        first_client = rest_fixture_client(pull=True)
        second_client = rest_fixture_client(pull=True)
        pull_url = "https://api.github.com/repos/acme/widget/pulls/7"
        first_client.opener.routes[pull_url] = Response(
            {
                "merged": False,
                "draft": False,
                "base": {"label": "acme:main", "repo": {"stargazers_count": 10}},
                "head": {"label": "owner:fix-suspend", "repo": {"updated_at": "first"}},
            }
        )
        second_client.opener.routes[pull_url] = Response(
            {
                "merged": False,
                "draft": False,
                "base": {"label": "acme:main", "repo": {"stargazers_count": 11}},
                "head": {"label": "owner:fix-suspend", "repo": {"updated_at": "second"}},
            }
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            capture(
                "https://github.com/acme/widget/pull/7",
                first,
                client=first_client,
                now=datetime(2026, 9, 2, tzinfo=UTC),
            )
            capture(
                "https://github.com/acme/widget/pull/7",
                second,
                client=second_client,
                now=datetime(2026, 9, 3, tzinfo=UTC),
            )

            self.assertEqual(
                (first / "capture.json").read_bytes(),
                (second / "capture.json").read_bytes(),
            )
            self.assertNotEqual(
                (first / "raw.json.gz").read_bytes(),
                (second / "raw.json.gz").read_bytes(),
            )

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
