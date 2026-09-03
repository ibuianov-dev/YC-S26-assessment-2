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
