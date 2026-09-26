# stage0.md: Rebuild Stage 0 (00 · Find Brands) around the new scraper design

Owner: Claude Code (Fable master, implementer + verifier on Opus 5). Scraper work: Haider.
Written: 25 Sep 2026 from the live instance. Read together with calls.md (Step 1 there is still urgent).
Status: PLAN APPROVED. Build in the order below. Stop at every STOP.

---

## 0. The target (Umer's words, 25 Sep 2026)

Stage 0: Brand Sourcing
1. Claude generates new unique DTC keywords.
2. FB scraper returns Brand Name, Website URL, Active Ads, Facebook Page URL.
   - Up to 8 scraper lanes running in parallel.
   - Discard any brand not running 50+ ads.
   - Discard any keyword that does not have 25+ active ads in US or UK.
3. Claude API call checks whether it is an ecom brand.
Bottom line: a verified ecom brand list.

### Umer's decisions (25 Sep 2026)
- **Keyword gate:** a keyword passes if it has **25+ active ads in the US OR 25+ in the UK**.
  It fails only if BOTH the US and the UK search came back under 25.
- **Canada, Australia, New Zealand:** keep them, but **only for keywords that passed the US/UK gate**.
- **Brands under 50 ads:** remember them and **check again after 30 days**.
- **Proxies from the start (25 Sep 2026):** all Facebook traffic from the scraper goes through
  proxies, including lane 1. The VPS's own IP is no longer used to talk to Facebook at all.
  Reason: the same service also serves the page lookups for 01 and 02, so a block on the VPS IP
  would stop the 50+ check and the research ads as well, not just brand searching.

### Unchanged (do not touch)
- The existing exhaustion rule (a keyword searched in 2+ countries with 0 new brands is
  exhausted) stays exactly as it is for keywords that pass the gate.
- Claude keyword generation and the Claude ecom check: working well, leave them. If the
  new gate starves the keyword queue, report it (see STOP 3); do not edit the prompt yourself.
- The 200/day target and the ledger. The bank/buffer logic.
- The 50+ threshold means LIVE ads (`number_of_ads > 49` on an active-only page lookup),
  exactly as 01 uses today.

### Standing rules
1. Verify from the live instance. Re-check every fact below before building on it.
2. Never trigger Apollo spend without Umer's yes (01 calls Apollo on qualified brands).
3. Rollback sticky note before every edit. Plain-language reports to Umer.

---

## 1. Facts found on 25 Sep 2026 (re-verify)

- 00 (`CtyWWHDoi0316VU7`) runs hourly at :45, 40 keyword-country pairs per run, ONE AT A TIME
  (`Loop Over Keywords`, batch size 1). Countries in order: US, GB, CA, AU, NZ, tracked by
  `countries_done` in the keyword table `cFy24pA43KZBEYNl`. So US and UK are already the first
  two searches of every keyword. The gate fits the existing order.
- Search body: `activeStatus: "active"`, `mediaType: "all"`, `maxItems` from `Select Keyword And Country`.
  Searches returned at most 30 ads in exec 4192.
- Exec 4192: only **5 of 40** searches returned 25+ ads. **27 of 40 returned `ads_returned: 1,
  ads_usable: 0`**. Find out what that single item is (probably a "no results" placeholder from
  the service). A placeholder must count as 0 ads, never 1.
- 00 already saves each new brand's `page_id`, page URL and name to Brand Facebook Pages
  (`MBqEUXa1TdjEdNJn`) via `Save Brand Page`. The ad that produced the brand's domain is the
  same ad that names the page, so page and domain are linked by the search itself.
- Page-id count lookup is proven: exec 2723 (`scraper-testing`, lane ADY 01), 53 brands,
  counts matched the domain lookup on 51/51 shared pages, gate agreement 100%.
