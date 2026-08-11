"""Zoho CRM OAuth token management and Attachments API upload, for the CRM leg of the confidentiality
workflow."""

import os
import re
import time
import requests

from app.services.downloads import generate_download_token
from app.services.firebase import get_firestore_client
from app import config

from firebase_admin import firestore as admin_firestore
from google.cloud.firestore_v1.base_query import FieldFilter

class ZohoTokenError(RuntimeError):
    pass


def save_processed_output_record(uid, feature, output_path, input_path=None, original_file_name=None):
    """Stores processed outputs for later CRM analysis. Never raises — a
    metadata-persistence failure must not fail an otherwise successful
    processing request.

    `input_path` is optional so older call sites (and any other future
    caller that doesn't have an input file handy) keep working — the
    unsatisfied-webhook flow simply skips attaching an input for docs
    where it's absent.

    `original_file_name` is the name the user's browser sent (the
    X-File-Name header), before it gets mangled into
    "{uid}_{uuid}_{name}" on disk. Stored separately rather than
    reverse-parsed out of that mangled name later, since a Firebase uid
    can itself contain underscores — parsing it back out isn't reliable.
    """
    try:
        client = get_firestore_client()
        output_basename = os.path.basename(output_path)
        download_token = generate_download_token(output_basename)
        download_url = f"/outputs/{output_basename}?token={download_token}"

        client.collection("processedOutputs").add({
            "uid": uid,
            "feature": feature,
            "outputPath": output_path,
            "inputPath": input_path,
            "originalFileName": original_file_name,
            "downloadURL": download_url,
            "createdAt": admin_firestore.SERVER_TIMESTAMP,
            "uploadedToZoho": False,
            "confidential": False,
        })
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to save processed output metadata: {exc}")


def log_usage_event(uid, feature):
    """Records one discrete "a file was successfully processed" event, in
    addition to the running filesUsed counter on usage/{uid}. Never raises —
    mirrors save_processed_output_record's best-effort pattern, since this is
    purely for later reporting (day/month breakdowns in Zoho Analytics) and
    must never block or fail an otherwise successful processing request.

    dateKey is a plain "YYYY-MM-DD" string (UTC) alongside the real
    timestamp, so Zoho Analytics / any report can group by day without
    needing timezone-aware date-truncation logic on the timestamp field.
    """
    try:
        from datetime import datetime, timezone

        client = get_firestore_client()
        now = datetime.now(timezone.utc)
        client.collection("usageEvents").add({
            "uid": uid,
            "feature": feature,
            "createdAt": admin_firestore.SERVER_TIMESTAMP,
            "dateKey": now.strftime("%Y-%m-%d"),
            "uploadedToZoho": False,
        })
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to log usage event for uid={uid}, feature={feature}: {exc}")


def get_contact_id_by_email(email):
    """Looks up a Contact's record id by email. Returns None if no
    contact matches — the caller decides whether that's an error or a
    silent skip."""
    response = requests.get(
        f"{config.ZOHO_API_DOMAIN}/crm/v8/Contacts/search",
        headers=_auth_headers(),
        params={"criteria": f"(Email:equals:{email})"},
        timeout=30,
    )
    if response.status_code == 204:  # Zoho returns 204, not an empty 200, for "no matches"
        return None
    response.raise_for_status()
    records = response.json().get("data", [])
    return records[0]["id"] if records else None


def get_unuploaded_outputs(firestore_client, uid, limit=None):
    """Returns every not-yet-uploaded, non-confidential processedOutputs doc
    for this user, newest first, capped at `limit` (defaults to
    config.MAX_UNSATISFIED_ATTACHMENTS) so a single webhook call can't try
    to process an unbounded backlog. Kept newest-first (same direction as
    the previous single-doc version) so it reuses the same composite index
    (uid, uploadedToZoho, confidential, createdAt) rather than requiring a
    new one."""
    if limit is None:
        limit = config.MAX_UNSATISFIED_ATTACHMENTS
    query = (
        firestore_client.collection("processedOutputs")
        .where(filter=FieldFilter("uid", "==", uid))
        .where(filter=FieldFilter("uploadedToZoho", "==", False))
        .where(filter=FieldFilter("confidential", "==", False))
        .order_by("createdAt", direction=admin_firestore.Query.DESCENDING)
        .limit(limit)
    )
    return list(query.stream())


