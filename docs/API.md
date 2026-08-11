# CandyVoice API — Developer Reference

Audio processing for voice products: background-noise removal, voice cloning,
dropped-frame recovery, and AI voice-deepfake detection. Plain REST for the
first three, plus a streaming HTTP endpoint and a WebSocket for detection
progress.

- **Base URL:** `https://api.candyvoice.com`
- **Auth:** Firebase ID token
- **Format:** raw audio bytes in, JSON out

## Overview

Every processing endpoint takes one audio file and returns one result —
there's no job queue or polling. Deepfake detection additionally offers a
streaming variant (chunked NDJSON or a WebSocket) so a UI can show live
progress on longer clips.

| Feature | Endpoint | Use it for |
|---|---|---|
| Noise filter | `POST /api/noise-filter` | Strip background noise from a recording |
| Voice imitation | `POST /api/imitation` | Re-voice a clip as one of 18 preset voices |
| Frame recovery | `POST /api/frame-recovery` | Reconstruct dropped or corrupted audio frames |
| Deepfake detection | `POST /api/deepfake-detect` or `wss:///ws/deepfake` | Score how likely a clip is synthetic speech |

## Authentication

There are no API keys. Every call is tied to a signed-in CandyVoice user:
authenticate with Firebase Authentication in the CandyVoice project, then
send that user's ID token as a bearer token. Usage, quota, and rate limiting
are all tracked per user ID (`uid`), not per app.

```http
Authorization: Bearer <firebase_id_token>
```

