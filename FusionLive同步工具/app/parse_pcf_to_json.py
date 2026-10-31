#!/usr/bin/env python3
"""Parse PCF files into the same viewer JSON shape used by IDF parsing."""

from __future__ import annotations

from app_paths import HOME, CODE

import argparse
import json
import math
import re
import shutil
from collections import defaultdict
from pathlib import Path

import parse_idf_to_json as idf


PHYSICAL_COMPONENT_TYPES = {
    "BEND", "BOLT", "CAP", "CLAMP", "CONNECTOR", "COUPLING", "CROSS", "CROSS-SET-ON", "CROSS-STUB",
    "ELBOLET", "ELBOW", "ELBOW-REDUCING", "FILTER", "FILTER-ANGLE", "FILTER-OFFSET",
    "FILTER-RETURN", "FLANGE", "FLANGE-BLIND", "FLANGE-REDUCING-CONCENTRIC",
    "FLANGE-REDUCING-ECCENTRIC", "GAP", "GASKET", "INSTRUMENT", "INSTRUMENT-3WAY",
    "INSTRUMENT-4WAY", "INSTRUMENT-ANGLE", "INSTRUMENT-BALLOON", "INSTRUMENT-DIAL",
    "INSTRUMENT-EXTERNAL", "INSTRUMENT-OFFSET", "INSTRUMENT-RETURN", "INSTRUMENT-TEE",
    "LAPJOINT-RING", "LAPJOINT-STUBEND", "MISC-COMPONENT", "MISC-COMPONENT-ANGLE",
    "MISC-COMPONENT-OFFSET", "MISC-COMPONENT-RETURN", "MISC-HYGIENIC", "MULTI-PORT-COMPONENT",
    "NOZZLE", "NUT", "OLET", "PENETRATION-PLATE", "PIPE", "PIPE-BLOCK-FIXED",
    "PIPE-BLOCK-VARIABLE", "PIPE-FIXED", "REDUCER-CONCENTRIC", "REDUCER-CONCENTRIC-TEED",
    "REDUCER-ECCENTRIC", "REDUCER-ECCENTRIC-TEED", "REINFORCEMENT-PAD", "SAFETY-DISC",
    "SUPPORT", "TEE", "TEE-SET-ON", "TEE-STUB", "TRAP", "TRAP-ANGLE", "TRAP-OFFSET",
    "TRAP-RETURN", "UNION", "VALVE", "VALVE-3WAY", "VALVE-4WAY", "VALVE-ANGLE",
    "VALVE-MULTIWAY", "WELD", "Y-PIECE-FABRICATED", "Y-PIECE-FITTING",
}

IGNORED_COMPONENT_TYPES = {"SUPPORT"}

INFORMATION_COMPONENT_TYPES = {
    "BIP-IDENTIFIER", "FLOW-ARROW", "FLOOR-SYMBOL", "INDUCTION-END", "INDUCTION-START",
    "INSULATION-SYMBOL", "ISO-SPLIT-POINT", "MESSAGE", "MESSAGE-CIRCLE", "MESSAGE-DIAMOND",
    "MESSAGE-DOUBLE-CIRCLE", "MESSAGE-ELLIPSE", "MESSAGE-POINTED", "MESSAGE-ROUND",
    "MESSAGE-SQUARE", "MESSAGE-TRIANGLE", "WALL-SYMBOL",
}

CONNECTION_COMPONENT_TYPES = {
    "END-CONNECTION-CORE", "END-CONNECTION-EQUIPMENT", "END-CONNECTION-JACKET",
    "END-CONNECTION-PIPELINE", "END-POSITION-CLOSED", "END-POSITION-DRAIN",
    "END-POSITION-NULL", "END-POSITION-OPEN", "END-POSITION-VENT",
}

HEADER_FIELD_NAMES = {
    "ISOGEN-FILES", "UNITS-BORE", "UNITS-CO-ORDS", "UNITS-WEIGHT", "UNITS-BOLT-DIA",
    "UNITS-BOLT-LENGTH", "UNITS-BOLT-QUANTITY", "UNITS-PIPELENGTH", "PIPELINE-REFERENCE",
    "PIPING-SPEC", "TRACING-SPEC", "INSULATION-SPEC", "PAINTING-SPEC", "PROJECT-IDENTIFIER",
    "LINE-ID", "REVISION", "DATE-DMY", "DATE-MDY", "DATE-YMD", "MESSAGE",
}

HEADER_FIELD_PREFIXES = ("ATTRIBUTE", "ITEM-ATTRIBUTE", "MISC-SPEC", "SPOOL-ATTRIBUTE", "PIPELINE-ATTRIBUTE")

COMPONENT_ID_BY_TYPE = {
    "pipe": 100,
    "elbow": 35,
    "branch": 45,
    "olet": 41,
    "reducer": 55,
    "flange": 105,
    "gasket": 110,
    "weld": 120,
    "cap": 125,
    "valve": 130,
    "angle-valve": 75,
    "three-way-valve": 80,
    "four-way-valve": 85,
    "instrument": 90,
    "trap": 132,
    "filter": 136,
    "misc-component": 95,
    "bolt": 115,
    "component": 0,
}

PCF_SOURCE_IDENTIFIER_OVERRIDES = {
    "BEND": 30,
    "ELBOW": 35,
    "ELBOW-REDUCING": 36,
    "OLET": 41,
    "INSTRUMENT-TEE": 42,
    "TEE": 45,
    "TEE-SET-ON": 46,
    "TEE-STUB": 47,
    "CROSS": 50,
    "CROSS-SET-ON": 51,
    "CROSS-STUB": 52,
    "REDUCER-CONCENTRIC": 55,
    "REDUCER-ECCENTRIC": 55,
    "REDUCER-CONCENTRIC-TEED": 60,
    "REDUCER-ECCENTRIC-TEED": 61,
    "BEND-TEED": 70,
    "ELBOW-TEED": 71,
    "VALVE-ANGLE": 75,
    "VALVE-3WAY": 80,
    "VALVE-4WAY": 85,
    "VALVE-MULTIWAY": 85,
    "INSTRUMENT-DIAL": 90,
    "INSTRUMENT": 90,
    "INSTRUMENT-ANGLE": 90,
    "INSTRUMENT-OFFSET": 90,
    "INSTRUMENT-RETURN": 90,
    "INSTRUMENT-3WAY": 90,
    "INSTRUMENT-4WAY": 90,
    "MISC-COMPONENT": 95,
    "MISC-COMPONENT-ANGLE": 96,
    "MISC-COMPONENT-OFFSET": 96,
    "MISC-COMPONENT-RETURN": 96,
    "PIPE": 100,
    "PIPE-FIXED": 101,
    "PIPE-BLOCK-FIXED": 102,
    "PIPE-BLOCK-VARIABLE": 103,
    "GAP": 104,
    "FLANGE": 105,
    "FLANGE-REDUCING-CONCENTRIC": 65,
    "FLANGE-REDUCING-ECCENTRIC": 65,
    "LAPJOINT-STUBEND": 106,
    "LAPJOINT-RING": 106,
    "FLANGE-BLIND": 107,
    "GASKET": 110,
    "CONNECTOR": 111,
    "NUT": 112,
    "CLAMP": 113,
    "MISC-HYGIENIC": 114,
    "BOLT": 115,
    "WELD": 120,
    "CAP": 125,
    "COUPLING": 126,
    "ELBOLET": 126,
    "UNION": 127,
    "VALVE": 130,
    "TRAP": 132,
    "TRAP-ANGLE": 133,
    "TRAP-OFFSET": 133,
    "TRAP-RETURN": 133,
    "SAFETY-DISC": 134,
    "FILTER": 136,
    "FILTER-ANGLE": 137,
    "FILTER-OFFSET": 137,
    "FILTER-RETURN": 137,
    "INSTRUMENT-BALLOON": 140,
    "SUPPORT": 150,
}