- About half of brands fail 50+ in 01 today (last 24h: 77 qualified, 75 disqualified).
- **Outage bug:** on 24 Sep the scraper returned zero ads for every search in 6 of 7 hourly runs
  (about 10:45 to 17:50 UTC, telemetry `apify_items: 0`, `apify_errors: 0`), then errored on
  34 and 39 of 40 searches (runs at 22:05 and 22:46 UTC). `Mark Keyword Used` advanced
  `countries_done` anyway and marked **about 109 keywords `exhausted`** (85 in the first window,
  24 in the second; e.g. "nursing scrubs set women", "modal trunk underwear men").

---

## 2. Build order

### Step 1: Honest search status (prerequisite for everything else)

**Haider (scraper):** every search result carries
`status: "ok" | "no_ads" | "blocked" | "error"` and `ads_found` (real ads only, no placeholders).
"no_ads" is only allowed when Facebook actually answered with zero results.

**Haider (scraper), same step: move to proxies.**
- Every request the service makes to Facebook (keyword searches AND the `/adyntel` page lookups
  used by 00, 01 and 02) goes through a proxy. Nothing goes out on the VPS IP.
- One stable IP per lane ("sticky" session), kept for the life of that lane's browser session,
  rather than a new IP on every request. A browser session that keeps changing IP looks worse to
  Facebook, not better.
- Prefer residential or ISP proxies in the US or UK. Datacenter proxies are cheaper but are the
  kind Facebook blocks first.
- When a proxy IP gets blocked: mark it, swap it for a fresh one, retry the search. Report
  `blocked` only after 3 tries on different IPs.
- Log per IP: requests, blocks, average response time. This is what the ramp in Step 4 is judged on.
- Before going live: re-run the 53-brand page-count harness (lane ADY 01, exec 2723) and one
  normal 40-search run through the proxy. Counts and results must match the VPS-IP results.

**STOP 0.** Before Haider buys anything: provider, proxy type, and monthly cost for 1 lane now
and for 8 lanes later. Umer approves the cost.

**Claude Code (n8n, 00):**
1. `Extract Dedupe And Filter` / `Mark Keyword Used`: only `ok` and `no_ads` update a keyword
   (countries_done, counters, status). `blocked` / `error` leave the keyword untouched so it is
   searched again later.
2. Run-level outage guard: if more than half of a run's searches are `blocked`/`error`, or the
   whole run returned zero ads, write nothing to the keyword table, skip keyword top-up for that
   run, and log `outage_run: 1` in telemetry so the Pipeline Health Check can alert on it.
3. Until Haider ships `status`, apply the same guard on what exists today: a run with
   `apify_items == 0` across all 40 searches, or errors on more than half, is an outage.
4. Restore the keywords wrongly retired on 24 Sep: rows with `status = exhausted` whose last
   `used_at` falls in the two outage windows (note `used_at` is stored with a +02:00 offset:
   filter 12:45 to 19:55 and 23:45 to 00:50 local). Set them back to `unused` and roll
   `countries_done` back by one. Record the list of ids in STATE.md. No Apollo is involved.

**Test:** replay exec 3809 (an outage run) against the new logic in `scraper-testing`: zero
keyword writes. Replay exec 4192 (a normal run): identical keyword writes to what happened live.

### Step 2: The 50+ brand check moves into 00

No scraper change needed: this uses the existing `POST /adyntel` with `page_id`.

1. New order after `Drop Ad Farms`:
   dedupe (known domains + reject list) -> **page count by `page_id`** -> drop under 50
   -> `Fetch Brand Homepage` -> `Claude: Verify DTC` -> append.
   Claude and the homepage fetch must only see brands with 50+ live ads.
2. Count call: `{ "page_id": "<id from the search ad>" }`, no `active_status`, no `media_type`
   (same live count 01 uses). Gate: `number_of_ads > 49`.
3. Under 50: write to `Sourcing Rejects` with `reason: under_50_ads`, the count and
   `checked_at`. `Index Known Domains` must treat these as known **only for 30 days**, then let
   the brand through again if it reappears in a search.
