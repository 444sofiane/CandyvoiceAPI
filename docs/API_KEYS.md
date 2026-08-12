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
| Quota (`files_used`/`max_files`) | Enforced, counted | **Bypassed** — not counted, can't be exhausted |
| Rate limit (5 req/60s) | Enforced | Enforced (same budget, same `uid`) |
| Output delivery | `output_url` (download link, file kept ~1h) | `output_audio_base64` inline in the response; **no file is kept on the server** |
| Processed-outputs history / Zoho sync | Saved | **Skipped entirely** — nothing is persisted or synced |
| Uploaded input file | Kept briefly (Zoho attachment flow) | **Deleted immediately** after the response is built |

In short: a key is for "this is my own application's data, processed and
handed straight back" — nothing about the request is retained
server-side once the response is sent.

## Getting a key (self-service, from your frontend)

These three endpoints take the **same Firebase ID token** your frontend
already sends everywhere else — a key can only ever be created for the
signed-in user's own account, there's no way to request one for someone
else through these.

### `POST /api/keys`

Creates a new key for the signed-in user.

| Header | Required | Notes |
|---|---|---|
| `Authorization` | required | `Bearer <firebase_id_token>` |

Body (optional):

```json
{ "label": "my mobile app" }
```

```bash
curl -X POST https://api.candyvoice.com/api/keys \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"label": "my mobile app"}'
```

```json
{
  "ok": true,
  "key_id": "3f9c2a...e01b",
  "api_key": "cvk_wK8h2s9F3n...Qz",
  "label": "my mobile app"
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
      "createdAt": "2026-08-12T10:03:00Z",
      "lastUsedAt": "2026-08-12T14:22:11Z",
      "revoked": false
    }
  ]
}
```

Use this to render an API-keys settings page: label, creation date,
last-used date, and a revoke button per row. There's no "last 4
characters" of the key to show, since the raw value was never stored —
`label` (set at creation) is what the user relies on to tell keys apart.

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

```bash
curl -X POST https://api.candyvoice.com/api/noise-filter \
  -H "X-API-Key: cvk_wK8h2s9F3n...Qz" \
  -H "X-File-Name: meeting.wav" \
  --data-binary @meeting.wav
```

Response — same envelope as [`API.md`'s response shape](API.md#response-shape),
except `output_url` is `null` and the processed audio comes back inline:

```json
{
  "ok": true,
  "exit_code": 0,
  "output_url": null,
  "output_audio_base64": "UklGRi...<base64 wav bytes>...",
  "quota_bypassed": true,
  "uid": "uid123",
  "duration_seconds": 18.2,
  "files_used": null,
  "max_files": 10,
  "stdout": "...",
  "stderr": ""
}
```

Decode `output_audio_base64` client-side to get the processed file — there
is nothing to download from `/outputs/` in this mode, since the file was
deleted right after this response was built.

## Errors specific to keys

| Status | Meaning |
|---|---|
| 401 | Missing, unknown, or revoked API key. |
| 429 | Rate limit exceeded — same 5 req/60s budget as Firebase auth, keyed by the underlying `uid`. Note this budget is shared with that user's own Firebase-session calls, not separate. |
| 503 | Key verification service (Firestore) temporarily unavailable — safe to retry. |

Everything else (400 bad upload, 500/504 processing failures) behaves the
same regardless of auth method — see [`API.md`](API.md#errors-quotas--rate-limits).
