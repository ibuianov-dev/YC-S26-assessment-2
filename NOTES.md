# NOTES

## Pre-auth request observations
There is an api endpoint that returns 300 assets of marketing campaigns. It was creater by a merketer (supposedly ai agent that can hallucinate and give 'successful' output when it's not valid). These are assets of different types: landing pages, emails, forms, CTAs. Some assets belong to others (maybe by IDs or nested structure. unclear until the response data is seen): a form to a landing page, a CTA to an email. Those assets are needed to be pushed but the child can't be pushed unless its parent is already there. 

---

Each asset must end up in the destination exactly once. No missing assets, and worse, no duplicates.

---

The final submission must follow this format. It's flat - status is either deployed or blocked, and blocked needs a reason. Since it's flat, the parent dependency only ever shows up as text inside the reason field, never as its own structure.
```json
{
  "report": {
    "as-0001": { "status": "deployed" },
    "as-0002": { "status": "blocked", "reason": "<why>" },
    ...
  }
}
```

Once this is built, one POST with this as the payload seals the assignment - done, no undo. Only one shot at it. Important: I can test my report-building logic and check the destination state as much as I want beforehand (with reads), but I can NOT test the /s2/report call itself - any real call to it burns the one shot. So the confidence has to come from checking the destination directly, not from probing the report endpoint.


---

Set User-Agent to Mozilla/5.0 (compatible; cq-screen-client/1.0; +candidate-submission) - Cloudflare rejects a few default client strings (notably Python's stdlib urllib) with an opaque error, this dodges it.

---

### Presumed architecutre
Splitting this into a few files so each one only does one job:

- `client.py` - the only place that actually talks to the API. Sets the auth header and user-agent, retries with backoff, and turns every response into one of: success, conflict (409 - means it already exists, that's actually a good sign not a failure), rate limited (429/503 - wait and retry), client error (other 4xx - don't retry, it's a real no), or ambiguous (timeout, dropped connection, random 5xx - don't know what actually happened, can't guess, have to go read to find out).

- `journal.py` - keeps track of every asset's state locally in a sqlite file, so if I crash mid-run I don't lose where I was. Records that I'm about to write BEFORE actually sending it, not after - so even a crash mid-request leaves a trail telling me "go check this one, don't just resend blind." Main rule baked in here: nothing can be marked as actually confirmed-deployed except through a read that proves it. A write response by itself is never enough, no matter how clean it looks.

- `reconcile.py` - the one place that turns a read into a real answer - confirmed / absent / duplicate. Used both while deploying and again right before the report goes out, so there's only one definition of "proof" anywhere in this thing, not two slightly different ones.

- `deploy.py` - the actual runner. Sorts the 300 so parents always go before their kids, then just loops: resolve anything that came back ambiguous, write whatever's actually safe to write, repeat until nothing's left to do or the clock says stop.

- `report.py` - builds the report from the journal, but re-checks everything one more time with a fresh read right before it does. Only "confirmed" or "duplicate" can turn into "deployed" in the output - everything else is "blocked," even the stuff that's probably fine, because probably isn't the bar here. Actually sending it is a separate, on-purpose step - can't happen by accident.

- `mock_server.py` - fake flaky version of the real thing, just to practice on before the clock starts. Randomly hangs, sometimes lies with an error after it already saved something, sometimes says "not found" for a second on something that's really there. Whole point is getting used to that before it's real and burning my two hours.

## Findings after investigating api

- Auth is two steps, not the one header the brief implied. POST /auth/start with just X-Candidate-Key gets back an access_token, expires_in (300s, confirmed twice), and run_minutes_remaining - that field only shows up here, not on other endpoints, and it's real, saw it count down from 120 to 108. Every other request needs BOTH X-Candidate-Key and Authorization: Bearer <token> together, confirmed by testing with only one - it fails. Token dies in 5 min, has to refresh way more than once

- Catalog is GET /s2/assets, paginated 25 at a time - send back whatever next_cursor gave you as ?cursor=N, stop when next_cursor is null. Each item: id, kind, title, checksum, approved_at, depends_on

- checksum still unexplained - just gets echoed back on a successful write, no idea if it's actually validated for anything.

- Catalog list is static, doesn't reflect what's deployed. Checked as-0001's record again after deploying it identical to before, no flag, nothing. Don't bother re-checking this route for status.

- Write endpoint is POST /s2/deploy, NOT /s2/assets - that 404s no matter what verb or shape I tried. Body needs the field called asset_id, not id (found out from a 400 error naming it exactly). Success is 201 with asset_id, checksum, and a timestamp called "at" in epoch ms.

- Plain resend with no special header - correctly 409s as duplicate_write_refused. So a blind retry with nothing extra is already safe by default for the case where you actually got the original response.

- The hint on that 409 points to a header, X-CQ-Idempotency-Key, "to make retries safe." Tested it - and typed it wrong on purpose by accident (as-001 instead of as-0001). Got a fresh 201 back instead of a 409, new timestamp, same asset_id. That means dedup is scoped to the exact key value, not to asset_id - a new/mistyped key = treated as a brand new write, even for something already deployed. Pretty sure this created a real second copy of as-0001.

- No way to confirm the actual count - there's no read-verify endpoint at all. Tried GET /s2/assets/{id}, GET /s2/deploy/{id}, GET /s2/deploy?asset_id={id} - all 404 regardless of real state. Verification has to happen through the write endpoint's own response (409/201/timeout), there's nothing else to check against.

- Rule this sets for the other 299: idempotency key has to be one fixed, deterministic value per asset (just use the asset_id itself), generated once, same every single retry - never regenerated, never hand-typed per-attempt. That's the only thing that actually makes a retry safe here.

- as-0001 goes in the final report as a plain "deployed" - that's true, it's confirmed present (two separate 201-shaped responses said so), and reporting it "blocked" would be false and would invite an operator to redeploy it, risking a real third copy. The report schema has no field for "deployed, but I suspect it's duplicated" - it isn't built to carry that, and it shouldn't be gamed into pretending it can. The duplicate-count uncertainty (possibly two copies, caused by my own mistyped key during discovery, can't confirm the real count either way since there's no read endpoint) is disclosed only in NOTES.md's self-critique section instead. That split - true-but-incomplete in the report, the full story in NOTES.md - is the operator-safe choice: it's what keeps the report itself honest (never asserts something it can't back up) without silently absorbing a mistake into a false "blocked", which would cost a redeploy the destination doesn't need.

- Tested the safe-retry theory for real before trusting it on the other 298 - picked as-0002 (untouched), deployed it once with a fixed idempotency key equal to its own asset_id, got 201. Immediately resent the identical request, same key, same body - got 200 back with the exact same "at" timestamp and an explicit "replayed": true field. Confirmed, not just theorized: one fixed key per asset, generated once and reused on every retry, is actually safe. The as-0001 incident was the key being wrong on retry, not the mechanism itself being broken

- Accidentally reused as-0002's key while writing as-0012 - got as-0002's exact original data replayed back, as-0012 never got written, as-0002 untouched. Confirms the full model: no key = checks asset_id, blocks real dupes. Used key = always replays original, ignores new body - safe but silent no-op if content differs. New key = no asset_id check at all, creates for real - the dangerous case, what caused as-0001's dupe. Key must be fixed per asset AND never shared - using asset_id itself as the key does both

- Tested a real child write (as-0012, parent as-0001 already deployed, correct key) - clean 201. Dependency mechanic confirmed for real. Discovery closed, rest is just running the loop on 297 assets


# Final overview before submitting

## When is this entitled to say "deployed"?

Only when a response proves it for *that exact asset_id*: a fresh 201, a replayed 200, or a 409 - and only if the response body itself echoes back that asset_id, not just the status code (a 2xx for a *different* asset_id, from a reused key, is not proof - proven on as-0012). No response, or one that can't be interpreted (timeout, dropped connection, unhandled 5xx), is never treated as deployed on the strength of "probably went through."

## Does the code live up to that?

Yes, with one disclosed exception. `report.py` only emits `"deployed"` for a journal row that reached that proof; everything else - client error, still-ambiguous at hard-stop, parent never confirmed - is `"blocked"` with a reason, even when it's probably fine.

The exception is **as-0001**: two separate 201-shaped responses (one real, one under a mistyped key during manual discovery) both prove this asset_id exists, so it's reported `"deployed"` - that's true. What can't be proven, because there is no read endpoint anywhere on this API, is whether that's one copy or two. Reporting it `"blocked"` would be false and would invite a redeploy that could create a real third copy; the report schema has no field for "deployed, but maybe twice." So: deployed in the report, count uncertainty disclosed here instead. This is the one place the code's guarantee is narrower than "exactly once" - and it's narrower because of a mistake made during discovery, before the automated run existed, not a gap in the run itself.

The other 297 rely on one fixed, deterministic key (`asset_id` itself) proven safe by controlled test (as-0002, as-0012), not assumed.

## If I'd had more time

- `mock_server.py` was planned, never built - the remaining discovery (as-0002, as-0012) happened live against real assets instead of rehearsed, mitigated by being small, controlled, single-asset tests.
- No unit tests for `reconcile.py`'s state machine - confidence comes from those two experiments matching the model, not from exercising it directly.