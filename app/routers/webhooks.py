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
    """Upload celui des fichiers input/output de ce doc qui a encore besoin
    d'être uploadé, comme pièces jointes Zoho sur `contact_id`. Retourne
    (attachment_ids, still_pending, notes) :

    - attachment_ids : {"input": id, "output": id} — tout ce qui est
      maintenant uploadé, y compris ce qui a été repris d'une tentative
      précédente (pour qu'un fichier déjà attaché ne soit jamais réuploadé
      en cas de nouvelle tentative)
    - still_pending : True si au moins un fichier existe sur disque mais
      que son upload a échoué pour une raison transitoire (erreur réseau/
      API Zoho) — le doc est laissé non marqué pour qu'un futur appel
      réessaie juste ce fichier
    - notes : chaînes lisibles décrivant ce qui a été sauté, manquant, ou
      qui a échoué, stockées sur le doc en `uploadError` pour la visibilité

    Ne lève jamais d'exception — le problème d'upload d'un doc ne doit pas
    interrompre le reste du lot.
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
            continue  # déjà uploadé lors d'un appel précédent à cet endpoint

        path = data.get(path_field)
        if not path:
            continue  # rien d'enregistré pour ce type (ex. un doc sauvé avant que inputPath n'existe)

        if not os.path.exists(path):
            notes.append(f"{kind} file no longer on disk")
            continue  # disparu (nettoyage par TTL ou autre) — on n'y peut plus rien, ça ne bloque pas la complétion

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
    """Appelé par une action webhook d'un workflow Zoho CRM quand le score
    de satisfaction d'un contact passe sous le seuil. Corps :
    {"firebase_uid": "...", "X-Webhook-Secret": "..."}. Serveur-à-serveur,
    protégé par un secret partagé.

    Le payload n'identifie pas de quel fichier/fonctionnalité la réponse
    au sondage parlait réellement, donc plutôt que d'uploader tous les
    fichiers en attente jamais soumis par cet utilisateur, ceci n'uploade
    que son unique fichier traité le plus récent, pas encore uploadé et
    non confidentiel — input et output (là où chacun existe encore sur
    disque) — comme pièces jointes Zoho, en tant que meilleur proxy
    disponible pour "le fichier dont on vient de lui parler".
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
        docs = get_unuploaded_outputs(firestore_client, firebase_uid, limit=1)
    except Exception as exc:  # noqa: BLE001 - most likely a missing composite index
        print(f"processedOutputs query failed for {firebase_uid}: {exc}")
        raise HTTPException(
            status_code=500,
            detail="Could not query processed outputs (check server logs — likely a missing Firestore index)",
        )

    if not docs:
        return {"ok": True, "uploaded": 0, "processed": 0, "reason": "No pending processed output for this user"}

    # Zoho attache les fichiers à une fiche, pas à un stockage de fichiers
    # générique — il faut l'id de la fiche Zoho du contact, résolu via l'e-mail
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
            # Défensif uniquement — get_unuploaded_outputs filtre déjà ça
            # au niveau de la requête, donc ça ne devrait jamais se déclencher.
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