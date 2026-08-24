from datetime import timedelta, datetime
from decimal import Decimal, InvalidOperation
from django.views.generic import TemplateView, View, ListView, CreateView, UpdateView, DetailView, DeleteView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.http import Http404, JsonResponse
from django.urls import reverse_lazy
from django.contrib import messages
from django.db.models import Q, Sum
import json
import zoneinfo

from .conf import app_settings
from .models import Printer, PrinterMetrics, Filament, FilamentColor, FilamentType, FilamentSnapshot, PrintJob, FilamentUsage, Hotend
from .forms import FilamentForm, FilamentColorForm, FilamentTypeForm

# Every field the chart serializers read must be listed here. A field that is
# accessed but missing triggers a deferred-field load — one extra SELECT per row,
# which turns a single-query page into thousands.
_METRICS_API_FIELDS = [
    'id', 'device_id', 'timestamp',
    'nozzle_temp', 'nozzle_target_temp',
    'nozzle_temp_left', 'nozzle_target_temp_left',
    'bed_temp', 'bed_target_temp',
    'print_percent', 'cooling_fan_speed', 'heatbreak_fan_speed',
    'wifi_signal_dbm', 'ams_humidity_raw', 'ams_temp',
    'layer_num', 'total_layer_num',
    'gcode_state', 'print_type', 'subtask_name',
    'external_spool',
]
# 24h at the collector's 30s cadence is ~2800 readings, and every one of them
# also drags in ~9 FilamentSnapshot rows — that snapshot fetch, not the metrics
# query, is what dominated the dashboard's load time (measured: 2.09s of context
# building and a 410 KB payload at 3000). 1440 caps the series at roughly one
# point per minute over a day, which is finer than any chart can resolve on
# screen, and cuts both the server time and the payload by ~4x.
_MAX_CHART_POINTS = 1440
# Fallback window for requests that don't specify a full date range. Without it a
# bare API call scans the entire metrics table.
_DEFAULT_WINDOW = timedelta(hours=24)


def numeric_json_value(value):
    if value is None:
        return None
    return float(value)


def resolve_printer_from_request(pk):
    """Resolve which Printer a dashboard/API view should show.

    `pk` given (URL kwarg) -> that exact printer, 404 if missing/inactive.
    `pk` omitted -> first active printer (today's single-printer default behavior).

    Both paths go through `Printer.objects`, which is category-scoped, so a
    non-printer row sharing `infrastructure_device` (a NAS, a router) can never be
    resolved as "the printer" — even when no active printer exists.
    """
    if pk is not None:
        return get_object_or_404(Printer, pk=pk, is_active=True)
    return Printer.objects.filter(is_active=True).first()


