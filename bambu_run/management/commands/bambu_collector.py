"""
Management command to continuously collect 3D printer MQTT data.
Collects printer metrics from Bambu Lab 3D printers.

Usage:
    python manage.py bambu_collector
    python manage.py bambu_collector --interval 60
    python manage.py bambu_collector --once
    python manage.py bambu_collector --verbose
"""

import logging
import os
import ssl
import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from bambu_run.conf import app_settings
from bambu_run.models import Printer, PrinterMetrics

logger = logging.getLogger("bambu_run.collector")


def resolve_printer_device(device_id: str, device_info: Optional[dict] = None) -> Printer:
    """Find-or-create the Printer row for a Bambu cloud device, keyed by serial number.

    `device_info` is one entry from BambuClient.get_devices() (keys: name,
    dev_product_name, dev_id, ...). Falls back to generic defaults when unavailable
    (e.g. local-only connections that never call get_devices()).
    """
    device_info = device_info or {}
    name = device_info.get("name") or "Bambu Lab Printer"
    model = device_info.get("dev_product_name") or "Bambu Lab"

    printer = Printer.objects.filter(serial_number=device_id).first()

    if printer is None:
        # Upgrade path: a pre-multi-printer deployment has exactly one Printer row
        # with no serial number yet. Backfill it instead of creating a duplicate.
        # If there's more than one such row, we can't tell which one this device
        # used to be, so don't guess — create a fresh row instead.
        legacy_candidates = list(Printer.objects.filter(serial_number__isnull=True)[:2])
        if len(legacy_candidates) == 1:
            printer = legacy_candidates[0]
            printer.serial_number = device_id

    if printer is None:
        printer = Printer(serial_number=device_id)

    printer.name = name
    printer.model = model
    printer.manufacturer = "Bambu Lab"
    printer.is_active = True
    printer.save()
    return printer


@dataclass
class DeviceSession:
    """Per-printer mutable state for one bound device in a multi-printer collector run."""

    device_id: str
    client: Any  # BambuPrinter
    printer: Printer
    current_print_job: Optional[Any] = None
    last_gcode_state: Optional[str] = None
    last_subtask_name: Optional[str] = None
    trays_used: set = field(default_factory=set)
    active_filament_segments: dict = field(default_factory=dict)
    filament_segments: list = field(default_factory=list)
    error_count: int = 0
    success_count: int = 0
    mqtt_connect_errors: int = 0


@dataclass
class FilamentPrintSegment:
    tray_id: int
    ams_unit_id: Optional[int]
    filament_id: int
    start_line_number: Optional[int]
    start_percent: Optional[int]
    starting_percent: int
    starting_weight_grams: Optional[int]
    end_line_number: Optional[int] = None
    end_percent: Optional[int] = None
    ending_percent: Optional[int] = None