4. Count lookup failed (blocked/error/timeout): do NOT reject. Keep the brand in a small retry
   table and try it on the next run. Three failures -> log it and move on, do not reject.
5. Append rows with `Ad Count` filled (02 reads it for `live_ad_count`), plus page id and URL.
6. Telemetry: `count_calls`, `count_errors`, `brands_under_50`, `brands_50_plus`.

**Change in 01 (`KPIhCvLKtyMYZR40`):** rows appended by the new 00 already passed 50+.
01 must skip its own ad count for rows whose `Ad Count` was written by 00 in the last 30 days,
and go straight to the next step. Older rows keep today's lookup. This replaces calls.md Step 2
for new brands.

**Test (no Apollo):** new lane in `scraper-testing` with 40 brands 01 decided in the last 48h.
Run the 00-style page-id count. Pass: same 50+ decision as 01 recorded for every brand (explain
any flip). Then run the new 00 once with the append step pointed at a scratch sheet tab.

**STOP 1.** Tell Umer before going live: 01 will now receive only 50+ brands, and as volume rises
towards 200/day, **Apollo spend in 01 rises with it** (roughly 2.5x today's qualified volume
at full target). Get his yes.

### Step 3: The 25+ keyword gate (US or UK)

1. `maxItems` for US and UK searches must be at least 25 (30 or more is fine).
2. Store per keyword: `us_ads`, `gb_ads`, `gate_status` (`pending` / `pass` / `fail`).
3. After a keyword's US search (`status` ok or no_ads):
   - `us_ads >= 25` -> `gate_status = pass`.
   - otherwise stay `pending` and search the UK next.
4. After the UK search: `gb_ads >= 25` -> `pass`. If both are under 25 -> `gate_status = fail`,
   keyword retired with reason `under_25_us_uk`. Never fail a keyword on a blocked/error search.
5. Canada, Australia, New Zealand are only searched for `pass` keywords.
6. Brands found in the US/UK searches still flow through Step 2 even if the keyword later fails
   (the search already happened, its brands are real).
7. Keywords already past their US/UK searches keep their current state (no ad counts were stored
   for them). The gate applies to searches from go-live on.
8. The existing exhaustion rule stays for `pass` keywords.
9. Telemetry: `gate_pass`, `gate_fail`, `gate_pending`, pass rate per run.

**STOP 2.** Before go-live, show Umer the expected impact: exec 4192 suggests only about 1 in 8
searches reaches 25+, so most current keywords will fail the gate and the queue will empty faster.

**STOP 3.** After 2 days live, report the gate pass rate and keyword-queue level. If the queue is
running dry, bring it to Umer. Changing the keyword prompt (broader terms) is his decision.

### Step 4: Up to 8 parallel scraper lanes (Haider, then n8n)

**WHY THE LANES EXIST: MORE PRODUCTION, NOT JUST SPEED.** The goal is more searches per day, so
more brands with 50+ ads reach the sheet, towards 200 a day. Using the lanes only to finish
today's 40 searches faster is a failure of this step. Every lane added must raise the number of
searches per run (or runs per day), and that must show up as more brands appended per day
(see Step 5). Report success as brands per day, not run time.

**Scraper (Haider):**
1. A batch endpoint: n8n sends one job with all of a run's searches (and later the page counts);
   the scraper spreads them over N lanes and returns every result with its `status`. Prefer
   submit + poll (job id) over one long request.
2. Each lane has its own identity: its own sticky proxy IP (from Step 1) and its own browser
   session. Lanes never share an IP.
3. Per-lane pacing; when a lane gets blocked it cools down and its search is retried on another
   lane (max 3 tries), then reported as `blocked`, never as `no_ads`.
4. `GET /health`: lanes up / cooling / blocked, block rate in the last hour. 00 checks it at the
   start of each run; if most lanes are blocked, skip the run and alert (no keyword writes).
