# CandyVoice API — API Keys

How a signed-in user gets a long-lived API key for their own application,
and how that key behaves differently from a normal Firebase-session call.
Companion to [`API.md`](API.md) — start there for the processing endpoints
themselves; this doc only covers what's specific to keys.

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

## Using a key against the processing endpoints

Swap `Authorization: Bearer ...` for `X-API-Key`, on any of `/api/noise-filter`,
`/api/imitation`, `/api/frame-recovery`, `/api/deepfake-detect` (not the
`/ws/deepfake` WebSocket — that one's browser-only and stays Firebase-only).

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