class Command(BaseCommand):
    """
    MQTT Poll -> PrinterMetrics -> FilamentSnapshot -> Auto-Match -> Update Filament
    """
    help = "Continuously collect 3D printer MQTT data from Bambu Lab printer"

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval", type=int, default=30,
            help="Data collection interval in seconds (default: 30)",
        )
        parser.add_argument(
            "--once", action="store_true",
            help="Run once and exit (useful for testing/cron)",
        )
        parser.add_argument(
            "--verbose", action="store_true", help="Enable verbose logging"
        )
        parser.add_argument(
            "--disable-ssl-verify", action="store_true",
            help="Disable SSL certificate verification (use with caution)",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sessions: Dict[str, DeviceSession] = {}
        self._token: Optional[str] = None
        self.verbose = False
        self.disable_ssl_verify = False
        self.start_time = None
        self._print_usage_cache = {}

    def handle(self, *args, **options):
        self.verbose = options["verbose"]
        self.disable_ssl_verify = options["disable_ssl_verify"]
        interval = options["interval"]
        run_once = options["once"]

        if self.disable_ssl_verify:
            logger.warning("SSL verification disabled - use with caution!")
            ssl._create_default_https_context = ssl._create_unverified_context
            os.environ["PYTHONHTTPSVERIFY"] = "0"
            os.environ["CURL_CA_BUNDLE"] = ""
            os.environ["REQUESTS_CA_BUNDLE"] = ""

            try:
                import paho.mqtt.client as mqtt_client

                original_tls_set = mqtt_client.Client.tls_set

                def patched_tls_set(
                    self, ca_certs=None, certfile=None, keyfile=None,
                    cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLS, ciphers=None,
                ):
                    return original_tls_set(
                        self, ca_certs, certfile, keyfile, ssl.CERT_NONE, tls_version, ciphers,
                    )

                mqtt_client.Client.tls_set = patched_tls_set
                logger.debug("Successfully patched paho-mqtt SSL verification")
            except ImportError:
                logger.debug("paho-mqtt not yet imported, will rely on SSL context")
            except Exception as e:
                logger.debug(f"Could not patch paho-mqtt: {e}")

        self._configure_logging()

        try:
            self._initialize_printers()
        except Exception as e:
            raise CommandError(f"Initialization failed: {e}")

        self.start_time = timezone.now()
        printer_names = ", ".join(s.printer.name for s in self.sessions.values())
        logger.info(f"Bambu Run data collector started for {len(self.sessions)} printer(s): {printer_names}")
        logger.info(f"Collection interval: {interval} seconds")
        logger.info(f"Mode: {'Single run' if run_once else 'Continuous'}")

        try:
            if run_once:
                import time as _time
                _time.sleep(5)
                for session in self.sessions.values():
                    self._collect_printer_data(session)
                logger.info("Single collection completed successfully")
            else:
                self._run_continuous_loop(interval)
        except KeyboardInterrupt:
            self._print_statistics()
            logger.info("Bambu Run data collector stopped by user")
        except Exception as e:
            logger.exception(f"Fatal error in main loop: {e}")
            raise CommandError(f"Runner failed: {e}")

    def _request_full_status_when_ready(self, client, timeout: float = 20.0) -> None:
        """Send pushall once the MQTT broker connection is confirmed.

        BambuPrinter._connected is set True immediately after connect(blocking=False),
        before the broker handshake. Poll MQTTClient.connected (set in _on_connect)
        instead, so publish() won't raise "Not connected to broker".
        """
        import time as _time
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            mqtt_client = getattr(client, "_mqtt", None)
            if mqtt_client is not None and getattr(mqtt_client, "connected", False):
                client._mqtt.request_full_status()
                logger.info("Sent MQTT pushall request")
                return
            _time.sleep(0.5)
        logger.warning("MQTT broker connection not confirmed within %.1fs; skipping pushall", timeout)

    def _configure_logging(self):
        log_level = logging.DEBUG if self.verbose else logging.INFO
        logger.setLevel(log_level)

        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setLevel(log_level)
            formatter = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)

    def _initialize_printers(self):
        """Authenticate once, discover every device bound to the account, and open
        one BambuPrinter (own MQTT thread) per device — all in this single process."""
        from bambu_run.mqtt_client import BambuPrinter

        bambu_username = os.environ.get("BAMBU_USERNAME")
        bambu_password = os.environ.get("BAMBU_PASSWORD")
        bambu_token = os.environ.get("BAMBU_TOKEN")
        bambu_device_id = os.environ.get("BAMBU_DEVICE_ID")

        if not bambu_token and not all([bambu_username, bambu_password]):
            raise CommandError(
                "Either BAMBU_TOKEN or both BAMBU_USERNAME and BAMBU_PASSWORD "
                "environment variables must be set"
            )

        logger.info("Authenticating with Bambu Lab cloud...")
        try:
            auth = BambuPrinter(
                username=bambu_username, password=bambu_password, token=bambu_token,
            )
            self._token = auth._ensure_token()
        except Exception as e:
            if "CERTIFICATE_VERIFY_FAILED" in str(e) or "SSL" in str(e):
                error_msg = (
                    f"SSL certificate verification failed: {e}\n\n"
                    "Solutions:\n"
                    "1. Run with --disable-ssl-verify flag\n"
                    "2. Install Python SSL certificates\n"
                    "3. pip install --upgrade certifi\n"
                )
                raise CommandError(error_msg)
            raise CommandError(f"Failed to authenticate: {e}")

        device_infos = self._discover_devices(bambu_device_id)
        for device_id, device_info in device_infos.items():
            try:
                self._add_session(device_id, device_info)
            except Exception as e:
                logger.error(f"Failed to initialize printer {device_id}: {e}")

        if not self.sessions:
            raise CommandError("No printer sessions could be initialized")

    def _discover_devices(self, explicit_device_id: Optional[str]) -> Dict[str, dict]:
        """Return {device_id: device_info} for every printer to monitor.

        device_info comes from BambuClient.get_devices() (name, dev_product_name,
        etc.) — empty dict when explicitly pinned to one device via BAMBU_DEVICE_ID
        and the cloud listing can't be reached.
        """
        from bambu_run.mqtt_client import BambuClient

        try:
            cloud = BambuClient(token=self._token)
            devices = cloud.get_devices()
        except Exception as e:
            if explicit_device_id:
                logger.warning(f"Could not list account devices ({e}); using BAMBU_DEVICE_ID only")
                return {explicit_device_id: {}}
            raise

        device_infos = {d.get("dev_id"): d for d in devices if d.get("dev_id")}

        if explicit_device_id:
            return {explicit_device_id: device_infos.get(explicit_device_id, {})}

        if not device_infos:
            raise CommandError("No devices found on this account")

        return device_infos

    def _add_session(self, device_id: str, device_info: dict) -> "DeviceSession":
        from bambu_run.mqtt_client import BambuPrinter

        logger.info(f"Connecting to printer {device_id} ({device_info.get('name', 'unknown')})...")
        client = BambuPrinter(token=self._token, device_id=device_id)
        client.connect(blocking=False)
        try:
            self._request_full_status_when_ready(client)
        except Exception as e:
            logger.warning("pushall request skipped (non-fatal): %s", e)

        printer = resolve_printer_device(device_id, device_info)
        session = DeviceSession(device_id=device_id, client=client, printer=printer)
        self.sessions[device_id] = session
        logger.info(f"Initialized session for printer: {printer}")
        return session

    def _run_continuous_loop(self, interval: int):
        iteration = 0
        while True:
            iteration += 1
            loop_start = time.time()

            if self.verbose:
                logger.debug(f"=== Iteration {iteration} ===")

            for session in list(self.sessions.values()):
                self._collect_printer_data(session)

            elapsed = time.time() - loop_start
            sleep_time = max(0, interval - elapsed)

            if self.verbose:
                logger.debug(f"Collection took {elapsed:.2f}s, sleeping for {sleep_time:.2f}s")

            if iteration % 100 == 0:
                self._print_statistics()
                self._refresh_devices()

            time.sleep(sleep_time)

    def _refresh_devices(self):
        """Pick up printers added to the account without restarting the process."""
        if os.environ.get("BAMBU_DEVICE_ID"):
            return  # pinned to a single explicit device — nothing to discover
        try:
            device_infos = self._discover_devices(None)
        except Exception as e:
            logger.warning(f"Device refresh skipped (non-fatal): {e}")
            return

        for device_id, device_info in device_infos.items():
            if device_id not in self.sessions:
                logger.info(f"New printer detected on account: {device_id}")
                try:
                    self._add_session(device_id, device_info)
                except Exception as e:
                    logger.error(f"Failed to initialize newly-detected printer {device_id}: {e}")

    def _convert_mqtt_color(self, mqtt_color):
        if not mqtt_color:
            return None
        color_hex = mqtt_color[:6] if len(mqtt_color) >= 6 else mqtt_color
        return f"#{color_hex.upper()}"

    def _clean_mqtt_identifier(self, value):
        """Treat blank/all-zero identifiers from third-party trays as missing."""
        if value is None:
            return None
        value = str(value).strip()
        normalized = value.replace("-", "").replace(":", "").lower()
        if not value or normalized in {"null", "none", "unknown", "n/a"}:
            return None
        if normalized and set(normalized) == {"0"}:
            return None
        return value

    def _to_int(self, value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _valid_percent(self, value):
        value = self._to_int(value)
        if value is None or value < 0 or value > 100:
            return None
        return value

    def _is_external_tray_data(self, tray_data):
        return bool(tray_data.get('is_external')) or self._to_int(tray_data.get('tray_id')) == 254

    def _external_spool_has_info(self, external_spool):
        if not external_spool:
            return False
        identifiers = (
            external_spool.get('type'),
            external_spool.get('sub_type'),
            external_spool.get('color'),
            external_spool.get('tag_uid'),
            external_spool.get('tray_uuid'),
            external_spool.get('tray_info_idx'),
        )
        if any(value not in (None, '', '00000000', '0000000000000000', '00000000000000000000000000000000') for value in identifiers):
            return True
        remain_percent = self._valid_percent(external_spool.get('remain_percent', external_spool.get('remain')))
        return remain_percent is not None and remain_percent > 0

    def _external_spool_tray_data(self, external_spool):
        return {
            **external_spool,
            'tray_id': self._to_int(external_spool.get('tray_id')) or 254,
            'remain_percent': external_spool.get('remain_percent', external_spool.get('remain')),
            'is_external': True,
            'ams_unit_id': None,
            'ams_type': 'External Spool',
        }

    def _inventory_color_for_mqtt(self, filament):
        if filament.color_hex:
            return f"{filament.color_hex.lstrip('#').upper()}FF"
        return "FFFFFFFF"

    def _match_loaded_slot_filament(self, tray_data, printer=None):
        """Match a manually assigned spool by physical AMS slot.

        Third-party AMS trays often have no stable RFID UUID, so the manual
        inventory assignment is the closest equivalent to SimplyPrint's
        "assign spool to AMS slot" workflow.
        """
        from bambu_run.models import Filament

        if self._is_external_tray_data(tray_data):
            qs = Filament.objects.filter(is_loaded_externally=True)
            if printer is not None:
                qs = qs.filter(current_printer=printer)
            if qs.count() == 1:
                return qs.order_by('-last_loaded_date', '-updated_at').first()
            return None

        tray_id = self._to_int(tray_data.get('tray_id'))
        if tray_id is None:
            return None

        qs = Filament.objects.filter(
            is_loaded_in_ams=True,
            current_tray_id=tray_id,
        )
        if printer is not None:
            qs = qs.filter(current_printer=printer)
        ams_unit_id = self._to_int(tray_data.get('ams_unit_id'))
        if ams_unit_id is not None:
            exact = qs.filter(ams_unit_id=ams_unit_id).order_by('-last_loaded_date', '-updated_at')
            filament = exact.first()
            if filament:
                return filament

            unspecified = qs.filter(ams_unit_id__isnull=True).order_by('-last_loaded_date', '-updated_at')
            if unspecified.count() == 1:
                return unspecified.first()
            return None

        if qs.count() == 1:
            return qs.order_by('-last_loaded_date', '-updated_at').first()
        return None

    def _match_filament_to_inventory(self, tray_data, printer=None):
        from bambu_run.models import Filament

        tray_id = tray_data.get('tray_id')
        tray_uuid = self._clean_mqtt_identifier(tray_data.get('tray_uuid'))
        tag_uid = self._clean_mqtt_identifier(tray_data.get('tag_uid'))
        tag_id = self._clean_mqtt_identifier(tray_data.get('tag_id'))
        type_val = tray_data.get('type')
        sub_type = tray_data.get('sub_type')
        color = tray_data.get('color')

        if tray_uuid:
            filament = Filament.objects.filter(tray_uuid=tray_uuid).first()
            if filament:
                if self.verbose:
                    logger.debug(f"Matched filament via tray_uuid: {tray_uuid[:16]}...")
                return filament, 'tray_uuid'

        if tag_uid:
            filament = Filament.objects.filter(tag_uid=tag_uid).first()
            if filament:
                if self.verbose:
                    logger.debug(f"Matched filament via tag_uid: {tag_uid}")
                return filament, 'tag_uid'

        if tag_id:
            filament = Filament.objects.filter(tag_id=tag_id).first()
            if filament:
                if self.verbose:
                    logger.debug(f"Matched filament via tag_id: {tag_id}")
                return filament, 'tag_id'

        filament = self._match_loaded_slot_filament(tray_data, printer=printer)
        if filament:
            if self.verbose:
                logger.debug(
                    f"Matched manually loaded filament via slot: "
                    f"unit={tray_data.get('ams_unit_id')} tray={tray_id}"
                )
            return filament, 'manual_loaded_slot'

        if type_val and color:
            query_filters = {'type': type_val, 'color': color}
            if sub_type:
                query_filters['sub_type'] = sub_type

            filament = (
                Filament.objects.filter(**query_filters, is_loaded_in_ams=False)
                .filter(is_loaded_externally=False)
                .order_by('remaining_percent', 'last_used')
                .first()
            )

            if not filament and printer is not None:
                filament = (
                    Filament.objects.filter(
                        **query_filters,
                        is_loaded_in_ams=True,
                        current_printer=printer,
                    )
                    .order_by('remaining_percent', 'last_used')
                    .first()
                )

            if filament:
                if self.verbose:
                    logger.debug(f"Matched filament via type+sub_type+color: {filament}")
                return filament, 'type_sub_type_color'

        if self.verbose:
            logger.info(f"No match found for tray {tray_id}. Auto-creating new filament...")

        filament = self._auto_create_filament(tray_data, printer=printer)
        return filament, 'auto_created'

    def _auto_create_filament(self, tray_data, printer=None):
        from bambu_run.models import Filament, FilamentType
        from bambu_run.utils import strip_color_padding, match_filament_color, is_mqtt_color_transparent

        tray_uuid = self._clean_mqtt_identifier(tray_data.get('tray_uuid'))
        tag_uid = self._clean_mqtt_identifier(tray_data.get('tag_uid'))
        type_val = tray_data.get('type', 'Unknown')
        sub_type = tray_data.get('sub_type', '')
        mqtt_color = tray_data.get('color')
        remain_percent = self._valid_percent(tray_data.get('remain_percent'))
        if remain_percent is None:
            remain_percent = 100
        diameter = tray_data.get('tray_diameter', 1.75)
        initial_weight = tray_data.get('tray_weight', 1000)

        default_brand = app_settings.AUTO_CREATE_BRAND

        transparent = is_mqtt_color_transparent(mqtt_color)
        color_code = strip_color_padding(mqtt_color)
        color_hex = f"#{color_code}" if color_code else None

        filament_color = match_filament_color(
            filament_type=type_val,
            filament_sub_type=sub_type,
            color_code=color_code,
            brand=default_brand
        )

        if filament_color:
            color_name = filament_color.color_name
            transparent = transparent or filament_color.is_transparent
            if self.verbose:
                logger.info(f"Matched color from database: {color_name} (#{color_code})")
        else:
            color_name = color_hex or mqtt_color
            if self.verbose:
                logger.warning(
                    f"No color match in database for {type_val} {sub_type} #{color_code}. "
                    f"Using hex code as color name."
                )

        filament_type_obj, ft_created = FilamentType.objects.get_or_create(
            type=type_val,
            sub_type=sub_type or None,
            brand=default_brand,
        )
        if ft_created and self.verbose:
            logger.info(f"Auto-created FilamentType: {filament_type_obj}")

        filament = Filament.objects.create(
            filament_type=filament_type_obj,
            tray_uuid=tray_uuid,
            tag_uid=tag_uid,
            type=type_val,
            sub_type=sub_type,
            brand=default_brand,
            color=color_name,
            color_hex=color_hex,
            is_transparent=transparent,
            diameter=diameter,
            initial_weight_grams=initial_weight,
            remaining_percent=remain_percent,
            created_by='Auto Detection',
            current_printer=printer,
            is_loaded_in_ams=not self._is_external_tray_data(tray_data),
            is_loaded_externally=self._is_external_tray_data(tray_data),
            current_tray_id=254 if self._is_external_tray_data(tray_data) else tray_data.get('tray_id'),
            ams_unit_id=None if self._is_external_tray_data(tray_data) else tray_data.get('ams_unit_id'),
            ams_type='' if self._is_external_tray_data(tray_data) else tray_data.get('ams_type', '') or '',
            last_loaded_date=timezone.now(),
        )

        filament.update_remaining_weight()
        filament.save()

        logger.info(
            f"Auto-created filament: {filament.brand} {filament.type} "
            f"{filament.sub_type} - {filament.color} (SN: {tray_uuid[:16] if tray_uuid else 'N/A'}...)"
        )

        return filament

    def _update_filament_status(self, filament, tray_id, remain_percent, tray_data=None, sync_remaining=True, printer=None):
        from bambu_run.models import Filament

        tray_data = tray_data or {}
        is_external = self._is_external_tray_data(tray_data)
        ams_unit_id = tray_data.get('ams_unit_id')
        ams_type_label = tray_data.get('ams_type', '') or ''

        remain_percent = self._valid_percent(remain_percent)
        if sync_remaining and remain_percent is not None and filament.remaining_percent != remain_percent:
            filament.remaining_percent = remain_percent
            filament.update_remaining_weight()
            filament.last_used = timezone.now()
            if self.verbose:
                logger.debug(f"Updated filament {filament}: {remain_percent}%")

        location_changed = (
            (not filament.is_loaded_externally if is_external else not filament.is_loaded_in_ams)
            or (printer is not None and filament.current_printer_id != printer.id)
            or filament.current_tray_id != tray_id
            or (not is_external and ams_unit_id is not None and filament.ams_unit_id != ams_unit_id)
        )
        if location_changed:
            # Unload anything previously occupying THIS exact (unit, tray) slot.
            if is_external:
                unload_qs = Filament.objects.filter(
                    is_loaded_externally=True
                ).exclude(id=filament.id)
                if printer is not None:
                    unload_qs = unload_qs.filter(current_printer=printer)
            else:
                unload_qs = Filament.objects.filter(
                    is_loaded_in_ams=True, current_tray_id=tray_id
                ).exclude(id=filament.id)
                if printer is not None:
                    unload_qs = unload_qs.filter(current_printer=printer)
                if ams_unit_id is not None:
                    unload_qs = unload_qs.filter(ams_unit_id=ams_unit_id)
            previous_filament = unload_qs.first()

            if previous_filament:
                previous_filament.is_loaded_in_ams = False
                previous_filament.is_loaded_externally = False
                previous_filament.current_printer = None
                previous_filament.current_tray_id = None
                previous_filament.ams_unit_id = None
                previous_filament.ams_type = ''
                previous_filament.save()
                logger.info(
                    f"Auto-unloaded {previous_filament} from "
                    f"{'external spool' if is_external else f'Tray {tray_id}'} "
                    f"(unit {ams_unit_id}; replaced by {filament.brand} {filament.type} - {filament.color})"
                )

            filament.is_loaded_in_ams = not is_external
            filament.is_loaded_externally = is_external
            if printer is not None:
                filament.current_printer = printer
            filament.current_tray_id = 254 if is_external else tray_id
            if is_external:
                filament.ams_unit_id = None
                filament.ams_type = ''
            elif ams_unit_id is not None:
                filament.ams_unit_id = ams_unit_id
            if ams_type_label and not is_external:
                filament.ams_type = ams_type_label
            filament.last_loaded_date = timezone.now()
            if self.verbose:
                logger.debug(f"Updated filament location: unit={ams_unit_id} tray={tray_id}")
        elif ams_type_label and not is_external and filament.ams_type != ams_type_label:
            # Same slot but ams_type was previously unknown — fill it in.
            filament.ams_type = ams_type_label

        filament.save()

    def _snapshot_values_for_filament(self, filament, tray_data, match_method):
        """Return display values for a snapshot, preferring manual inventory data."""
        remain_percent = self._valid_percent(tray_data.get('remain_percent'))
        if filament and match_method in {'manual_loaded_slot', 'manual_loaded_external'}:
            return {
                'type': filament.type,
                'sub_type': filament.sub_type,
                'brand': filament.brand,
                'color': self._inventory_color_for_mqtt(filament),
                'remain_percent': filament.remaining_percent,
            }

        return {
            'type': tray_data.get('type'),
            'sub_type': tray_data.get('sub_type'),
            'brand': tray_data.get('brand'),
            'color': tray_data.get('color'),
            'remain_percent': remain_percent,
        }

    def _create_filament_snapshots(self, printer_metric, filaments_data, snapshot):
        from bambu_run.models import Filament, FilamentSnapshot

        external_spool = snapshot.get('external_spool') or {}
        if self._external_spool_has_info(external_spool):
            filaments_data = list(filaments_data) + [self._external_spool_tray_data(external_spool)]

        ams_units = {
            u.get('unit_id'): u for u in snapshot.get('ams_units', [])
        }
        represented_slots = set()

        for tray_data in filaments_data:
            tray_id = tray_data.get('tray_id')
            if tray_id is None:
                continue

            filament, match_method = self._match_filament_to_inventory(
                tray_data,
                printer=printer_metric.device,
            )
            if self._is_external_tray_data(tray_data) and match_method == 'manual_loaded_slot':
                match_method = 'manual_loaded_external'
            unit_id_int = self._to_int(tray_data.get('ams_unit_id'))
            is_external = self._is_external_tray_data(tray_data)
            if unit_id_int is None and filament and filament.ams_unit_id is not None and not is_external:
                unit_id_int = self._to_int(filament.ams_unit_id)

            # Keep downstream status/snapshot code consistent when the printer
            # reports tray data without the unit id but inventory has the manual
            # AMS assignment.
            tray_data = {
                **tray_data,
                'ams_unit_id': unit_id_int,
            }
            unit_data = ams_units.get(str(unit_id_int)) if unit_id_int is not None else {}
            ams_type_label = (
                tray_data.get('ams_type')
                or (unit_data or {}).get('ams_type')
                or (filament.ams_type if filament and not is_external else '')
                or ''
            )
            if is_external:
                unit_id_int = None
                ams_type_label = 'External Spool'
            tray_data['ams_type'] = ams_type_label
            represented_slots.add((unit_id_int, self._to_int(tray_id)))

            if filament:
                remain_percent = tray_data.get('remain_percent')
                self._update_filament_status(
                    filament,
                    tray_id,
                    remain_percent,
                    tray_data,
                    sync_remaining=match_method not in {'manual_loaded_slot', 'manual_loaded_external'},
                    printer=printer_metric.device,
                )

            snapshot_values = self._snapshot_values_for_filament(filament, tray_data, match_method)

            FilamentSnapshot.objects.create(
                printer_metric=printer_metric,
                filament=filament,
                tray_id=self._to_int(tray_id),
                ams_unit_id=unit_id_int,
                ams_type=ams_type_label,
                slot_name=tray_data.get('slot'),
                type=snapshot_values['type'],
                sub_type=snapshot_values['sub_type'],
                brand=snapshot_values['brand'],
                color=snapshot_values['color'],
                remain_percent=snapshot_values['remain_percent'],
                k_value=tray_data.get('k'),
                temp=self._to_decimal(unit_data.get('temp')),
                humidity=unit_data.get('humidity'),
                tag_uid=self._clean_mqtt_identifier(tray_data.get('tag_uid')),
                tray_uuid=self._clean_mqtt_identifier(tray_data.get('tray_uuid')),
                state=tray_data.get('state'),
                auto_matched=bool(filament) and match_method not in {'manual_loaded_slot', 'manual_loaded_external'},
                match_method=match_method
            )

        available_units = set(ams_units.keys())
        for filament in (
            Filament.objects.filter(
                is_loaded_in_ams=True,
                current_printer=printer_metric.device,
            )
            .exclude(current_tray_id__isnull=True)
        ):
            tray_id = self._to_int(filament.current_tray_id)
            unit_id_int = self._to_int(filament.ams_unit_id)
            if (unit_id_int, tray_id) in represented_slots:
                continue
            if available_units and unit_id_int is not None and str(unit_id_int) not in available_units:
                continue

            unit_data = ams_units.get(str(unit_id_int), {})
            FilamentSnapshot.objects.create(
                printer_metric=printer_metric,
                filament=filament,
                tray_id=tray_id,
                ams_unit_id=unit_id_int,
                ams_type=filament.ams_type or unit_data.get('ams_type', '') or '',
                type=filament.type,
                sub_type=filament.sub_type,
                brand=filament.brand,
                color=self._inventory_color_for_mqtt(filament),
                remain_percent=filament.remaining_percent,
                temp=self._to_decimal(unit_data.get('temp')),
                humidity=unit_data.get('humidity'),
                auto_matched=False,
                match_method='manual_loaded_slot',
            )

        if (None, 254) not in represented_slots:
            external_filament = (
                Filament.objects.filter(
                    is_loaded_externally=True,
                    current_printer=printer_metric.device,
                )
                .order_by('-last_loaded_date', '-updated_at')
                .first()
            )
            if external_filament:
                FilamentSnapshot.objects.create(
                    printer_metric=printer_metric,
                    filament=external_filament,
                    tray_id=254,
                    ams_unit_id=None,
                    ams_type='External Spool',
                    type=external_filament.type,
                    sub_type=external_filament.sub_type,
                    brand=external_filament.brand,
                    color=self._inventory_color_for_mqtt(external_filament),
                    remain_percent=external_filament.remaining_percent,
                    auto_matched=False,
                    match_method='manual_loaded_external',
                )

    def _update_hotends(self, printer, printer_metric, hotends_data):
        from bambu_run.models import Hotend, HotendSnapshot

        for h in hotends_data:
            if h.get("is_empty"):
                continue

            hotend, _ = Hotend.objects.update_or_create(
                printer=printer,
                serial_number=h.get("serial_number"),
                defaults={
                    "raw_id": h.get("raw_id", 0),
                    "nozzle_type": h.get("nozzle_type", ""),
                    "diameter": self._to_decimal(h.get("diameter")),
                    "slot_number": h.get("slot_number"),
                    "is_toolhead": bool(h.get("is_toolhead")),
                    "last_filament_profile_id": h.get("fila_id", ""),
                    "last_color": h.get("color") or "",
                    "used_time_seconds": h.get("used_time_seconds", 0),
                    "wear_percent": h.get("wear_percent", 0),
                },
            )

            HotendSnapshot.objects.create(
                printer_metric=printer_metric,
                hotend=hotend,
                raw_id=h.get("raw_id", 0),
                used_time_seconds=h.get("used_time_seconds", 0),
                wear_percent=h.get("wear_percent", 0),
                stat=h.get("stat"),
            )

    def _track_print_job(self, session, metric, snapshot):
        from bambu_run.models import PrintJob

        gcode_state = snapshot.get('gcode_state')
        subtask_name = snapshot.get('subtask_name')

        if self._is_print_starting(session, gcode_state, subtask_name):
            if session.current_print_job:
                self._finalize_print_job(session, metric, snapshot)

            raw_task_id = snapshot.get('task_id')
            session.current_print_job = PrintJob.objects.create(
                device=session.printer,
                project_name=subtask_name,
                gcode_file=snapshot.get('gcode_file'),
                start_time=metric.timestamp,
                start_metric=metric,
                total_layers=snapshot.get('total_layer_num'),
                completion_percent=snapshot.get('print_percent', 0),
                cloud_task_id_raw=int(raw_task_id) if raw_task_id else None,
            )
            session.trays_used = set()
            logger.info(f"[{session.device_id}] Print job started: {subtask_name}")

        if session.current_print_job:
            tray_now = snapshot.get('tray_now', '')
            if tray_now not in (None, '', '255'):
                try:
                    tray_id = int(tray_now)
                    if 0 <= tray_id <= 15 or tray_id == 254:
                        session.trays_used.add(tray_id)
                except (ValueError, TypeError):
                    pass
            self._record_active_filament_segments(session, metric, snapshot)

        if self._is_print_ending(session, gcode_state) and session.current_print_job:
            self._finalize_print_job(session, metric, snapshot)

        session.last_gcode_state = gcode_state
        session.last_subtask_name = subtask_name

    def _is_print_starting(self, session, gcode_state, subtask_name):
        is_printing = gcode_state not in ['FINISH', 'IDLE', 'FAILED', None, '']
        has_new_job = subtask_name and subtask_name != session.last_subtask_name
        return is_printing and has_new_job

    def _is_print_ending(self, session, gcode_state):
        ending_states = ['FINISH', 'FAILED']
        return gcode_state in ending_states and session.last_gcode_state not in ending_states

    def _record_active_filament_segments(self, session, metric, snapshot):
        tray_id = self._to_int(snapshot.get('tray_now'))
        if tray_id is None or tray_id == 255:
            return

        current_line = self._to_int(snapshot.get('print_line_number'))
        current_percent = self._valid_percent(snapshot.get('print_percent'))
        snapshots = metric.filament_snapshots.filter(
            tray_id=tray_id,
            filament__isnull=False,
        )

        for filament_snapshot in snapshots:
            slot_key = (filament_snapshot.ams_unit_id, filament_snapshot.tray_id)
            active_segment = session.active_filament_segments.get(slot_key)

            if active_segment and active_segment.filament_id == filament_snapshot.filament_id:
                active_segment.ending_percent = filament_snapshot.remain_percent
                continue

            if active_segment:
                self._close_filament_segment(
                    active_segment,
                    line_number=current_line,
                    percent=current_percent,
                    ending_percent=0,
                )
                session.filament_segments.append(active_segment)

            session.active_filament_segments[slot_key] = FilamentPrintSegment(
                tray_id=filament_snapshot.tray_id,
                ams_unit_id=filament_snapshot.ams_unit_id,
                filament_id=filament_snapshot.filament_id,
                start_line_number=current_line,
                start_percent=current_percent,
                starting_percent=filament_snapshot.remain_percent or 100,
                starting_weight_grams=filament_snapshot.filament.remaining_weight_grams,
                ending_percent=filament_snapshot.remain_percent,
            )

    def _close_filament_segment(self, segment, line_number=None, percent=None, ending_percent=None):
        segment.end_line_number = line_number
        segment.end_percent = percent
        if ending_percent is not None:
            segment.ending_percent = ending_percent

    def _finalize_print_job(self, session, metric, snapshot):
        from bambu_run.models import FilamentUsage

        job = session.current_print_job
        job.end_time = metric.timestamp
        job.end_metric = metric
        job.final_status = snapshot.get('gcode_state')
        job.completion_percent = snapshot.get('print_percent', 0)
        job.calculate_duration()
        job.save()

        try:
            from bambu_run.bambu_cloud import fetch_and_upsert_task
            fetch_and_upsert_task(session.client._client, job)
        except Exception as e:
            logger.warning(f"Cloud task sync skipped (non-fatal): {e}")

        start_metric = job.start_metric
        if not start_metric:
            logger.warning(f"No start_metric for job {job.id}, skipping filament usage")
        elif not session.trays_used:
            logger.warning(f"No trays tracked for job {job.project_name}, skipping filament usage")
        elif self._finalize_segmented_filament_usage(session, job, metric, snapshot):
            pass
        else:
            # A bare tray_id (from `tray_now`) doesn't identify which physical AMS
            # unit was active when multiple units share the same slot numbering —
            # so create one usage row per (unit, tray) that had a tracked filament
            # loaded at job start, rather than guessing a single "correct" unit.
            created_usages = []
            for tray_id in session.trays_used:
                start_snaps = start_metric.filament_snapshots.filter(
                    tray_id=tray_id, filament__isnull=False
                )
                for start_snap in start_snaps:
                    end_snap = metric.filament_snapshots.filter(
                        filament=start_snap.filament,
                        tray_id=tray_id,
                        ams_unit_id=start_snap.ams_unit_id,
                    ).first()

                    usage = FilamentUsage.objects.create(
                        print_job=job,
                        filament=start_snap.filament,
                        tray_id=tray_id,
                        ams_unit_id=start_snap.ams_unit_id,
                        starting_percent=start_snap.remain_percent or 100,
                        ending_percent=end_snap.remain_percent if end_snap else None,
                    )
                    usage.calculate_consumed()
                    created_usages.append(usage)

            for usage in created_usages:
                if usage.consumed_grams is None or usage.consumed_grams <= 0:
                    local_grams = self._local_file_usage_grams_for_usage(job, usage, created_usages)
                    if local_grams:
                        self._apply_grams_usage_to_filament(usage, local_grams)
                    cloud_grams = None if local_grams else self._cloud_usage_grams_for_usage(job, usage, created_usages)
                    if cloud_grams:
                        self._apply_grams_usage_to_filament(usage, cloud_grams)

                usage.is_primary = len(created_usages) == 1
                usage.save()

                if self.verbose:
                    logger.debug(
                        f"Filament usage for {usage.filament} (unit {usage.ams_unit_id}, tray {usage.tray_id}): "
                        f"{usage.starting_percent}% -> {usage.ending_percent}%, consumed {usage.consumed_percent}%"
                    )

        logger.info(
            f"[{session.device_id}] Print job finished: {job.project_name} "
            f"({job.final_status}) - Duration: {job.duration_minutes} min, "
            f"Trays used: {sorted(session.trays_used) if session.trays_used else 'none tracked'}"
        )

        session.current_print_job = None
        session.trays_used = set()
        session.active_filament_segments = {}
        session.filament_segments = []

    def _finalize_segmented_filament_usage(self, session, job, metric, snapshot):
        for segment in session.active_filament_segments.values():
            self._close_filament_segment(
                segment,
                line_number=self._to_int(snapshot.get('print_line_number')),
                percent=self._valid_percent(snapshot.get('print_percent')),
                ending_percent=segment.ending_percent,
            )
            session.filament_segments.append(segment)
        session.active_filament_segments = {}

        segments = [
            segment for segment in session.filament_segments
            if segment.filament_id and segment.tray_id in session.trays_used
        ]
        changed_slots = {
            (segment.ams_unit_id, segment.tray_id)
            for segment in segments
        }
        has_filament_change = any(
            len({
                segment.filament_id for segment in segments
                if (segment.ams_unit_id, segment.tray_id) == slot_key
            }) > 1
            for slot_key in changed_slots
        )
        if not has_filament_change:
            return False

        first_filament = self._filament_for_segment(segments[0]) if segments else None
        total_grams = self._total_usage_grams_for_job(job, first_filament)
        if not total_grams:
            logger.warning(
                f"Filament changed during job {job.id}, but no local/cloud total usage was available"
            )
            return False

        parsed = self._parsed_print_usage_for_job(job)
        allocated_grams = 0.0
        created_usage = False

        for index, segment in enumerate(segments):
            is_final_segment = index == len(segments) - 1
            consumed_grams = None
            known_runout = not is_final_segment and segment.starting_weight_grams is not None

            if known_runout:
                consumed_grams = min(float(segment.starting_weight_grams), max(0.0, total_grams - allocated_grams))
                allocated_grams += consumed_grams
            elif is_final_segment:
                consumed_grams = max(0.0, total_grams - allocated_grams)
            else:
                filament = self._filament_for_segment(segment)
                grams_until_change = self._local_file_grams_until_line(
                    job,
                    segment.end_line_number,
                    parsed=parsed,
                    filament=filament,
                )
                if grams_until_change is not None:
                    allocated_grams = max(allocated_grams, min(float(total_grams), grams_until_change))

            usage = self._create_segment_usage(job, segment)
            created_usage = True

            if consumed_grams is not None and consumed_grams > 0:
                self._apply_grams_usage_to_filament(usage, consumed_grams)
                allocated_grams = max(allocated_grams, min(float(total_grams), allocated_grams))
            elif not is_final_segment:
                self._mark_segment_filament_depleted(usage)

            usage.is_primary = len(segments) == 1
            usage.save()

        return created_usage

    def _create_segment_usage(self, job, segment):
        from bambu_run.models import FilamentUsage

        filament = self._filament_for_segment(segment)
        return FilamentUsage.objects.create(
            print_job=job,
            filament=filament,
            tray_id=segment.tray_id,
            ams_unit_id=segment.ams_unit_id,
            starting_percent=segment.starting_percent,
            ending_percent=segment.ending_percent,
            consumed_percent=None,
            consumed_grams=None,
            is_primary=False,
        )

    def _filament_for_segment(self, segment):
        from bambu_run.models import Filament

        return Filament.objects.get(pk=segment.filament_id)

    def _mark_segment_filament_depleted(self, usage):
        filament = usage.filament
        filament.remaining_weight_grams = 0 if filament.initial_weight_grams else filament.remaining_weight_grams
        filament.remaining_percent = 0
        filament.last_used = timezone.now()
        filament.save(update_fields=['remaining_weight_grams', 'remaining_percent', 'last_used'])
        usage.ending_percent = 0

    def _total_usage_grams_for_job(self, job, filament=None):
        parsed = self._parsed_print_usage_for_job(job)
        if parsed:
            if parsed.total_grams:
                return float(parsed.total_grams)
            if parsed.per_tool_grams:
                return float(sum(parsed.per_tool_grams))
            if parsed.total_mm and filament:
                return self._grams_for_filament_length(filament, parsed.total_mm)
            if parsed.per_tool_mm and filament:
                return self._grams_for_filament_length(filament, sum(parsed.per_tool_mm))

        cloud_task = job.cloud_task
        if cloud_task and cloud_task.weight_grams and cloud_task.weight_grams > 0:
            return float(cloud_task.weight_grams)
        return None

    def _local_file_grams_until_line(self, job, line_number, parsed=None, filament=None):
        parsed = parsed or self._parsed_print_usage_for_job(job)
        if not parsed or not parsed.has_line_usage:
            return None

        mm_until_line = parsed.mm_until_line(line_number)
        total_mm = parsed.cumulative_total_mm
        if mm_until_line is None or not total_mm:
            return None

        if parsed.total_grams:
            return float(parsed.total_grams) * (mm_until_line / total_mm)
        if parsed.per_tool_grams:
            return float(sum(parsed.per_tool_grams)) * (mm_until_line / total_mm)
        if filament:
            return self._grams_for_filament_length(filament, mm_until_line)
        return None

    def _local_file_usage_grams_for_usage(self, job, usage, created_usages):
        """Use a mounted local print file as an offline usage source.

        Without the slicer-to-AMS mapping, a local file only proves which physical
        spool to charge when the collector observed exactly one used slot.
        """
        if len(created_usages) != 1:
            return None

        parsed = self._parsed_print_usage_for_job(job)
        if not parsed:
            return None

        if parsed.total_grams:
            return parsed.total_grams
        if parsed.per_tool_grams:
            return sum(parsed.per_tool_grams)
        if parsed.total_mm:
            return self._grams_for_filament_length(usage.filament, parsed.total_mm)
        if parsed.per_tool_mm:
            return self._grams_for_filament_length(usage.filament, sum(parsed.per_tool_mm))
        return None

    def _parsed_print_usage_for_job(self, job):
        if job.id in self._print_usage_cache:
            return self._print_usage_cache[job.id]

        from bambu_run.print_usage import find_print_file, parse_print_file_usage

        print_file = find_print_file(job, app_settings.PRINT_FILE_DIRS)
        if not print_file:
            self._print_usage_cache[job.id] = None
            return None

        try:
            parsed = parse_print_file_usage(print_file)
        except Exception as e:
            logger.warning(f"Local print usage parse failed for job #{job.id}: {e}")
            parsed = None

        if parsed and parsed.has_usage:
            logger.info(f"Job #{job.id}: local print usage parsed from {parsed.source_path}")
            self._print_usage_cache[job.id] = parsed
            return parsed

        self._print_usage_cache[job.id] = None
        return None

    def _grams_for_filament_length(self, filament, length_mm):
        from bambu_run.print_usage import grams_from_length

        return grams_from_length(
            length_mm,
            diameter_mm=filament.diameter or 1.75,
            material_type=filament.type,
        )

    def _cloud_usage_grams_for_usage(self, job, usage, created_usages):
        cloud_task = job.cloud_task
        if not cloud_task:
            return None

        for entry in cloud_task.ams_detail_mapping or []:
            grams = self._to_decimal(entry.get('weight'))
            if not grams or grams <= 0:
                continue
            if self._cloud_mapping_matches_usage(entry, usage):
                return grams

        if len(created_usages) == 1 and cloud_task.weight_grams and cloud_task.weight_grams > 0:
            return cloud_task.weight_grams
        return None

    def _cloud_mapping_matches_usage(self, entry, usage):
        slot_id = self._to_int(entry.get('slotId'))
        ams_id = self._to_int(entry.get('amsId'))
        if slot_id is not None:
            if usage.tray_id != slot_id:
                return False
            if usage.tray_id == 254:
                return True
            return usage.ams_unit_id is None or ams_id is None or usage.ams_unit_id == ams_id

        absolute_slot = self._to_int(entry.get('ams'))
        if absolute_slot is None:
            return False
        if absolute_slot == 254:
            return usage.tray_id == 254
        if absolute_slot >= 128:
            return usage.ams_unit_id == absolute_slot and usage.tray_id == 0
        return usage.ams_unit_id == absolute_slot // 4 and usage.tray_id == absolute_slot % 4

    def _apply_grams_usage_to_filament(self, usage, grams):
        grams_decimal = self._quantize_decimal(grams)
        if grams_decimal <= 0:
            return

        filament = usage.filament
        usage.consumed_grams = grams_decimal

        if filament.initial_weight_grams:
            current_weight = filament.remaining_weight_grams
            if current_weight is None:
                current_weight = self._quantize_decimal(
                    Decimal(str(filament.initial_weight_grams))
                    * Decimal(str(filament.remaining_percent))
                    / Decimal("100")
                )

            new_weight = max(Decimal("0"), Decimal(str(current_weight)) - grams_decimal)
            filament.remaining_weight_grams = new_weight
            filament.remaining_percent = self._quantize_decimal(
                max(
                    Decimal("0"),
                    min(Decimal("100"), new_weight / Decimal(str(filament.initial_weight_grams)) * Decimal("100")),
                )
            )
            filament.last_used = timezone.now()
            filament.save(update_fields=['remaining_weight_grams', 'remaining_percent', 'last_used'])

            usage.consumed_percent = self._quantize_decimal(
                max(
                    Decimal("0"),
                    grams_decimal / Decimal(str(filament.initial_weight_grams)) * Decimal("100"),
                )
            )
            usage.ending_percent = filament.remaining_percent

            end_metric = usage.print_job.end_metric
            if end_metric:
                end_metric.filament_snapshots.filter(
                    filament=filament,
                    tray_id=usage.tray_id,
                    ams_unit_id=usage.ams_unit_id,
                ).update(remain_percent=filament.remaining_percent)

    def _quantize_decimal(self, value):
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def _collect_printer_data(self, session: "DeviceSession"):
        try:
            snapshot = session.client.get_snapshot()

            if snapshot is None:
                session.mqtt_connect_errors += 1
                if session.mqtt_connect_errors <= 5 or self.verbose:
                    logger.warning(
                        f"[{session.device_id}] MQTT not connected yet or no data available "
                        f"(attempt {session.mqtt_connect_errors})"
                )
                return

            if not self._snapshot_has_observed_data(snapshot):
                session.mqtt_connect_errors += 1
                if session.mqtt_connect_errors <= 5 or self.verbose:
                    logger.warning(
                        f"[{session.device_id}] MQTT snapshot had no observable printer data "
                        f"(attempt {session.mqtt_connect_errors})"
                    )
                return

            with transaction.atomic():
                metric = PrinterMetrics.objects.create(
                    device=session.printer,
                    timestamp=timezone.now(),
                    nozzle_temp=self._to_decimal(snapshot.get("nozzle_temp")),
                    nozzle_target_temp=self._to_decimal(snapshot.get("nozzle_target_temp")),
                    bed_temp=self._to_decimal(snapshot.get("bed_temp")),
                    bed_target_temp=self._to_decimal(snapshot.get("bed_target_temp")),
                    chamber_temp=self._to_decimal(snapshot.get("chamber_temp")),
                    nozzle_diameter=self._to_decimal(snapshot.get("nozzle_diameter")),
                    nozzle_type=snapshot.get("nozzle_type"),
                    nozzle_temp_left=self._to_decimal(snapshot.get("nozzle_temp_left")),
                    nozzle_target_temp_left=self._to_decimal(snapshot.get("nozzle_target_temp_left")),
                    nozzle_diameter_left=self._to_decimal(snapshot.get("nozzle_diameter_left")),
                    nozzle_type_left=snapshot.get("nozzle_type_left"),
                    gcode_state=snapshot.get("gcode_state"),
                    print_type=snapshot.get("print_type"),
                    print_percent=snapshot.get("print_percent"),
                    remaining_time_min=snapshot.get("remaining_time_min"),
                    layer_num=snapshot.get("layer_num"),
                    total_layer_num=snapshot.get("total_layer_num"),
                    print_line_number=snapshot.get("print_line_number"),
                    subtask_name=snapshot.get("subtask_name"),
                    gcode_file=snapshot.get("gcode_file"),
                    cooling_fan_speed=snapshot.get("cooling_fan_speed"),
                    heatbreak_fan_speed=snapshot.get("heatbreak_fan_speed"),
                    big_fan1_speed=snapshot.get("big_fan1_speed"),
                    big_fan2_speed=snapshot.get("big_fan2_speed"),
                    spd_lvl=snapshot.get("spd_lvl"),
                    spd_mag=snapshot.get("spd_mag"),
                    wifi_signal_dbm=snapshot.get("wifi_signal_dbm"),
                    print_error=snapshot.get("print_error", 0),
                    has_errors=snapshot.get("has_errors", False),
                    chamber_light=snapshot.get("chamber_light"),
                    ipcam_record=snapshot.get("ipcam_record"),
                    timelapse=snapshot.get("timelapse"),
                    stg_cur=snapshot.get("stg_cur"),
                    sdcard=snapshot.get("sdcard"),
                    gcode_file_prepare_percent=snapshot.get("gcode_file_prepare_percent"),
                    lifecycle=snapshot.get("lifecycle"),
                    hms=snapshot.get("hms", []),
                    ams_unit_count=snapshot.get("ams_unit_count"),
                    ams_status=snapshot.get("ams_status"),
                    ams_rfid_status=snapshot.get("ams_rfid_status"),
                    ams_humidity=snapshot.get("ams_humidity"),
                    ams_humidity_raw=snapshot.get("ams_humidity_raw"),
                    ams_temp=self._to_decimal(snapshot.get("ams_temp")),
                    ams_version=snapshot.get("ams_version"),
                    tray_is_bbl_bits=snapshot.get("tray_is_bbl_bits"),
                    tray_read_done_bits=snapshot.get("tray_read_done_bits"),
                    filaments=snapshot.get("filaments", []),
                    ams_units=snapshot.get("ams_units", []),
                    external_spool=snapshot.get("external_spool", {}),
                    lights_report=snapshot.get("lights_report", []),
                    vortek_raw=snapshot.get("vortek_raw", {}),
                    nozzle_info=snapshot.get("hotends", []),
                )

                filaments_data = snapshot.get('filaments', [])
                self._create_filament_snapshots(metric, filaments_data, snapshot)

                hotends_data = snapshot.get('hotends', [])
                if hotends_data:
                    self._update_hotends(session.printer, metric, hotends_data)

                self._track_print_job(session, metric, snapshot)

                session.success_count += 1

                if self.verbose:
                    logger.debug(
                        f"[{session.device_id}] Printer Metrics: Nozzle={snapshot.get('nozzle_temp')}C, "
                        f"Bed={snapshot.get('bed_temp')}C, "
                        f"Progress={snapshot.get('print_percent')}%, "
                        f"State={snapshot.get('gcode_state')}"
                    )

        except Exception as e:
            session.error_count += 1
            logger.error(f"[{session.device_id}] Error collecting printer data (total errors: {session.error_count}): {e}")
            if self.verbose:
                logger.exception("Detailed traceback:")

    def _snapshot_has_observed_data(self, snapshot):
        if snapshot.get("gcode_state"):
            return True
        if (
            snapshot.get("filaments")
            or snapshot.get("ams_units")
            or self._external_spool_has_info(snapshot.get("external_spool"))
        ):
            return True
        if snapshot.get("hotends") or snapshot.get("hms"):
            return True
        if snapshot.get("chamber_light") not in (None, "", "unknown"):
            return True
        if snapshot.get("wifi_signal_dbm"):
            return True

        scalar_fields = (
            "nozzle_temp", "nozzle_target_temp", "bed_temp", "bed_target_temp",
            "chamber_temp", "print_percent", "remaining_time_min", "layer_num",
            "total_layer_num", "print_line_number",
        )
        return any(snapshot.get(field) is not None for field in scalar_fields)

    def _to_decimal(self, value) -> Optional[Decimal]:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (ValueError, TypeError):
            return None

    def _print_statistics(self):
        if self.start_time:
            runtime = timezone.now() - self.start_time
            success_count = sum(s.success_count for s in self.sessions.values())
            error_count = sum(s.error_count for s in self.sessions.values())
            mqtt_connect_errors = sum(s.mqtt_connect_errors for s in self.sessions.values())
            total_collections = success_count + error_count
            success_rate = (
                (success_count / total_collections * 100)
                if total_collections > 0
                else 0
            )

            logger.info("=== Statistics ===")
            logger.info(f"Runtime: {runtime}")
            logger.info(f"Printers tracked: {len(self.sessions)}")
            logger.info(f"Successful collections: {success_count}")
            logger.info(f"Failed collections: {error_count}")
            logger.info(f"MQTT connection warnings: {mqtt_connect_errors}")
            logger.info(f"Success rate: {success_rate:.1f}%")