5. Also look at the ~40 second pause seen on every 4th search, and the 24 Sep logs for
   ~10:45-17:50 UTC and ~22:00-22:50 UTC. Report the cause.

**n8n (Claude Code):** replace the one-at-a-time loop with: build batch -> submit -> wait/poll
-> results, keeping each result paired to its keyword and country (the 20 Sep pairing defect in
`Build DTC Prompt` is the warning: assert counts and keys match).

**Ramp:** 1 -> 2 -> 4 -> 8 lanes. At each level run at least 24h and compare block rate, searches
per hour and brands per hour. Only go up if the block rate stays low.

**STOP 4.** Each ramp step adds proxy IPs and cost. Get Umer's yes before each step that raises
the monthly proxy bill.

### Step 5: Size the run to the 200/day target
Rough estimate from 25 Sep: about 4 searches per appended brand today; with half failing 50+,
about 8 per brand that reaches the sheet, so about 1,700 searches a day plus about 400 page
counts for 200/day. Today's ceiling is about 960 searches a day. After Step 4, set searches per
run (or run frequency) from measured lane throughput. Do not add daily caps; the existing 200/day
ledger already stops runs once the target is met.

---

## Outcome notes (Claude Code, 25 Sep 2026)

**Step 1 (proxies) and Step 4 (lanes), scraper side: built on branch `lanes` (v0.4.0), deployed as a second container for testing, production untouched.** Every Facebook request now runs on a lane; a lane is one sticky DataImpulse port (one exit IP), its own cookie jars, its own 20/min limiter, its own throttle memory and mint breaker. The VPS address is never used. Blocked lanes cool down and move to a fresh port; a refused search is retried on another lane, never the same one twice, three tries, then reported `blocked`, never `no_ads`. `POST /jobs` takes a whole run (searches and counts) and `GET /jobs/{id}` polls for results paired by id. `/health` lists every lane, its exit IP, state, block rate over the last hour and per-IP counters. Details: `docs/architecture.md` (Lanes), `docs/api.md` (Batch jobs), `docs/deployment.md` (The lanes container).

