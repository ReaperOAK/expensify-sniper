# GitHub API behaviours that each produced a silent "no bounties"

Measured 2026-08-08/09. Every one of these shipped as a bug first and was caught by running
against live data, not by tests.

## 1. There is a secondary rate limit, and the budget guard cannot see it

A 403 arrived with `X-RateLimit-Remaining: 23` of 30 **unspent**. GitHub's secondary
(burst) guard fires on request *rate*, independent of the documented pools. Its
`X-RateLimit-Reset` header describes the PRIMARY window and is meaningless for deciding
when to retry — the body says only "wait a few minutes".

Detect with `"secondary rate limit" in body`. `RateLimited` carries `.secondary` and
`.retry_after` for this reason.

## 2. `per_page=100` trips it on the first request

Reproducibly, on a label query with a negation. `per_page=50` and `per_page=30` returned
200 seconds later. GitHub weights the guard by how expensive a query is to serve. **At
`per_page=100` the tracker returned an empty digest on every single run.** Hence
`config.SEARCH_PER_PAGE = 50`.

## 3. A throttle mid-pagination must not discard completed pages

Letting `RateLimited` propagate out of the paging loop binned three pages of real
candidates and printed `top 0 by score` — an empty world reported because of a throttle.
Partial results now return with `incomplete=True`. A throttle on page one still raises:
there is nothing to salvage, and an empty result would be indistinguishable from "no
bounties matched".

## 4. Secondary limits are worth waiting out; primary ones are not

A secondary limit clears in minutes. Abandoning a query over one cost **420 of 520
candidates** and produced a digest where 21 of 22 entries came from a single repo. Retry
with doubling backoff (60/120/240s), honouring `Retry-After` when present. A primary limit
needs the whole window to roll — do not block a run on it.

## 5. Search results carry no repo metadata, and no ETag

A search item has only `repository_url` — no nested `repository` object — so `fork`,
`archived`, `stargazers_count` and `language` all require a separate core call. That is why
rejection is split into a free stage and a post-enrichment stage.

Search responses also carry **no `ETag`**, so every page costs quota and there is nothing to
revalidate. Core responses do carry one, and a `304` leaves `X-RateLimit-Remaining`
completely unchanged.

## 6. `/issues/events` is the cheapest watch GitHub offers

Core pool (5000/hr, not search's 30/min), ETag support, and it surfaces a label landing on
an issue that already exists — which `created` ordering never would.

**One page holds 100 events.** That covered a single DAY on `go-gitea/gitea` and 5h18m on
`Expensify/App`. Poll faster than a repo's event velocity or labels roll off the page
unseen. The original sniper polled Expensify every 20 seconds for this reason; the watch
runs every 10 minutes.
