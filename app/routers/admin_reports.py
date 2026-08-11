from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import config
from app.deps import get_current_user
from app.services.reporting import build_report, send_report_email

router = APIRouter()

_VALID_RANGES = {"day", "week", "3months", "6months"}


class SendReportRequest(BaseModel):
    range: str
    # Optional "YYYY-MM-DD" — defaults to today (UTC) if omitted. Lets an
    # admin pull a report for a specific past date instead of always "as of
    # now".
    reference_date: str | None = None


def require_admin(decoded_token: dict = Depends(get_current_user)) -> dict:
    """Layered on top of get_current_user: same bearer-token verification
    every other route uses, plus an allow-list check. This is the actual
    access control for /api/admin/* — the client-side check in
    admin-reports.js is UX only and can't be trusted on its own, since
    anyone can call this endpoint directly with a valid-but-non-admin
    token."""
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
        from datetime import datetime

        try:
            reference_date = datetime.strptime(body.reference_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="reference_date must be YYYY-MM-DD")

    try:
        report = build_report(body.range, reference_date)
        send_report_email(report)
    except RuntimeError as exc:
        # Config problems (missing SMTP2GO creds, empty recipient list) —
        # surface as a clear 500 rather than a generic one, since these are
        # almost always a one-time setup issue an admin can fix themselves.
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        print(f"send_report failed: {exc}")
        raise HTTPException(status_code=500, detail="Failed to generate or send the report")

    return {
        "ok": True,
        "range": body.range,
        "sentTo": config.ADMIN_REPORT_RECIPIENTS,
    }