**The `status` field (Step 1):** every job result and every `/facebook` answer now says `ok | no_ads | blocked | error` (`X-Status` header; `no_ads` only when Facebook's own page said 0). The n8n side of Step 1 (only `ok`/`no_ads` update a keyword, the outage guard, restoring the keywords retired on 24 Sep) is not built yet.

**Step 4 item 5, the ~40 s pause on every 4th search:** it was the service's own rate limiter at its cutover setting `RATE_LIMIT_PER_MIN=4`, which makes a fifth GET inside a minute wait ~45 s ("every 4th call" is that); production has run 20 since 22 Sep. What stayed slow after that is a withheld search recovering over GraphQL to the 8-page cap at 2 to 5 s a page, about 40 s; lanes page GraphQL at 1 to 2 s. **The 24 Sep windows:** 10:45 to 17:50 UTC Meta withheld the ad payload from the VPS address and the service answered `200 []` (fixed the same day, commit `fa544d9`); 22:05 and 22:46 UTC were GraphQL `1675004` refusals with repeated session mints (commit `32d6d8d`). The container logs for those hours are rotated away (10 MB × 3); the evidence is the commit history and 00's telemetry.

**Sticky ports, measured 25 Sep 2026:** DataImpulse ports 11510 and 11511 each kept one IP across three checks and differed from each other; port 823 changed IP on every call; production's 11500 has its own. Rotation interval to be set to 120 min in the dashboard (Haider).

**First 2-lane test run, 25 Sep 2026 15:19-15:24 UTC (container `facebook-ad-library-lanes:8003`, ports 11510 + 11511, production untouched):**

| Test | Result |
|---|---|
| Search: the 40 pairs of 00's 13:45 run as one job (scraper-testing exec 4327) | 40 of 40 back, ids match, 24 ok / 16 no ads / 0 blocked / 0 errors, **84 s** end to end (production took 5 min 55 s for the same pairs); no pair came back empty where production had ads; 2 broad pairs gave the page's 30 ads where production's throttled-then-recovered path had paged to 71 and 80; 1 ad of 176 without a page id or caption; median 4.1 s a search; both exits used; 25 MB decoded |
| Counts: the ADY 01 brand set as one job of 51 page counts (exec 4329) | 51 of 51 back, 50 found / 0 not found / 1 blocked / 0 errors, 121 s; 34 of 45 within the 3-day-drift tolerance of the Adyntel baselines and **all 11 outside it match production's own count in the same minute** (e.g. snapmaker 960, cryptozoic 106, nordicpirates 66); 3 gate flips, all real drift (48→51, 52→47, 33→66); the 1 blocked page (paversongames, 5 ads reported, none served on either lane) is withheld from production too (503 there) |
| Lanes after both runs | block rate 2.2 % (2 withheld tries of 92), 0 rotations, 0 IP changes, 2 sessions minted, 2 challenges, 0 errors; ~0.7 MB decoded per request (the proxy bills the wire, about a fifth) |

Bar for the run (plan): met, with the two notes above (30 vs 71/80 ads on broad keywords is the rendered page's own limit, the same limit production has when it is not throttled; the withheld page is Meta's, not the lane's).

**Second run, 8 lanes, 200 pairs, 25 Sep 2026 15:43-15:46 UTC (same container, `LANE_COUNT=8`, ports 11510-11517, `JOB_ITEM_MAX_WAIT_S=1800`; Haider asked for more keywords and 8 lanes; production untouched):**

| Test | Result |
|---|---|
| Search: the 200 pairs of 00's last five hourly runs (execs 4233-4315, 10:45-14:45 UTC) as one job (scraper-testing exec 4338) | 200 of 200 back, ids match, 115 ok / 84 no ads / 1 blocked / 0 errors, **141 s** end to end (production spent five hours' worth of runs, ~30 min of scraping, on the same pairs); no pair empty where production had ads; 1,223 ads against production's 1,317 usable (the gap is the two broad pairs at the page's 30-ad limit again, 30 vs 80 and 30 vs 71, plus small drift on 10 pairs); 44 of 1,223 ads without a page id or caption; median 4.6 s, p90 6.7 s, 140 MB decoded; 7 of 8 lanes carried 27-30 searches each |
| The 1 blocked pair | `oral motor chew tool` NZ: Meta reports 5 ads and served none on three different exits; production got nothing usable for it either |
| Lane 5 | its exit (a US RCN address) failed the TLS handshake three times, the lane cooled for 120 s, its 3 searches were retried on other lanes and all came back ok, and it returned to `up` after the run; 4 retries in total, 3 withheld + 3 errors of 205 tries, block rate 1.5 % |

Throughput seen: about 85 searches a minute on 8 lanes against 00's 40 an hour today. A cut 2-lane attempt at the same 200 pairs (exec 4335) was stopped by the container restart when the lane count went to 8 and is not a data point.

**STOP 0 / STOP 4 (cost):** bytes scale with requests, not lanes. At about 0.19 MB on the wire per rendered request, today's ~990 requests a day are ~0.2 GB ≈ $0.20/day; the 200-a-day target (1,700 searches + 400 counts) ~0.45 GB ≈ $0.45/day; eight lanes flat out ~0.8 GB ≈ $0.80/day. Roughly $6 to $25 a month before DataImpulse's minimum top-up. The 2-lane test run checks the per-request figure against the account balance.

## 3. Rollout and reporting
- One step live at a time, each with a rollback note and 24h of watching telemetry.
- STATE.md after every step: what changed, proving execution ids, what is next.
- Final report to Umer in plain words: brands per day at 50+, searches per brand, gate pass
  rate, block rate per lane count, and cost.