def build_attachment_filename(feature, created_at, kind, original_path, original_file_name=None):
    """Builds a human-readable name for a Zoho attachment. `kind` is
    "input" or "output".

    When the customer's original filename is known (original_file_name —
    stored on the doc since save_processed_output_record started
    recording it; see that function's docstring for why it isn't just
    reverse-parsed out of the on-disk path instead), it leads the
    attachment name, e.g.
    'VoixEtBruit90dB_noise-filter_output_2026-08-06_16h27m22s.wav' — so
    support can reference "the VoixEtBruit90dB file" when following up
    with a customer. Falls back to
    'noise-filter_2026-08-06_16h27m22s_output.wav' for older docs saved
    before this field existed.

    The feature name stays in both forms — the same original file can be
    submitted to more than one feature (e.g. the same recording run
    through both NoizOff and Voice Imitation), and without it those two
    outputs would be indistinguishable except by timestamp.

    A date+time also stays in the name either way, to keep it unique:
    with names that collide exactly, Zoho silently appends its own
    timestamp suffix to the second+ upload sharing a filename, which
    reads as a rename/duplication bug from the CRM side rather than what
    it actually is (several distinct files, ambiguously named — e.g. a
    customer re-testing with a file of the same name twice in one day).
    An input and its matching output share the same `created_at` (both
    come from the same processedOutputs doc), so they still end up as a
    clearly paired set in Zoho's attachment list either way.
    """
    ext = os.path.splitext(original_path)[1]
    date_str = created_at.strftime("%Y-%m-%d_%Hh%Mm%Ss") if created_at else "unknown-date"
    safe_feature = feature or "unknown-feature"

    if original_file_name:
        stem = os.path.splitext(os.path.basename(original_file_name))[0]
        # Keep it close to what the customer actually named their file —
        # only swap out characters that are awkward in a filename Zoho
        # will display and let someone click on.
        safe_stem = re.sub(r"[^\w\-. ]+", "_", stem).strip() or "file"
        return f"{safe_stem}_{safe_feature}_{kind}_{date_str}{ext}"

    # Fallback for docs saved before original_file_name existed.
    return f"{safe_feature}_{date_str}_{kind}{ext}"

_access_token = None
_expires_at = 0.0


def get_access_token():
    """Returns a cached access token, refreshing via ZOHO_REFRESH_TOKEN
    when expired. Not thread-safe against a first concurrent refresh race,
    but a duplicate refresh call is harmless (Zoho just issues another
    valid access token), so no lock is needed here."""
    global _access_token, _expires_at

    if _access_token and time.time() < _expires_at - 60:
        return _access_token

    if not config.ZOHO_REFRESH_TOKEN:
        raise ZohoTokenError("ZOHO_REFRESH_TOKEN is not configured on this server.")

    response = requests.post(
        f"{config.ZOHO_ACCOUNTS_URL}/oauth/v2/token",
        data={
            "refresh_token": config.ZOHO_REFRESH_TOKEN,
            "client_id": config.ZOHO_CLIENT_ID,
            "client_secret": config.ZOHO_CLIENT_SECRET,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    if "access_token" not in data:
        raise ZohoTokenError(f"Zoho token refresh failed: {data}")

    _access_token = data["access_token"]
    _expires_at = time.time() + data.get("expires_in", 3600)
    return _access_token


def _auth_headers():
    return {"Authorization": f"Zoho-oauthtoken {get_access_token()}"}


def update_contact_confidential_flag(record_id, is_confidential):
    response = requests.put(
        f"{config.ZOHO_API_DOMAIN}/crm/v8/Contacts/{record_id}",
        json={"data": [{"isFileConfidential": is_confidential}]},
        headers=_auth_headers(),
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    record_result = result.get("data", [{}])[0]
    if record_result.get("status") != "success":
        raise ZohoTokenError(f"Zoho update rejected: {record_result}")
    return result

def sync_confidential_flag_to_zoho(uid, confidential):
    """Reflects the confidentiality flag the user declared for their most
    recent file onto their Zoho Contact record. Never raises — a Zoho sync
    hiccup here must not block file processing itself."""
    try:
        from firebase_admin import auth as firebase_auth

        user = firebase_auth.get_user(uid)
        if not user.email:
            return

        contact_id = get_contact_id_by_email(user.email)
        if contact_id is None:
            print(f"No Zoho contact found for {user.email}; skipping confidential flag sync.")
            return

        update_contact_confidential_flag(contact_id, confidential)
    except Exception as exc:  # noqa: BLE001
        print(f"Zoho confidential flag sync failed for uid={uid}: {exc}")


def upload_file_to_zoho(file_path, record_id, display_filename):
    """Uploads `file_path` as an Attachment on Zoho Contact `record_id`,
    shown in Zoho as `display_filename` rather than the file's raw name on
    disk (which is an internal '{uid}_{uuid}_{original name}' string)."""
    with open(file_path, "rb") as handle:
        response = requests.post(
            f"{config.ZOHO_API_DOMAIN}/crm/v8/Contacts/{record_id}/Attachments",
            files={"file": (display_filename, handle)},
            headers=_auth_headers(),
            timeout=60,
        )
    response.raise_for_status()
    return response.json()