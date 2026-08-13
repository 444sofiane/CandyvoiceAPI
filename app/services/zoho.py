"""Gestion des tokens OAuth Zoho CRM et upload via l'API Attachments, pour
le volet CRM du workflow de confidentialité."""

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
    """Stocke les sorties traitées pour une analyse CRM ultérieure. Ne
    lève jamais — un échec de persistance des métadonnées ne doit pas
    faire échouer une requête de traitement par ailleurs réussie.

    `input_path` est optionnel pour que les anciens sites d'appel (et tout
    futur appelant qui n'a pas de fichier d'entrée sous la main) continuent
    de fonctionner — le flux du webhook d'insatisfaction saute simplement
    l'attachement d'une entrée pour les docs où elle est absente.
    Symétriquement, `output_path` peut valoir None pour une fonctionnalité
    qui n'a pas de sortie traitée propre (ex. la détection de deepfake, qui
    ne retourne qu'un score) — le flux du webhook attache alors juste
    l'entrée.

    `original_file_name` est le nom envoyé par le navigateur de
    l'utilisateur (l'en-tête X-File-Name), avant qu'il ne soit transformé
    en "{uid}_{uuid}_{name}" sur disque. Stocké séparément plutôt que
    reparsé en sens inverse à partir de ce nom transformé plus tard,
    puisqu'un uid Firebase peut lui-même contenir des underscores —
    l'extraire par parsing inverse n'est pas fiable.
    """
    try:
        client = get_firestore_client()
        download_url = None
        if output_path:
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
    """Enregistre un événement discret "un fichier a été traité avec
    succès", en plus du compteur courant filesUsed sur usage/{uid}. Ne
    lève jamais — reflète le principe au mieux sans garantie de
    save_processed_output_record, puisque ceci est purement pour du
    reporting ultérieur (répartitions par jour/mois dans Zoho Analytics)
    et ne doit jamais bloquer ni faire échouer une requête de traitement
    par ailleurs réussie.

    dateKey est une simple chaîne "YYYY-MM-DD" (UTC) à côté du vrai
    timestamp, pour que Zoho Analytics / n'importe quel rapport puisse
    grouper par jour sans avoir besoin de logique de troncature de date
    sensible au fuseau horaire sur le champ timestamp.
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
    """Recherche l'id de fiche d'un Contact par e-mail. Retourne None si
    aucun contact ne correspond — c'est à l'appelant de décider si c'est
    une erreur ou un abandon silencieux."""
    response = requests.get(
        f"{config.ZOHO_API_DOMAIN}/crm/v8/Contacts/search",
        headers=_auth_headers(),
        params={"criteria": f"(Email:equals:{email})"},
        timeout=30,
    )
    if response.status_code == 204:  # Zoho retourne 204, pas un 200 vide, pour "aucune correspondance"
        return None
    response.raise_for_status()
    records = response.json().get("data", [])
    return records[0]["id"] if records else None


def get_unuploaded_outputs(firestore_client, uid, limit=None):
    """Retourne chaque doc processedOutputs pas encore uploadé et non
    confidentiel pour cet utilisateur, du plus récent au plus ancien,
    plafonné à `limit` (par défaut config.MAX_UNSATISFIED_ATTACHMENTS)
    pour qu'un seul appel de webhook ne puisse pas essayer de traiter un
    backlog illimité. Gardé du plus récent au plus ancien (même sens que
    la précédente version à un seul doc) pour réutiliser le même index
    composite (uid, uploadedToZoho, confidential, createdAt) plutôt que
    d'en exiger un nouveau."""
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
    """Construit un nom lisible pour une pièce jointe Zoho. `kind` vaut
    "input" ou "output".

    Quand le nom de fichier original du client est connu
    (original_file_name — stocké sur le doc depuis que
    save_processed_output_record a commencé à l'enregistrer ; voir la
    docstring de cette fonction pour savoir pourquoi il n'est pas
    simplement reparsé en sens inverse à partir du chemin sur disque à la
    place), il ouvre le nom de la pièce jointe, ex.
    'VoixEtBruit90dB_noise-filter_output_2026-08-06_16h27m22s.wav' — pour
    que le support puisse référencer "le fichier VoixEtBruit90dB" en
    faisant un suivi avec un client. Retombe sur
    'noise-filter_2026-08-06_16h27m22s_output.wav' pour les anciens docs
    sauvegardés avant que ce champ n'existe.

    Le nom de la fonctionnalité reste dans les deux formes — le même
    fichier original peut être soumis à plus d'une fonctionnalité (ex. le
    même enregistrement passé à la fois par NoizOff et Voice Imitation),
    et sans ça ces deux sorties seraient indiscernables sauf par
    timestamp.

    Une date+heure reste aussi dans le nom dans les deux cas, pour rester
    unique : avec des noms qui entrent exactement en collision, Zoho
    ajoute silencieusement son propre suffixe de timestamp au deuxième
    upload et suivants partageant un nom de fichier, ce qui se lit comme
    un bug de renommage/duplication côté CRM plutôt que ce que c'est
    réellement (plusieurs fichiers distincts, nommés de façon ambiguë —
    ex. un client qui refait un test avec un fichier du même nom deux
    fois dans la même journée). Une entrée et sa sortie correspondante
    partagent le même `created_at` (les deux viennent du même doc
    processedOutputs), donc elles finissent quand même comme une paire
    clairement identifiable dans la liste de pièces jointes de Zoho.
    """
    ext = os.path.splitext(original_path)[1]
    date_str = created_at.strftime("%Y-%m-%d_%Hh%Mm%Ss") if created_at else "unknown-date"
    safe_feature = feature or "unknown-feature"

    if original_file_name:
        stem = os.path.splitext(os.path.basename(original_file_name))[0]
        # Reste proche du nom que le client a réellement donné à son
        # fichier — remplace seulement les caractères gênants dans un nom
        # de fichier que Zoho va afficher et laisser quelqu'un cliquer.
        safe_stem = re.sub(r"[^\w\-. ]+", "_", stem).strip() or "file"
        return f"{safe_stem}_{safe_feature}_{kind}_{date_str}{ext}"

    # Repli pour les docs sauvegardés avant que original_file_name n'existe.
    return f"{safe_feature}_{date_str}_{kind}{ext}"

_access_token = None
_expires_at = 0.0


def get_access_token():
    """Retourne un access token mis en cache, en le rafraîchissant via
    ZOHO_REFRESH_TOKEN une fois expiré. Pas thread-safe contre une
    première course de rafraîchissement concurrent, mais un appel de
    rafraîchissement en double est inoffensif (Zoho émet juste un autre
    access token valide), donc aucun verrou n'est nécessaire ici."""
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
    """Répercute sur la fiche Contact Zoho de l'utilisateur le drapeau de
    confidentialité qu'il a déclaré pour son fichier le plus récent. Ne
    lève jamais — un pépin de synchro Zoho ici ne doit pas bloquer le
    traitement du fichier lui-même."""
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
    """Upload `file_path` comme pièce jointe sur le Contact Zoho
    `record_id`, affiché dans Zoho comme `display_filename` plutôt que le
    nom brut du fichier sur disque (qui est une chaîne interne
    '{uid}_{uuid}_{nom original}')."""
    with open(file_path, "rb") as handle:
        response = requests.post(
            f"{config.ZOHO_API_DOMAIN}/crm/v8/Contacts/{record_id}/Attachments",
            files={"file": (display_filename, handle)},
            headers=_auth_headers(),
            timeout=60,
        )
    response.raise_for_status()
    return response.json()