"""Builds the admin usage/satisfaction report and sends it by email via
SMTP2GO. Reads straight from the usageEvents/satisfactionEvents Firestore
collections (the same source these get synced *from* into Zoho CRM), so the
report is always at least as fresh as anything in Zoho Analytics and doesn't
need separate Zoho Analytics API credentials.
"""
import io
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app import config
from app.services.firebase import get_firestore_client

# Friendlier labels for the workbook than the raw internal feature keys.
# "noizoff" is old test data logged before the naming-consistency fix
# (auth-guard.js now sends "noiseFilter" to match config.FEATURE_KEY_*) —
# kept as a distinct legacy label rather than merged into "NoizOff", so old
# rows don't silently get counted alongside new ones under one name.
FEATURE_DISPLAY_NAMES = {
    "noiseFilter": "NoizOff",
    "imitation": "Voice Imitation",
    "deepfake": "Deepfake Detection",
    "frameRecovery": "Frame Recovery",
    "unknown": "Unspecified",
}

# Old/alternate spellings that should be counted as the same feature during
# aggregation. "noizoff" was what the survey link sent before it was
# aligned to match Usage_Events' "noiseFilter" — same feature, just logged
# under a different string for a while. Applied at read time in
# build_report(), so it merges cleanly into one "NoizOff" row without
# touching the underlying Zoho/Firestore records.
FEATURE_ALIASES = {
    "noizoff": "noiseFilter",
}


def _normalize_feature(feature):
    feature = feature or "unknown"
    return FEATURE_ALIASES.get(feature, feature)


_HEADER_FILL = PatternFill(start_color="5A4BFF", end_color="5A4BFF", fill_type="solid")
_HEADER_FONT = Font(color="FFFFFF", bold=True)

RANGE_LABELS = {
    "day": "Last day",
    "week": "Last 7 days",
    "3months": "Last 3 months",
    "6months": "Last 6 months",
}

# Longer ranges are bucketed by month instead of by day, so a 6-month email
# stays a handful of rows instead of ~180 — day/week ranges are short enough
# that a per-day breakdown is still readable.
_MONTH_BUCKETED_RANGES = {"3months", "6months"}

_RANGE_TO_DAYS = {
    "day": 1,
    "week": 7,
    # Calendar months vary in length; a fixed day-count is an approximation
    # good enough for a reporting window (not used for anything billing- or
    # legally-sensitive). Avoids pulling in a dateutil dependency just for
    # this.
    "3months": 91,
    "6months": 182,
}


def resolve_date_range(range_key, reference_date=None):
    """Returns (start, end) as UTC datetimes, end being the end of
    reference_date (defaults to today) and start being `range_key` back from
    there, inclusive."""
    if range_key not in _RANGE_TO_DAYS:
        raise ValueError(f"Unknown range: {range_key!r}. Expected one of {sorted(_RANGE_TO_DAYS)}.")

    if reference_date is None:
        reference_date = datetime.now(timezone.utc).date()

    end = datetime.combine(reference_date, datetime.max.time(), tzinfo=timezone.utc)
    start = end - timedelta(days=_RANGE_TO_DAYS[range_key]) + timedelta(microseconds=1)
    return start, end


def _bucket_label(dt, range_key):
    if range_key in _MONTH_BUCKETED_RANGES:
        return dt.strftime("%Y-%m")
    return dt.strftime("%Y-%m-%d")


def _fetch_events(collection_name, start, end):
    client = get_firestore_client()
    query = (
        client.collection(collection_name)
        .where("createdAt", ">=", start)
        .where("createdAt", "<=", end)
    )
    docs = []
    for snap in query.stream():
        data = snap.to_dict() or {}
        created_at = data.get("createdAt")
        # Firestore timestamps come back as datetime objects already.
        if created_at is not None:
            docs.append(data)
    return docs