> The WebSocket endpoint can't carry a browser `Authorization` header on its
> handshake, so it takes the same token as the first message on the
> connection instead — see [Deepfake detection (live)](#wss-wsdeepfake).

> CORS is locked to CandyVoice's own web origins, so browser JavaScript on a
> third-party site can't call this API directly. Calling from a backend,
> script, or mobile app is unaffected — CORS is a browser-only restriction.

## Making a request

Every processing endpoint takes the file as a **raw binary body** — not
`multipart/form-data` — plus the original filename in a header.

| Header | Required | Notes |
|---|---|---|
| `Authorization` | required | `Bearer <firebase_id_token>` |
| `X-File-Name` | required | Original filename. Can also be sent as a `?file_name=` query param. |

> **Send WAV.** The upload is accepted if it merely looks like audio by
> content, but clip duration is read from a WAV header — a non-WAV file can
> pass the initial check and still fail a moment later with `400 Could not
> determine audio duration`. Transcode to WAV before uploading.
>
> Clips must be **30 seconds or shorter**. Longer files are rejected with a
> 400 before any processing starts.

```bash
# noise filter, as a worked example — same shape for every processing endpoint
curl -X POST https://api.candyvoice.com/api/noise-filter \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "X-File-Name: meeting.wav" \
  --data-binary @meeting.wav
```

## Response shape

The three synchronous endpoints (noise filter, imitation, frame recovery)
all return the same envelope, plus a couple of endpoint-specific fields.

```json
{
  "ok": true,
  "exit_code": 0,
  "output_url": "/outputs/uid123_9f2c..._meeting_filtered.wav?token=eyJhbGciOi...",
  "output_file": "/app/outputs/uid123_9f2c..._meeting_filtered.wav",
  "uid": "uid123",
  "duration_seconds": 18.2,
  "files_used": 4,
  "max_files": 10,
  "stdout": "...",
  "stderr": "",
  "command": ["…diagnostic only…"]
}
```

| Field | Meaning |
|---|---|
| `output_url` | Relative path to [download the result](#get-outputsoutput_name). Carries its own short-lived token — use it as-is. |
| `output_file` | The file's path on the server. Informational only; not reachable directly. |
| `files_used` / `max_files` | This feature's quota counter — see [Quotas](#errors-quotas--rate-limits). |
| `stdout` / `stderr` / `command` | Raw diagnostics from the processing engine. Useful when filing a support request; not a stable contract to parse. |

> Output files are deleted automatically about an hour after creation.
> Download or forward `output_url` promptly rather than storing it for later.

## Errors, quotas & rate limits

Errors are JSON: `{"error": "..."}` for straightforward failures, or a
richer object with `stdout`/`stderr` when the processing engine itself
failed.

| Status | Meaning |
|---|---|
| 400 | Bad request — missing filename, unparseable/oversized audio, invalid parameter value. |
| 401 | Missing, invalid, or expired Firebase token. |
| 429 | Rate limit or per-feature quota exceeded (the message says which). |
| 500 | The processing engine failed, or an unexpected server error. |
| 503 | Quota service temporarily unavailable — safe to retry. |
| 504 | Processing exceeded the 10-minute server-side timeout. |

**Rate limit** — up to 5 requests per 60 seconds, per authenticated user,
across all endpoints:

```json
{ "error": "Too many requests — max 5 per 60s. Try again shortly." }
```

**Per-feature quota** — each feature carries its own allowance of 10
processed files, tracked independently. Using up noise filter doesn't touch
your imitation or deepfake allowance:

```json
{ "error": "You've used all 10 files allowed for this feature." }
```

---

## Endpoints

### `GET /health`

Unauthenticated liveness check — safe for uptime monitors and
load-balancer health probes.

```bash
curl https://api.candyvoice.com/health
```

```json
{ "ok": true }
```

### `POST /api/noise-filter`

Aliases: `/process`, `/api/process`

Removes background noise from a spoken-audio clip.

| Header | | Notes |
|---|---|---|
| `Authorization` | required | Bearer token |
| `X-File-Name` | required | Or `?file_name=` |
| `X-Output-Name` | optional | Or `?output_file=`. Defaults to `<input>_filtered.wav`. |
| `X-Confidential-Check` | optional | `true` skips saving a copy to your processed-outputs history. |

```bash
curl -X POST https://api.candyvoice.com/api/noise-filter \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "X-File-Name: meeting.wav" \
  --data-binary @meeting.wav
```

Response: the [standard envelope](#response-shape), no extra fields.

### `POST /api/imitation`

Alias: `/api/voice-imitation`

Re-voices the input speech as one of eighteen preset voice models.

| Header | | Notes |
|---|---|---|
| `Authorization` | required | Bearer token |
| `X-File-Name` | required | Or `?file_name=` |
| `X-Voice-Model` | required | One of the model IDs below. |
| `X-Output-Name` | optional | Or `?output_file=` |
| `X-Confidential-Check` | optional | `true` skips saving to your outputs history. |

**Voice models:** `model_barack` `model_chloe` `model_cortana` `model_degaulle`
`model_dombasle` `model_etienne` `model_frederic` `model_isabelle`
`model_jeanne` `model_JLS` `model_marine` `model_mbappe` `model_michelleo`
`model_mitterrand` `model_pierre` `model_tatiana` `model_trump` `model_valentin`

```bash
curl -X POST https://api.candyvoice.com/api/imitation \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "X-File-Name: line.wav" \
  -H "X-Voice-Model: model_trump" \
  --data-binary @line.wav
```

Response: the [standard envelope](#response-shape) plus `"voice_model": "model_trump"`.

### `POST /api/frame-recovery`

Alias: `/api/frameRecovery`

Reconstructs dropped or corrupted frames in a clip.

| Header | | Notes |
|---|---|---|
| `Authorization` | required | Bearer token |
| `X-File-Name` | required | Or `?file_name=` |
| `X-Frame-Recovery-Factor` | required | Number, `0 < factor ≤ 0.5` |
| `X-Output-Name` | optional | Or `?output_file=` |
| `X-Confidential-Check` | optional | `true` skips saving to your outputs history. |

```bash
curl -X POST https://api.candyvoice.com/api/frame-recovery \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "X-File-Name: dropout.wav" \
  -H "X-Frame-Recovery-Factor: 0.3" \
  --data-binary @dropout.wav
```

Response: the [standard envelope](#response-shape) plus `"frame_recovery_factor": 0.3`.

### `POST /api/deepfake-detect`

Aliases: `/api/deepfake`, `/api/deepfake-detection`

Scores how likely a clip is synthetic ("deepfake") speech. Unlike the
endpoints above, this streams progress as newline-delimited JSON
(`application/x-ndjson`) instead of returning one JSON object.

| Header | | Notes |
|---|---|---|
| `Authorization` | required | Bearer token |
| `X-File-Name` | required | Or `?file_name=` |

```bash
curl -N -X POST https://api.candyvoice.com/api/deepfake-detect \
  -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
  -H "X-File-Name: clip.wav" \
  --data-binary @clip.wav
```

```jsonl
{"type": "info", "total_frames": 2582, "estimated_duration_sec": 25.8}
{"type": "progress", "percent_processed": 3.9, "elapsed_sec": 1.0, "instant_percent": 0.0, "average_percent": 0.0}
… one "progress" line per second of audio processed …
{"type": "result", "ok": true, "exit_code": 0, "deepfake_percent": 0.0, "threshold_percent": 50.0, "verdict": "genuine", "uid": "uid123", "duration_seconds": 25.8, "files_used": 5, "max_files": 10}
```

| `type` | When | Fields |
|---|---|---|
| `warning` | 0+, informational | `message` |
| `info` | 0–1, once frame count is known | `total_frames`, `estimated_duration_sec` |
| `progress` | 0+, while processing | `percent_processed`, `elapsed_sec`, `instant_percent`, `average_percent` |
| `result` | exactly 1, terminal | `deepfake_percent`, `threshold_percent`, `verdict` ("genuine" / "synthetic"), `files_used`, `max_files` |
| `error` | terminal, instead of `result` | `error`, `stdout` |

### `WSS /ws/deepfake`

The same deepfake detector as above, over a WebSocket — a cleaner fit than
chunked HTTP for a live progress bar in a UI.

**Protocol:**

1. Open `wss://api.candyvoice.com/ws/deepfake`.
2. Send `{"type": "auth", "token": "<firebase_id_token>"}`.
3. Receive `{"type": "auth_ok"}` — or an error frame followed by a close.
4. Send `{"type": "start", "file_name": "clip.wav"}`.
5. Send one binary frame: the raw file bytes.
6. Receive zero or more `warning` / `info` / `progress` frames, same shapes
   as the streaming HTTP endpoint.
7. Receive exactly one final `result` or `error` frame, then the server
   closes the connection.

```javascript
const ws = new WebSocket("wss://api.candyvoice.com/ws/deepfake");

ws.onopen = () => {
  ws.send(JSON.stringify({ type: "auth", token: firebaseIdToken }));
};

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);

  // once auth is confirmed, kick off the job
  if (msg.type === "auth_ok") {
    ws.send(JSON.stringify({ type: "start", file_name: "clip.wav" }));
    ws.send(fileBytes); // ArrayBuffer / Blob, right after "start"
    return;
  }

  if (msg.type === "progress") updateProgressBar(msg.percent_processed);
  if (msg.type === "result" || msg.type === "error") ws.close();
};
```

**Close codes:**

| Code | Meaning |
|---|---|
| 4403 | Browser `Origin` header present and not on the allow-list. |
| 4401 | Protocol violation — an unexpected message where `auth` or `start` was required. |
| 1013 | Rate limited — same 5 req/60s budget as the HTTP endpoints. |
| 1008 | Upload rejected (bad audio, quota exceeded, etc.) — check the `error` frame sent just before the close. |

> The `4403` origin check only fires when an `Origin` header is present —
> that's a browser thing. Server-to-server WebSocket clients that don't send
> one aren't restricted by it.

### `GET /outputs/{output_name}`

Downloads a file produced by any endpoint above.

**Authorization — either of:**

| Method | Notes |
|---|---|
| `?token=` | The short-lived signed token already embedded in `output_url` — use the URL as returned. This is what an `<audio>` or `<a download>` tag should use, since they can't attach an Authorization header. |
| Authorization header | A bearer token belonging to the same `uid` that produced the file. |

```bash
curl -L "https://api.candyvoice.com/outputs/uid123_9f2c..._meeting_filtered.wav?token=eyJhbGciOi..." \
  -o meeting_filtered.wav
```

> Both "the file doesn't exist" and "you're not authorized to see it" return
> a plain `404`, on purpose — a permissions failure here won't look any
> different from a typo in the filename.

---

## All endpoints

| Method | Path | Auth |
|---|---|---|
| GET | `/health` | — |
| POST | `/api/noise-filter` | Bearer |
| POST | `/api/imitation` | Bearer |
| POST | `/api/frame-recovery` | Bearer |
| POST | `/api/deepfake-detect` | Bearer |
| WSS | `/ws/deepfake` | First message |
| GET | `/outputs/{output_name}` | Token or Bearer |

Limits shown (30s clip cap, 10 files per feature, 5 req/60s) are current
server defaults and may be tuned over time — treat the error message on a
400/429 response as the source of truth.
