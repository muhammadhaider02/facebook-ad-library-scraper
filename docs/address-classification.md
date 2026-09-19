# Why the VPS gets `1675004`, and what works from our own infrastructure

Measured 19 Sep 2026, 08:45 to 09:05 UTC, on commit `ba3ef30`. Every request body, page and header set is saved under `diag-out/address-classification-2026-09-19/` (laptop) and `/opt/fb-diag/` (VPS); the probe that produced them is `fbprobe.py` in the same folder. No proxy of any kind was used. n8n was not touched.

## The answer

**One bootstrap GET per keyword-country pair, no GraphQL.** The Ad Library page Meta serves to the VPS already contains the first 30 ads of the search as server-rendered JSON, with every field Stage 0 reads. That GET is not throttled from the VPS: 41 of them in a row at 3 to 4 s spacing, all `200`, no challenge, no `1675004`. The GraphQL endpoint, which is what the service currently paginates with, is refused from every address the VPS has, with any session, fresh or after 10 minutes idle. The service was changed from "GET once, then GraphQL three times" to "GET once, read the page" the same day; [architecture.md](architecture.md) describes what runs now.

## Results

| # | Test | Source address | Jar minted on | Bootstrap page | Requests before first `1675004` | Ads | Notes |
|---|---|---|---|---|---|---|---|
| 0 | GraphQL control | laptop, residential v4 | laptop | 848 KB, 30 SSR ads | none in 10 | 84 | 10 of 10 searches OK, ~1 s each |
| 1 | GraphQL, laptop jar replayed | VPS v4 `2.25.219.125` | laptop | n/a | **0** (call 1 refused, 46 ms) | 0 | same jar gave 10/10 from the laptop; Meta scores every call by source address, not by session |
| 1b | GraphQL, fresh own jar | VPS v4 | VPS | 840 KB, 30 SSR ads | **0** | 0 | bootstrap, challenge, 9 tokens and `doc_id` all fine |
| 2 | GraphQL over IPv6 | VPS v6 `2a02:4780:95:b033::1` | VPS over v6 | 844 KB, 30 SSR ads | **0** | 0 | v6 reaches Facebook (`200`, remote `2a03:2880:…`); same refusal, 53 to 66 ms |
| 2b | Other addresses in the /64 | `…b033::2`, `…b033:1234:5678:9abc:def0` | | | | | **not routed**: added on `eth0`, connections time out. Hostinger routes only `::1` to this VM; the `/48` mask is cosmetic. Removed again |
| 3 | Page variants by locale and country | VPS v4 | | NZ 1.27 MB, DE 1.22 MB (`Werbebibliothek`), US 1.10 MB, GB 1.18 MB | | 30 each | size tracks the ads rendered, not the address; no consent markers; VPS geolocates to Boston, US (AS47583), not the EU |
| 4a | Same jar after 10 min idle | VPS v4 | VPS | | **0** | 0 | |
| 4b | Same jar after 30 and 60 min idle | VPS v4 | VPS | | not run | | the scheduled retest was stopped and removed on request before it fired; 4a is the only idle measurement |
| 4d | **SSR only: 20 bootstrap GETs, 20 keywords** | VPS v4 | VPS | 0.57 to 1.74 MB | none in 20 | **345** | 15 pages with ads (up to 30 each), 5 with no data blob; no challenge on any |
| 4e | SSR only: retry of the 3 no-data keywords, ×3 | VPS v4 | VPS | | none in 9 | 171 | 6 of 9 carried ads; the miss is per request, not per keyword |
| 4f | SSR only: the 2 exhausted keywords, ×3 | VPS v4 | VPS | 582 KB | none in 6 | 0 | 5 of 6 pages carried an **empty** result blob (`search_results_connection` present, no edges); 1 was a no-data miss. GraphQL from the laptop also gives 0 for both |
| 4g | SSR only, laptop control | laptop v4 | laptop | | none in 10 | 194 | ad counts per keyword identical to the VPS run (30/30/30/29/21/24/12/14/2/2), same first advertisers |
| 5a | Legacy `/ads/library/async/search_ads/` | laptop and VPS | | | | | `404 Not Found` for POST and GET, both addresses. Endpoint is gone |
| 5b | `first` = 10 / 30 / 60 / 100 | laptop v4 | laptop | | | 10 each | identical 217 KB body for all four; Meta ignores `first` and returns ~10 edges. Not our parameter |

