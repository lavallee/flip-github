---
name: github-evidence
description: Capture and interpret GitHub issues and pull requests when current state, comments, reviews, fixes, regressions, or release status matter.
---

# github-evidence

Use GitHub discussions as evolving records. The opening report is not the
current answer.

1. Run `flip config show` and confirm the named `github` web fetcher is the
   `flip-github {url} {dest}` adapter.
2. Start or resume a Flip session before the research sweep.
3. Capture a new source with
   `flip add-source <github-url> --kind web --via github`, or refresh existing
   custody with `flip source recheck <source-id> --via github`.
4. Open `capture.json`. Confirm `comments_complete` is true, then read the
   original body, current state, every issue comment, pull-request reviews and
   inline comments when present, and the lifecycle timeline. Do not grade or
   cite the source from the opening body alone.
5. Classify what the whole record establishes:
   - `open` and `closed` are repository states, not conclusions;
   - call an issue fixed only when the discussion, linked change, or owner
     follow-up establishes the fix;
   - a merged pull request establishes that code entered its base branch, not
     that a release containing it reached users;
   - distinguish duplicate, declined, stale, cannot-reproduce, workaround,
     fixed, released, and owner-confirmed working outcomes;
   - keep conflicting later reports visible as possible regressions.
6. Record the capture date in any current-status claim. Before publication,
   recheck GitHub sources whose state affects the reader's answer.
7. Grade only after reading the complete capture, update dependent claims, add
   a reopen condition tied to source or release changes, and finish with
   `flip doctor`.

The adapter preserves current public API responses. It cannot recover deleted
or pre-edit content, prove that a commenter is correct, or turn repository
closure into evidence that a fix shipped.

