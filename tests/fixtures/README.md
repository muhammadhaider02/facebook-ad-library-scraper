# Fixtures

Saved from the live Ad Library on 2026-09-19 via `facebook-ad-library diag --save-dir diag-out`
and trimmed by hand so the suite stays small and offline. Refresh them the same way if Meta
changes the page: run the diag, then cut the new dumps down as below.

| File | What it is | How it was trimmed |
|---|---|---|
| `challenge_403.html` | the 481-byte `403 Client challenge` page, verbatim | not trimmed |
| `bootstrap_trimmed.html` | the 200 search page cut to a ±160-char window around each `TOKEN_PATTERNS` match, plus three real bundle `<script src>` tags | token values replaced with obviously fake ones of the same shape |
| `bundle_snippet.js` | the three-module shape around `AdLibrarySearchPaginationQuery_facebookRelayOperation` | real module text; one neighbour rewritten in the 2025 `"use strict";e.exports` form so both variants are pinned |
| `search_page1.json` | a GraphQL body with three real collated results and a next cursor | ads beyond the third dropped; the envelope (`data.ad_library_main.search_results_connection`) rebuilt around them |
| `search_page2_last.json` | same, `has_next_page: false` | same |
| `search_empty.json` | a body with no edges | synthetic |
| `rate_limited_1675004.json` | the throttle body from facebook.md §4.3 | synthetic; capture a real one when it happens |
| `data_null.json` | `data: null` with a non-throttle error code | synthetic |
| `html_200.html` | a 200 whose body is a login page | synthetic |

`diag --save-dir` also writes `search_page<n>_raw.json`, the untouched GraphQL body; use it to
rebuild the two search fixtures when the envelope changes.
