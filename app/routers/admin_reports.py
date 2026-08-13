from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import config
from app.deps import get_current_user
from app.services.api_usage_report import build_api_usage_report, send_api_usage_report_email
from app.services.reporting import build_report, send_report_email

router = APIRouter()

_VALID_RANGES = {"day", "week", "3months", "6months"}


class SendReportRequest(BaseModel):
    range: str
    # "YYYY-MM-DD" optionnel — par défaut aujourd'hui (UTC) si omis. Permet
    # à un admin de récupérer un rapport pour une date passée précise plutôt
    # que toujours "à l'instant présent".
    reference_date: str | None = None


class SendApiUsageReportRequest(BaseModel):
    # "YYYY-MM" optionnel — par défaut le mois calendaire UTC en cours si
    # omis. Permet à un admin de récupérer à la demande le rapport d'un
    # mois passé, plutôt que de ne voir que le mois en cours.
    period: str | None = None


def require_admin(decoded_token: dict = Depends(get_current_user)) -> dict:
    """Superposé à get_current_user : même vérification de bearer token que
    toutes les autres routes, plus une vérification par liste d'autorisation.
    C'est le véritable contrôle d'accès pour /api/admin/* — la vérification
    côté client dans admin-reports.js n'est que pour le confort d'usage et
    ne peut pas être fiable seule, puisque n'importe qui peut appeler cet
    endpoint directement avec un token valide mais non-admin."""
    email = (decoded_token.get("email") or "").strip().lower()
    if not email or email not in config.ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Not authorized to access admin reports")
    return decoded_token


@router.post("/api/admin/send-report")
async def send_report(body: SendReportRequest, _admin: dict = Depends(require_admin)):
    if body.range not in _VALID_RANGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid range {body.range!r} — expected one of {sorted(_VALID_RANGES)}",
        )

    reference_date = None
    if body.reference_date:
        try:
            reference_date = datetime.strptime(body.reference_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="reference_date must be YYYY-MM-DD")

    try:
        report = build_report(body.range, reference_date)
        send_report_email(report)
    except RuntimeError as exc:
        # Problèmes de config (identifiants SMTP2GO manquants, liste de
        # destinataires vide) — remontés en 500 clair plutôt qu'en erreur
        # générique, puisque c'est presque toujours un problème de
        # configuration ponctuel qu'un admin peut corriger lui-même.
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        print(f"send_report failed: {exc}")
        raise HTTPException(status_code=500, detail="Failed to generate or send the report")

    return {
        "ok": True,
        "range": body.range,
        "sentTo": config.ADMIN_REPORT_RECIPIENTS,
    }


@router.post("/api/admin/send-api-usage-report")
async def send_api_usage_report(body: SendApiUsageReportRequest, _admin: dict = Depends(require_admin)):
    """Envoie par e-mail un récapitulatif par utilisateur de l'usage des
    clés API pour un mois calendaire — l'usage de chaque clé (les mêmes
    données que GET /api/keys/{key_id}/usage expose par clé) sommé par
    utilisateur, en pièce jointe .xlsx. Voir api_usage_report.py pour
    l'agrégation elle-même."""
    period = None
    if body.period:
        try:
            period = datetime.strptime(body.period, "%Y-%m").strftime("%Y-%m")
        except ValueError:
            raise HTTPException(status_code=400, detail="period must be YYYY-MM")

    try:
        report = build_api_usage_report(period)
        send_api_usage_report_email(report)
    except RuntimeError as exc:
        # Problèmes de config (identifiants SMTP2GO manquants, liste de
        # destinataires vide) — remontés en 500 clair plutôt qu'en erreur
        # générique, puisque c'est presque toujours un problème de
        # configuration ponctuel qu'un admin peut corriger lui-même.
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        print(f"send_api_usage_report failed: {exc}")
        raise HTTPException(status_code=500, detail="Failed to generate or send the API usage report")

    return {
        "ok": True,
        "period": report["period"],
        "users": len(report["rows"]),
        "sentTo": config.ADMIN_REPORT_RECIPIENTS,
    }
