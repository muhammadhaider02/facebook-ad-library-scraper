# Fixtures

Saved from the live site on 2026-09-19 and trimmed so the tests run offline against what Meta
actually sends. Tokens are scrubbed; nothing here identifies a session.

| File | What it is | How it was made |
|---|---|---|
| `challenge_403.html` | the `403 Client challenge` page the first GET on a fresh jar gets, verbatim (481 B) | saved as-is |
| `ssr_ads.html` | the search page with its results blob: 3 edges, 5 Shakti Mat ads, for `acupressure mat for back pain` / NZ | the `RelayPrefetchedStreamCache` blob was cut out of the 848 KB laptop page, its edges cut to the first three, its cursor replaced, and wrapped in a minimal page with an `LSD` token blob |
| `ssr_empty.html` | the same page shape with an empty `search_results_connection` (what an exhausted keyword gets) | the blob above with `edges: []`, `count: 0`, `has_next_page: false` |
| `ssr_miss.html` | the ~573 KB page shape that has no results blob at all | a minimal page with the `LSD` token blob and one unrelated `ScheduledServerJS` blob |
| `html_200.html` | a `200` that is not the Ad Library page (no `LSD` token) | hand-written |

Regenerate the three `ssr_*.html` files from a fresh page with:

```bash
uv run facebook-ad-library diag --query "acupressure mat for back pain" --country NZ --save-dir diag-out
```

then cut the blob that contains `search_results_connection` out of `diag-out/page1_ads.html` and
trim its `edges` as above. The tests pin the counts (3 edges, 5 ads, first caption
`shaktimat.com`, page id `775991435791863`), so update them if the trim changes.