def build_report(range_key, reference_date=None):
    """Returns a dict with everything render_report_html needs: the
    resolved date window, a bucketed usage table (unique users + event count
    per bucket per feature), a bucketed satisfaction table (average score +
    response count per bucket per feature), and whole-period totals per
    feature for a quick top-line summary."""
    start, end = resolve_date_range(range_key, reference_date)

    usage_events = _fetch_events("usageEvents", start, end)
    satisfaction_events = _fetch_events("satisfactionEvents", start, end)

    # bucket -> feature -> set(uid)   (unique users, deduped per bucket)
    usage_buckets = {}
    # feature -> set(uid) across the whole period, for the top-line summary
    usage_totals = {}

    for event in usage_events:
        feature = _normalize_feature(event.get("feature"))
        uid = event.get("uid")
        created_at = event.get("createdAt")
        if created_at is None or uid is None:
            continue
        bucket = _bucket_label(created_at, range_key)
        usage_buckets.setdefault(bucket, {}).setdefault(feature, set()).add(uid)
        usage_totals.setdefault(feature, set()).add(uid)

    # bucket -> feature -> list[score]
    satisfaction_buckets = {}
    # feature -> list[score] across the whole period
    satisfaction_totals = {}

    for event in satisfaction_events:
        feature = _normalize_feature(event.get("feature"))
        score = event.get("score")
        created_at = event.get("createdAt")
        if created_at is None or score is None:
            continue
        try:
            score = float(score)
        except (TypeError, ValueError):
            continue
        bucket = _bucket_label(created_at, range_key)
        satisfaction_buckets.setdefault(bucket, {}).setdefault(feature, []).append(score)
        satisfaction_totals.setdefault(feature, []).append(score)

    return {
        "range_key": range_key,
        "range_label": RANGE_LABELS[range_key],
        "start": start,
        "end": end,
        "usage_buckets": usage_buckets,
        "usage_totals": {feature: len(uids) for feature, uids in usage_totals.items()},
        "satisfaction_buckets": satisfaction_buckets,
        "satisfaction_totals": {
            feature: {"average": sum(scores) / len(scores), "count": len(scores)}
            for feature, scores in satisfaction_totals.items()
        },
    }


def _fmt_avg(value):
    return f"{value:.1f}"


def _display_feature(feature):
    return FEATURE_DISPLAY_NAMES.get(feature, feature)


def _style_header_row(ws, row_idx, num_cols):
    for col_idx in range(1, num_cols + 1):
        cell = ws.cell(row=row_idx, column=col_idx)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center")


def _autofit_columns(ws, widths):
    for col_idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width


def _write_summary_sheet(ws, report):
    features = sorted(set(report["usage_totals"]) | set(report["satisfaction_totals"]))

    ws["A1"] = "CandyVoice usage & satisfaction report"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = report["range_label"]
    ws["A3"] = f"{report['start'].date()} — {report['end'].date()}"
    ws["A2"].font = ws["A3"].font = Font(color="666666")

    header_row = 5
    headers = ["Feature", "Unique users", "Avg. satisfaction", "Ratings"]
    for col_idx, header in enumerate(headers, start=1):
        ws.cell(row=header_row, column=col_idx, value=header)
    _style_header_row(ws, header_row, len(headers))

    for offset, feature in enumerate(features, start=1):
        row = header_row + offset
        satisfaction = report["satisfaction_totals"].get(feature)
        ws.cell(row=row, column=1, value=_display_feature(feature))
        ws.cell(row=row, column=2, value=report["usage_totals"].get(feature, 0))
        ws.cell(row=row, column=3, value=round(satisfaction["average"], 1) if satisfaction else None)
        ws.cell(row=row, column=4, value=satisfaction["count"] if satisfaction else 0)

    last_row = header_row + len(features)
    _autofit_columns(ws, [26, 14, 16, 10])

    if not features:
        ws.cell(row=header_row + 1, column=1, value="No activity in this period.")
        return

    # Unique users by feature
    users_chart = BarChart()
    users_chart.title = "Unique users by feature"
    users_chart.y_axis.title = "Unique users"
    users_data = Reference(ws, min_col=2, min_row=header_row, max_row=last_row)
    categories = Reference(ws, min_col=1, min_row=header_row + 1, max_row=last_row)
    users_chart.add_data(users_data, titles_from_data=True)
    users_chart.set_categories(categories)
    users_chart.width, users_chart.height = 14, 8
    ws.add_chart(users_chart, f"F{header_row}")

    # Average satisfaction by feature
    satisfaction_chart = BarChart()
    satisfaction_chart.title = "Average satisfaction by feature (0-10)"
    satisfaction_chart.y_axis.title = "Avg. score"
    satisfaction_chart.y_axis.scaling.min = 0
    satisfaction_chart.y_axis.scaling.max = 10
    satisfaction_data = Reference(ws, min_col=3, min_row=header_row, max_row=last_row)
    satisfaction_chart.add_data(satisfaction_data, titles_from_data=True)
    satisfaction_chart.set_categories(categories)
    satisfaction_chart.width, satisfaction_chart.height = 14, 8
    ws.add_chart(satisfaction_chart, f"F{header_row + 17}")


