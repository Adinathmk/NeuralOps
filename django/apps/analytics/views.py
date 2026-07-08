from datetime import timedelta

from django.db.models import Avg, Count, F, FloatField, IntegerField, Sum
from django.db.models.functions import ExtractHour, ExtractWeekDay, TruncDate
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.permissions import IsTenantUser
from .models import IncidentSnapshot


class DashboardAnalyticsViewSet(viewsets.ViewSet):
    """
    Analytics ViewSet for the unified developer dashboard.
    """
    permission_classes = [IsTenantUser]

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_days(self, request, default=30):
        """Parse the ?days= query param, clamp between 1 and 90."""
        try:
            days = int(request.query_params.get("days", default))
            return max(1, min(days, 90))
        except (ValueError, TypeError):
            return default

    # ── Original endpoints (used by DashboardPage) ────────────────────────────

    @action(detail=False, methods=["get"])
    def metrics(self, request):
        tenant_id = request.tenant_id
        now = timezone.now()
        yesterday = now - timedelta(days=1)

        # 1. Active Criticals
        active_criticals = IncidentSnapshot.objects.exclude(status='draft').filter(
            tenant_id=tenant_id,
            status="open",
            severity="critical"
        ).count()

        # 2. New Issues (created in last 24h)
        new_issues = IncidentSnapshot.objects.exclude(status='draft').filter(
            tenant_id=tenant_id,
            created_at__gte=yesterday
        ).count()

        # 3. Avg MTTR
        resolved_incidents = IncidentSnapshot.objects.exclude(status='draft').filter(
            tenant_id=tenant_id,
            status__in=["resolved", "closed"],
            resolved_at__isnull=False,
            created_at__isnull=False
        )
        avg_mttr_delta = resolved_incidents.aggregate(
            avg_mttr=Avg(F("resolved_at") - F("created_at"))
        )["avg_mttr"]

        avg_mttr_str = "0m"
        if avg_mttr_delta:
            total_minutes = int(avg_mttr_delta.total_seconds() / 60)
            if total_minutes > 60:
                hours = total_minutes / 60
                avg_mttr_str = f"{hours:.1f}h"
            else:
                avg_mttr_str = f"{total_minutes}m"

        return Response({
            "active_criticals": active_criticals,
            "new_issues": new_issues,
            "avg_mttr": avg_mttr_str
        })

    @action(detail=False, methods=["get"], url_path="crash-locations")
    def crash_locations(self, request):
        tenant_id = request.tenant_id

        locations = IncidentSnapshot.objects.exclude(status='draft').filter(
            tenant_id=tenant_id,
            crash_file__isnull=False
        ).exclude(
            crash_file=""
        ).values(
            "crash_file", "error_type"
        ).annotate(
            total_count=Sum("occurrence_count")
        ).order_by("-total_count")[:5]

        result = [
            {
                "path": loc["crash_file"],
                "type": loc["error_type"] or "UnknownError",
                "count": loc["total_count"]
            }
            for loc in locations
        ]

        return Response(result)

    @action(detail=False, methods=["get"], url_path="actionable-incidents")
    def actionable_incidents(self, request):
        tenant_id = request.tenant_id

        incidents = IncidentSnapshot.objects.filter(
            tenant_id=tenant_id,
            status__in=["open", "investigating", "draft"]
        ).order_by("-created_at")[:5]

        result = []
        for inc in incidents:
            result.append({
                "id": str(inc.incident_id),
                "error_type": inc.error_type,
                "error_message": inc.error_message,
                "severity": inc.severity,
                "service_name": inc.service_name,
                "created_at": inc.created_at.isoformat() if inc.created_at else None,
                "file_path": f"{inc.crash_file}:{inc.crash_method}" if inc.crash_file else inc.crash_method,
            })

        return Response(result)

    @action(detail=False, methods=["get"], url_path="insights")
    def insights(self, request):
        tenant_id = request.tenant_id

        incidents = IncidentSnapshot.objects.exclude(status='draft').filter(
            tenant_id=tenant_id,
        ).exclude(
            root_cause=""
        ).order_by("-created_at")[:3]

        result = []
        for inc in incidents:
            result.append({
                "id": str(inc.incident_id),
                "title": f"Pattern in {inc.service_name}" if inc.service_name else "Pattern detected",
                "description": inc.root_cause[:100] + "..." if len(inc.root_cause) > 100 else inc.root_cause,
                "type": "performance" if "memory" in inc.root_cause.lower() else "error"
            })

        return Response(result)

    # ── New Analytics Endpoints ───────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="incident-trend")
    def incident_trend(self, request):
        """
        Daily incident counts for the last N days, grouped by current status.
        Returns a list of { date, open, investigating, resolved, closed }.
        """
        tenant_id = request.tenant_id
        days = self._get_days(request, default=30)
        since = timezone.now() - timedelta(days=days)

        rows = (
            IncidentSnapshot.objects.exclude(status='draft')
            .filter(tenant_id=tenant_id, created_at__gte=since)
            .annotate(date=TruncDate("created_at"))
            .values("date", "status")
            .annotate(count=Count("incident_id"))
        )

        date_map = {}
        for r in rows:
            d = r["date"].isoformat()
            if d not in date_map:
                date_map[d] = {"date": d, "open": 0, "investigating": 0, "resolved": 0, "closed": 0}
            
            status = r["status"]
            if status in date_map[d]:
                date_map[d][status] += r["count"]

        result = sorted(date_map.values(), key=lambda x: x["date"])
        return Response(result)

    @action(detail=False, methods=["get"], url_path="severity-distribution")
    def severity_distribution(self, request):
        """
        Count of incidents grouped by severity for the last N days.

        Returns a list of { severity, count } ordered by count descending.
        """
        tenant_id = request.tenant_id
        days = self._get_days(request, default=30)
        since = timezone.now() - timedelta(days=days)

        rows = (
            IncidentSnapshot.objects.exclude(status='draft')
            .filter(tenant_id=tenant_id, created_at__gte=since)
            .values("severity")
            .annotate(count=Count("incident_id"))
            .order_by("-count")
        )

        SEVERITY_ORDER = ["critical", "high", "medium", "low", "info", "unknown"]
        result = sorted(
            [{"severity": r["severity"], "count": r["count"]} for r in rows],
            key=lambda x: SEVERITY_ORDER.index(x["severity"]) if x["severity"] in SEVERITY_ORDER else 99
        )
        return Response(result)

    @action(detail=False, methods=["get"], url_path="status-distribution")
    def status_distribution(self, request):
        """
        Count of incidents grouped by status.

        Returns a list of { status, count }.
        """
        tenant_id = request.tenant_id
        days = self._get_days(request, default=30)
        since = timezone.now() - timedelta(days=days)

        rows = (
            IncidentSnapshot.objects.exclude(status='draft')
            .filter(tenant_id=tenant_id, created_at__gte=since)
            .values("status")
            .annotate(count=Count("incident_id"))
            .order_by("-count")
        )

        result = [{"status": r["status"], "count": r["count"]} for r in rows]
        return Response(result)

    @action(detail=False, methods=["get"], url_path="service-breakdown")
    def service_breakdown(self, request):
        """
        Top services ranked by incident count.

        Returns a list of { service_name, count, avg_confidence, top_severity }.
        """
        tenant_id = request.tenant_id
        days = self._get_days(request, default=30)
        since = timezone.now() - timedelta(days=days)

        rows = (
            IncidentSnapshot.objects.exclude(status='draft')
            .filter(
                tenant_id=tenant_id,
                created_at__gte=since,
            )
            .exclude(service_name="")
            .values("service_name")
            .annotate(
                count=Count("incident_id"),
                avg_confidence=Avg("confidence_score"),
            )
            .order_by("-count")[:10]
        )

        # Determine top severity for each service
        SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4, "unknown": 5}
        result = []
        for row in rows:
            # Find worst severity for this service
            worst = (
                IncidentSnapshot.objects.exclude(status='draft')
                .filter(
                    tenant_id=tenant_id,
                    service_name=row["service_name"],
                    created_at__gte=since,
                )
                .values("severity")
                .annotate(c=Count("incident_id"))
                .order_by()
            )
            top_sev = min(
                (r["severity"] for r in worst),
                key=lambda s: SEVERITY_RANK.get(s, 99),
                default="unknown"
            )
            result.append({
                "service_name": row["service_name"],
                "count": row["count"],
                "avg_confidence": round(row["avg_confidence"] or 0, 2),
                "top_severity": top_sev,
            })

        return Response(result)

    @action(detail=False, methods=["get"], url_path="error-type-breakdown")
    def error_type_breakdown(self, request):
        """
        Top error types ranked by total occurrence count.

        Returns a list of { error_type, incident_count, total_occurrences }.
        """
        tenant_id = request.tenant_id
        days = self._get_days(request, default=30)
        since = timezone.now() - timedelta(days=days)

        rows = (
            IncidentSnapshot.objects.exclude(status='draft')
            .filter(tenant_id=tenant_id, created_at__gte=since)
            .exclude(error_type="")
            .values("error_type")
            .annotate(
                incident_count=Count("incident_id"),
                total_occurrences=Sum("occurrence_count"),
            )
            .order_by("-total_occurrences")[:10]
        )

        result = [
            {
                "error_type": r["error_type"],
                "incident_count": r["incident_count"],
                "total_occurrences": r["total_occurrences"] or 0,
            }
            for r in rows
        ]
        return Response(result)

    @action(detail=False, methods=["get"], url_path="mttr-trend")
    def mttr_trend(self, request):
        """
        Average MTTR (mean time to resolve) per day for resolved incidents.

        Returns a list of { date, avg_minutes }.
        """
        tenant_id = request.tenant_id
        days = self._get_days(request, default=14)
        since = timezone.now() - timedelta(days=days)

        rows = (
            IncidentSnapshot.objects.exclude(status='draft')
            .filter(
                tenant_id=tenant_id,
                status__in=["resolved", "closed"],
                resolved_at__gte=since,
                resolved_at__isnull=False,
                created_at__isnull=False,
            )
            .annotate(date=TruncDate("resolved_at"))
            .values("date")
            .annotate(avg_delta=Avg(F("resolved_at") - F("created_at")))
            .order_by("date")
        )

        result = []
        for row in rows:
            avg_delta = row["avg_delta"]
            avg_minutes = round(avg_delta.total_seconds() / 60, 1) if avg_delta else 0
            result.append({"date": row["date"].isoformat(), "avg_minutes": avg_minutes})

        return Response(result)

    @action(detail=False, methods=["get"], url_path="resolution-funnel")
    def resolution_funnel(self, request):
        """
        Counts at each stage of the resolution lifecycle.

        Returns { open, investigating, resolved, closed, draft } for the period.
        """
        tenant_id = request.tenant_id
        days = self._get_days(request, default=30)
        since = timezone.now() - timedelta(days=days)

        rows = (
            IncidentSnapshot.objects.exclude(status='draft')
            .filter(tenant_id=tenant_id, created_at__gte=since)
            .values("status")
            .annotate(count=Count("incident_id"))
        )

        stage_map = {r["status"]: r["count"] for r in rows}
        funnel = [
            {"stage": "Open",          "count": stage_map.get("open", 0)},
            {"stage": "Investigating", "count": stage_map.get("investigating", 0)},
            {"stage": "Resolved",      "count": stage_map.get("resolved", 0)},
            {"stage": "Closed",        "count": stage_map.get("closed", 0)},
        ]
        return Response(funnel)

    @action(detail=False, methods=["get"], url_path="environment-breakdown")
    def environment_breakdown(self, request):
        """
        Incident counts grouped by deployment environment.

        Returns a list of { environment, count }.
        """
        tenant_id = request.tenant_id
        days = self._get_days(request, default=30)
        since = timezone.now() - timedelta(days=days)

        rows = (
            IncidentSnapshot.objects.exclude(status='draft')
            .filter(tenant_id=tenant_id, created_at__gte=since)
            .exclude(environment="")
            .values("environment")
            .annotate(count=Count("incident_id"))
            .order_by("-count")
        )

        result = [{"environment": r["environment"], "count": r["count"]} for r in rows]
        return Response(result)

    @action(detail=False, methods=["get"], url_path="heatmap")
    def heatmap_data(self, request):
        """
        Incident counts by day-of-week (1=Sunday..7=Saturday) and hour-of-day.

        Returns a list of { day, hour, count } for use in a heatmap grid.
        """
        tenant_id = request.tenant_id
        days = self._get_days(request, default=30)
        since = timezone.now() - timedelta(days=days)

        rows = (
            IncidentSnapshot.objects.exclude(status='draft')
            .filter(tenant_id=tenant_id, created_at__gte=since)
            .annotate(
                dow=ExtractWeekDay("created_at"),
                hour=ExtractHour("created_at"),
            )
            .values("dow", "hour")
            .annotate(count=Count("incident_id"))
            .order_by("dow", "hour")
        )

        result = [
            {"day": r["dow"], "hour": r["hour"], "count": r["count"]}
            for r in rows
        ]
        return Response(result)

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        """
        Aggregated KPI summary for the analytics page header.

        Returns total_incidents, resolved_count, resolution_rate,
        critical_count, avg_mttr_minutes, and period_delta comparisons.
        """
        tenant_id = request.tenant_id
        days = self._get_days(request, default=30)
        now = timezone.now()
        since = now - timedelta(days=days)
        prev_since = since - timedelta(days=days)

        base_qs = IncidentSnapshot.objects.exclude(status='draft').filter(tenant_id=tenant_id)

        # Current period
        current = base_qs.filter(created_at__gte=since)
        total = current.count()
        resolved = current.filter(status__in=["resolved", "closed"]).count()
        critical = current.filter(severity="critical").count()

        # Previous period
        previous = base_qs.filter(created_at__gte=prev_since, created_at__lt=since)
        prev_total = previous.count()
        prev_resolved = previous.filter(status__in=["resolved", "closed"]).count()
        prev_critical = previous.filter(severity="critical").count()

        # MTTR for current period
        avg_mttr_delta = (
            current.filter(
                status__in=["resolved", "closed"],
                resolved_at__isnull=False,
            ).aggregate(avg_mttr=Avg(F("resolved_at") - F("created_at")))["avg_mttr"]
        )
        avg_mttr_minutes = round(avg_mttr_delta.total_seconds() / 60, 1) if avg_mttr_delta else 0

        # Previous period MTTR
        prev_avg_mttr_delta = (
            previous.filter(
                status__in=["resolved", "closed"],
                resolved_at__isnull=False,
            ).aggregate(avg_mttr=Avg(F("resolved_at") - F("created_at")))["avg_mttr"]
        )
        prev_mttr_minutes = round(prev_avg_mttr_delta.total_seconds() / 60, 1) if prev_avg_mttr_delta else 0

        def pct_change(curr, prev):
            if prev == 0:
                return None
            return round(((curr - prev) / prev) * 100, 1)

        resolution_rate = round((resolved / total * 100), 1) if total > 0 else 0
        prev_resolution_rate = round((prev_resolved / prev_total * 100), 1) if prev_total > 0 else 0

        return Response({
            "period_days": days,
            "total_incidents": total,
            "total_delta_pct": pct_change(total, prev_total),
            "resolved_count": resolved,
            "resolution_rate": resolution_rate,
            "resolution_rate_delta_pct": pct_change(resolution_rate, prev_resolution_rate),
            "critical_count": critical,
            "critical_delta_pct": pct_change(critical, prev_critical),
            "avg_mttr_minutes": avg_mttr_minutes,
            "mttr_delta_pct": pct_change(avg_mttr_minutes, prev_mttr_minutes),
        })
