"""Local print-file filament usage parsing.

This is intentionally conservative. It can prove total usage for one-spool jobs
from slicer comments or extrusion moves, but it does not guess AMS tray mapping
for multi-material jobs unless another source supplies that mapping.
"""

import math
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


GRAMS_RE = re.compile(r"filament\s+used\s*\[g\]\s*=\s*([0-9.,\s]+)", re.IGNORECASE)
MM_RE = re.compile(r"filament\s+used\s*\[mm\]\s*=\s*([0-9.,\s]+)", re.IGNORECASE)
TOTAL_GRAMS_RE = re.compile(r"total\s+filament\s+used\s*\[g\]\s*=\s*([0-9.]+)", re.IGNORECASE)
TOTAL_MM_RE = re.compile(r"total\s+filament\s+used\s*\[mm\]\s*=\s*([0-9.]+)", re.IGNORECASE)

MOVE_RE = re.compile(r"^(?:G0|G1)\b", re.IGNORECASE)
E_RE = re.compile(r"(?:^|\s)E(-?[0-9.]+)")
TOOL_RE = re.compile(r"^T(\d+)\b", re.IGNORECASE)
G92_E_RE = re.compile(r"^G92\b.*(?:^|\s)E(-?[0-9.]+)", re.IGNORECASE)

MATERIAL_DENSITY_G_CM3 = {
    "PLA": 1.24,
    "PETG": 1.27,
    "PET": 1.27,
    "ABS": 1.04,
    "ASA": 1.07,
    "TPU": 1.21,
    "PA": 1.14,
    "NYLON": 1.14,
    "PC": 1.20,
    "PPS": 1.35,
}


@dataclass
class ParsedPrintUsage:
    source_path: str
    per_tool_grams: list[float] = field(default_factory=list)
    total_grams: Optional[float] = None
    per_tool_mm: list[float] = field(default_factory=list)
    total_mm: Optional[float] = None
    cumulative_mm_by_line: list[float] = field(default_factory=list)

    @property
    def has_usage(self):
        return bool(self.total_grams or self.total_mm or self.per_tool_grams or self.per_tool_mm)

    @property
    def has_line_usage(self):
        return bool(self.cumulative_mm_by_line and self.cumulative_mm_by_line[-1] > 0)

    @property
    def cumulative_total_mm(self):
        if not self.cumulative_mm_by_line:
            return None
        return self.cumulative_mm_by_line[-1]

    def mm_until_line(self, line_number):
        if not self.cumulative_mm_by_line:
            return None
        if line_number in (None, ""):
            return self.cumulative_mm_by_line[-1]
        try:
            line_index = int(line_number) - 1
        except (TypeError, ValueError):
            return None
        line_index = max(0, min(line_index, len(self.cumulative_mm_by_line) - 1))
        return self.cumulative_mm_by_line[line_index]


def grams_from_length(length_mm, diameter_mm=1.75, material_type="PLA"):
    density = MATERIAL_DENSITY_G_CM3.get((material_type or "").upper(), 1.24)
    radius_mm = float(diameter_mm or 1.75) / 2.0
    volume_mm3 = math.pi * radius_mm * radius_mm * float(length_mm)
    return volume_mm3 / 1000.0 * density


def candidate_print_file_names(job):
    names = []
    for value in (job.gcode_file, job.project_name, job.display_name):
        if not value:
            continue
        base = Path(str(value)).name
        names.append(base)
        stem = Path(base).stem
        names.extend([
            f"{stem}.gcode",
            f"{stem}.gco",
            f"{stem}.3mf",
            f"{stem}.gcode.3mf",
        ])
    return list(dict.fromkeys(names))


def find_print_file(job, roots):
    candidates = candidate_print_file_names(job)
    if not roots or not candidates:
        return None

    candidate_set = set(candidates)
    for root in roots:
        root_path = Path(root).expanduser()
        if not root_path.exists():
            continue

        for candidate in candidates:
            candidate_path = Path(candidate)
            if candidate_path.is_absolute():
                try:
                    resolved = candidate_path.resolve()
                    root_resolved = root_path.resolve()
                except OSError:
                    continue
                if root_resolved in resolved.parents or resolved == root_resolved:
                    if resolved.exists() and resolved.is_file():
                        return resolved
                continue

            direct = root_path / candidate
            if direct.exists() and direct.is_file():
                return direct

        for dirpath, _, filenames in os.walk(root_path):
            for filename in filenames:
                if filename in candidate_set:
                    return Path(dirpath) / filename
    return None


