"""
Management command to import Bambu Lab filament color catalogs into the FilamentColor database.

Parses .txt color catalog files (one file per filament sub-type) and creates or skips
global FilamentColor records.

Usage:
    # Import a single file
    python manage.py bambu_import_colors "docs/Bambu_Color_Catalog/PLA Basic.txt"

    # Import all .txt files in a directory
    python manage.py bambu_import_colors docs/Bambu_Color_Catalog/

    # Dry-run (preview without writing)
    python manage.py bambu_import_colors docs/Bambu_Color_Catalog/ --dry-run

File naming convention:
    The stem is used to infer finish when possible:
      PLA Matte.txt   → finish=Matte
      PLA Silk.txt    → finish=Silk
      PLA Basic.txt   → finish=Default

Supported file formats:
    Format 1 (multi-line):    Format 2 (same-line / tab-separated):
      Jade White                Black Walnut    #4F3F24
      Hex:#FFFFFF               Rosewood        #4C241C

    Hex values may appear as: Hex:#RRGGBB  Hex: #RRGGBB  #RRGGBB  RRGGBB
"""

import logging
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from bambu_run.models import FilamentColor

logger = logging.getLogger("bambu_run.import_colors")

# ─── Parsing helpers ──────────────────────────────────────────────────────────

_SAME_LINE_RE = re.compile(
    r'^(.+?)\s+(?:Hex\s*:\s*)?#?([0-9A-Fa-f]{6})\s*$', re.IGNORECASE
)
_HEX_ONLY_RE = re.compile(
    r'^\s*(?:Hex\s*:\s*)?#?([0-9A-Fa-f]{6})\s*$', re.IGNORECASE
)


def _stem_to_finish(stem):
    stem_lower = stem.lower()
    finish_terms = [
        ('transparent', 'Transparent'),
        ('translucent', 'Translucent'),
        ('carbon fiber', 'Carbon Fiber'),
        ('carbon-fiber', 'Carbon Fiber'),
        ('cf', 'Carbon Fiber'),
        ('matte', 'Matte'),
        ('satin', 'Satin'),
        ('silk', 'Silk'),
        ('glossy', 'Glossy'),
        ('metallic', 'Metallic'),
        ('metal', 'Metallic'),
        ('sparkle', 'Sparkle'),
        ('glitter', 'Sparkle'),
        ('galaxy', 'Galaxy'),
        ('marble', 'Marble'),
        ('glow', 'Glow'),
        ('wood', 'Wood'),
        ('filled', 'Filled'),
        ('composite', 'Filled'),
        ('dual', 'Dual Color'),
        ('tri', 'Tri Color'),
    ]
    for needle, finish in finish_terms:
        if needle in stem_lower:
            return finish
    return 'Default'