def _pivot_usage_by_period(report):
    periods = sorted(report["usage_buckets"].keys())
    features = sorted({f for bucket in report["usage_buckets"].values() for f in bucket})
    rows = [
        [len(report["usage_buckets"].get(period, {}).get(feature, set())) for feature in features]
        for period in periods
    ]
    return periods, features, rows


def _pivot_satisfaction_by_period(report):
    periods = sorted(report["satisfaction_buckets"].keys())
    features = sorted({f for bucket in report["satisfaction_buckets"].values() for f in bucket})
    rows = []
    for period in periods:
        row = []
        for feature in features:
            scores = report["satisfaction_buckets"].get(period, {}).get(feature, [])
            row.append(round(sum(scores) / len(scores), 1) if scores else None)
        rows.append(row)
    return periods, features, rows


def _write_pivot_sheet(ws, periods, features, rows, value_label, chart_cls, bucket_word):
    ws["A1"] = f"{value_label} by {bucket_word} and feature"
    ws["A1"].font = Font(bold=True, size=12)

    header_row = 3
    ws.cell(row=header_row, column=1, value=bucket_word.capitalize())
    for col_idx, feature in enumerate(features, start=2):
        ws.cell(row=header_row, column=col_idx, value=_display_feature(feature))
    _style_header_row(ws, header_row, len(features) + 1)

    for row_offset, period in enumerate(periods, start=1):
        row = header_row + row_offset
        ws.cell(row=row, column=1, value=period)
        for col_idx, value in enumerate(rows[row_offset - 1], start=2):
            ws.cell(row=row, column=col_idx, value=value)

    _autofit_columns(ws, [14] + [22] * len(features))

    if not periods or not features:
        ws.cell(row=header_row + 1, column=1, value="No activity in this period.")
        return

    last_row = header_row + len(periods)
    chart = chart_cls()
    chart.title = f"{value_label} over time"
    data = Reference(ws, min_col=2, max_col=1 + len(features), min_row=header_row, max_row=last_row)
    categories = Reference(ws, min_col=1, min_row=header_row + 1, max_row=last_row)
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(categories)
    chart.width, chart.height = 20, 10
    ws.add_chart(chart, f"A{last_row + 3}")