def sample_metrics(metrics_list, max_points=None):
    """Evenly thin a metrics list to at most `max_points`, always keeping the last
    reading — the stat cards are built from it."""
    max_points = max_points or _MAX_CHART_POINTS
    total = len(metrics_list)
    if total <= max_points:
        return metrics_list
    step = (total // max_points) + 1
    sampled = metrics_list[::step]
    if sampled[-1] is not metrics_list[-1]:
        sampled.append(metrics_list[-1])
    return sampled


def fetch_snapshots_by_metric(metrics_list):
    """Load filament snapshots for exactly the metrics we're serializing.

    Beats `prefetch_related` on the unsampled queryset, which pulls a snapshot row
    for every metric in the window (~25k rows for 24h) including the ones sampling
    just discarded.
    """
    if not metrics_list:
        return {}
    snapshots_by_metric = {}
    for snap in FilamentSnapshot.objects.filter(
        printer_metric_id__in=[m.id for m in metrics_list]
    ):
        snapshots_by_metric.setdefault(snap.printer_metric_id, []).append(snap)
    return snapshots_by_metric


class PrinterDashboardView(LoginRequiredMixin, TemplateView):
    template_name = "bambu_run/printer_dashboard.html"

    def _get_date_range(self, request):
        """Return (start_dt, end_dt) for the dashboard query. Override for custom date logic."""
        time_24h_ago = timezone.now() - timedelta(hours=24)
        return time_24h_ago, None  # None means "now"

    def _valid_display_percent(self, value):
        try:
            value = Decimal(str(value))
        except (TypeError, ValueError, InvalidOperation):
            return None
        if Decimal("0") <= value <= Decimal("100"):
            return value
        return None

    def _ams_units_meta_by_id(self, ams_units):
        units_meta = {}
        for unit in ams_units or []:
            unit_id = unit.get('unit_id')
            units_meta[unit_id] = unit
            try:
                units_meta[int(unit_id)] = unit
            except (TypeError, ValueError):
                pass
        return units_meta

    def _ams_display_humidity(self, unit_meta):
        """Return the RH percentage Bambu Studio shows, not the AMS humidity level."""
        for key in ('humidity_raw', 'humidity'):
            try:
                value = int(unit_meta.get(key))
            except (TypeError, ValueError):
                continue
            if 0 <= value <= 100:
                return value
        return None

    def _filament_display_color(self, value, default='FFFFFFFF'):
        value = (value or '').strip()
        if value.startswith('#'):
            value = value[1:]
        if len(value) == 6:
            return f"{value.upper()}FF"
        if len(value) >= 8:
            return value[:8].upper()
        return default

    def _external_spool_has_display_info(self, external_spool):
        if not external_spool:
            return False
        useful_values = (
            external_spool.get('type') or external_spool.get('tray_type'),
            external_spool.get('sub_type') or external_spool.get('tray_sub_brands'),
            external_spool.get('color') or external_spool.get('tray_color'),
            external_spool.get('tray_uuid'),
            external_spool.get('tag_uid'),
            external_spool.get('tray_info_idx'),
        )
        blank_values = {'', '0', '00000000', '0000000000000000', '00000000000000000000000000000000'}
        if any(str(value).strip() not in blank_values for value in useful_values if value is not None):
            return True
        remain_percent = self._valid_display_percent(
            external_spool.get('remain_percent', external_spool.get('remain'))
        )
        return remain_percent is not None and remain_percent > 0

    def _external_filament_from_inventory(self, filament):
        remain_percent = self._valid_display_percent(filament.remaining_percent)
        return {
            'tray_id': 254,
            'type': filament.type or 'Unknown',
            'brand': filament.brand or 'Unknown',
            'color': self._filament_display_color(filament.color_hex),
            'color_name': filament.color,
            'remain_percent': remain_percent,
            'remain_percent_known': remain_percent is not None,
            'ams_unit_id': None,
            'ams_type': 'External Spool',
            'is_external': True,
            'filament_pk': filament.pk,
            'is_transparent': filament.is_transparent,
        }

    def _external_filament_from_metric(self, external_spool):
        remain_percent = self._valid_display_percent(
            external_spool.get('remain_percent', external_spool.get('remain'))
        )
        return {
            'tray_id': 254,
            'type': external_spool.get('type') or external_spool.get('tray_type') or 'Unknown',
            'brand': (
                external_spool.get('sub_type')
                or external_spool.get('tray_sub_brands')
                or 'External Spool'
            ),
            'color': self._filament_display_color(
                external_spool.get('color') or external_spool.get('tray_color')
            ),
            'remain_percent': remain_percent,
            'remain_percent_known': remain_percent is not None,
            'ams_unit_id': None,
            'ams_type': 'External Spool',
            'is_external': True,
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['bambu_run_base_template'] = app_settings.BASE_TEMPLATE

        all_printers = Printer.objects.filter(is_active=True)
        context["all_printers"] = all_printers
        # Shown even with a single printer — hints that multi-printer support exists.
        context["show_printer_switcher"] = all_printers.exists()

        try:
            printer_device = resolve_printer_from_request(self.kwargs.get("pk"))
            if not printer_device:
                context["error"] = (
                    "No 3D printer device found. Please run bambu_collector first."
                )
                return context
        except Http404:
            raise
        except Exception as e:
            context["error"] = f"Error loading printer device: {str(e)}"
            return context

        tz = zoneinfo.ZoneInfo(app_settings.TIMEZONE)

        # Get date range (overridable by subclasses)
        start_dt, end_dt = self._get_date_range(self.request)
        query = PrinterMetrics.objects.filter(
            device=printer_device, timestamp__gte=start_dt
        )
        if end_dt:
            query = query.filter(timestamp__lte=end_dt)

        # Chart series only need the columns the serializer below reads, and only
        # as many points as a chart can render. Fetching every column (including
        # the large JSON blobs) for every row is what made this page slow.
        metrics = sample_metrics(
            list(query.only(*_METRICS_API_FIELDS).order_by("timestamp"))
        )
        snapshots_by_metric = fetch_snapshots_by_metric(metrics)

        # The stat cards read far more fields than the charts do, so the latest
        # reading is fetched separately as a full instance rather than deferring
        # (a deferred field on a sampled row costs an extra query per access).
        latest_metric = (
            query.prefetch_related('filament_snapshots__filament')
            .order_by("-timestamp")
            .first()
        )

        printer_data_json = {
            "timestamps": [
                m.timestamp.astimezone(tz).strftime("%H:%M") for m in metrics
            ],
            "dates": [
                m.timestamp.astimezone(tz).strftime("%Y-%m-%d") for m in metrics
            ],
            "nozzle_temp": [
                float(m.nozzle_temp) if m.nozzle_temp else None for m in metrics
            ],
            "nozzle_target_temp": [
                float(m.nozzle_target_temp) if m.nozzle_target_temp else None
                for m in metrics
            ],
            "nozzle_temp_left": [
                float(m.nozzle_temp_left) if m.nozzle_temp_left is not None else None
                for m in metrics
            ],
            "nozzle_target_temp_left": [
                float(m.nozzle_target_temp_left) if m.nozzle_target_temp_left is not None else None
                for m in metrics
            ],
            "bed_temp": [float(m.bed_temp) if m.bed_temp else None for m in metrics],
            "bed_target_temp": [
                float(m.bed_target_temp) if m.bed_target_temp else None for m in metrics
            ],
            "print_percent": [
                m.print_percent if m.print_percent else 0 for m in metrics
            ],
            "print_type": [m.print_type for m in metrics],
            "gcode_state": [m.gcode_state for m in metrics],
            "cooling_fan_speed": [
                m.cooling_fan_speed if m.cooling_fan_speed else 0 for m in metrics
            ],
            "heatbreak_fan_speed": [
                m.heatbreak_fan_speed if m.heatbreak_fan_speed else 0 for m in metrics
            ],
            "wifi_signal_dbm": [
                m.wifi_signal_dbm if m.wifi_signal_dbm else None for m in metrics
            ],
            "ams_humidity_raw": [
                m.ams_humidity_raw if m.ams_humidity_raw else None for m in metrics
            ],
            "ams_temp": [
                float(m.ams_temp) if m.ams_temp else None for m in metrics
            ],
            "layer_num": [
                m.layer_num if m.layer_num else 0 for m in metrics
            ],
            "total_layer_num": [
                m.total_layer_num if m.total_layer_num else 0 for m in metrics
            ],
            "filament_timeline": self._prepare_filament_timeline(
                metrics, snapshots_by_metric
            ),
        }

        stats = {}
        if latest_metric:
            filaments_list = []
            units_meta = self._ams_units_meta_by_id(latest_metric.ams_units)
            try:
                # `.all()` (not `.select_related()`) so the prefetch cache is used
                filament_snapshots = latest_metric.filament_snapshots.all()
                for snapshot in filament_snapshots:
                    display_type = snapshot.type or 'Unknown'
                    display_brand = snapshot.sub_type or snapshot.brand or 'Unknown'
                    display_color = snapshot.color or 'FFFFFFFF'
                    display_remaining = self._valid_display_percent(snapshot.remain_percent)
                    display_ams_unit_id = snapshot.ams_unit_id
                    display_ams_type = snapshot.ams_type or ''
                    is_external = snapshot.tray_id == 254 or snapshot.match_method == 'manual_loaded_external'
                    use_inventory_display = (
                        snapshot.filament
                        and snapshot.match_method != 'auto_created'
                    )
                    if snapshot.filament:
                        if snapshot.filament.is_loaded_externally:
                            is_external = True
                        if display_ams_unit_id is None and not is_external:
                            display_ams_unit_id = snapshot.filament.ams_unit_id
                        if not display_ams_type:
                            display_ams_type = snapshot.filament.ams_type or ''
                    if is_external:
                        display_ams_unit_id = None
                        display_ams_type = 'External Spool'
                    if display_ams_unit_id is not None and not display_ams_type:
                        unit_meta = units_meta.get(display_ams_unit_id, {})
                        display_ams_type = unit_meta.get('ams_type') or ''
                    filament_dict = {
                        'tray_id': snapshot.tray_id,
                        'type': display_type,
                        'brand': display_brand,
                        'color': display_color,
                        'remain_percent': display_remaining,
                        'remain_percent_known': display_remaining is not None,
                        'ams_unit_id': display_ams_unit_id,
                        'ams_type': display_ams_type,
                        'is_external': is_external,
                    }
                    if use_inventory_display:
                        filament_dict['type'] = snapshot.filament.type or display_type
                        filament_dict['brand'] = snapshot.filament.brand or display_brand
                        filament_dict['color'] = (
                            f"{snapshot.filament.color_hex.lstrip('#').upper()}FF"
                            if snapshot.filament.color_hex else display_color
                        )
                        filament_dict['remain_percent'] = self._valid_display_percent(
                            snapshot.filament.remaining_percent
                        )
                        filament_dict['remain_percent_known'] = filament_dict['remain_percent'] is not None
                        filament_dict['color_name'] = snapshot.filament.color
                    if snapshot.filament:
                        filament_dict['filament_pk'] = snapshot.filament.pk
                        filament_dict['is_transparent'] = snapshot.filament.is_transparent
                    filaments_list.append(filament_dict)
            except Exception:
                filaments_list = []

            if not any(f.get('is_external') for f in filaments_list):
                external_filament = (
                    Filament.objects
                    .filter(current_printer=printer_device, is_loaded_externally=True)
                    .order_by('-last_loaded_date', '-updated_at', '-pk')
                    .first()
                )
                if external_filament:
                    filaments_list.append(
                        self._external_filament_from_inventory(external_filament)
                    )
                elif self._external_spool_has_display_info(latest_metric.external_spool or {}):
                    filaments_list.append(
                        self._external_filament_from_metric(latest_metric.external_spool or {})
                    )

            # Build a lookup from unit_id → AMS unit metadata (humidity, temp, info code)
            # first so we can enrich blank ams_type values derived from old snapshots.
            # Distinct AMS units in this snapshot. ams_type stored on FilamentSnapshot
            # may be blank for rows written before the multi-AMS deploy — fall back to
            # re-deriving from the unit's info code so labels always show correctly.
            from .models import ams_type_from_info as _ams_type_from_info
            seen_units = {}
            for f in filaments_list:
                if f.get('is_external'):
                    continue
                uid = f.get('ams_unit_id')
                if uid is not None and uid not in seen_units:
                    label = f.get('ams_type') or ''
                    if not label:
                        unit_meta = units_meta.get(uid, {})
                        label = _ams_type_from_info(unit_meta.get('info', ''))
                    seen_units[uid] = label
            ams_units_list = [
                {'ams_unit_id': uid, 'ams_type': label}
                for uid, label in sorted(seen_units.items())
            ]

            ams_groups = []
            external_filaments = [f for f in filaments_list if f.get('is_external')]
            if external_filaments:
                ams_groups.append({
                    'unit_id': 'external',
                    'ams_type': 'External Spool',
                    'label': 'External Spool',
                    'humidity': None,
                    'temp': None,
                    'filaments': external_filaments,
                })
            ungrouped = [f for f in filaments_list if f.get('ams_unit_id') is None and not f.get('is_external')]
            if ungrouped:
                ams_groups.append({
                    'unit_id': None,
                    'ams_type': '',
                    'label': 'AMS',
                    'humidity': None,
                    'temp': None,
                    'filaments': ungrouped,
                })
            for uid, label in sorted(seen_units.items()):
                unit_meta = units_meta.get(uid, {})
                ams_groups.append({
                    'unit_id': uid,
                    'ams_type': label,
                    'label': f"{label or 'AMS'} (Unit {uid})",
                    'humidity': self._ams_display_humidity(unit_meta),
                    'humidity_level': unit_meta.get('humidity'),
                    'humidity_raw': unit_meta.get('humidity_raw'),
                    'temp': unit_meta.get('temp'),
                    'filaments': [f for f in filaments_list if f.get('ams_unit_id') == uid],
                })

            subtask_name = latest_metric.subtask_name or "No active print"
            # Look up active PrintJob for a better display name (cloud design_title)
            job_display_name = subtask_name
            if latest_metric.subtask_name:
                active_job = (
                    PrintJob.objects.filter(
                        device=printer_device,
                        project_name=latest_metric.subtask_name,
                        end_time__isnull=True,
                    ).select_related('cloud_task').first()
                    or PrintJob.objects.filter(
                        device=printer_device,
                        project_name=latest_metric.subtask_name,
                    ).select_related('cloud_task').order_by('-start_time').first()
                )
                if active_job:
                    job_display_name = active_job.display_name

            stats = {
                "nozzle_temp": float(latest_metric.nozzle_temp) if latest_metric.nozzle_temp is not None else None,
                "nozzle_target_temp": float(latest_metric.nozzle_target_temp) if latest_metric.nozzle_target_temp is not None else None,
                "nozzle_diameter": float(latest_metric.nozzle_diameter) if latest_metric.nozzle_diameter else None,
                "nozzle_type": latest_metric.nozzle_type or "",
                "nozzle_temp_left": float(latest_metric.nozzle_temp_left) if latest_metric.nozzle_temp_left is not None else None,
                "nozzle_target_temp_left": float(latest_metric.nozzle_target_temp_left) if latest_metric.nozzle_target_temp_left is not None else None,
                "nozzle_diameter_left": float(latest_metric.nozzle_diameter_left) if latest_metric.nozzle_diameter_left is not None else None,
                "nozzle_type_left": latest_metric.nozzle_type_left or "",
                "is_dual_nozzle": latest_metric.nozzle_temp_left is not None,
                "bed_temp": float(latest_metric.bed_temp) if latest_metric.bed_temp is not None else None,
                "chamber_temp": float(latest_metric.chamber_temp) if latest_metric.chamber_temp is not None else None,
                "print_percent": latest_metric.print_percent,
                "gcode_state": latest_metric.gcode_state or "Unknown",
                "print_type": latest_metric.print_type or "idle",
                "subtask_name": subtask_name,
                "job_display_name": job_display_name,
                "chamber_light": latest_metric.chamber_light or "unknown",
                "ams_temp": float(latest_metric.ams_temp) if latest_metric.ams_temp else None,
                "ams_humidity": latest_metric.ams_humidity,
                "filaments": filaments_list,
                "ams_units": ams_units_list,
                "ams_groups": ams_groups,
                "hotends": list(
                    Hotend.objects.filter(printer=printer_device)
                    .order_by('-is_toolhead', 'slot_number', 'serial_number')
                ),
                # Nozzle positions with no induction chip (no stable serial number to
                # key a Hotend registry row on, e.g. H2C's fixed left nozzle) — shown
                # read-only from the latest poll, not persisted/historical. Entries with
                # no readable type/diameter at all (i.e. genuinely nothing there) are
                # dropped rather than shown as an empty placeholder.
                "nozzle_positions": [
                    h for h in (latest_metric.nozzle_info or [])
                    if h.get('is_empty') and (h.get('nozzle_type') or h.get('diameter'))
                ],
                "external_spool": latest_metric.external_spool or {},
                "timestamp": latest_metric.timestamp.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S"),
            }

        project_markers = self._calculate_project_markers(metrics, tz, printer_device)
        printer_data_json["project_markers"] = project_markers

        context["printer_device"] = printer_device
        context["device_name"] = printer_device.name
        context["stats"] = stats
        context["metrics_count"] = len(metrics)
        context["printer_data_json"] = json.dumps(printer_data_json)

        return context

    def _calculate_project_markers(self, metrics, timezone_info, device):
        """Calculate where print jobs start and end, using cloud design_title when available."""
        if not metrics:
            return []

        # Build a lookup: subtask_name -> display_name from PrintJobs in this time window
        window_start = metrics[0].timestamp
        window_end = metrics[-1].timestamp
        jobs_qs = PrintJob.objects.filter(
            device=device,
            start_time__gte=window_start - timedelta(minutes=5),
            start_time__lte=window_end + timedelta(minutes=5),
        ).select_related('cloud_task')
        # Map project_name (= subtask_name) -> best display name
        subtask_to_display = {}
        for job in jobs_qs:
            subtask_to_display[job.project_name] = job.display_name

        markers = []
        current_job = None
        last_state = None

        for idx, metric in enumerate(metrics):
            subtask = metric.subtask_name
            gcode_state = metric.gcode_state

            is_printing = gcode_state not in ['FINISH', 'IDLE', None, '']

            if subtask and subtask != current_job and is_printing:
                display = subtask_to_display.get(subtask, subtask)
                markers.append({
                    'type': 'start',
                    'index': idx,
                    'timestamp': metric.timestamp.astimezone(timezone_info).isoformat(),
                    'project_name': display,
                })
                current_job = subtask
                last_state = gcode_state

            elif current_job and last_state and last_state not in ['FINISH', 'IDLE'] and gcode_state in ['FINISH', 'IDLE']:
                display = subtask_to_display.get(current_job, current_job)
                markers.append({
                    'type': 'end',
                    'index': idx,
                    'timestamp': metric.timestamp.astimezone(timezone_info).isoformat(),
                    'project_name': display,
                })
                current_job = None

            last_state = gcode_state

        return markers

    def _prepare_filament_timeline(self, metrics, snapshots_by_metric):
        """Prepare filament data organized by unique filament configurations.

        Snapshots are passed in pre-grouped by metric id; reading them off each
        metric instance instead would issue one query per point.
        """
        filament_data = {}
        total_points = len(metrics)

        for idx, metric in enumerate(metrics):
            for snapshot in snapshots_by_metric.get(metric.id, []):
                tray_id = snapshot.tray_id
                ams_unit_id = snapshot.ams_unit_id
                ams_type = snapshot.ams_type or ''
                fil_type = snapshot.type or 'Unknown'
                fil_sub_type = snapshot.sub_type or 'Unknown'
                fil_color = snapshot.color or 'FFFFFFFF'

                unique_key = f"{ams_unit_id}_{tray_id}_{fil_type}_{fil_sub_type}_{fil_color}"

                if unique_key not in filament_data:
                    filament_data[unique_key] = {
                        'tray_id': tray_id,
                        'ams_unit_id': ams_unit_id,
                        'ams_type': ams_type,
                        'type': fil_type,
                        'brand': fil_sub_type,
                        'color': fil_color,
                        'remain_data': [None] * total_points,
                        'start_idx': idx,
                    }

                remain_percent = numeric_json_value(snapshot.remain_percent) or 0
                filament_data[unique_key]['remain_data'][idx] = remain_percent

        for idx, metric in enumerate(metrics):
            external = metric.external_spool or {}
            if external.get('type'):
                fil_type = external.get('type', 'Unknown')
                fil_color = external.get('color', '161616FF')
                unique_key = f"External_{fil_type}_{fil_color}"

                if unique_key not in filament_data:
                    filament_data[unique_key] = {
                        'tray_id': 'External',
                        'type': fil_type,
                        'brand': 'External',
                        'color': fil_color,
                        'remain_data': [None] * total_points,
                        'start_idx': idx,
                    }

                remain_percent = external.get('remain', 0)
                filament_data[unique_key]['remain_data'][idx] = remain_percent

        return filament_data


class PrinterDataAPIView(LoginRequiredMixin, View):
    """API endpoint for dynamic printer chart updates"""

    def get(self, request, pk=None):
        start_date = request.GET.get("start_date")
        end_date = request.GET.get("end_date")
        start_time = request.GET.get("start_time", "00:00")
        end_time = request.GET.get("end_time", "23:59")

        try:
            if pk is not None:
                printer_device = Printer.objects.filter(pk=pk, is_active=True).first()
                if not printer_device:
                    return JsonResponse({"error": "Printer not found"}, status=404)
            else:
                printer_device = Printer.objects.filter(is_active=True).first()
                if not printer_device:
                    return JsonResponse({"error": "No printer device found"}, status=404)

            tz = zoneinfo.ZoneInfo(app_settings.TIMEZONE)

            # Stage A: only() + step calculation
            query = (
                PrinterMetrics.objects
                .filter(device=printer_device)
                .only(*_METRICS_API_FIELDS)
            )

            # Both bounds are always applied. A missing bound falls back to a 24h
            # window rather than being left open — an unbounded range would scan
            # every metric ever recorded.
            def _parse(date_str, time_str):
                return datetime.strptime(
                    f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=tz)

            end_dt = _parse(end_date, end_time) if end_date else timezone.now()
            start_dt = _parse(start_date, start_time) if start_date else end_dt - _DEFAULT_WINDOW
            query = query.filter(timestamp__gte=start_dt, timestamp__lte=end_dt)

            # Stage B: single DB round-trip, downsample in Python
            metrics_list = sample_metrics(list(query.order_by("timestamp")))

            total_points = len(metrics_list)

            # Stage C: targeted snapshot fetch (only sampled IDs)
            snapshots_by_metric = fetch_snapshots_by_metric(metrics_list)

            # Stage D: single-pass serialization
            timestamps = []
            timestamps_iso = []
            dates = []
            nozzle_temp = []
            nozzle_target_temp = []
            nozzle_temp_left = []
            nozzle_target_temp_left = []
            bed_temp = []
            bed_target_temp = []
            print_percent = []
            cooling_fan_speed = []
            heatbreak_fan_speed = []
            wifi_signal_dbm = []
            ams_humidity_raw = []
            ams_temp = []
            layer_num = []
            total_layer_num = []
            gcode_state = []
            print_type = []
            subtask_name = []

            project_markers = []
            current_job = None
            last_state = None

            filament_data = {}

            for idx, m in enumerate(metrics_list):
                ts = m.timestamp.astimezone(tz)
                timestamps.append(ts.strftime('%H:%M'))
                timestamps_iso.append(ts.isoformat())
                dates.append(ts.strftime('%Y-%m-%d'))
                nozzle_temp.append(float(m.nozzle_temp) if m.nozzle_temp else None)
                nozzle_target_temp.append(float(m.nozzle_target_temp) if m.nozzle_target_temp else None)
                nozzle_temp_left.append(float(m.nozzle_temp_left) if m.nozzle_temp_left is not None else None)
                nozzle_target_temp_left.append(float(m.nozzle_target_temp_left) if m.nozzle_target_temp_left is not None else None)
                bed_temp.append(float(m.bed_temp) if m.bed_temp else None)
                bed_target_temp.append(float(m.bed_target_temp) if m.bed_target_temp else None)
                print_percent.append(m.print_percent if m.print_percent else 0)
                cooling_fan_speed.append(m.cooling_fan_speed if m.cooling_fan_speed else 0)
                heatbreak_fan_speed.append(m.heatbreak_fan_speed if m.heatbreak_fan_speed else 0)
                wifi_signal_dbm.append(m.wifi_signal_dbm if m.wifi_signal_dbm else None)
                ams_humidity_raw.append(m.ams_humidity_raw if m.ams_humidity_raw else None)
                ams_temp.append(float(m.ams_temp) if m.ams_temp else None)
                layer_num.append(m.layer_num if m.layer_num else 0)
                total_layer_num.append(m.total_layer_num if m.total_layer_num else 0)
                gcode_state.append(m.gcode_state)
                print_type.append(m.print_type)
                subtask_name.append(m.subtask_name)

                # Project marker detection (inline)
                subtask = m.subtask_name
                gs = m.gcode_state
                is_printing = gs not in ['FINISH', 'IDLE', None, '']
                if subtask and subtask != current_job and is_printing:
                    project_markers.append({
                        'type': 'start',
                        'index': idx,
                        'timestamp': ts.isoformat(),
                        'project_name': subtask,
                    })
                    current_job = subtask
                    last_state = gs
                elif current_job and last_state and last_state not in ['FINISH', 'IDLE'] and gs in ['FINISH', 'IDLE']:
                    project_markers.append({
                        'type': 'end',
                        'index': idx,
                        'timestamp': ts.isoformat(),
                        'project_name': current_job,
                    })
                    current_job = None
                last_state = gs

                # Filament timeline (inline)
                for snap in snapshots_by_metric.get(m.id, []):
                    tray_id = snap.tray_id
                    fil_type = snap.type or 'Unknown'
                    fil_sub_type = snap.sub_type or 'Unknown'
                    fil_color = snap.color or 'FFFFFFFF'
                    unique_key = f"{tray_id}_{fil_type}_{fil_sub_type}_{fil_color}"
                    if unique_key not in filament_data:
                        filament_data[unique_key] = {
                            'tray_id': tray_id,
                            'type': fil_type,
                            'brand': fil_sub_type,
                            'color': fil_color,
                            'remain_data': [None] * total_points,
                            'start_idx': idx,
                        }
                    filament_data[unique_key]['remain_data'][idx] = numeric_json_value(snap.remain_percent) or 0

                external = m.external_spool or {}
                if external.get('type'):
                    fil_type = external.get('type', 'Unknown')
                    fil_color = external.get('color', '161616FF')
                    unique_key = f"External_{fil_type}_{fil_color}"
                    if unique_key not in filament_data:
                        filament_data[unique_key] = {
                            'tray_id': 'External',
                            'type': fil_type,
                            'brand': 'External',
                            'color': fil_color,
                            'remain_data': [None] * total_points,
                            'start_idx': idx,
                        }
                    filament_data[unique_key]['remain_data'][idx] = external.get('remain', 0)

            data = {
                "timestamps": timestamps,
                "timestamps_iso": timestamps_iso,
                "dates": dates,
                "nozzle_temp": nozzle_temp,
                "nozzle_target_temp": nozzle_target_temp,
                "nozzle_temp_left": nozzle_temp_left,
                "nozzle_target_temp_left": nozzle_target_temp_left,
                "bed_temp": bed_temp,
                "bed_target_temp": bed_target_temp,
                "print_percent": print_percent,
                "cooling_fan_speed": cooling_fan_speed,
                "heatbreak_fan_speed": heatbreak_fan_speed,
                "wifi_signal_dbm": wifi_signal_dbm,
                "ams_humidity_raw": ams_humidity_raw,
                "ams_temp": ams_temp,
                "layer_num": layer_num,
                "total_layer_num": total_layer_num,
                "gcode_state": gcode_state,
                "print_type": print_type,
                "subtask_name": subtask_name,
                "project_markers": project_markers,
                "filament_timeline": filament_data,
            }

            return JsonResponse(data)

        except Exception as e:
            import traceback
            traceback.print_exc()
            return JsonResponse({"error": str(e)}, status=500)


class FilamentUsageDataAPIView(LoginRequiredMixin, View):
    """API endpoint for filament usage history with date/time filtering"""

    def get(self, request, pk):
        start_date = request.GET.get("start_date")
        end_date = request.GET.get("end_date")
        start_time = request.GET.get("start_time", "00:00")
        end_time = request.GET.get("end_time", "23:59")

        try:
            filament = Filament.objects.get(pk=pk)
            tz = zoneinfo.ZoneInfo(app_settings.TIMEZONE)
            query = filament.usage_snapshots.select_related('printer_metric')

            if start_date and start_time:
                from datetime import datetime
                start_dt_naive = datetime.strptime(f"{start_date} {start_time}", "%Y-%m-%d %H:%M")
                start_dt = start_dt_naive.replace(tzinfo=tz)
                query = query.filter(printer_metric__timestamp__gte=start_dt)

            if end_date and end_time:
                from datetime import datetime
                end_dt_naive = datetime.strptime(f"{end_date} {end_time}", "%Y-%m-%d %H:%M")
                end_dt = end_dt_naive.replace(tzinfo=tz)
                query = query.filter(printer_metric__timestamp__lte=end_dt)

            fallback_used = False
            if not start_date and not end_date:
                time_24h_ago = timezone.now() - timedelta(hours=24)
                default_query = query.filter(printer_metric__timestamp__gte=time_24h_ago)
                if default_query.exists():
                    snapshots = default_query.order_by('printer_metric__timestamp')
                else:
                    # Fallback: show 24h window ending at the most recent available snapshot
                    last_snapshot = query.order_by('-printer_metric__timestamp').first()
                    if last_snapshot:
                        last_ts = last_snapshot.printer_metric.timestamp
                        fallback_start = last_ts - timedelta(hours=24)
                        snapshots = query.filter(
                            printer_metric__timestamp__gte=fallback_start,
                            printer_metric__timestamp__lte=last_ts
                        ).order_by('printer_metric__timestamp')
                        fallback_used = True
                    else:
                        snapshots = query.none()
            else:
                snapshots = query.order_by('printer_metric__timestamp')

            data = {
                "timestamps": [s.printer_metric.timestamp.astimezone(tz).strftime('%Y-%m-%d %H:%M') for s in snapshots],
                "remaining": [numeric_json_value(s.remain_percent) for s in snapshots],
                "fallback_used": fallback_used,
            }

            return JsonResponse(data)

        except Filament.DoesNotExist:
            return JsonResponse({"error": "Filament not found"}, status=404)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return JsonResponse({"error": str(e)}, status=500)


# ==================== Filament CRUD Views ====================

class FilamentListView(LoginRequiredMixin, ListView):
    model = Filament
    template_name = 'bambu_run/filament_list.html'
    context_object_name = 'filaments'
    paginate_by = 20
    sort_fields = {
        'color': ['color'],
        'brand': ['brand'],
        'type': ['type'],
        'sub_type': ['sub_type'],
        'remaining': ['remaining_percent'],
        'location': ['is_loaded_in_ams', 'is_loaded_externally', 'current_printer__name', 'ams_unit_id', 'current_tray_id'],
        'created_by': ['created_by'],
        'last_used': ['last_used'],
    }

    def get_queryset(self):
        queryset = Filament.objects.select_related('current_printer')

        filament_type = self.request.GET.get('type')
        if filament_type:
            queryset = queryset.filter(type=filament_type)

        loaded = self.request.GET.get('loaded')
        if loaded == 'yes':
            queryset = queryset.filter(Q(is_loaded_in_ams=True) | Q(is_loaded_externally=True))
        elif loaded == 'ams':
            queryset = queryset.filter(is_loaded_in_ams=True)
        elif loaded == 'external':
            queryset = queryset.filter(is_loaded_externally=True)
        elif loaded == 'no':
            queryset = queryset.filter(is_loaded_in_ams=False, is_loaded_externally=False)

        ams_type = self.request.GET.get('ams_type')
        if ams_type:
            queryset = queryset.filter(ams_type=ams_type)

        search = self.request.GET.get('search')
        if search:
            queryset = queryset.filter(
                Q(brand__icontains=search) |
                Q(color__icontains=search) |
                Q(type__icontains=search)
            )

        sort_key = self.request.GET.get('sort')
        sort_dir = self.request.GET.get('dir')
        sort_fields = self.sort_fields.get(sort_key)
        if sort_fields:
            if sort_dir == 'desc':
                sort_fields = [f'-{field}' for field in sort_fields]
            queryset = queryset.order_by(*sort_fields)

        return queryset

    def _sort_url(self, column):
        params = self.request.GET.copy()
        params.pop('page', None)
        params['sort'] = column
        params['dir'] = (
            'desc'
            if self.request.GET.get('sort') == column and self.request.GET.get('dir') != 'desc'
            else 'asc'
        )
        return f"?{params.urlencode()}"

    def _page_url(self, page_number):
        params = self.request.GET.copy()
        params['page'] = page_number
        return f"?{params.urlencode()}"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['bambu_run_base_template'] = app_settings.BASE_TEMPLATE
        current_sort = self.request.GET.get('sort')
        if current_sort not in self.sort_fields:
            current_sort = ''
        current_dir = self.request.GET.get('dir') if self.request.GET.get('dir') in {'asc', 'desc'} else 'asc'
        context['current_sort'] = current_sort
        context['current_dir'] = current_dir
        context['sort_urls'] = {
            column: self._sort_url(column)
            for column in self.sort_fields
        }
        if context.get('is_paginated'):
            page_obj = context['page_obj']
            context['page_urls'] = {
                'first': self._page_url(1),
                'previous': self._page_url(page_obj.previous_page_number()) if page_obj.has_previous() else '',
                'next': self._page_url(page_obj.next_page_number()) if page_obj.has_next() else '',
                'last': self._page_url(page_obj.paginator.num_pages),
            }
        context['total_spools'] = Filament.objects.count()
        context['loaded_spools'] = Filament.objects.filter(
            Q(is_loaded_in_ams=True) | Q(is_loaded_externally=True)
        ).count()
        context['low_filaments'] = Filament.objects.filter(remaining_percent__lt=20).count()
        context['filament_types'] = sorted(
            set(Filament.objects.exclude(type__isnull=True).exclude(type='').values_list('type', flat=True))
        )
        context['ams_type_choices'] = sorted(
            set(
                Filament.objects.filter(is_loaded_in_ams=True).exclude(ams_type='').values_list('ams_type', flat=True)
            )
        )
        return context


def _filament_type_map():
    """Return a JSON-serialisable dict mapping FilamentType pk → {type, sub_type, brand}."""
    return {
        str(ft.pk): {'type': ft.type, 'sub_type': ft.sub_type or '', 'brand': ft.brand}
        for ft in FilamentType.objects.all()
    }


def _tray_ids_for_ams_type(ams_type):
    if ams_type == 'AMS HT':
        return [0]
    return [0, 1, 2, 3]


def _latest_ams_units_for_form():
    """Return AMS unit choices from the most recent metric for active printers."""
    units_by_id = {}
    for printer in Printer.objects.filter(is_active=True):
        metric = (
            PrinterMetrics.objects.filter(device=printer)
            .order_by('-timestamp')
            .first()
        )
        ams_units = metric.ams_units if metric else []

        for unit in ams_units or []:
            raw_unit_id = unit.get('unit_id')
            try:
                unit_id = int(raw_unit_id)
            except (TypeError, ValueError):
                continue

            ams_type = unit.get('ams_type') or ''
            if not ams_type:
                from .models import ams_type_from_info
                ams_type = ams_type_from_info(unit.get('info', ''))

            units_by_id[(printer.pk, unit_id)] = {
                'printer_id': printer.pk,
                'printer_name': printer.name,
                'unit_id': unit_id,
                'ams_type': ams_type,
                'label': f"{printer.name} - {ams_type or 'AMS'} (Unit {unit_id})",
                'tray_ids': _tray_ids_for_ams_type(ams_type),
            }

        if not ams_units:
            units_by_id[(printer.pk, 0)] = {
                'printer_id': printer.pk,
                'printer_name': printer.name,
                'unit_id': 0,
                'ams_type': 'AMS',
                'label': f"{printer.name} - AMS (Unit 0)",
                'tray_ids': _tray_ids_for_ams_type('AMS'),
            }

    return [units_by_id[key] for key in sorted(units_by_id)]


class FilamentCreateView(LoginRequiredMixin, CreateView):
    model = Filament
    form_class = FilamentForm
    template_name = 'bambu_run/filament_form.html'
    success_url = reverse_lazy('bambu_run:filament_list')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['ams_units'] = _latest_ams_units_for_form()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['bambu_run_base_template'] = app_settings.BASE_TEMPLATE
        context['filament_type_map'] = json.dumps(_filament_type_map())
        context['ams_slot_map'] = json.dumps(_latest_ams_units_for_form())
        return context

    def form_valid(self, form):
        messages.success(self.request, f'Filament spool "{form.instance}" added successfully!')
        return super().form_valid(form)


class FilamentUpdateView(LoginRequiredMixin, UpdateView):
    model = Filament
    form_class = FilamentForm
    template_name = 'bambu_run/filament_form.html'
    success_url = reverse_lazy('bambu_run:filament_list')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['ams_units'] = _latest_ams_units_for_form()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['bambu_run_base_template'] = app_settings.BASE_TEMPLATE
        context['filament_type_map'] = json.dumps(_filament_type_map())
        context['ams_slot_map'] = json.dumps(_latest_ams_units_for_form())
        return context

    def form_valid(self, form):
        messages.success(self.request, f'Filament spool "{form.instance}" updated successfully!')
        return super().form_valid(form)


class FilamentDeleteView(LoginRequiredMixin, DeleteView):
    model = Filament
    template_name = 'bambu_run/filament_confirm_delete.html'
    success_url = reverse_lazy('bambu_run:filament_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['bambu_run_base_template'] = app_settings.BASE_TEMPLATE
        return context

    def delete(self, request, *args, **kwargs):
        filament = self.get_object()
        messages.success(self.request, f'Filament spool "{filament}" has been deleted.')
        return super().delete(request, *args, **kwargs)


class FilamentDetailView(LoginRequiredMixin, DetailView):
    model = Filament
    template_name = 'bambu_run/filament_detail.html'
    context_object_name = 'filament'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['bambu_run_base_template'] = app_settings.BASE_TEMPLATE
        filament = self.object

        context['print_usages'] = filament.print_usages.select_related('print_job__cloud_task').order_by('-print_job__start_time')[:20]

        total_consumed = filament.print_usages.aggregate(
            total=Sum('consumed_percent')
        )['total'] or 0
        context['total_consumed_percent'] = total_consumed

        return context


# ==================== FilamentColor Views ====================

class FilamentColorListView(LoginRequiredMixin, ListView):
    model = FilamentColor
    template_name = 'bambu_run/filament_color_list.html'
    context_object_name = 'colors'
    paginate_by = 50

    def get_queryset(self):
        return FilamentColor.objects.all().order_by('finish', 'color_name')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['bambu_run_base_template'] = app_settings.BASE_TEMPLATE
        context['total_colors'] = FilamentColor.objects.count()
        return context


class FilamentColorCreateView(LoginRequiredMixin, CreateView):
    model = FilamentColor
    form_class = FilamentColorForm
    template_name = 'bambu_run/filament_color_form.html'
    success_url = reverse_lazy('bambu_run:filament_color_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['bambu_run_base_template'] = app_settings.BASE_TEMPLATE
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        self._update_matching_filaments(self.object)
        return response

    def _update_matching_filaments(self, filament_color):
        from .utils import match_and_update_filament_color
        updated_count = match_and_update_filament_color(filament_color)
        if updated_count > 0:
            messages.success(
                self.request,
                f"Color '{filament_color.color_name}' created! "
                f"Updated {updated_count} matching filament spool(s)."
            )


class FilamentColorUpdateView(LoginRequiredMixin, UpdateView):
    model = FilamentColor
    form_class = FilamentColorForm
    template_name = 'bambu_run/filament_color_form.html'
    success_url = reverse_lazy('bambu_run:filament_color_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['bambu_run_base_template'] = app_settings.BASE_TEMPLATE
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        self._update_matching_filaments(self.object)
        return response

    def _update_matching_filaments(self, filament_color):
        from .utils import match_and_update_filament_color
        updated_count = match_and_update_filament_color(filament_color)
        if updated_count > 0:
            messages.success(
                self.request,
                f"Color '{filament_color.color_name}' updated! "
                f"Updated {updated_count} matching filament spool(s)."
            )


class FilamentColorDeleteView(LoginRequiredMixin, DeleteView):
    model = FilamentColor
    template_name = 'bambu_run/filament_color_confirm_delete.html'
    success_url = reverse_lazy('bambu_run:filament_color_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['bambu_run_base_template'] = app_settings.BASE_TEMPLATE
        return context

    def delete(self, request, *args, **kwargs):
        messages.success(request, f"Color '{self.get_object().color_name}' deleted successfully!")
        return super().delete(request, *args, **kwargs)


# ==================== FilamentType Views ====================

class FilamentTypeListView(LoginRequiredMixin, ListView):
    model = FilamentType
    template_name = 'bambu_run/filament_type_list.html'
    context_object_name = 'types'
    paginate_by = 50

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['bambu_run_base_template'] = app_settings.BASE_TEMPLATE
        context['total_types'] = FilamentType.objects.count()
        return context


class FilamentTypeCreateView(LoginRequiredMixin, CreateView):
    model = FilamentType
    form_class = FilamentTypeForm
    template_name = 'bambu_run/filament_type_form.html'
    success_url = reverse_lazy('bambu_run:filament_type_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['bambu_run_base_template'] = app_settings.BASE_TEMPLATE
        context['existing_types'] = list(
            FilamentType.objects.values_list('type', flat=True).distinct().order_by('type')
        )
        context['existing_sub_types'] = list(
            FilamentType.objects.exclude(sub_type__isnull=True).exclude(sub_type='')
            .values_list('sub_type', flat=True).distinct().order_by('sub_type')
        )
        context['existing_brands'] = list(
            FilamentType.objects.values_list('brand', flat=True).distinct().order_by('brand')
        )
        context['preset_types'] = FilamentTypeForm.PRESET_TYPES
        context['preset_sub_types'] = FilamentTypeForm.PRESET_SUB_TYPES
        context['preset_brands'] = FilamentTypeForm.PRESET_BRANDS
        return context

    def form_valid(self, form):
        messages.success(self.request, f'Filament type "{form.instance}" added successfully!')
        return super().form_valid(form)


class FilamentTypeUpdateView(LoginRequiredMixin, UpdateView):
    model = FilamentType
    form_class = FilamentTypeForm
    template_name = 'bambu_run/filament_type_form.html'
    success_url = reverse_lazy('bambu_run:filament_type_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['bambu_run_base_template'] = app_settings.BASE_TEMPLATE
        context['existing_types'] = list(
            FilamentType.objects.values_list('type', flat=True).distinct().order_by('type')
        )
        context['existing_sub_types'] = list(
            FilamentType.objects.exclude(sub_type__isnull=True).exclude(sub_type='')
            .values_list('sub_type', flat=True).distinct().order_by('sub_type')
        )
        context['existing_brands'] = list(
            FilamentType.objects.values_list('brand', flat=True).distinct().order_by('brand')
        )
        context['preset_types'] = FilamentTypeForm.PRESET_TYPES
        context['preset_sub_types'] = FilamentTypeForm.PRESET_SUB_TYPES
        context['preset_brands'] = FilamentTypeForm.PRESET_BRANDS
        return context

    def form_valid(self, form):
        messages.success(self.request, f'Filament type "{form.instance}" updated successfully!')
        return super().form_valid(form)


class FilamentTypeDeleteView(LoginRequiredMixin, DeleteView):
    model = FilamentType
    template_name = 'bambu_run/filament_type_confirm_delete.html'
    success_url = reverse_lazy('bambu_run:filament_type_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['bambu_run_base_template'] = app_settings.BASE_TEMPLATE
        return context

    def delete(self, request, *args, **kwargs):
        messages.success(request, f"Filament type '{self.get_object()}' deleted successfully!")
        return super().delete(request, *args, **kwargs)