def _parse_file(path):
    """
    Parse a color catalog file and return a list of (color_name, hex_code) tuples.

    hex_code is always 6-char uppercase without '#'.

    Raises ValueError if the file cannot be read.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"Cannot read file: {exc}") from exc

    lines = text.splitlines()
    colors = []
    i = 0

    while i < len(lines):
        stripped = lines[i].strip()
        i += 1

        if not stripped:
            continue

        # ── Format 2: color name + hex on the same line ─────────────────────
        m = _SAME_LINE_RE.match(stripped)
        if m:
            colors.append((m.group(1).strip(), m.group(2).upper()))
            continue

        # ── Orphaned hex line with no preceding name — skip ──────────────────
        if _HEX_ONLY_RE.match(stripped):
            logger.warning("  [parse] Orphaned hex line (no preceding name): '%s'", stripped)
            continue

        # ── Format 1: color name on this line, hex on the next ──────────────
        color_name = stripped
        found_hex = False

        while i < len(lines):
            next_stripped = lines[i].strip()
            i += 1  # tentatively consume

            if not next_stripped:
                continue  # skip blank lines between name and hex

            m_hex = _HEX_ONLY_RE.match(next_stripped)
            if m_hex:
                colors.append((color_name, m_hex.group(1).upper()))
                found_hex = True
            else:
                # Not a hex line — put it back for the outer loop
                i -= 1
                logger.warning(
                    "  [parse] Expected hex after '%s', got '%s' — skipping name",
                    color_name,
                    next_stripped,
                )
            break  # look-ahead done (one non-empty line checked)

        if not found_hex:
            logger.warning(
                "  [parse] Color '%s' has no hex line following it — skipping", color_name
            )

    return colors


# ─── Command ──────────────────────────────────────────────────────────────────


class Command(BaseCommand):
    help = (
        "Import Bambu Lab filament color catalog .txt files into the FilamentColor database. "
        "Accepts a single .txt file or a directory of .txt files."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "path",
            help="Path to a single .txt catalog file or a directory containing .txt files.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview what would be imported without writing to the database.",
        )

    def handle(self, *args, **options):
        input_path = Path(options["path"]).expanduser().resolve()
        dry_run = options["dry_run"]

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be written.\n"))

        # ── Collect files to process ─────────────────────────────────────────
        if input_path.is_dir():
            files = sorted(input_path.glob("*.txt"))
            if not files:
                raise CommandError(f"No .txt files found in: {input_path}")
            self.stdout.write(f"Found {len(files)} .txt file(s) in {input_path}\n")
        elif input_path.is_file():
            if input_path.suffix.lower() != ".txt":
                raise CommandError(f"Expected a .txt file, got: {input_path.name}")
            files = [input_path]
        else:
            raise CommandError(f"Path does not exist: {input_path}")

        # ── Counters ─────────────────────────────────────────────────────────
        total_created = 0
        total_skipped_dup = 0
        total_errors = 0

        for file_path in files:
            created, skipped_dup, errors = self._process_file(file_path, dry_run=dry_run)
            total_created += created
            total_skipped_dup += skipped_dup
            total_errors += errors

        # ── Summary ──────────────────────────────────────────────────────────
        self.stdout.write("\n" + "─" * 50)
        self.stdout.write(
            self.style.SUCCESS(f"  Created:              {total_created}")
        )
        self.stdout.write(f"  Skipped (duplicate):  {total_skipped_dup}")
        if total_errors:
            self.stdout.write(
                self.style.ERROR(f"  Errors:               {total_errors}")
            )
        if dry_run:
            self.stdout.write(self.style.WARNING("\nDRY RUN complete — nothing was written."))

    # ── Per-file processing ───────────────────────────────────────────────────

    def _process_file(self, file_path, *, dry_run):
        """Process one catalog file. Returns (created, skipped_dup, errors)."""
        stem = file_path.stem
        finish = _stem_to_finish(stem)

        self.stdout.write(
            f"\nProcessing: {file_path.name}  "
            f"→  finish={finish!r}"
        )

        # ── Parse file ───────────────────────────────────────────────────────
        try:
            colors = _parse_file(file_path)
        except ValueError as exc:
            self.stderr.write(self.style.ERROR(f"  ERROR reading file: {exc}"))
            return 0, 0, 1

        if not colors:
            self.stdout.write(self.style.WARNING("  No colors parsed — skipping file."))
            return 0, 0, 0

        self.stdout.write(f"  Parsed {len(colors)} color(s).")

        # ── Import colors ────────────────────────────────────────────────────
        created = skipped_dup = errors = 0

        for color_name, hex_code in colors:
            result = self._import_color(
                color_name=color_name,
                hex_code=hex_code,
                finish=finish,
                dry_run=dry_run,
            )
            if result == "created":
                created += 1
            elif result == "duplicate":
                skipped_dup += 1
            elif result == "error":
                errors += 1

        self.stdout.write(
            f"  → created={created}  duplicate={skipped_dup}  "
            f"errors={errors}"
        )
        return created, skipped_dup, errors

    def _import_color(
        self,
        *,
        color_name,
        hex_code,
        finish,
        dry_run,
    ):
        """
        Import a single (color_name, hex_code) entry.

        Returns one of: "created", "duplicate", "error"
        """
        # ── Transparent detection ────────────────────────────────────────────
        # "Translucent" (no colour qualifier) + #000000 = clear/transparent filament.
        # Bambu Lab AMS reports these as 00000000 (alpha=00).
        is_transparent = finish in {"Transparent", "Translucent"} or (
            color_name.strip().lower() == "translucent" and hex_code == "000000"
        )

        # ── Duplicate check ──────────────────────────────────────────────────
        duplicate = FilamentColor.objects.filter(
            color_code=hex_code,
            color_name__iexact=color_name,
            finish=finish,
        ).exists()

        if duplicate:
            logger.debug("  Duplicate — skipping: %s #%s", color_name, hex_code)
            return "duplicate"

        if dry_run:
            transparent_note = "  [transparent]" if is_transparent else ""
            self.stdout.write(
                f"  [dry-run] Would create: {color_name!r} #{hex_code}  "
                f"({finish}){transparent_note}"
            )
            return "created"

        # ── Write to database ────────────────────────────────────────────────
        try:
            with transaction.atomic():
                FilamentColor.objects.create(
                    color_code=hex_code,
                    color_name=color_name,
                    finish=finish,
                    is_transparent=is_transparent,
                )
            self.stdout.write(
                f"  + {color_name!r} #{hex_code}  ({finish})"
            )
            return "created"
        except Exception as exc:
            self.stderr.write(
                self.style.ERROR(
                    f"  ERROR saving {color_name!r} #{hex_code}: {exc}"
                )
            )
            return "error"