def build_report_workbook(report):
    """Builds the .xlsx attachment: a Summary sheet (per-feature totals +
    two bar charts) and two detail sheets pivoted period-by-feature (usage,
    satisfaction), each with its own trend chart. Using a real workbook
    instead of CSV sidesteps two problems at once: comma-delimited CSVs
    open garbled in Excel under locales that expect ";" as the list
    separator (e.g. French), and a CSV can't hold charts at all — it's
    plain text.
    """
    wb = Workbook()

    summary_ws = wb.active
    summary_ws.title = "Summary"
    _write_summary_sheet(summary_ws, report)

    bucket_word = "month" if report["range_key"] in _MONTH_BUCKETED_RANGES else "day"

    usage_ws = wb.create_sheet("Usage detail")
    _write_pivot_sheet(
        usage_ws, *_pivot_usage_by_period(report),
        value_label="Unique users", chart_cls=BarChart, bucket_word=bucket_word,
    )

    satisfaction_ws = wb.create_sheet("Satisfaction detail")
    _write_pivot_sheet(
        satisfaction_ws, *_pivot_satisfaction_by_period(report),
        value_label="Avg. satisfaction", chart_cls=LineChart, bucket_word=bucket_word,
    )

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def render_report_html(report):
    features = sorted(
        set(report["usage_totals"]) | set(report["satisfaction_totals"])
    )

    summary_rows = "".join(
        f"<tr>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{feature}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>"
        f"{report['usage_totals'].get(feature, 0)}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>"
        f"{_fmt_avg(report['satisfaction_totals'][feature]['average']) if feature in report['satisfaction_totals'] else '—'}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>"
        f"{report['satisfaction_totals'][feature]['count'] if feature in report['satisfaction_totals'] else 0}</td>"
        f"</tr>"
        for feature in features
    ) or "<tr><td colspan='4' style='padding:12px'>No activity in this period.</td></tr>"

    bucket_labels = sorted(set(report["usage_buckets"]) | set(report["satisfaction_buckets"]))
    detail_rows = ""
    for bucket in bucket_labels:
        usage_by_feature = report["usage_buckets"].get(bucket, {})
        satisfaction_by_feature = report["satisfaction_buckets"].get(bucket, {})
        bucket_features = sorted(set(usage_by_feature) | set(satisfaction_by_feature))
        for feature in bucket_features:
            unique_users = len(usage_by_feature.get(feature, set()))
            scores = satisfaction_by_feature.get(feature, [])
            avg_score = _fmt_avg(sum(scores) / len(scores)) if scores else "—"
            detail_rows += (
                f"<tr>"
                f"<td style='padding:4px 12px;border-bottom:1px solid #f2f2f2'>{bucket}</td>"
                f"<td style='padding:4px 12px;border-bottom:1px solid #f2f2f2'>{feature}</td>"
                f"<td style='padding:4px 12px;border-bottom:1px solid #f2f2f2;text-align:right'>{unique_users}</td>"
                f"<td style='padding:4px 12px;border-bottom:1px solid #f2f2f2;text-align:right'>{avg_score}</td>"
                f"</tr>"
            )
    if not detail_rows:
        detail_rows = "<tr><td colspan='4' style='padding:12px'>No activity in this period.</td></tr>"

    period = f"{report['start'].date()} — {report['end'].date()}"

    return f"""
    <html><body style="font-family:Arial,Helvetica,sans-serif;color:#222">
      <h2 style="margin-bottom:0">CandyVoice usage &amp; satisfaction report</h2>
      <p style="color:#666;margin-top:4px">{report['range_label']} ({period})</p>

      <h3>Summary</h3>
      <table style="border-collapse:collapse;width:100%;max-width:640px">
        <thead>
          <tr style="text-align:left;border-bottom:2px solid #ccc">
            <th style="padding:6px 12px">Feature</th>
            <th style="padding:6px 12px;text-align:right">Unique users</th>
            <th style="padding:6px 12px;text-align:right">Avg. satisfaction</th>
            <th style="padding:6px 12px;text-align:right">Ratings</th>
          </tr>
        </thead>
        <tbody>{summary_rows}</tbody>
      </table>

      <h3 style="margin-top:32px">Breakdown by {'month' if report['range_key'] in {'3months', '6months'} else 'day'}</h3>
      <table style="border-collapse:collapse;width:100%;max-width:640px;font-size:13px">
        <thead>
          <tr style="text-align:left;border-bottom:2px solid #ccc">
            <th style="padding:4px 12px">Period</th>
            <th style="padding:4px 12px">Feature</th>
            <th style="padding:4px 12px;text-align:right">Unique users</th>
            <th style="padding:4px 12px;text-align:right">Avg. satisfaction</th>
          </tr>
        </thead>
        <tbody>{detail_rows}</tbody>
      </table>

      <p style="color:#999;font-size:12px;margin-top:32px">
        Generated automatically from CandyVoice's usageEvents / satisfactionEvents data.
      </p>
    </body></html>
    """


def send_report_email(report):
    if not config.SMTP2GO_USERNAME or not config.SMTP2GO_PASSWORD:
        raise RuntimeError(
            "SMTP2GO_USERNAME / SMTP2GO_PASSWORD are not configured — set them as "
            "environment variables before sending admin reports."
        )
    if not config.ADMIN_REPORT_RECIPIENTS:
        raise RuntimeError("ADMIN_REPORT_RECIPIENTS is empty — nothing to send the report to.")

    period = f"{report['start'].date()} — {report['end'].date()}"
    workbook_bytes = build_report_workbook(report)
    filename = f"candyvoice-report-{report['range_key']}-{report['end'].date()}.xlsx"

    message = MIMEMultipart("mixed")
    message["Subject"] = f"CandyVoice report — {report['range_label']}"
    message["From"] = f"{config.SMTP2GO_FROM_NAME} <{config.SMTP2GO_FROM_EMAIL}>"
    message["To"] = ", ".join(config.ADMIN_REPORT_RECIPIENTS)

    body_text = (
        f"CandyVoice usage & satisfaction report — {report['range_label']} ({period}).\n\n"
        f"See the attached workbook ({filename}): a Summary sheet with per-feature "
        f"totals and charts, plus Usage detail / Satisfaction detail sheets broken "
        f"down by {'month' if report['range_key'] in _MONTH_BUCKETED_RANGES else 'day'}, "
        f"each with its own trend chart."
    )
    message.attach(MIMEText(body_text, "plain"))

    attachment = MIMEApplication(
        workbook_bytes,
        _subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    attachment.add_header("Content-Disposition", "attachment", filename=filename)
    message.attach(attachment)

    with smtplib.SMTP(config.SMTP2GO_HOST, config.SMTP2GO_PORT) as server:
        server.starttls()
        server.login(config.SMTP2GO_USERNAME, config.SMTP2GO_PASSWORD)
        server.sendmail(
            config.SMTP2GO_FROM_EMAIL,
            config.ADMIN_REPORT_RECIPIENTS,
            message.as_string(),
        )
