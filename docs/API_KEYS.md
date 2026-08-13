# CandyVoice API — API Keys

How a signed-in user gets a long-lived API key for their own application,
how that key behaves differently from a normal Firebase-session call, and
the staff-only endpoints for managing keys and reporting on usage across
all users. Companion to
[`API.md`](API.md) — start there for the processing endpoints themselves;
this doc only covers what's specific to keys.

## Why a key, and how it differs from your Firebase session

The website itself keeps using Firebase auth exactly as today — nothing
changes there. A key is for the case where a user wants to call the
processing endpoints from **their own backend/app**, outside the browser
session, without a human re-logging-in on every call.

Calling with a key instead of a Firebase bearer token changes processing
behavior, not just how you authenticate:

| | Firebase session | API key |
|---|---|---|
| Quota (`files_used`/`max_files`) | Flat lifetime cap (10 files/feature, no reset) | **Per-key, per calendar month, per plan** — see [Plans](#plans--limits) |
| Rate limit | 5 req/60s, shared across all your Firebase-session calls | Per-key, per plan (5–100 req/60s) — see [Plans](#plans--limits) |
| Response shape | JSON envelope, `output_url` download link (file kept ~1h) | **Raw audio bytes as the response body** (`audio/wav`), metadata in headers; no file kept on the server — see [below](#using-a-key-against-the-processing-endpoints) |
| Processed-outputs history / Zoho sync | Saved | **Skipped entirely** — nothing is persisted or synced |
| Uploaded input file | Kept briefly (Zoho attachment flow) | **Deleted immediately** after the response is built |

In short: a key is for "this is my own application's data, processed and
handed straight back" — nothing about the request is retained
server-side once the response is sent, but usage still counts against
that key's plan.

## Plans & limits

Chosen when the key is created (see `POST /api/keys` below) and shown on
the pricing page. Numbers are current provisional defaults, not a stable
contract — treat the `limit`/`rate_limit` fields returned by the API
itself (creation response, [usage endpoint](#get-apikeyskey_idusage)) as
the source of truth, the same way `API.md` already tells you to for the
Firebase-session limits.

| Plan | Files / month / tool | Rate limit |
|---|---|---|
| `starter` | 50 | 5 req/60s |
| `pro` | 500 | 20 req/60s |
| `enterprise` | Unlimited (custom limits: contact us) | 100 req/60s |

"Per tool" means each processing endpoint (noise filter, imitation, frame
recovery, deepfake detection) has its own independent monthly allowance —
using up noise filter doesn't touch your imitation allowance. Usage resets
to 0 at the start of each calendar month (UTC), per key.

> **Trust boundary today:** `plan` on `POST /api/keys` is currently
> whatever the client sends — there's no payment verification behind it
> yet, since checkout itself is still a mockup. Before this goes live as a
> paid product, plan assignment needs to move behind a server-verified
> event (e.g. a Stripe webhook fired after checkout), otherwise nothing
> stops a caller from just requesting `"enterprise"`. Until then, treat
> self-service plan selection as provisional/demo behavior.

## Getting a key (self-service, from your frontend)

These endpoints take the **same Firebase ID token** your frontend already
sends everywhere else — a key can only ever be created for or scoped to
the signed-in user's own account, there's no way to touch anyone else's
key through these.

### `POST /api/keys`

Creates a new key for the signed-in user, on the plan they picked on the
pricing page.

| Header | Required | Notes |
|---|---|---|
| `Authorization` | required | `Bearer <firebase_id_token>` |

Body:

```json
{ "label": "my mobile app", "plan": "pro" }
```

Both fields are optional — `label` defaults to `null`, `plan` defaults to
`"starter"`. `plan` must be one of `starter` / `pro` / `enterprise`; an
unrecognized value is a `400`.

```bash
curl -X POST https://api.candyvoice.com/api/keys \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"label": "my mobile app", "plan": "pro"}'
```

```json
{
  "ok": true,
  "key_id": "3f9c2a...e01b",
  "api_key": "cvk_wK8h2s9F3n...Qz",
  "label": "my mobile app",
  "plan": "pro"
}
```

> **`api_key` is shown exactly once.** Only its hash is stored server-side
> — there's no "forgot my key" recovery, only revoke-and-create-a-new-one.
> Show it to the user once, in a copy-to-clipboard box, and tell them to
> save it somewhere safe (a password manager, their own app's secrets
> store). Treat it like a password: never log it, never put it in a URL.

### `GET /api/keys`

Lists the signed-in user's own keys — metadata only, the raw key is never
returned again after creation.

```bash
curl https://api.candyvoice.com/api/keys \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN"
```

```json
{
  "ok": true,
  "keys": [
    {
      "key_id": "3f9c2a...e01b",
      "label": "my mobile app",
      "plan": "pro",
      "createdAt": "2026-08-12T10:03:00Z",
      "lastUsedAt": "2026-08-12T14:22:11Z",
      "revoked": false
    }
  ]
}
```

Use this to render an API-keys settings page: label, plan badge, creation
date, last-used date, and a revoke button per row. There's no "last 4
characters" of the key to show, since the raw value was never stored —
`label` (set at creation) is what the user relies on to tell keys apart.
Link each row to the usage endpoint below for the actual quota bars.

### `GET /api/keys/{key_id}/usage`

Current-billing-period usage and limits for one key — everything a usage
dashboard needs, in one call.

```bash
curl https://api.candyvoice.com/api/keys/3f9c2a...e01b/usage \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN"
```

```json
{
  "ok": true,
  "key_id": "3f9c2a...e01b",
  "label": "my mobile app",
  "plan": "pro",
  "revoked": false,
  "period": "2026-08",
  "usage": {
    "noiseFilter": { "filesUsed": 12, "limit": 500 },
    "imitation": { "filesUsed": 0, "limit": 500 },
    "deepfake": { "filesUsed": 3, "limit": 500 },
    "frameRecovery": { "filesUsed": 0, "limit": 500 }
  },
  "rate_limit": { "max_requests": 20, "window_seconds": 60 }
}
```

`limit` is `null` for an unlimited (`enterprise`) plan. `period` is the
current UTC calendar month (`YYYY-MM`) — usage under a past period isn't
exposed through this endpoint today, only the live one.

### `POST /api/keys/{key_id}/revoke`

Revokes one of the signed-in user's own keys immediately. Any subsequent
call made with it gets a `401`.

```bash
curl -X POST https://api.candyvoice.com/api/keys/3f9c2a...e01b/revoke \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN"
```

```json
{ "ok": true, "key_id": "3f9c2a...e01b", "revoked": true }
```

A `key_id` that doesn't exist, is already revoked, or belongs to a
different user all return the same `404 API key not found` — a user can't
use this to probe whether some other account's key exists.

## Admin (staff-only)

For internal tooling — a support/ops dashboard, not the customer-facing
frontend. Gated by the same `ADMIN_EMAILS` allow-list as
`/api/admin/send-report`: send a Firebase ID token belonging to an admin
account as `Authorization: Bearer ...`, same as any other admin route.
Unlike everything above, these act on *any* user's keys, not just the
caller's own.

### `POST /api/admin/api-keys`

Issues a key for an arbitrary `uid` — e.g. provisioning one on a
customer's behalf, or backfilling a key for support to hand over
manually.

```bash
curl -X POST https://api.candyvoice.com/api/admin/api-keys \
  -H "Authorization: Bearer $ADMIN_FIREBASE_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"uid": "uid456", "label": "support-issued", "plan": "pro"}'
```

```json
{ "ok": true, "key_id": "8b1e0f...4a2c", "api_key": "cvk_...", "uid": "uid456", "label": "support-issued", "plan": "pro" }
```

### `GET /api/admin/api-keys?uid=`

Lists one user's keys — same shape as the self-service `GET /api/keys`,
for any `uid` you supply.

```bash
curl "https://api.candyvoice.com/api/admin/api-keys?uid=uid456" \
  -H "Authorization: Bearer $ADMIN_FIREBASE_ID_TOKEN"
```

```json
{ "ok": true, "uid": "uid456", "keys": [ { "key_id": "8b1e0f...4a2c", "label": "support-issued", "plan": "pro", "createdAt": "2026-08-10T09:15:00Z", "lastUsedAt": null, "revoked": false } ] }
```

### `GET /api/admin/api-keys/all`

Every key across every user, newest first — this is the one for a "list
all API keys, their plans, and who they belong to" admin table.
Paginated: pass `?limit=` (default 50, capped at 200) and, for the next
page, the previous response's `next_cursor` back as `?cursor=`;
`next_cursor: null` means there's no more.

```bash
curl "https://api.candyvoice.com/api/admin/api-keys/all?limit=50" \
  -H "Authorization: Bearer $ADMIN_FIREBASE_ID_TOKEN"
```

```json
{
  "ok": true,
  "keys": [
    { "key_id": "3f9c2a...e01b", "uid": "uid123", "email": "alice@example.com", "label": "my mobile app", "plan": "pro", "createdAt": "2026-08-12T10:03:00Z", "lastUsedAt": "2026-08-12T14:22:11Z", "revoked": false },
    { "key_id": "8b1e0f...4a2c", "uid": "uid456", "email": null, "label": "support-issued", "plan": "pro", "createdAt": "2026-08-10T09:15:00Z", "lastUsedAt": null, "revoked": false }
  ],
  "next_cursor": "8b1e0f...4a2c"
}
```

`email` is resolved from Firebase Auth in one batched lookup per page
(`firebase_admin.auth.get_users()`, up to 100 uids per call — cheap even
at the max page size of 200, which is at most 2 calls) rather than one
lookup per key. It's `null` when that `uid` no longer has a Firebase Auth
account (e.g. deleted) rather than a failed request — a single unresolved
email never blocks the rest of the list.

### `POST /api/admin/api-keys/{key_id}/revoke`

Revokes any key, regardless of owner — for a lost/compromised-key support
request. Same response shape and `404` behavior as the self-service
version above, just without the ownership check.

```bash
curl -X POST https://api.candyvoice.com/api/admin/api-keys/8b1e0f...4a2c/revoke \
  -H "Authorization: Bearer $ADMIN_FIREBASE_ID_TOKEN"
```

```json
{ "ok": true, "key_id": "8b1e0f...4a2c", "revoked": true }
```

### `POST /api/admin/api-keys/{key_id}/plan`

Changes a key's plan directly — a manual Enterprise ("sur mesure")
sign-up, or a support-handled upgrade/downgrade outside the self-service
flow. `plan` must be one of `starter` / `pro` / `enterprise` (`400` if
not); unknown `key_id` is a `404`.

```bash
curl -X POST https://api.candyvoice.com/api/admin/api-keys/8b1e0f...4a2c/plan \
  -H "Authorization: Bearer $ADMIN_FIREBASE_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"plan": "enterprise"}'
```

```json
{ "ok": true, "key_id": "8b1e0f...4a2c", "plan": "enterprise" }
```

### `POST /api/admin/send-api-usage-report`

Emails a per-user rollup of API-key usage for one calendar month to
`ADMIN_REPORT_RECIPIENTS` (same mailing list — and same SMTP2GO delivery —
as the website's own `/api/admin/send-report`; not necessarily identical
to `ADMIN_EMAILS`, the *access-control* list for these routes). Every
key's usage — the same per-feature counters `GET
/api/keys/{key_id}/usage` exposes for one key — is summed per owning
user, so someone with three keys shows up as one row.

Body (optional):

```json
{ "period": "2026-07" }
```

`period` is `"YYYY-MM"`, defaults to the current UTC calendar month if
omitted, and lets you re-pull a past month on demand rather than only
ever seeing the month in progress. Malformed value is a `400`.

```bash
curl -X POST https://api.candyvoice.com/api/admin/send-api-usage-report \
  -H "Authorization: Bearer $ADMIN_FIREBASE_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

```json
{ "ok": true, "period": "2026-08", "users": 12, "sentTo": ["jl.crebouw@candyvoice.com"] }
```

The email itself carries an `.xlsx` attachment — one row per user with
usage that period (email, uid, plan(s), key count, per-feature file
counts, total), sorted by total descending. Users with zero API-key
activity that month aren't included, so the sheet stays focused on
actual usage rather than every signed-up account. Revoked keys' usage
still counts — a key revoked mid-month still consumed quota while it was
active.

> **Not on a schedule yet.** This endpoint has to be triggered — nothing
> calls it automatically once a month. Wire that up externally (a k8s
> `CronJob` hitting this endpoint on a schedule, or any other scheduler)
> if you want it to actually fire monthly on its own.

## Using a key against the processing endpoints

Swap `Authorization: Bearer ...` for `X-API-Key`, on any of `/api/noise-filter`,
`/api/imitation`, `/api/frame-recovery`, `/api/deepfake-detect` (not the
`/ws/deepfake` WebSocket — that one's browser-only and stays Firebase-only).

> Sending both headers on one request isn't a supported way to combine
> them — `X-API-Key` is tried first. If it's valid, that's what's used. If
> it's missing/invalid and an `Authorization` bearer token is also
> present, the request falls back to that instead of failing outright
> (e.g. a stale test key left alongside a real browser session shouldn't
> block a request that would otherwise succeed as a normal login). Only
> when neither works, or `X-API-Key` was sent alone and is bad, do you get
> `401 Invalid or revoked API key`.

### Noise filter / imitation / frame recovery — raw audio, not JSON

These three produce a processed audio file. With a key, a successful
response is the **raw audio bytes as the HTTP body** (`Content-Type:
audio/wav`), not a JSON envelope — metadata rides along in response
headers instead of JSON fields:

```bash
curl -X POST https://api.candyvoice.com/api/noise-filter \
  -H "X-API-Key: cvk_wK8h2s9F3n...Qz" \
  -H "X-File-Name: meeting.wav" \
  --data-binary @meeting.wav \
  -o filtered.wav -D -
```

```
HTTP/1.1 200 OK
Content-Type: audio/wav
X-Exit-Code: 0
X-Uid: uid123
X-Duration-Seconds: 18.2
X-Files-Used: 13
X-Max-Files: 500
X-Plan: pro
<...raw WAV bytes...>
```

Why not JSON with the audio base64-encoded inline, like an earlier version
of this endpoint did? Base64 adds ~33% size overhead on top of an already
uncompressed WAV — for a full 30-second clip that pushed some responses
into the tens-of-megabytes range, which is painful for both server memory
and client-side JSON parsing. Raw bytes avoid that entirely and let you
stream/save the response directly (`-o filtered.wav` above, or
`response.arrayBuffer()` in JS).

`/api/imitation` and `/api/frame-recovery` add their own extra header —
`X-Voice-Model` / `X-Frame-Recovery-Factor` respectively — mirroring the
`voice_model` / `frame_recovery_factor` extra fields those two add to the
JSON envelope on the Firebase-session path.

`X-Files-Used` / `X-Max-Files` are this key's count for the current
calendar month on this specific feature — see [Plans & limits](#plans--limits).
There's nothing to download from `/outputs/` in this mode: the file was
deleted from disk right after this response was built.

If quota tracking fails *after* processing already succeeded (a
transient Firestore hiccup during commit — rare, but the file was already
produced by that point), `X-Files-Used` comes back empty rather than a
number, and an `X-Usage-Warning` header is added explaining that the
count may be off. The audio itself is still returned either way; only the
usage bookkeeping is in question.

**Errors still come back as JSON** (`{"error": "..."}` or a richer object
with `stdout`/`stderr`), exactly as documented in
[`API.md`](API.md#errors-quotas--rate-limits) — only the success response
differs by auth method, not the failure shape.

### Deepfake detection — unchanged NDJSON stream

`/api/deepfake-detect` has no processed audio output of its own (just a
score), so it's unaffected by the above — same streaming NDJSON shape as
[`API.md`](API.md#post-apideepfake-detect) either way. With a key, the
final `result` event's `files_used`/`max_files` reflect that key's
monthly plan allowance instead of the website's lifetime cap, and it
carries a `"plan"` field:

```jsonl
{"type": "result", "ok": true, "exit_code": 0, "deepfake_percent": 3.1, "threshold_percent": 50.0, "verdict": "genuine", "uid": "uid123", "duration_seconds": 12.4, "files_used": 4, "max_files": 500, "plan": "pro"}
```

## Errors specific to keys

| Status | Meaning |
|---|---|
| 401 | Missing, unknown, or revoked API key. |
| 429 | Rate limit or monthly quota exceeded for this key's plan (the message says which) — each key has its own independent budget, sized to its plan, not shared with that user's Firebase-session calls or their other keys. |
| 503 | Key verification / quota service (Firestore) temporarily unavailable — safe to retry. |

Everything else (400 bad upload, 500/504 processing failures) behaves the
same regardless of auth method — see [`API.md`](API.md#errors-quotas--rate-limits).
