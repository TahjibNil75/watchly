"""CSV renderings of what the API already computes: a site's stats series and
a project's monthly report. Pure functions over those results; the routes do
the access checks."""

import csv
import io

from fastapi import Response

from app.monitoring.alerts.events import ReportEvent
from app.monitoring.websites.schemas import WebsiteStats

#: A cell starting with one of these is read as a formula by spreadsheets, and
#: site names are typed by users.
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def _safe(text: str) -> str:
    return f"'{text}" if text.startswith(_FORMULA_STARTS) else text


def _cell(value: float | int | None) -> float | int | str:
    return "" if value is None else value


def _render(header: list[str], rows: list[list]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()


def stats_csv(stats: WebsiteStats, *, packet_loss: bool = False) -> str:
    """One row per bucket, oldest first, including empty ones (blank figures).
    `packet_loss` adds a column for it, for a ping check."""
    header = [
        "bucket_start_utc",
        "checks",
        "up_checks",
        "uptime_percent",
        "avg_response_ms",
        "p95_response_ms",
    ]
    if packet_loss:
        header.append("packet_loss_percent")
    rows = []
    for b in stats.series:
        row = [
            b.start.isoformat().replace("+00:00", "Z"),
            b.checks,
            b.up_checks,
            _cell(b.uptime_percent),
            _cell(b.avg_response_ms),
            _cell(b.p95_response_ms),
        ]
        if packet_loss:
            row.append(_cell(b.packet_loss_percent))
        rows.append(row)
    return _render(header, rows)


def report_csv(event: ReportEvent) -> str:
    """One row per site that had checks in the month."""
    return _render(
        [
            "site",
            "url",
            "checks",
            "up_checks",
            "uptime_percent",
            "downtime_seconds",
            "incidents",
            "longest_outage_seconds",
            "avg_response_ms",
            "p95_response_ms",
        ],
        [
            [
                _safe(site.name),
                _safe(site.url),
                site.checks,
                site.up_checks,
                round(site.uptime_percent, 3),
                round(site.downtime_seconds),
                site.incidents,
                round(site.longest_outage_seconds),
                _cell(site.avg_response_ms),
                _cell(site.p95_response_ms),
            ]
            for site in event.sites
        ],
    )


def csv_response(content: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