def parse_print_file_usage(path):
    path = Path(path)
    if zipfile.is_zipfile(path):
        return _parse_zip_usage(path)
    return _parse_text_usage(path, path)


def _parse_zip_usage(path):
    best = ParsedPrintUsage(source_path=str(path))
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            lower = name.lower()
            if not (lower.endswith(".gcode") or lower.endswith(".gco")):
                continue
            with zf.open(name) as fh:
                text = fh.read().decode("utf-8", errors="replace")
            parsed = parse_gcode_text(text, f"{path}!{name}")
            if parsed.has_usage:
                return parsed
    return best


def _parse_text_usage(path, source_path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return parse_gcode_text(text, str(source_path))


def parse_gcode_text(text, source_path="<memory>"):
    metadata = _parse_usage_comments(text)
    parsed = metadata if metadata.has_usage else ParsedPrintUsage(source_path=source_path)
    if metadata.has_usage:
        parsed.source_path = source_path

    per_tool_mm, cumulative_mm_by_line = _parse_extrusion_moves(text)
    if per_tool_mm:
        parsed.per_tool_mm = per_tool_mm
        if parsed.total_mm is None:
            parsed.total_mm = sum(per_tool_mm)
    if cumulative_mm_by_line and cumulative_mm_by_line[-1] > 0:
        parsed.cumulative_mm_by_line = cumulative_mm_by_line
    return parsed


def _parse_usage_comments(text):
    parsed = ParsedPrintUsage(source_path="<comments>")
    grams_match = GRAMS_RE.search(text)
    if grams_match:
        parsed.per_tool_grams = _parse_number_list(grams_match.group(1))
        parsed.total_grams = sum(parsed.per_tool_grams) if parsed.per_tool_grams else None
        return parsed

    total_grams_match = TOTAL_GRAMS_RE.search(text)
    if total_grams_match:
        parsed.total_grams = float(total_grams_match.group(1))
        return parsed

    mm_match = MM_RE.search(text)
    if mm_match:
        parsed.per_tool_mm = _parse_number_list(mm_match.group(1))
        parsed.total_mm = sum(parsed.per_tool_mm) if parsed.per_tool_mm else None
        return parsed

    total_mm_match = TOTAL_MM_RE.search(text)
    if total_mm_match:
        parsed.total_mm = float(total_mm_match.group(1))
    return parsed


def _parse_number_list(value):
    return [
        float(part)
        for part in re.split(r"[,\s]+", value.strip())
        if part
    ]


def _parse_extrusion_moves(text):
    absolute_e = True
    current_tool = 0
    last_e_by_tool = {}
    used_by_tool = {}
    cumulative_mm_by_line = []
    total_used_mm = 0.0

    for raw_line in text.splitlines():
        line = raw_line.split(";", 1)[0].strip()
        if not line:
            cumulative_mm_by_line.append(total_used_mm)
            continue

        upper = line.upper()
        if upper.startswith("M82"):
            absolute_e = True
            cumulative_mm_by_line.append(total_used_mm)
            continue
        if upper.startswith("M83"):
            absolute_e = False
            cumulative_mm_by_line.append(total_used_mm)
            continue

        tool_match = TOOL_RE.match(line)
        if tool_match:
            current_tool = int(tool_match.group(1))
            cumulative_mm_by_line.append(total_used_mm)
            continue

        g92_match = G92_E_RE.match(line)
        if g92_match:
            last_e_by_tool[current_tool] = float(g92_match.group(1))
            cumulative_mm_by_line.append(total_used_mm)
            continue

        if not MOVE_RE.match(line):
            cumulative_mm_by_line.append(total_used_mm)
            continue

        e_match = E_RE.search(line)
        if not e_match:
            cumulative_mm_by_line.append(total_used_mm)
            continue

        e_value = float(e_match.group(1))
        if absolute_e:
            previous = last_e_by_tool.get(current_tool, 0.0)
            delta = e_value - previous
            last_e_by_tool[current_tool] = e_value
        else:
            delta = e_value

        if delta > 0:
            used_by_tool[current_tool] = used_by_tool.get(current_tool, 0.0) + delta
            total_used_mm += delta
        cumulative_mm_by_line.append(total_used_mm)

    if not used_by_tool:
        return [], cumulative_mm_by_line

    max_tool = max(used_by_tool)
    return [used_by_tool.get(tool, 0.0) for tool in range(max_tool + 1)], cumulative_mm_by_line