def component_identifier_for_pcf(ctype: str, source_type: str, skey: str = "", description: str = "") -> int:
    source = str(source_type or "").upper()
    skey_upper = str(skey or "").upper()
    if ctype == "flange" and (
        source == "FLANGE-BLIND"
        or "BLIND" in source
        or skey_upper.startswith("BL")
        or "BLIND" in str(description or "").upper()
        or "法兰盖" in description
    ):
        return 107
    if source in PCF_SOURCE_IDENTIFIER_OVERRIDES:
        return PCF_SOURCE_IDENTIFIER_OVERRIDES[source]
    return COMPONENT_ID_BY_TYPE.get(ctype, 0)


def normalize_whitespace(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def safe_float(value, default=0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def normalize_uci(value) -> str:
    return normalize_whitespace(value).upper()


def material_list_mode(value) -> str:
    text = normalize_whitespace(value).upper()
    if text == "EXCLUDE":
        return "exclude"
    if text == "INCLUDE":
        return "include"
    if text.startswith("1"):
        return "exclude"
    return "include"


def component_outputs_material(component: dict) -> bool:
    return material_list_mode(component.get("attributes", {}).get("MATERIAL-LIST", "")) != "exclude"


def read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk", "cp936", "latin1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(errors="ignore")


def is_header_field_name(name: str) -> bool:
    normalized = str(name or "").strip().upper()
    return normalized in HEADER_FIELD_NAMES or any(normalized.startswith(prefix) for prefix in HEADER_FIELD_PREFIXES)


def is_component_start(line: str) -> bool:
    stripped = line.strip()
    if not stripped or line.startswith(" "):
        return False
    return not is_header_field_name(stripped.split(None, 1)[0])


def split_pcf_sections(lines: list[str]) -> dict:
    header_lines = []
    component_lines = []
    component_line_records = []
    materials_lines = []
    section = "header"
    for line_number, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if upper == "MATERIALS":
            section = "materials"
            continue
        if section == "header":
            if is_component_start(raw_line) and upper not in INFORMATION_COMPONENT_TYPES:
                section = "components"
                component_lines.append(raw_line)
                component_line_records.append({"lineNumber": line_number, "text": raw_line})
            else:
                header_lines.append(raw_line)
        elif section == "materials":
            materials_lines.append(raw_line)
        else:
            component_lines.append(raw_line)
            component_line_records.append({"lineNumber": line_number, "text": raw_line})
    return {
        "header_lines": header_lines,
        "component_lines": component_lines,
        "component_line_records": component_line_records,
        "materials_lines": materials_lines,
    }


def parse_header_metadata(header_lines: list[str]) -> dict:
    meta = {}
    for raw_line in header_lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        key = parts[0].upper()
        value = parts[1].strip() if len(parts) > 1 else ""
        if is_header_field_name(key):
            meta[key] = value
    return meta


def parse_pipeline_reference(header_lines: list[str], path: Path) -> str:
    for raw_line in header_lines:
        stripped = raw_line.strip()
        if stripped.upper().startswith("PIPELINE-REFERENCE"):
            value = stripped.split(None, 1)[1].strip() if len(stripped.split(None, 1)) > 1 else ""
            return value or path.stem
    return path.stem


def split_component_blocks(component_lines: list[str]) -> list[list[str]]:
    blocks = []
    current = []
    for raw_line in component_lines:
        if is_component_start(raw_line):
            if current:
                blocks.append(current)
            current = [raw_line]
        elif current:
            current.append(raw_line)
    if current:
        blocks.append(current)
    return blocks


def split_component_blocks_with_line_numbers(component_line_records: list[dict]) -> list[dict]:
    blocks = []
    current_lines = []
    current_line_number = 0
    for record in component_line_records:
        raw_line = record["text"]
        if is_component_start(raw_line):
            if current_lines:
                blocks.append({"lineNumber": current_line_number, "lines": current_lines})
            current_line_number = record["lineNumber"]
            current_lines = [raw_line]
        elif current_lines:
            current_lines.append(raw_line)
    if current_lines:
        blocks.append({"lineNumber": current_line_number, "lines": current_lines})
    return blocks


def parse_point_line(line: str) -> dict:
    stripped = line.strip()
    parts = stripped.split()
    point_type = parts[0].upper() if parts else ""
    coords = None
    if len(parts) >= 4:
        coords = [safe_float(parts[1]), safe_float(parts[2]), safe_float(parts[3])]
    size = None
    end_type = ""
    if len(parts) >= 5:
        size = parts[4]
    if len(parts) >= 6:
        end_type = parts[5]
    return {"type": point_type, "raw": stripped, "coords": coords, "size": size, "endType": end_type}


def load_pcf_structure(path: Path) -> dict:
    lines = read_text(path).splitlines()
    sections = split_pcf_sections(lines)
    header_meta = parse_header_metadata(sections["header_lines"])
    return {
        "lines": lines,
        "header_lines": sections["header_lines"],
        "component_lines": sections["component_lines"],
        "component_line_records": sections["component_line_records"],
        "materials_lines": sections["materials_lines"],
        "header_meta": header_meta,
        "pipeline_ref": parse_pipeline_reference(sections["header_lines"], path),
    }


def extract_number(value) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    return float(match.group(0)) if match else None


def parse_component_block(block_lines: list[str], pipeline_ref: str, header_meta: dict, pcf_line_number: int = 0, pcf_block_index: int = 0) -> dict:
    component_type = block_lines[0].strip().upper()
    item_code = ""
    component_identifier = ""
    master_component_identifier = ""
    material_identifier = ""
    skey = ""
    uci = ""
    description_parts = []
    specs = []
    points = []
    attributes = {}
    no_material_flag = False
    quantity = 1.0
    pending_tap_connection = False

    for raw_line in block_lines[1:]:
        stripped = raw_line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if upper == "TAP-CONNECTION":
            pending_tap_connection = True
            continue
        if upper.startswith(("END-POINT", "BRANCH1-POINT", "BRANCH2-POINT", "CENTRE-POINT", "CO-ORDS")):
            point = parse_point_line(raw_line)
            if pending_tap_connection:
                point["tapConnection"] = True
            points.append(point)
            if point.get("size") and not (component_type == "PIPE" and point["type"] == "CO-ORDS" and pending_tap_connection):
                specs.append(point["size"])
            pending_tap_connection = False
            continue
        pending_tap_connection = False
        parts = stripped.split(None, 1)
        key = parts[0].upper()
        value = parts[1].strip() if len(parts) > 1 else ""
        if key in {"ITEM-CODE", "BOLT-ITEM-CODE"}:
            item_code = value
        elif key == "COMPONENT-IDENTIFIER":
            component_identifier = value
        elif key == "MASTER-COMPONENT-IDENTIFIER":
            master_component_identifier = value
        elif key == "MATERIAL-IDENTIFIER":
            material_identifier = value
        elif key in {"UCI", "UNIQUE-COMPONENT-IDENTIFIER"}:
            uci = value
        elif key == "SKEY":
            skey = value
        elif key in {"ITEM-DESCRIPTION", "BOLT-ITEM-DESCRIPTION", "DESCRIPTION", "TEXT"}:
            if value:
                description_parts.append(value)
        elif key == "CUT-PIECE-LENGTH":
            quantity = safe_float(value, quantity)
        elif key in {"BOLT-DIA", "BOLT-LENGTH"}:
            attributes[key] = value
        elif key == "BOLT-QUANTITY":
            quantity = safe_float(value, quantity)
            attributes[key] = value
        elif key == "MATERIAL-LIST":
            attributes["MATERIAL-LIST"] = value
            if material_list_mode(value) == "exclude":
                no_material_flag = True
        elif key.startswith("COMPONENT-ATTRIBUTE"):
            attributes[key] = value
            if "BOMCOLUMN_NoMaterial" in value or "不出料" in value:
                no_material_flag = True
        else:
            attributes[key] = value

    return {
        "pipelineRef": pipeline_ref,
        "sourceType": component_type,
        "pcfLineNumber": pcf_line_number,
        "pcfBlockIndex": pcf_block_index,
        "skey": skey,
        "uci": uci,
        "normalizedUci": normalize_uci(uci),
        "componentIdentifier": component_identifier,
        "masterComponentIdentifier": master_component_identifier,
        "materialIdentifier": material_identifier,
        "itemCode": item_code,
        "description": normalize_whitespace(" ".join(description_parts)),
        "specs": specs,
        "points": points,
        "attributes": attributes,
        "pipeSpec": attributes.get("PIPING-SPEC") or header_meta.get("PIPING-SPEC", ""),
        "noMaterialFlag": no_material_flag,
        "materialListMode": material_list_mode(attributes.get("MATERIAL-LIST", "")),
        "quantity": quantity,
    }


def parse_materials_section(pcf_data: dict) -> list[dict]:
    results = []
    pending_codes = []
    pending_groups = []
    current = None

    def flush():
        nonlocal pending_codes, pending_groups, current
        if not pending_codes:
            pending_groups = []
            current = None
            return
        if not pending_groups:
            pending_groups = [{} for _ in pending_codes]
        if len(pending_groups) == 1 and len(pending_codes) > 1:
            pending_groups = pending_groups * len(pending_codes)
        count = max(len(pending_codes), len(pending_groups))
        for index in range(count):
            outer_code = pending_codes[index] if index < len(pending_codes) else ""
            group = pending_groups[index] if index < len(pending_groups) else {}
            raw_code = group.get("ITEM-CODE") or outer_code
            if isinstance(raw_code, list):
                raw_code = " ".join(str(item) for item in raw_code if str(item).strip())
            code = normalize_whitespace(raw_code)
            description = normalize_whitespace(" ".join(group.get("DESCRIPTION", [])) if isinstance(group.get("DESCRIPTION"), list) else group.get("DESCRIPTION", ""))
            if code or description or outer_code:
                results.append({
                    "index": len(results) + 1,
                    "identifier": normalize_whitespace(outer_code),
                    "code": code,
                    "description": description or code or normalize_whitespace(outer_code),
                })
        pending_codes = []
        pending_groups = []
        current = None

    def ensure_group():
        nonlocal current
        if current is None:
            current = defaultdict(list)
            pending_groups.append(current)
        return current

    for raw_line in pcf_data["materials_lines"]:
        line = raw_line.rstrip()
        stripped = raw_line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        key = parts[0].upper()
        value = parts[1].strip() if len(parts) > 1 else ""
        is_outer = not line.startswith(" ")
        if is_outer and key in {"ITEM-CODE", "MATERIAL-IDENTIFIER"}:
            if pending_codes and pending_groups:
                flush()
            pending_codes.append(normalize_whitespace(value))
            continue
        if not pending_codes:
            continue
        if key == "DESCRIPTION":
            group = ensure_group()
            if group.get("DESCRIPTION") and len(group.keys()) > 1:
                current = None
                group = ensure_group()
            group["DESCRIPTION"].append(value)
        elif key == "ITEM-CODE":
            group = ensure_group()
            if group.get("ITEM-CODE"):
                current = None
                group = ensure_group()
            group["ITEM-CODE"].append(value)
        else:
            group = ensure_group()
            group[key].append(value)
    flush()
    return results


def parse_upper_components(pcf_data: dict) -> list[dict]:
    components = []
    for block_index, block in enumerate(split_component_blocks_with_line_numbers(pcf_data["component_line_records"]), start=1):
        block_lines = block["lines"]
        component_type = block_lines[0].strip().upper()
        if component_type in IGNORED_COMPONENT_TYPES:
            continue
        if component_type in INFORMATION_COMPONENT_TYPES or component_type in CONNECTION_COMPONENT_TYPES or component_type == "MATERIALS":
            continue
        if component_type not in PHYSICAL_COMPONENT_TYPES and not component_type.startswith(("VALVE", "INSTRUMENT", "MISC", "FILTER", "TRAP")):
            continue
        components.append(parse_component_block(
            block_lines,
            pcf_data["pipeline_ref"],
            pcf_data["header_meta"],
            pcf_line_number=block["lineNumber"],
            pcf_block_index=block_index,
        ))
    infer_missing_secondary_specs(components, pcf_data["lines"])
    return components


def infer_missing_secondary_specs(components: list[dict], lines: list[str]) -> None:
    point_sizes = defaultdict(set)
    for component in components:
        for point in component.get("points", []):
            if point.get("coords") and point.get("size"):
                point_sizes[point_key(point["coords"])].add(point["size"])
    for component in components:
        if component["sourceType"] not in {"OLET", "COUPLING"} or len(component["specs"]) >= 2:
            continue
        own = set(component["specs"])
        for point in component.get("points", []):
            for size in point_sizes.get(point_key(point.get("coords")), set()):
                if size not in own:
                    component["specs"].append(size)
                    own.add(size)
                if len(component["specs"]) >= 2:
                    break
            if len(component["specs"]) >= 2:
                break
    if lines:
        return


def point_key(point) -> str:
    if not point:
        return ""
    return ",".join(f"{safe_float(value):.3f}" for value in point)


def coord_scale(header_meta: dict) -> float:
    unit = str(header_meta.get("UNITS-CO-ORDS", "")).strip().upper()
    if unit in {"MM", "MILLIMETER", "MILLIMETERS", ""}:
        return 100.0
    if unit in {"M", "METER", "METERS"}:
        return 100000.0
    if unit in {"INCH", "INCHES", "INS", "IN"}:
        return 2540.0
    return 100.0


def normalize_point(point: dict | None, scale: float) -> list[float] | None:
    coords = point.get("coords") if point else None
    if not coords:
        return None
    return [round(safe_float(value) * scale, 3) for value in coords]


def point_size_dn(size, header_meta: dict) -> float:
    value = extract_number(size)
    if value is None or value <= 0:
        return 0.0
    bore_unit = str(header_meta.get("UNITS-BORE", "")).strip().upper()
    if bore_unit in {"INCH", "INCHES", "INS", "IN"}:
        return idf.nps_to_dn(value)
    return float(value)


def component_spec(component: dict, header_meta: dict) -> float:
    sizes = [point_size_dn(item, header_meta) for item in component.get("specs", [])]
    sizes = [item for item in sizes if item > 0]
    return max(sizes) if sizes else 0.0


def component_display_spec(component: dict, header_meta: dict) -> str:
    sizes = sorted({point_size_dn(item, header_meta) for item in component.get("specs", []) if point_size_dn(item, header_meta) > 0}, reverse=True)
    if len(sizes) >= 2:
        return f"{round(sizes[0])}x{round(sizes[1])}"
    if sizes:
        return str(round(sizes[0]))
    return ""


def classify_pcf_component(source_type: str, skey: str, description: str) -> str:
    source = source_type.upper()
    skey_upper = str(skey or "").upper()
    desc = str(description or "").lower()
    if source == "PIPE" or source.startswith("PIPE-"):
        return "pipe"
    if source in {"BEND-TEED", "ELBOW-TEED"}:
        return "teed-elbow"
    if source in {"ELBOW", "ELBOW-REDUCING", "BEND"}:
        return "elbow"
    if source.startswith("TEE") or source.startswith("CROSS") or source.startswith("Y-PIECE"):
        return "branch"
    if source in {"OLET", "ELBOLET"}:
        return "olet"
    if source in {"INSTRUMENT-TEE"}:
        return "olet"
    if source in {"REDUCER-CONCENTRIC-TEED", "REDUCER-ECCENTRIC-TEED"}:
        return "teed-reducer"
    if source.startswith("REDUCER"):
        return "reducer"
    if source.startswith("FLANGE") or source.startswith("LAPJOINT") or skey_upper.startswith("FL"):
        return "flange"
    if source == "GASKET":
        return "gasket"
    if source == "WELD":
        return "weld"
    if source == "BOLT":
        return "bolt"
    if source == "CAP" or "cap" in desc:
        return "cap"
    if source in {"COUPLING", "UNION", "CONNECTOR", "NUT", "CLAMP", "MISC-HYGIENIC", "SAFETY-DISC", "GAP", "NOZZLE", "PENETRATION-PLATE", "REINFORCEMENT-PAD"}:
        return "misc-component"
    if source.startswith("VALVE"):
        if "ANGLE" in source:
            return "angle-valve"
        if "3WAY" in source:
            return "three-way-valve"
        if "4WAY" in source or "MULTIWAY" in source:
            return "four-way-valve"
        return "valve"
    if source.startswith("INSTRUMENT"):
        if "ANGLE" in source:
            return "angle-valve"
        if "3WAY" in source or "TEE" in source:
            return "three-way-valve"
        if "4WAY" in source:
            return "four-way-valve"
        return "instrument"
    if source.startswith("TRAP"):
        return "trap"
    if source.startswith("FILTER"):
        return "filter"
    if source.startswith("MISC"):
        return "misc-component"
    return "component"


def ordered_points(component: dict, scale: float) -> dict[str, list[list[float]]]:
    result = defaultdict(list)
    for point in component.get("points", []):
        coords = normalize_point(point, scale)
        if coords:
            result[point["type"]].append(coords)
    return result


def pcf_connection_end_types(component: dict, scale: float) -> dict[str, str]:
    result = {}
    for point in component.get("points", []):
        coords = normalize_point(point, scale)
        end_type = normalize_whitespace(point.get("endType", "")).upper()
        if coords and end_type:
            result[idf.normalize_point_key(coords)] = end_type
    return result


def first_two_endpoints(points_by_type: dict[str, list[list[float]]]) -> tuple[list[float] | None, list[float] | None]:
    endpoints = points_by_type.get("END-POINT", [])
    if len(endpoints) >= 2:
        return endpoints[0], endpoints[1]
    candidates = []
    for key in ("BRANCH1-POINT", "BRANCH2-POINT", "CENTRE-POINT", "CO-ORDS"):
        candidates.extend(points_by_type.get(key, []))
    all_points = endpoints + candidates
    if len(all_points) >= 2:
        return all_points[0], all_points[1]
    if len(all_points) == 1:
        return all_points[0], None
    return None, None


def build_segments_for_component(viewer_component: dict, pcf_component: dict, points_by_type: dict[str, list[list[float]]]) -> list[dict]:
    ctype = viewer_component["type"]
    spec = viewer_component.get("spec", 0)
    od = viewer_component.get("outerDiameterMm", 0)

    def segment(start, end, segment_type=None):
        return {
            "start": start,
            "end": end,
            "identifier": viewer_component["identifier"],
            "lineNumber": viewer_component["lineNumber"],
            "lineNumbers": viewer_component.get("lineNumbers") or [viewer_component["lineNumber"]],
            "type": segment_type or ctype,
            "spec": spec,
            "outerDiameterMm": od,
        }

    if ctype in {"branch", "olet", "teed-reducer", "teed-elbow", "three-way-valve", "four-way-valve"}:
        centre = (points_by_type.get("CENTRE-POINT") or points_by_type.get("CO-ORDS") or [viewer_component.get("start")])[0]
        branches = []
        for key in ("END-POINT", "BRANCH1-POINT", "BRANCH2-POINT"):
            branches.extend(points_by_type.get(key, []))
        unique_branches = []
        for point in branches:
            if point and not idf.same_point(point, centre) and point_key(point) not in {point_key(item) for item in unique_branches}:
                unique_branches.append(point)
        return [segment(centre, point) for point in unique_branches[:4]]

    if ctype == "elbow":
        centre_points = points_by_type.get("CENTRE-POINT") or []
        endpoints = points_by_type.get("END-POINT") or []
        if centre_points and len(endpoints) >= 2:
            centre = centre_points[0]
            return [segment(endpoints[0], centre), segment(centre, endpoints[1])]

    if viewer_component.get("start") and viewer_component.get("end"):
        return [segment(viewer_component["start"], viewer_component["end"])]
    return []


def material_map(materials: list[dict]) -> dict[str, dict]:
    result = {}
    for material in materials:
        for key in (material.get("identifier"), material.get("code")):
            if key:
                result.setdefault(str(key), material)
    return result


def pcf_component_to_viewer(component: dict, path: Path, header_meta: dict, materials_by_code: dict) -> dict | None:
    scale = coord_scale(header_meta)
    source_type = component["sourceType"]
    ctype = classify_pcf_component(source_type, component.get("skey", ""), component.get("description", ""))
    points_by_type = ordered_points(component, scale)
    connection_end_types = pcf_connection_end_types(component, scale)
    start, end = first_two_endpoints(points_by_type)
    if ctype == "weld":
        start = start or (points_by_type.get("CO-ORDS") or points_by_type.get("CENTRE-POINT") or [None])[0]
        end = None
    if ctype == "instrument" and not start:
        start = (points_by_type.get("CO-ORDS") or points_by_type.get("CENTRE-POINT") or [None])[0]
    if not start and ctype not in {"gasket", "bolt"}:
        return None

    material = materials_by_code.get(component.get("materialIdentifier")) or materials_by_code.get(component.get("itemCode")) or {}
    material_code = component.get("itemCode") or material.get("code", "")
    material_description = component.get("description") or material.get("description", "") or material_code
    spec = component_spec(component, header_meta)
    identifier = component_identifier_for_pcf(ctype, source_type, component.get("skey", ""), material_description)
    line_number = int(component.get("pcfLineNumber") or component.get("pcfBlockIndex") or 0)
    block_index = int(component.get("pcfBlockIndex") or 0)
    viewer_component = {
        "id": f"{path.name}:{line_number or block_index}",
        "lineNumber": line_number,
        "lineNumbers": [line_number],
        "identifier": identifier,
        "identifiers": [identifier],
        "type": ctype,
        "start": start,
        "end": end,
        "rawSpec": spec,
        "boreInputUnits": header_meta.get("UNITS-BORE", ""),
        "spec": spec,
        "displaySpec": component_display_spec(component, header_meta),
        "outerDiameterMm": idf.get_outer_diameter_mm(spec),
        "materialIndex": material.get("index", 0),
        "materialCode": material_code,
        "materialDescription": material_description,
        "skey": component.get("skey", ""),
        "pcfConnectionEndTypes": connection_end_types,
        "reducerKind": "eccentric" if "ECCENTRIC" in source_type else "concentric" if "REDUCER" in source_type else "",
        "instrumentTag": infer_instrument_tag(component),
        "noMaterialFlag": component.get("noMaterialFlag", False),
        "raw": source_type,
        "pcfSourceType": source_type,
        "pcfBlockIndex": block_index,
        "pcfLineNumber": line_number,
        "pcfComponentIdentifier": component.get("componentIdentifier", ""),
        "pcfMaterialIdentifier": component.get("materialIdentifier", ""),
        "quantity": component.get("quantity") or 1,
    }
    if source_type == "BOLT":
        viewer_component["hideInModel"] = True
        viewer_component["boltDiameter"] = component.get("attributes", {}).get("BOLT-DIA", "")
        viewer_component["boltLength"] = component.get("attributes", {}).get("BOLT-LENGTH", "")
        viewer_component["materialSpecLabel"] = idf.format_bolt_spec(
            viewer_component["boltDiameter"],
            viewer_component["boltLength"],
            header_meta.get("UNITS-BOLT-DIA", ""),
            header_meta.get("UNITS-BOLT-LENGTH", ""),
        )
    segments = build_segments_for_component(viewer_component, component, points_by_type)
    if segments:
        viewer_component["segments"] = segments
        viewer_component["start"] = segments[0]["start"]
        viewer_component["end"] = segments[-1]["end"]
    if ctype == "weld":
        viewer_component["weldType"] = idf.infer_component_weld_type(viewer_component)
        viewer_component["end"] = None
    elif ctype == "pipe":
        viewer_component["quantity"] = idf.calculate_component_quantity(viewer_component)
    return viewer_component


PCF_END_TYPE_WELD_TYPES = {
    "BW": "bw",
    "BE": "bw",
    "PE": "bw",
    "SW": "sw",
    "SC": "scw",
    "SCRD": "scw",
    "THD": "scw",
    "THRD": "scw",
    "MTE": "scw",
}


def infer_pcf_weld_type_from_skey(skey: str) -> str:
    skey = str(skey or "").upper()
    if skey in idf.PIPE_OPENING_WELD_SKEYS:
        return "seton"
    if skey in idf.EXPLICIT_SKEY_WELD_TYPES:
        return idf.EXPLICIT_SKEY_WELD_TYPES[skey]
    suffix = skey[-2:] if len(skey) >= 2 else ""
    if suffix in idf.END_CONDITION_WELD_TYPES:
        return idf.END_CONDITION_WELD_TYPES[suffix]
    if skey in idf.SLIP_ON_FLANGE_SKEYS:
        return "so"
    return ""


def infer_pcf_weld_type_from_description(description: str) -> str:
    description_weld_type = idf.infer_weld_type_from_material_description(description)
    if description_weld_type:
        return description_weld_type
    text = f" {str(description or '').upper()} "
    tokens = {token.strip() for token in re.split(r"[;,/()\s]+", text) if token.strip()}
    if {"SOCKOLET", "WELDOLET", "BRANCH"}.intersection(tokens):
        return "olet"
    return ""


def component_point_role(component: dict, point: list[float] | None) -> str:
    if not point:
        return ""
    if component.get("start") and idf.same_point(component.get("start"), point):
        return "start"
    if component.get("end") and idf.same_point(component.get("end"), point):
        return "end"
    for segment in component.get("segments") or []:
        if segment.get("start") and idf.same_point(segment.get("start"), point):
            return "start"
        if segment.get("end") and idf.same_point(segment.get("end"), point):
            return "end"
    return ""


def infer_pcf_component_weld_type(component: dict, point: list[float] | None = None) -> str:
    if component.get("type") == "olet" and component_point_role(component, point) == "start":
        return "olet"

    for candidate in (
        infer_pcf_weld_type_from_description(component.get("materialDescription", "")),
    ):
        if candidate:
            return candidate

    if point:
        end_type = (component.get("pcfConnectionEndTypes") or {}).get(idf.normalize_point_key(point), "")
        mapped = PCF_END_TYPE_WELD_TYPES.get(str(end_type or "").upper())
        if mapped:
            return mapped

    for candidate in (
        infer_pcf_weld_type_from_skey(component.get("skey", "")),
        infer_pcf_weld_type_from_description(component.get("raw", "")),
    ):
        if candidate:
            return candidate
    return "bw"


def infer_pcf_connection_weld_type(weld: dict, component_by_id: dict) -> str:
    point = weld.get("start") or weld.get("displayStart")
    connected = [
        component_by_id[component_id]
        for component_id in weld.get("connectedComponentIds") or []
        if component_id in component_by_id
    ]
    if not connected:
        return weld.get("weldType") or "bw"
    if any(component.get("type") == "olet" and component_point_role(component, point) == "start" for component in connected):
        return "olet"

    preferred = [component for component in connected if component.get("type") not in {"pipe", "reducer"}]
    candidates = [infer_pcf_component_weld_type(component, point) for component in (preferred or connected)]
    for weld_type in ("seton", "olet", "sw", "scw", "so"):
        if weld_type in candidates:
            return weld_type
    return next((weld_type for weld_type in candidates if weld_type), weld.get("weldType") or "bw")


def apply_pcf_weld_types(model: dict) -> None:
    component_by_id = {component.get("id"): component for component in model.get("components", [])}
    for component in model.get("components", []):
        if component.get("type") == "weld" and component.get("connectedComponentIds"):
            component["weldType"] = infer_pcf_connection_weld_type(component, component_by_id)
    idf.assign_weld_numbers(
        model.get("components", []),
        model.get("segments", []),
        default_pipeline_id=idf.get_model_pipeline_id(model, "UNKNOWN"),
        default_pipeline_name=model.get("pipelineName") or model.get("fileName") or "UNKNOWN",
    )


def infer_instrument_tag(component: dict) -> str:
    for key in ("TAG", "INSTRUMENT-TAG", "TAG-NUMBER"):
        value = component.get("attributes", {}).get(key)
        if value:
            return normalize_whitespace(value)
    description = component.get("description", "")
    match = re.search(r"\b([A-Z]{1,4}[-_]?\d+[A-Z0-9-]*)\b", description)
    return match.group(1) if match else ""


def parse_pcf(path: Path, excluded_duplicate_uci: set[str] | None = None) -> dict:
    excluded_duplicate_uci = excluded_duplicate_uci or set()
    pcf_data = load_pcf_structure(path)
    materials = parse_materials_section(pcf_data)
    materials_by_code = material_map(materials)
    split_markers = parse_split_markers(pcf_data, path)
    upper_components = parse_upper_components(pcf_data)
    components = []
    for component in upper_components:
        if should_skip_duplicate_uci_component(component, excluded_duplicate_uci):
            continue
        viewer_component = pcf_component_to_viewer(component, path, pcf_data["header_meta"], materials_by_code)
        if viewer_component:
            components.append(viewer_component)

    model = idf.build_viewer_model({
        "pipelineName": pcf_data["pipeline_ref"],
        "fileName": path.name,
        "materials": materials,
        "components": components,
        "drawingOptions": {
            "raw": {},
            "named": {},
            "sourceType": "pcf",
            "units": {
                "bore": pcf_data["header_meta"].get("UNITS-BORE", ""),
                "coords": pcf_data["header_meta"].get("UNITS-CO-ORDS", ""),
            },
        },
        "drawingSplitMarkers": split_markers,
        "sourceType": "pcf",
    })
    apply_pcf_weld_types(model)
    return model


def should_skip_duplicate_uci_component(component: dict, excluded_duplicate_uci: set[str]) -> bool:
    uci = component.get("normalizedUci", "")
    return bool(uci and uci in excluded_duplicate_uci and not component_outputs_material(component))


def collect_duplicate_uci_exclusions(pcf_files: list[Path]) -> set[str]:
    entries_by_uci = defaultdict(list)
    for path in pcf_files:
        pcf_data = load_pcf_structure(path)
        for component in parse_upper_components(pcf_data):
            uci = component.get("normalizedUci", "")
            if not uci:
                continue
            entries_by_uci[uci].append({
                "path": path,
                "lineNumber": component.get("pcfLineNumber", 0),
                "blockIndex": component.get("pcfBlockIndex", 0),
                "outputsMaterial": component_outputs_material(component),
                "materialListMode": component.get("materialListMode", "include"),
                "sourceType": component.get("sourceType", ""),
            })

    excluded_uci = set()
    duplicate_count = 0
    skipped_component_count = 0
    for uci, entries in entries_by_uci.items():
        if len(entries) <= 1:
            continue
        duplicate_count += 1
        material_entries = [entry for entry in entries if entry["outputsMaterial"]]
        if len(material_entries) == 1:
            excluded_uci.add(uci)
            skipped_component_count += sum(1 for entry in entries if not entry["outputsMaterial"])

    if duplicate_count:
        print(
            f"UCI duplicate scan: {duplicate_count} duplicated UCI values, "
            f"{len(excluded_uci)} UCI values eligible for adjacent-line exclusion, "
            f"{skipped_component_count} non-material component instances will be skipped"
        )
    else:
        print("UCI duplicate scan: no duplicated UCI values found")
    return excluded_uci


def parse_split_markers(pcf_data: dict, path: Path) -> list[dict]:
    markers = []
    scale = coord_scale(pcf_data["header_meta"])
    for block_index, block in enumerate(split_component_blocks_with_line_numbers(pcf_data["component_line_records"]), start=1):
        block_lines = block["lines"]
        if block_lines[0].strip().upper() != "ISO-SPLIT-POINT":
            continue
        point = None
        for raw_line in block_lines[1:]:
            stripped = raw_line.strip().upper()
            if stripped.startswith(("END-POINT", "CENTRE-POINT", "CO-ORDS")):
                point = normalize_point(parse_point_line(raw_line), scale)
                break
        marker = {
            "id": f"{path.name}:split:{block['lineNumber']}",
            "lineNumber": block["lineNumber"],
            "pcfBlockIndex": block_index,
            "raw": "\n".join(line.rstrip() for line in block_lines),
            "sourceType": "pcf",
        }
        if point:
            marker["point"] = point
        markers.append(marker)
    return markers


def find_pcf_files(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path] if input_path.suffix.lower() == ".pcf" else []
    return sorted([path for path in input_path.rglob("*") if path.is_file() and path.suffix.lower() == ".pcf"], key=lambda item: str(item).lower())


def group_pcf_files_by_unit(input_path: Path) -> list[tuple[str, list[Path]]]:
    files = find_pcf_files(input_path)
    if input_path.is_file():
        return [(input_path.parent.name or "PCF", files)]
    groups = {}
    for path in files:
        try:
            relative = path.relative_to(input_path)
        except ValueError:
            relative = Path(path.name)
        unit_name = relative.parts[0] if len(relative.parts) > 1 else input_path.name
        groups.setdefault(unit_name, []).append(path)
    return sorted(
        ((unit_name, sorted(paths, key=lambda item: str(item).lower())) for unit_name, paths in groups.items()),
        key=lambda item: item[0].lower(),
    )


def clear_unit_output_dir(output_dir: Path) -> None:
    if not output_dir.exists():
        return
    if output_dir.name != idf.safe_output_name(output_dir.name) or not output_dir.name.endswith("-units"):
        raise ValueError(f"Refuse to clear unsafe unit output directory: {output_dir}")
    for child in output_dir.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


PCF_WELD_TABLE_FIELDS = [
    "单元号", "管线号", "焊口号", "公称直径", "寸径", "焊接类型",
    "材料描述1", "材料代码1", "材料唯一码1", "数量1", "单位1",
    "材料描述2", "材料代码2", "材料唯一码2", "数量2", "单位2", "焊点坐标",
]

PCF_MATERIAL_TABLE_FIELDS = [
    "单元号", "管线号", "材料描述", "材料代码", "规格", "source_type", "skey", "序号", "数量", "单位", "不出料标识", "开口焊不计料",
]


def pcf_weld_row_has_material(row: dict) -> bool:
    return any(str(row.get(key) or "").strip() for key in ("材料描述1", "材料代码1", "材料唯一码1", "材料描述2", "材料代码2", "材料唯一码2"))


def build_pcf_weld_table_rows(model: dict) -> list[dict]:
    rows = [row for row in idf.build_weld_table_rows(model) if pcf_weld_row_has_material(row)]
    return sorted(rows, key=lambda row: (
        row["单元号"],
        row["管线号"],
        idf.weld_no_sort_value(row["_sortWeldNo"]),
        row["公称直径"],
    ))


def build_pcf_material_table_rows(model: dict) -> list[dict]:
    grouped = {}
    for component in model.get("components", []):
        if component.get("type") in idf.MATERIAL_TABLE_EXCLUDED_TYPES:
            continue
        if not component.get("materialCode") and not component.get("materialDescription"):
            continue
        unit_name = component.get("unitName") or model.get("unitName", "")
        pipeline_name = component.get("pipelineName") or model.get("pipelineName", "")
        key = (
            unit_name,
            component.get("pipelineId", ""),
            pipeline_name,
            component.get("materialDescription", ""),
            component.get("materialCode", ""),
            idf.component_spec_label(component),
            component.get("skey", ""),
            idf.component_unit(component),
            bool(component.get("noMaterialFlag")),
            bool(component.get("pipeOpeningWeldNoMaterial")),
            component.get("materialIndex", 0),
        )
        entry = grouped.setdefault(key, {
            "单元号": unit_name,
            "管线号": pipeline_name,
            "材料描述": component.get("materialDescription", ""),
            "材料代码": component.get("materialCode", ""),
            "规格": idf.component_spec_label(component),
            "source_type": set(),
            "skey": component.get("skey", ""),
            "序号": component.get("materialIndex", 0),
            "数量": 0.0,
            "单位": idf.component_unit(component),
            "不出料标识": "是" if component.get("noMaterialFlag") else "",
            "开口焊不计料": "是" if component.get("pipeOpeningWeldNoMaterial") else "",
            "_sortLine": idf.get_component_sort_line(component),
        })
        source_type = component.get("pcfSourceType") or component.get("raw") or component.get("type") or ""
        if source_type:
            entry["source_type"].add(str(source_type))
        if not component.get("noMaterialFlag"):
            entry["数量"] += idf.material_quantity_for_table(component)
        entry["_sortLine"] = min(entry["_sortLine"], idf.get_component_sort_line(component))

    rows = []
    for entry in grouped.values():
        if abs(idf.safe_float(entry["数量"])) <= 1e-9:
            continue
        rows.append({
            **{key: value for key, value in entry.items() if not key.startswith("_")},
            "source_type": "/".join(sorted(entry["source_type"])),
            "数量": idf.format_quantity(entry["数量"]),
        })
    return sorted(rows, key=lambda row: (
        row["单元号"],
        row["管线号"],
        idf.safe_float(row["序号"]),
        row["材料描述"],
        row["材料代码"],
        row["规格"],
    ))


def write_pcf_derived_tables(model: dict, weld_table_output: Path | None, material_table_output: Path | None) -> None:
    if weld_table_output:
        weld_rows = build_pcf_weld_table_rows(model)
        idf.write_csv(weld_table_output, weld_rows, PCF_WELD_TABLE_FIELDS)
        print(f"Wrote {weld_table_output} with {len(weld_rows)} weld rows")

    if material_table_output:
        material_rows = build_pcf_material_table_rows(model)
        idf.write_csv(material_table_output, material_rows, PCF_MATERIAL_TABLE_FIELDS)
        print(f"Wrote {material_table_output} with {len(material_rows)} material rows")


def write_unit_outputs(input_path: Path, output_dir: Path, manifest_output: Path, project_name: str, weld_table_output: Path | None, material_table_output: Path | None) -> dict:
    parsed_units = []
    all_pipeline_models = []
    pcf_file_groups = group_pcf_files_by_unit(input_path)
    all_pcf_files = [path for _, pcf_files in pcf_file_groups for path in pcf_files]
    excluded_duplicate_uci = collect_duplicate_uci_exclusions(all_pcf_files)
    for unit_name, pcf_files in pcf_file_groups:
        pipeline_models = [idf.apply_model_unit_context(parse_pcf(path, excluded_duplicate_uci), unit_name) for path in pcf_files]
        parsed_units.append({"unitName": unit_name, "files": pcf_files, "models": pipeline_models})
        all_pipeline_models.extend(pipeline_models)

    global_model = idf.prepare_project_models(all_pipeline_models, project_name)
    write_pcf_derived_tables(global_model, weld_table_output, material_table_output)

    clear_unit_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "projectName": project_name,
        "sourceType": "pcf-unit-directory",
        "unitCount": len(parsed_units),
        "pipelineCount": sum(len(unit["models"]) for unit in parsed_units),
        "units": [],
    }

    for unit in parsed_units:
        unit_name = unit["unitName"]
        unit_dir = output_dir / idf.safe_output_name(unit_name)
        pipeline_dir = unit_dir / "pipelines"
        unit_model = idf.merge_models(unit["models"], f"{project_name} {unit_name}")
        unit_model["unitName"] = unit_name
        unit_model["sourceType"] = "pcf"
        idf.sync_weld_fields_from_global_model(unit_model, global_model)
        unit_model_path = unit_dir / "unit.json"
        idf.write_json(unit_model_path, unit_model)
        unit_entry = {
            "unitName": unit_name,
            "modelUrl": idf.public_url_for_path(unit_model_path),
            "pipelineCount": len(unit["models"]),
            "componentCount": len(unit_model.get("components", [])),
            "weldCount": sum(1 for component in unit_model.get("components", []) if component.get("type") == "weld"),
            "pipelines": [],
        }
        for index, model in enumerate(unit["models"], start=1):
            pipeline_file = pipeline_dir / f"{index:04d}-{idf.safe_output_name(model.get('pipelineName') or model.get('fileName'))}.json"
            model["sourceType"] = "pcf"
            idf.sync_weld_fields_from_global_model(model, global_model)
            idf.write_json(pipeline_file, model)
            unit_entry["pipelines"].append({
                "pipelineId": idf.get_model_pipeline_id(model, ""),
                "unitName": unit_name,
                "pipelineName": model.get("pipelineName", ""),
                "fileName": model.get("fileName", ""),
                "modelUrl": idf.public_url_for_path(pipeline_file),
                "componentCount": len(model.get("components", [])),
                "weldCount": sum(1 for component in model.get("components", []) if component.get("type") == "weld"),
            })
        manifest["units"].append(unit_entry)

    idf.write_json(manifest_output, manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse PCF files to viewer JSON.")
    parser.add_argument("--input", help="Input .pcf file path")
    parser.add_argument("--input-dir", help="Input directory containing .pcf files")
    parser.add_argument("--output", help="Output JSON file path for single file or merged output")
    parser.add_argument("--project-name", default="PCF Batch Model", help="Project name")
    parser.add_argument("--unit-output-dir", help="Output directory for unit and pipeline JSON files")
    parser.add_argument("--manifest-output", help="Output manifest JSON path for unit mode")
    parser.add_argument("--weld-table-output", help="Output weld CSV path")
    parser.add_argument("--material-table-output", help="Output material CSV path")
    parser.add_argument("--no-derived-tables", action="store_true", help="Do not write weld/material CSV tables")
    parser.add_argument("--cs-split-length", type=idf.positive_float, default=idf.DEFAULT_PIPE_SPLIT_LENGTH_BY_RULE_M["CS"])
    parser.add_argument("--ss-split-length", type=idf.positive_float, default=idf.DEFAULT_PIPE_SPLIT_LENGTH_BY_RULE_M["SS"])
    parser.add_argument("--pipe-split-min-remainder", type=idf.non_negative_float, default=idf.DEFAULT_PIPE_SPLIT_MIN_REMAINDER_M)
    parser.add_argument("--weld-prefix-bw", default="")
    parser.add_argument("--weld-prefix-sw", default="")
    parser.add_argument("--weld-prefix-scw", default="")
    parser.add_argument("--weld-prefix-olet", default="")
    parser.add_argument("--weld-prefix-seton", default="")
    parser.add_argument("--shop-weld-suffix", default="S")
    parser.add_argument("--field-weld-suffix", default="F")
    parser.add_argument("--weld-type-number-mode", choices=[idf.WELD_TYPE_NUMBER_MODE_ALL, idf.WELD_TYPE_NUMBER_MODE_CONFIGURED, idf.WELD_TYPE_NUMBER_MODE_NONE], default=idf.WELD_TYPE_NUMBER_MODE_CONFIGURED)
    parser.add_argument("--pipe-split-number-mode", choices=[idf.PIPE_SPLIT_NUMBER_MODE_PIPE_END, idf.PIPE_SPLIT_NUMBER_MODE_SEQUENCE], default=idf.PIPE_SPLIT_NUMBER_MODE_PIPE_END)
    args = parser.parse_args()

    if not args.input_dir and not args.input:
        raise SystemExit("--input or --input-dir is required")
    idf.set_current_parse_options(idf.build_parser_options(args))
    input_path = Path(args.input_dir or args.input)
    if not find_pcf_files(input_path):
        raise SystemExit(f"No .pcf files found in {input_path}")

    output_path = Path(args.output) if args.output else None
    weld_table_output = Path(args.weld_table_output) if args.weld_table_output else (
        output_path.with_name(f"{output_path.stem}-weld-table.csv") if output_path else None
    )
    material_table_output = Path(args.material_table_output) if args.material_table_output else (
        output_path.with_name(f"{output_path.stem}-material-table.csv") if output_path else None
    )

    if args.unit_output_dir:
        unit_output_dir = Path(args.unit_output_dir)
        manifest_output = Path(args.manifest_output) if args.manifest_output else unit_output_dir.with_name(f"{unit_output_dir.name}-manifest.json")
        manifest = write_unit_outputs(
            input_path,
            unit_output_dir,
            manifest_output,
            args.project_name,
            None if args.no_derived_tables else weld_table_output,
            None if args.no_derived_tables else material_table_output,
        )
        print(f"Wrote {manifest_output} with {manifest['unitCount']} units, {manifest['pipelineCount']} pipelines")
        if output_path is None:
            return

    files = find_pcf_files(input_path)
    excluded_duplicate_uci = collect_duplicate_uci_exclusions(files)
    models = [
        idf.apply_model_unit_context(parse_pcf(path, excluded_duplicate_uci), unit_name)
        for unit_name, unit_files in group_pcf_files_by_unit(input_path)
        for path in unit_files
    ]
    if input_path.is_file() and output_path:
        model = models[0]
        idf.assign_material_unique_codes(model)
    else:
        model = idf.prepare_project_models(models, args.project_name)
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {output_path} with {len(model.get('components', []))} components")
        if not args.no_derived_tables:
            write_pcf_derived_tables(model, weld_table_output, material_table_output)


if __name__ == "__main__":
    main()
