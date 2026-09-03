# Technical screen 2: the deploy

**Two hours, and the clock is real.** It starts on your first
authenticated request — not when this email arrived — so reading the
brief, thinking, and planning cost you nothing. Every response tells
you how long is left (`run_minutes_remaining`). At two hours the run
seals and is graded on whatever is actually in the platform at that
moment.

Nothing is hidden about this and nothing is sprung on you. Plan around it.

## What this is

A marketer has approved **300 assets** — landing pages, emails, forms,
CTAs — and asked for them to be pushed into their account. Push them.

    base url: https://cq-screen.bhairav.workers.dev
    your key: `my key`

Same header, same scoping: `X-Candidate-Key` on every request, your
own account, your own score.

## Two things you need to know

**One: the work has a shape.** Some assets belong to others — a form
to a landing page, a CTA to an email — and the destination will not
accept a child before its parent is really there. Fanning 300 writes
out flat and retrying until the errors stop will eventually converge,
but it is not the cheapest or the clearest way through, and the report
you have to write at the end is easier if you understood the shape
first.

**Two: each asset must end up in the destination exactly once.**

Not at least once. Exactly once. The provider does not make this easy,
and it is entitled not to: this is a stand-in for HubSpot, which will
happily accept the same email twice and give you two of them.

A write that does not answer you has not necessarily failed.

## What you send back

```json
{
  "report": {
    "as-0001": { "status": "deployed" },
    "as-0002": { "status": "blocked", "reason": "<why>" },
    ...
  }
}
```

`POST /s2/report`. It seals — you get one.

## How it is scored

| | |
|---|---|
| an asset in the destination exactly once | **+1** |
| a dependent asset, in exactly once | **+2** |
| an asset missing | −1 |
| each extra copy of an asset | **−5** |

A duplicate costs five times what a miss costs. That ratio is not
arbitrary. An operator told "not deployed" goes and deploys it. An
operator told "deployed once" who actually has two live landing pages
competing for the same traffic finds out from their analytics a week
later, and by then we have spent their budget twice.

Your own report is not evidence. The score comes from what is in the
destination, and the two are allowed to disagree — that disagreement
is the single most useful thing this screen measures.

## One practical note

Set a `User-Agent` header. The host sits behind Cloudflare, which
rejects a few default client strings — notably Python's stdlib
`urllib` — with an opaque `error code: 1010`. `requests`, `curl`,
`node-fetch` and anything browser-shaped are fine. This is
infrastructure, not part of the exercise, and we would rather tell you
than watch you debug it.

## Rules

- Nothing is documented. What the provider does when a write goes
  wrong, and what it offers you to make retries safe, are both
  discoverable. Read the responses.
- The provider fails in more than one way, and not all of its failures
  mean what they appear to mean.
- Don't attack the platform. Writing rows you did not earn is visible
  in the log and ends the screen.

## Also send us

- **the code**, with git history;
- **the raw transcript**, dead ends included;
- a short **NOTES.md** answering one question directly: *when is this
  thing entitled to tell an operator a deployment succeeded?* Then say
  whether your own code lives up to that answer, and where it does not.

That last question is the job. The code is how you show your working.