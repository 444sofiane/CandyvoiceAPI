import hmac
import os

from fastapi import APIRouter, Header, HTTPException, Request
from firebase_admin import auth as firebase_auth

from app import config
from app.services.firebase import FirestoreUnavailableError, get_firestore_client
from app.services.zoho import (
    build_attachment_filename,
    get_contact_id_by_email,
    get_unuploaded_outputs,
    upload_file_to_zoho,
)

router = APIRouter()


def _upload_doc_files(doc, contact_id):
    """Uploads whichever of this doc's input/output files still need
    uploading, as Zoho Attachments on `contact_id`. Returns
    (attachment_ids, still_pending, notes):

    - attachment_ids: {"input": id, "output": id} — everything now
      uploaded, including anything carried over from a previous attempt
      (so a file already attached is never re-uploaded on retry)
    - still_pending: True if at least one file exists on disk but its
      upload failed for a transient reason (network/Zoho API error) — the
      doc is left unmarked so a future call retries just that file
    - notes: human-readable strings describing anything skipped, missing,
      or failed, stored on the doc as `uploadError` for visibility

    Never raises — one doc's upload problem shouldn't abort the rest of
    the batch.
    """
    data = doc.to_dict()
    feature = data.get("feature")
    created_at = data.get("createdAt")
    original_file_name = data.get("originalFileName")
    attachment_ids = dict(data.get("zohoAttachmentIds") or {})
    still_pending = False
    notes = []

    for kind, path_field in (("output", "outputPath"), ("input", "inputPath")):
        if kind in attachment_ids:
            continue  # already uploaded in a previous call to this endpoint

        path = data.get(path_field)
        if not path:
            continue  # nothing recorded for this kind (e.g. a doc saved before inputPath existed)

        if not os.path.exists(path):
            notes.append(f"{kind} file no longer on disk")
            continue  # gone (TTL cleanup or otherwise) — nothing more we can do, doesn't block completion

        display_name = build_attachment_filename(feature, created_at, kind, path, original_file_name)
        try:
            result = upload_file_to_zoho(path, contact_id, display_name)
        except Exception as exc:  # noqa: BLE001 - network/HTTP errors from the Zoho API
            print(f"Zoho attachment upload failed ({kind}, doc={doc.id}): {exc}")
            notes.append(f"{kind} upload failed: {exc}")
            still_pending = True
            continue

        attachment_ids[kind] = result.get("data", [{}])[0].get("details", {}).get("id")
        try:
            os.remove(path)
        except OSError as exc:
            print(f"Could not delete local {kind} file {path} after Zoho upload: {exc}")

    return attachment_ids, still_pending, notes


@router.post("/api/zoho/unsatisfied")
async def zoho_unsatisfied_webhook(
    request: Request,
    x_webhook_secret: str | None = Header(default=None, alias=config.ZOHO_UNSATISFIED_WEBHOOK_HEADER),
):
    """Called by a Zoho CRM workflow's webhook action when a contact's
    satisfaction score drops below the threshold. Body:
    {"firebase_uid": "...", "X-Webhook-Secret": "..."}. Server-to-server,
    gated by a shared secret.

    Uploads every pending (not yet uploaded, non-confidential) processed
    file for this user as Zoho Attachments — both the input the user
    submitted and the output CandyVoice produced, wherever each still
    exists on disk — rather than only the single most recent output.
    Capped per call at config.MAX_UNSATISFIED_ATTACHMENTS docs.
    """
    try:
        body = await request.json()
    except Exception:
        raw = await request.body()
        print(f"zoho_unsatisfied_webhook: could not parse JSON body: {raw!r}")
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if not config.ZOHO_UNSATISFIED_WEBHOOK_SECRET:
        print("ZOHO_UNSATISFIED_WEBHOOK_SECRET is not set — refusing all webhook calls.")
        raise HTTPException(status_code=503, detail="Webhook is not configured on this server")

    provided_secret = x_webhook_secret or str(body.get("X-Webhook-Secret", ""))
    if not hmac.compare_digest(provided_secret, config.ZOHO_UNSATISFIED_WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")

    firebase_uid = body.get("firebase_uid")
    if not firebase_uid or not isinstance(firebase_uid, str):
        raise HTTPException(status_code=400, detail="firebase_uid is required")

    try:
        firestore_client = get_firestore_client()
    except FirestoreUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    try:
        docs = get_unuploaded_outputs(firestore_client, firebase_uid)
    except Exception as exc:  # noqa: BLE001 - most likely a missing composite index
        print(f"processedOutputs query failed for {firebase_uid}: {exc}")
        raise HTTPException(
            status_code=500,
            detail="Could not query processed outputs (check server logs — likely a missing Firestore index)",
        )

    if not docs:
        return {"ok": True, "uploaded": 0, "processed": 0, "reason": "No pending processed output for this user"}

    # Zoho attaches files to a record, not to a generic file store — need
    # the contact's Zoho record id, resolved via email
    try:
        user = firebase_auth.get_user(firebase_uid)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not look up Firebase user {firebase_uid}: {exc}")
        raise HTTPException(status_code=500, detail="Could not resolve user email")

    if not user.email:
        raise HTTPException(status_code=400, detail="User has no email on file")

    try:
        contact_id = get_contact_id_by_email(user.email)
    except Exception as exc:  # noqa: BLE001
        print(f"Zoho contact lookup failed for {user.email}: {exc}")
        raise HTTPException(status_code=502, detail="Could not look up Zoho contact")

    if contact_id is None:
        raise HTTPException(status_code=404, detail=f"No Zoho contact found for {user.email}")

    uploaded_count = 0
    results = []

    for doc in docs:
        data = doc.to_dict()
        if data.get("confidential"):
            # Defensive only — get_unuploaded_outputs already filters this
            # at the query level, so this should never actually trigger.
            continue

        attachment_ids, still_pending, notes = _upload_doc_files(doc, contact_id)

        update_fields = {"zohoContactId": contact_id, "uploadedToZoho": not still_pending}
        if attachment_ids:
            update_fields["zohoAttachmentIds"] = attachment_ids
        if notes:
            update_fields["uploadError"] = "; ".join(notes)
        doc.reference.update(update_fields)

        if attachment_ids:
            uploaded_count += 1
        results.append({
            "doc_id": doc.id,
            "feature": data.get("feature"),
            "attachment_ids": attachment_ids,
            "pending_retry": still_pending,
            "notes": notes,
        })

    return {
        "ok": True,
        "uploaded": uploaded_count,
        "processed": len(docs),
        "zoho_contact_id": contact_id,
        "results": results,
    }