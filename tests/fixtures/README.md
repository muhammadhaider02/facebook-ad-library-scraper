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

Page views and the vanity resolvers, saved 2026-09-22 from the laptop (the raw pages are under `diag-out/adyntel-probe/`):

| File | What it is | How it was made |
|---|---|---|
| `page_view_ads.html` | the `view_all_page_id` page of Muscle Mat (105396194411046): `count` 1783, 4 ads in 4 edges chosen to carry an IMAGE, a VIDEO and two DCO ads with card videos, captions in mixed case with paths and `.com.au` / `.co.nz` | the page's record blob (`page_name`, `page_is_deleted`) and its results blob were cut out, the edges picked, and both wrapped in the minimal page with the `LSD` blob |
| `page_view_video.html` | Shakti Mat's page view with `media_type=video`: `count` 489, 14 ads in 2 edges, every `display_format` VIDEO | same cut, first two edges |
| `page_view_zero.html` | a known page with no ads: the record kept, `count: 0`, `edges: []` | synthesised from `page_view_ads.html` |
| `page_view_unknown.html` | the page view of a page id Meta does not know (1234): `count: 0`, `{"page_info": null}` in every copy | cut from the saved page |
| `keyword_mixed_owners.html` | the keyword search for `gymshark.com`: 31 ads from six advertisers (Gymshark 22, Planet Fitness 4, two Instagram-caption pages, Gymshark Women, and one Salty Dagger ad with a `null` caption), stripped to the fields the resolver reads | the results blob's edges with each ad reduced to id, page, dates, caption, link URL, like count and card links; the null-caption ad appended from Salty Dagger's page view |
| `plugin_page.html` | the public page plugin for `facebook.com/shaktimats`: the link `facebook.com/775991435791863?ref=embed_page` | a 600-byte window around that link, with the `LSD` blob |
| `plugin_unknown.html` | the plugin for a handle that does not exist (`shaktimat`): renders, carries the token blob, has no `?ref=embed_page` link | hand-written from the saved 19 KB page's shape |
| `profile_page.html` | the profile page of `shaktimats`: `"delegate_page":{"id":"775991435791863"}` plus the `userID` / `al:android:url` decoy (100064593973677, the user id the Ad Library does not know) | a 600-byte window around `delegate_page`, the decoy and the `LSD` blob |
| `profile_unknown.html` | the "content isn't available" page for a handle that does not exist: token blob, no `delegate_page` | hand-written from the saved page's shape |
| `profile_wall.html` | a login wall: no token blob at all | hand-written |

Regenerate the page views with `diag --page-id <id> [--status all] [--media video] --save-dir diag-out` and the plugin and profile pages with `diag --slug <handle> --save-dir diag-out`, then cut as above.

Regenerate the three `ssr_*.html` files from a fresh page with:

```bash
uv run facebook-ad-library diag --query "acupressure mat for back pain" --country NZ --save-dir diag-out
```

then cut the blob that contains `search_results_connection` out of `diag-out/page1_ads.html` and
trim its `edges` as above. The tests pin the counts (3 edges, 5 ads, first caption
`shaktimat.com`, page id `775991435791863`), so update them if the trim changes.
- `page_view_withheld.html` - the throttle's signature: a page view whose total says 1039 ads
  and whose `edges` are empty. Meta serves this to an address it is withholding from, with no
  403 and no 429. Made from `page_view_zero.html` by raising the count, because the two differ
  in exactly that one field and telling them apart is the whole point.