## What each test established

**Session versus address (1, 1b, 4a).** A jar minted on the laptop is refused on the VPS at call one; the same jar keeps working on the laptop. A jar minted on the VPS is refused at call one and is still refused after 10 minutes idle. The refusal takes 30 to 70 ms, which is an edge decision, not a backend one. `1675004` is keyed on the source address, checked on every GraphQL call, and the bucket for this address is empty before we send anything.

**IPv6 (2, 2b).** The v6 address is a working, separate path to Facebook and gets exactly the same refusal, so the key is at least as wide as Hostinger's allocation, not the single v4 address. Rotating inside the /64 is not possible on this VPS: only `::1` is routed.

**Page variant (3).** There is no consent or EU variant. There are two page shapes from every address, including the laptop: the ~573 KB page, which has no `RelayPrefetchedStreamCache` blob and therefore no ads, and the 0.8 to 1.7 MB page, which carries the search's first 30 results as a prefetched Relay stream. The headers are identical between laptop and VPS except round-trip timings. Which shape you get is decided per request on Meta's side, roughly 1 in 4 misses on both machines; a second GET usually carries the data.

**The three page outcomes are distinguishable**, which is what makes SSR usable:

| Page | Size | `search_results_connection` | `RelayPrefetchedStreamCache` | Meaning |
|---|---|---|---|---|
| ads | 0.6 to 1.7 MB | present, with edges | present | the result; up to 30 ads |
| empty | ~582 KB | present, no edges | present | the keyword really has no ads; do not retry |
| miss | ~573 KB | absent | absent | Meta skipped the prefetch; retry once |

**Fields (4d, 4g).** The SSR ad objects are the same shape as the GraphQL `collated_results` nodes. Run through the existing `mapping.to_item` unchanged, 30 of 30 items carry `page_id`, `page_name`, `page_profile_uri`, `page_category`, `page_like_count`, `snapshot.caption`, `snapshot.link_url` and `snapshot.page_like_count`; `page_alias` is `""` as it is on the GraphQL path and was from the actor. The page also carries `page_info.end_cursor`, which is only useful with GraphQL and therefore not useful here.

**Volume (4d, 5b).** SSR gives up to 30 ads per GET. GraphQL gives about 10 per call whatever `first` says, so the current three-call design also tops out at 30. Nothing is lost by dropping GraphQL, and the productive versus exhausted split in fb.md §6 is reproduced: 30/30/30/29 for the productive keywords, 2/2 for the thin ones, 0 with an explicit empty blob for the two exhausted ones.

## Design that works from our own infrastructure

Built as described here, the same day: one page GET per pair, parse the `RelayPrefetchedStreamCache` blobs for ad nodes, map them exactly as now, and treat the three page shapes as above: ads → `200` with items, empty → `200 []`, miss → one retry after the normal spacing, then `200 []` with a counter. The session, challenge and cookie handling stay as they are; the GraphQL client, `doc_id` discovery and the bundle download (7.6 MB per mint) become dead code and go. Measured cost from the VPS: 0.6 to 1.7 MB and 0.3 to 2.4 s per GET; Stage 0's 40 pairs an hour plus about 25% retries is roughly 50 GETs and 50 MB an hour from the VPS, inside Hostinger's included transfer. The 41 consecutive GETs measured today had no challenge and no throttle; the rate to use in production is the current `RATE_LIMIT_PER_MIN=4`, and `/health` will show whether a challenge ever reappears mid-session.

No other option was needed. The remaining zero-cost alternatives, for the record and not to be built: scheduling the searches from the laptop or any residential machine we own and pushing results to the VPS (works, per test 0, but ties production to a machine that is not always on); a second VPS with a different provider's address range, which is a bet that the range is not throttled, with the CI runner's result as evidence against it. Neither is needed while the SSR path holds.
