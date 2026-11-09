
from paths import HOME, CODE
import argparse
import copy
import csv
import json
import math
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path


IDF_NUMBER_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")
IGNORED_IDENTIFIERS = {0, 3, 148, 149, 150, 151, 152, 153, 160, 161, 200, 201, 300, 301, 501, 502, 999}
NO_MATERIAL_FLAGS = {"1000000", "1100000", "1200000"}
PIPE_OPENING_WELD_SKEYS = {"TESO", "TERF", "TSSO", "TSRF"}
EXPLICIT_SKEY_WELD_TYPES = {
    "FLWN": "bw",
}
SLIP_ON_FLANGE_SKEYS = {"FLSO", "FOSO", "JFSO", "FLSJ"}
IGNORE_DECODE_SEQUENCES = {"OS&Y"}
MATERIAL_TABLE_EXCLUDED_TYPES = {"weld", "equipment", "olet-marker"}
TOPOLOGY_BRANCH_COMPONENT_TYPES = {"olet", "branch", "teed-reducer", "teed-elbow"}
FITTING_CENTER_CORRECTION_TYPES = {"elbow", "branch", "teed-reducer", "teed-elbow"}
TOPOLOGY_FLANGED_INLINE_COMPONENT_TYPES = {
    "flange",
    "gasket",
    "valve",
    "angle-valve",
    "three-way-valve",
    "four-way-valve",
    "instrument",
    "misc-component",
    "trap",
    "filter",
}
IDF_UNITS_PER_MM = 100
IDF_UNITS_PER_OFFSET_METER = 100000
GLOBAL_CONNECTION_TOLERANCE_UNITS = 20
SPATIAL_INDEX_CELL_SIZE = 100000
PIPE_SPLIT_RULES_PATH = HOME/"material_split_rules.json"
DEFAULT_PIPE_SPLIT_LENGTH_BY_RULE_M = {
    "CS": 12.0,
    "SS": 6.0,
}
DEFAULT_PIPE_SPLIT_MIN_REMAINDER_M = 0.2
PIPE_SPLIT_NUMBER_MODE_PIPE_END = "pipe-end"
PIPE_SPLIT_NUMBER_MODE_SEQUENCE = "sequence"
WELD_TYPE_NUMBER_MODE_ALL = "all"
WELD_TYPE_NUMBER_MODE_CONFIGURED = "configured"
WELD_TYPE_NUMBER_MODE_NONE = "none"
NUMBERED_WELD_TYPES = {"bw", "sw", "scw", "olet", "seton"}
END_CONDITION_WELD_TYPES = {
    "BW": "bw",
    "SW": "sw",
    "SC": "scw",
}
MATERIAL_DESCRIPTION_WELD_TYPE_TOKENS = {
    "BW": "bw",
    "BE": "bw",
    "PE": "bw",
    "BUTTWELD": "bw",
    "BUTT-WELD": "bw",
    "SW": "sw",
    "SOCKETWELD": "sw",
    "SOCKET-WELD": "sw",
    "SC": "scw",
    "SCRD": "scw",
    "THD": "scw",
    "THRD": "scw",
    "MTE": "scw",
    "NPT": "scw",
    "FNPT": "scw",
    "MNPT": "scw",
    "SO": "so",
}
MATERIAL_DESCRIPTION_WELD_TYPE_PHRASES = {
    ("BUTT", "WELD"): "bw",
    ("SOCKET", "WELD"): "sw",
    ("SCREWED",): "scw",
    ("THREADED",): "scw",
    ("THREAD",): "scw",
    ("SLIP", "ON"): "so",
}
MATERIAL_DESCRIPTION_WELD_TYPE_SEPARATORS_RE = re.compile(r"[\s;,，；、/\-()（）]+")


@dataclass
class ParserOptions:
    pipe_split_lengths_m: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_PIPE_SPLIT_LENGTH_BY_RULE_M))
    pipe_split_min_remainder_m: float = DEFAULT_PIPE_SPLIT_MIN_REMAINDER_M
    weld_prefix_by_type: dict[str, str] = field(default_factory=dict)
    shop_weld_suffix: str = "S"
    field_weld_suffix: str = "F"
    weld_type_number_mode: str = WELD_TYPE_NUMBER_MODE_CONFIGURED
    pipe_split_number_mode: str = PIPE_SPLIT_NUMBER_MODE_PIPE_END


CURRENT_PARSE_OPTIONS = ParserOptions()
PIPE_OUTER_DIAMETER_BY_DN = {
    6: 10.3,
    8: 13.7,
    10: 17.1,
    15: 21.34,
    20: 26.67,
    25: 33.4,
    32: 42.2,
    40: 48.26,
    50: 60.33,
    65: 73.0,
    80: 88.9,
    90: 101.6,
    100: 114.3,
    125: 141.3,
    150: 168.3,
    200: 219.1,
    250: 273.0,
    300: 323.8,
    350: 355.6,
    400: 406.4,
    450: 457.0,
    500: 508.0,
    550: 559.0,
    600: 610.0,
    650: 660.0,
    700: 711.0,
    750: 762.0,
    800: 813.0,
    850: 864.0,
    900: 914.0,
    950: 965.0,
    1000: 1016.0,
    1050: 1066.8,
    1100: 1117.6,
    1150: 1168.4,
    1200: 1219.2,
    1300: 1320.8,
    1400: 1422.4,
    1500: 1524.0,
    1600: 1625.6,
    1700: 1727.2,
    1800: 1828.8,
    1900: 1930.4,
    2000: 2032.0,
}
PIPE_NPS_BY_DN = {
    6: 0.125,
    8: 0.25,
    10: 0.375,
    15: 0.5,
    20: 0.75,
    25: 1,
    32: 1.25,
    40: 1.5,
    50: 2,
    65: 2.5,
    80: 3,
    90: 3.5,
    100: 4,
    125: 5,
    150: 6,
    200: 8,
    250: 10,
    300: 12,
    350: 14,
    400: 16,
    450: 18,
    500: 20,
    550: 22,
    600: 24,
    650: 26,
    700: 28,
    750: 30,
    800: 32,
    850: 34,
    900: 36,
    950: 38,
    1000: 40,
    1050: 42,
    1100: 44,
    1150: 46,
    1200: 48,
    1300: 52,
    1400: 56,
    1500: 60,
    1600: 64,
    1700: 68,
    1800: 72,
    1900: 76,
    2000: 80,
}
PIPE_DN_BY_NPS = {
    nps: dn
    for dn, nps in PIPE_NPS_BY_DN.items()
}
METRIC_BOLT_DIAMETERS_MM = [
    3, 4, 5, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 27, 30, 33, 36,
    39, 42, 45, 48, 52, 56, 60, 64, 68, 72, 76, 80, 85, 90, 95, 100,
]
OPTION_SWITCH_DEFINITIONS = {
    7: "pipelineSplittingUserControl",
    10: "drawingMarginLeft",
    11: "drawingMarginRight",
    12: "drawingMarginTop",
    13: "drawingMarginBottom",
    14: "drawingSizeStandardPostscript",
    15: "drawingSizeHeight",
    16: "drawingSizeWidth",
    31: "drawingOutputFileScreen",
    32: "drawingOutputScaling",
    34: "drawingOutputPictureScale",
    35: "drawingReservedAreas",
    38: "pipelineSplittingAutomatic",
    41: "boreDimensionWeightControl",
    42: "drawingViewpoint",
    52: "equipmentTrimDrawings",
    65: "boltingUnits",
    108: "pipelineSplittingInPipe",
}
NORTH_ARROW_BY_VIEWPOINT = {
    0: "top-left",
    1: "bottom-right",
    2: "top-right",
    3: "top-left",
    4: "bottom-left",
    5: "bottom-right-boxed",
    6: "top-right-boxed",
    7: "top-left-boxed",
    8: "bottom-left-boxed",
}


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def format_number_label(value) -> str:
    number = safe_float(value)
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f"{number:.6f}".rstrip("0").rstrip(".")


def nearest_metric_bolt_diameter(mm_value: float) -> int:
    if mm_value <= 0:
        return 0
    return min(METRIC_BOLT_DIAMETERS_MM, key=lambda item: abs(item - mm_value))


def is_inch_unit(unit: str) -> bool:
    return str(unit or "").strip().upper() in {"IN", "INCH", "INCHES", "INS"}


def bolt_diameter_to_metric_label(value, unit: str = "") -> str:
    diameter = safe_float(value)
    if diameter <= 0:
        return ""
    unit_upper = str(unit or "").strip().upper()
    if is_inch_unit(unit_upper) or (not unit_upper and diameter < 3):
        return f"M{nearest_metric_bolt_diameter(diameter * 25.4)}"
    return f"M{nearest_metric_bolt_diameter(diameter)}"


def bolt_length_to_mm_label(value, unit: str = "") -> str:
    length = safe_float(value)
    if length <= 0:
        return ""
    if is_inch_unit(unit):
        length *= 25.4
    return format_number_label(round(length, 3))


def format_bolt_spec(diameter, length, diameter_unit: str = "", length_unit: str = "") -> str:
    diameter_label = bolt_diameter_to_metric_label(diameter, diameter_unit)
    length_label = bolt_length_to_mm_label(length, length_unit)
    if diameter_label and length_label:
        return f"{diameter_label}x{length_label}"
    if diameter_label:
        return diameter_label
    return ""


def bolt_diameter_unit_from_idf_bore_units(bore_units: dict) -> str:
    bore_input = str((bore_units or {}).get("boreInput") or "").lower()
    if bore_input == "inch-sixteenths":
        return "INCH_SIXTEENTHS"
    if bore_input == "millimeters":
        return "MM"
    return ""


def format_idf_bolt_spec(raw_diameter, length, bore_units: dict | None = None) -> str:
    unit = bolt_diameter_unit_from_idf_bore_units(bore_units or {})
    diameter = safe_float(raw_diameter)
    if unit == "INCH_SIXTEENTHS":
        diameter = diameter / 16.0
        unit = "INCH"
    return format_bolt_spec(diameter, length, unit, "MM")


def decode_gb2312_from_ascii(text: str) -> str:
    result = []
    index = 0
    while index < len(text):
        code = ord(text[index])
        next_code = ord(text[index + 1]) if index + 1 < len(text) else 0
        if 32 <= code <= 126 and 32 <= next_code <= 126:
            byte1 = code + 0x80
            byte2 = next_code + 0x80
            if 0xA1 <= byte1 <= 0xFE and 0xA1 <= byte2 <= 0xFE:
                try:
                    decoded = bytes([byte1, byte2]).decode("gb2312", errors="ignore")
                    if decoded:
                        result.append(decoded)
                        index += 2
                        continue
                except Exception:
                    pass
        result.append(text[index])
        index += 1
    return "".join(result)


def clean_decoded_text(text: str) -> str:
    value = text
    if value.startswith("&~"):
        value = value[2:]
    if value.endswith(" &"):
        value = value[:-2]
    elif value.endswith("&"):
        value = value[:-1]
    return value.strip()


def find_encoded_blocks(text: str) -> list[str]:
    blocks = []
    search_end = len(text)
    while True:
        valid_end = -1
        cursor = search_end
        while True:
            candidate_end = text.rfind("&", 0, cursor)
            if candidate_end < 0:
                break
            ignored = False
            for sequence in IGNORE_DECODE_SEQUENCES:
                try:
                    amp_index = sequence.index("&")
                except ValueError:
                    continue
                start = candidate_end - amp_index
                if start >= 0 and text[start:start + len(sequence)] == sequence:
                    ignored = True
                    break
            if not ignored:
                valid_end = candidate_end
                break
            cursor = candidate_end
        if valid_end < 0:
            break
        start = text.rfind("&~", 0, valid_end)
        if start >= 0:
            blocks.insert(0, text[start:valid_end + 1])
            search_end = start
        else:
            search_end = valid_end
    return blocks


def process_idf_text_value(text: str) -> str:
    if not text:
        return text
    reconstructed = text
    for block in sorted(find_encoded_blocks(text), key=len, reverse=True):
        decoded = clean_decoded_text(decode_gb2312_from_ascii(block))
        if decoded and decoded != block:
            reconstructed = reconstructed.replace(block, decoded)
    return reconstructed.strip()


def read_continuation_text(lines: list[str], start_index: int, marker: str, marker_length: int) -> str:
    line = lines[start_index].lstrip() if start_index < len(lines) else ""
    if not line.startswith(marker):
        return ""
    value = line[marker_length:].strip()
    cursor = start_index + 1
    while cursor < len(lines) and lines[cursor].lstrip().startswith("-1 "):
        value += lines[cursor].lstrip()[3:].strip()
        cursor += 1
    return process_idf_text_value(value)


_MATERIAL_SPLIT_RULES_CACHE: list[dict] | None = None


def load_material_split_rules() -> list[dict]:
    global _MATERIAL_SPLIT_RULES_CACHE
    if _MATERIAL_SPLIT_RULES_CACHE is not None:
        return _MATERIAL_SPLIT_RULES_CACHE
    if not PIPE_SPLIT_RULES_PATH.exists():
        _MATERIAL_SPLIT_RULES_CACHE = []
        return _MATERIAL_SPLIT_RULES_CACHE
    try:
        rules = json.loads(PIPE_SPLIT_RULES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        rules = []
    _MATERIAL_SPLIT_RULES_CACHE = sorted(
        [
            {
                "materialCode": str(item.get("materialCode", "")).strip(),
                "splitRule": str(item.get("splitRule", "")).strip().upper(),
            }
            for item in rules
            if str(item.get("materialCode", "")).strip()
        ],
        key=lambda item: (-len(item["materialCode"]), item["materialCode"]),
    )
    return _MATERIAL_SPLIT_RULES_CACHE


def get_pipe_split_rule(material_code: str) -> str:
    normalized_code = str(material_code or "").upper()
    if not normalized_code:
        return "CS"
    for rule in load_material_split_rules():
        tag = rule["materialCode"].upper()
        if tag and tag in normalized_code:
            return rule["splitRule"] or "N/A"
    return "CS"


def get_model_pipeline_id(model: dict, fallback: str = "") -> str:
    return model.get("pipelineId") or model.get("fileName") or model.get("pipelineName") or fallback


def make_pipeline_id(unit_name: str, model: dict) -> str:
    base = model.get("fileName") or model.get("pipelineName") or "UNKNOWN"
    return f"{unit_name}::{base}" if unit_name else base


def apply_model_unit_context(model: dict, unit_name: str) -> dict:
    unit_name = str(unit_name or "").strip()
    if not unit_name:
        return model
    model["unitName"] = unit_name
    model["pipelineId"] = make_pipeline_id(unit_name, model)
    prefix = f"{unit_name}::"
    old_to_new = {}
    for component in model.get("components", []):
        old_id = component.get("id")
        if old_id:
            old_to_new[old_id] = old_id if str(old_id).startswith(prefix) else f"{prefix}{old_id}"

    def qualify_id(value):
        if not value:
            return value
        text = str(value)
        if text in old_to_new:
            return old_to_new[text]
        return text if text.startswith(prefix) else f"{prefix}{text}"

    for component in model.get("components", []):
        if component.get("id"):
            component["id"] = qualify_id(component["id"])
        component["unitName"] = unit_name
        component["pipelineId"] = model["pipelineId"]
        component["pipelineName"] = model.get("pipelineName", "")
        for list_key in ("connectedComponentIds", "sourceComponentIds"):
            if component.get(list_key):
                component[list_key] = [qualify_id(item) for item in component[list_key]]
        if component.get("sourcePipeComponentId"):
            component["sourcePipeComponentId"] = qualify_id(component["sourcePipeComponentId"])

    for segment in model.get("segments", []):
        if segment.get("id"):
            segment["id"] = qualify_id(segment["id"])
        if segment.get("componentId"):
            segment["componentId"] = qualify_id(segment["componentId"])
        segment["unitName"] = unit_name
        segment["pipelineId"] = model["pipelineId"]

    for node in model.get("nodes", []):
        if node.get("componentIds"):
            node["componentIds"] = [qualify_id(item) for item in node["componentIds"]]

    for component in model.get("symbolComponents", []):
        if component.get("id"):
            component["id"] = qualify_id(component["id"])
        component["unitName"] = unit_name
        component["pipelineId"] = model["pipelineId"]
        component["pipelineName"] = model.get("pipelineName", "")
        if component.get("connectedComponentIds"):
            component["connectedComponentIds"] = [qualify_id(item) for item in component["connectedComponentIds"]]

    for material in model.get("materials", []):
        material["unitName"] = unit_name
        material["pipelineId"] = model["pipelineId"]
    for marker in model.get("drawingSplitMarkers", []):
        marker["unitName"] = unit_name
        marker["pipelineId"] = model["pipelineId"]
        marker["pipelineName"] = model.get("pipelineName", "")
    return model


def get_outer_diameter_mm(dn):
    normalized = round(safe_float(dn))
    if normalized <= 0:
        return 0.0
    if normalized > 2000:
        return float(normalized + 20)
    return PIPE_OUTER_DIAMETER_BY_DN.get(normalized, float(normalized))


def option_switch_first_position(value) -> int:
    try:
        switch_value = abs(int(value))
    except (TypeError, ValueError):
        return 0
    if switch_value == 0:
        return 0
    return int(str(switch_value)[0])


def bore_unit_mode_from_options(drawing_options: dict) -> dict:
    os41 = drawing_options.get("named", {}).get("boreDimensionWeightControl", 0)
    unit_type = option_switch_first_position(os41)
    if unit_type in {0, 1}:
        return {
            "optionSwitch41": os41,
            "dimensionUnitsType": unit_type,
            "boreInput": "inch-sixteenths",
            "specOutput": "DN",
        }
    if unit_type in {2, 3}:
        return {
            "optionSwitch41": os41,
            "dimensionUnitsType": unit_type,
            "boreInput": "millimeters",
            "specOutput": "DN",
        }
    return {
        "optionSwitch41": os41,
        "dimensionUnitsType": unit_type,
        "boreInput": "unknown",
        "specOutput": "raw",
    }


def nps_to_dn(nps: float) -> float:
    if nps <= 0:
        return 0.0
    rounded_nps = round(nps, 6)
    if rounded_nps in PIPE_DN_BY_NPS:
        return float(PIPE_DN_BY_NPS[rounded_nps])
    nearest_nps, nearest_dn = min(
        PIPE_DN_BY_NPS.items(),
        key=lambda item: abs(item[0] - rounded_nps),
    )
    if abs(nearest_nps - rounded_nps) < 1e-6:
        return float(nearest_dn)
    return round(nps * 25.4, 3)


def normalize_idf_bore_spec(raw_spec: float, bore_units: dict) -> float:
    raw_value = safe_float(raw_spec)
    if raw_value <= 0:
        return 0.0
    if bore_units.get("boreInput") == "inch-sixteenths":
        return nps_to_dn(raw_value / 16.0)
    return raw_value


def read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk", "cp936", "latin1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(errors="ignore")


def parse_idf_component_line(line: str) -> list[str]:
    segments = line.split(",")
    if len(segments) < 5:
        parts = line.split()
        if len(parts) >= 14:
            return parts[:14]
        raise ValueError(f"Unable to parse IDF component line: {line}")

    tail_parts = segments[-1].split()
    if len(tail_parts) != 2:
        raise ValueError(f"Unable to parse columns 13/14: {line}")

    col12 = segments[-2].strip()
    col11 = segments[-3].strip()
    col10 = segments[-4].strip()
    left_with_col9 = ",".join(segments[:-4]).rstrip(" ,")
    col9_match = re.search(r"(\S+)\s*$", left_with_col9)
    if not col9_match:
        raise ValueError(f"Unable to parse column 9: {line}")

    col9 = col9_match.group(1).strip().strip(",")
    left = left_with_col9[: col9_match.start()].rstrip()
    first_numbers = IDF_NUMBER_RE.findall(left)
    if len(first_numbers) < 7:
        raise ValueError(f"Unable to parse leading columns: {line}")

    first8 = first_numbers[:8] if len(first_numbers) >= 8 else first_numbers[:7] + [""]
    return first8 + [col9, col10, col11, col12] + tail_parts


def parse_pipeline_name(lines: list[str]) -> str:
    for index, line in enumerate(lines):
        line_lstrip = line.lstrip()
        if not line_lstrip.startswith("-6 "):
            continue
        name = read_continuation_text(lines, index, "-6 ", 3)
        return name or "UNKNOWN"
    return "UNKNOWN"


def parse_materials(lines: list[str]) -> list[dict]:
    materials = []
    index = 0
    while index < len(lines):
        line = lines[index].lstrip()
        if not line.startswith("-20 "):
            index += 1
            continue

        code = line[4:].strip()
        index += 1
        while index < len(lines) and lines[index].lstrip().startswith("-1 "):
            code += lines[index].lstrip()[3:].strip()
            index += 1

        description = ""
        if index < len(lines) and lines[index].lstrip().startswith("-21 "):
            description = lines[index].lstrip()[4:].strip()
            index += 1
            while index < len(lines) and lines[index].lstrip().startswith("-1 "):
                description += lines[index].lstrip()[3:].strip()
                index += 1

        if code:
            materials.append({
                "index": len(materials) + 1,
                "code": process_idf_text_value(code),
                "description": process_idf_text_value(description or code),
            })
    return materials


def parse_skey(parts: list[str]) -> str:
    for part in parts[9:14]:
        for candidate in str(part or "").split(","):
            value = candidate.strip()
            if re.match(r"^[A-Z]{2,4}$", value):
                return value
    return ""


def is_gasket_material(material: dict) -> bool:
    code = material.get("code", "")
    description = material.get("description", "")
    return code.upper().startswith("PGG") or "垫片" in description or "gasket" in description.lower()


def is_cap_material(material: dict) -> bool:
    code = material.get("code", "")
    description = material.get("description", "")
    return code.upper().startswith("CAP") or "管帽" in description or "cap" in description.lower()


def classify_component(identifier: int, skey: str, material: dict) -> str:
    if is_gasket_material(material):
        return "gasket"
    if identifier == 125 or is_cap_material(material):
        return "cap"
    if identifier == 41:
        return "olet"
    if identifier in {40, 42}:
        return "olet-marker"
    if identifier in {60, 61, 62}:
        return "teed-reducer"
    if identifier in {70, 71, 72}:
        return "teed-elbow"
    if identifier in {75, 76}:
        return "angle-valve"
    if identifier in {80, 81, 82}:
        return "three-way-valve"
    if identifier in {85, 86, 87, 88}:
        return "four-way-valve"
    if identifier in {90, 91, 92, 93}:
        return "instrument"
    if identifier in {95, 96}:
        return "misc-component"
    if identifier in {132, 133}:
        return "trap"
    if identifier in {136, 137}:
        return "filter"
    if identifier == 100:
        return "pipe"
    if identifier in {35, 36}:
        return "elbow"
    if 40 <= identifier <= 47:
        return "branch"
    if identifier == 55:
        return "reducer"
    if 90 <= identifier <= 99:
        return "equipment"
    if identifier == 105 or skey.startswith("FL"):
        return "flange"
    if identifier == 110:
        return "gasket"
    if identifier == 115:
        return "bolt"
    if identifier == 120:
        return "weld"
    if skey.startswith("V"):
        return "valve"
    return "component"


def get_reducer_kind(skey: str) -> str:
    if skey.upper().startswith("RE"):
        return "eccentric"
    if skey.upper().startswith("RC"):
        return "concentric"
    return ""


def is_zero_point(point: list[float]) -> bool:
    return all(abs(value) < 1e-6 for value in point)


def normalize_point_key(point: list[float]) -> str:
    return ",".join(f"{value:.3f}" for value in point)


def normalize_global_connection_key(point: list[float]) -> str:
    return ",".join(
        f"{round(value / GLOBAL_CONNECTION_TOLERANCE_UNITS) * GLOBAL_CONNECTION_TOLERANCE_UNITS:.3f}"
        for value in point
    )


def apply_coordinate_offset(point: list[float], offset: list[float]) -> list[float]:
    if is_zero_point(point):
        return point
    return [round(point[index] + offset[index], 3) for index in range(3)]


def parse_option_switch_row(raw_line: str) -> list[int]:
    token_values = [int(token) for token in re.findall(r"-?\d+", raw_line.strip())]
    fixed_candidates = []
    for width in (8, 10):
        if len(raw_line) % width != 0:
            continue
        values = []
        valid = True
        for index in range(len(raw_line) // width):
            chunk = raw_line[index * width:(index + 1) * width].strip()
            if not re.fullmatch(r"[+-]?\d+", chunk):
                valid = False
                break
            values.append(int(chunk))
        if valid:
            fixed_candidates.append(values)
    for values in fixed_candidates:
        if len(values) == len(token_values):
            return values
    for values in fixed_candidates:
        if len(values) > len(token_values) and len(values) <= 16 and len(token_values) < 14:
            return values
    return token_values


def parse_option_switches(lines: list[str]) -> dict:
    values = []
    raw_rows = []
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("-"):
            break
        row_values = parse_option_switch_row(raw_line)
        if not row_values:
            break
        raw_rows.append(row_values)
        values.extend(row_values)
        if len(values) >= 140:
            break
    values = values[:140]
    switches = {str(index + 1): value for index, value in enumerate(values)}
    positions = {
        str(index + 1): list(str(abs(value)))
        for index, value in enumerate(values)
    }
    named = {
        name: switches.get(str(number), 0)
        for number, name in OPTION_SWITCH_DEFINITIONS.items()
    }
    drawing_size = {
        "standardPostscript": named.get("drawingSizeStandardPostscript", 0),
        "heightMm": named.get("drawingSizeHeight", 0),
        "widthMm": named.get("drawingSizeWidth", 0),
        "marginsMm": {
            "left": named.get("drawingMarginLeft", 0),
            "right": named.get("drawingMarginRight", 0),
            "top": named.get("drawingMarginTop", 0),
            "bottom": named.get("drawingMarginBottom", 0),
        },
        "outputScalePercent": named.get("drawingOutputScaling", 0) or 100,
        "pictureScalePercent": named.get("drawingOutputPictureScale", 0) or 100,
        "reservedAreas": named.get("drawingReservedAreas", 0),
    }
    viewpoint = int(named.get("drawingViewpoint", 0) or 0)
    result = {
        "count": len(values),
        "switches": switches,
        "positions": positions,
        "named": named,
        "drawingSize": drawing_size,
        "pipelineSplitting": {
            "userControl": named.get("pipelineSplittingUserControl", 0),
            "automatic": named.get("pipelineSplittingAutomatic", 0),
            "inPipe": named.get("pipelineSplittingInPipe", 0),
        },
        "viewpoint": {
            "option": viewpoint,
            "northArrow": NORTH_ARROW_BY_VIEWPOINT.get(viewpoint, "unknown"),
        },
        "rawRows": raw_rows,
    }
    result["boreUnits"] = bore_unit_mode_from_options(result)
    return result


def parse_idf(path: Path) -> dict:
    text = read_text(path)
    lines = text.splitlines()
    drawing_options = parse_option_switches(lines)
    bore_units = drawing_options.get("boreUnits", {})
    pipeline_name = parse_pipeline_name(lines)
    materials = parse_materials(lines)
    material_by_index = {item["index"]: item for item in materials}
    components = []
    drawing_split_markers = []
    in_geometry = False
    coordinate_offset = [0.0, 0.0, 0.0]

    for index, raw_line in enumerate(lines):
        line = raw_line.lstrip()
        if in_geometry and line.startswith("-20 "):
            in_geometry = False
        elif line.startswith("-6 "):
            in_geometry = True
        if not in_geometry:
            continue

        if re.match(r"^-38(?:\s|$)", line):
            drawing_split_markers.append({
                "id": f"{path.name}:split:{index + 1}",
                "lineNumber": index + 1,
                "raw": raw_line.rstrip(),
            })
            continue

        try:
            parts = parse_idf_component_line(line)
            identifier = int(parts[0])
        except (ValueError, IndexError):
            continue
        if identifier < 0:
            continue

        raw_start = [round(safe_float(value), 3) for value in parts[1:4]]
        raw_end = [round(safe_float(value), 3) for value in parts[4:7]]
        if identifier == 300:
            offset_values = raw_start if not is_zero_point(raw_start) else raw_end
            coordinate_offset = [
                round(value * IDF_UNITS_PER_OFFSET_METER, 3)
                for value in offset_values
            ]
            continue
        start = apply_coordinate_offset(raw_start, coordinate_offset)
        end = apply_coordinate_offset(raw_end, coordinate_offset)
        has_geometry = not is_zero_point(raw_start) or not is_zero_point(raw_end)
        raw_spec = safe_float(parts[7])
        spec = normalize_idf_bore_spec(raw_spec, bore_units)
        material_index = int(str(parts[8]).split(",")[0] or 0)
        skey = parse_skey(parts)
        if identifier in IGNORED_IDENTIFIERS:
            continue
        material = material_by_index.get(material_index, {})
        zeroline_spec = find_following_zeroline_spec(lines, index, bore_units)
        instrument_tag = find_following_instrument_tag(lines, index) if identifier == 90 else ""
        no_material_flag = str(parts[10]).strip() in NO_MATERIAL_FLAGS if len(parts) > 10 else False
        pipe_opening_weld_no_material = identifier in {45, 46, 47} and skey in PIPE_OPENING_WELD_SKEYS
        bolt_quantity = safe_float(str(parts[10]).strip(",")) if identifier == 115 and len(parts) > 10 else None
        bolt_length = safe_float(str(parts[12]).strip(",")) if identifier == 115 and len(parts) > 12 else None
        bolt_spec_label = format_idf_bolt_spec(raw_spec, bolt_length, bore_units) if identifier == 115 else ""

        component = {
            "id": f"{path.name}:{index + 1}",
            "lineNumber": index + 1,
            "identifier": identifier,
            "type": classify_component(identifier, skey, material),
            "start": start if has_geometry else None,
            "end": end if has_geometry and not is_zero_point(end) else None,
            "rawSpec": raw_spec,
            "boreInputUnits": bore_units.get("boreInput", "unknown"),
            "spec": spec,
            "outerDiameterMm": get_outer_diameter_mm(spec),
            "materialIndex": material_index,
            "materialCode": material.get("code", ""),
            "materialDescription": material.get("description", ""),
            "skey": skey,
            "reducerKind": get_reducer_kind(skey) if identifier == 55 else "",
            "zerolineSpec": zeroline_spec,
            "instrumentTag": instrument_tag,
            "noMaterialFlag": no_material_flag,
            "pipeOpeningWeldNoMaterial": pipe_opening_weld_no_material,
            "raw": raw_line.rstrip(),
        }
        if identifier == 115:
            component["boltQuantity"] = bolt_quantity
            component["boltLength"] = bolt_length
            component["materialSpecLabel"] = bolt_spec_label
        component["quantity"] = calculate_component_quantity(component)
        if component["type"] == "weld":
            component["weldType"] = infer_component_weld_type(component)
        components.append(component)

    return build_viewer_model({
        "pipelineName": pipeline_name,
        "fileName": path.name,
        "materials": materials,
        "components": normalize_components(components, drawing_split_markers),
        "drawingOptions": drawing_options,
        "drawingSplitMarkers": drawing_split_markers,
    })


def find_following_instrument_tag(lines: list[str], start_index: int) -> str:
    cursor = start_index + 1
    while cursor < len(lines):
        line = lines[cursor].lstrip()
        if not line:
            cursor += 1
            continue
        if line.startswith("-22 "):
            return read_continuation_text(lines, cursor, "-22 ", 4)
        first_token = line.split()[0]
        if first_token.startswith("-"):
            cursor += 1
            continue
        if re.match(r"^\d+\s", line):
            return ""
        cursor += 1
    return ""


def find_following_zeroline_spec(lines: list[str], start_index: int, bore_units: dict | None = None) -> float:
    cursor = start_index + 1
    while cursor < len(lines):
        line = lines[cursor].lstrip()
        if not line:
            cursor += 1
            continue
        first_token = line.split()[0]
        if first_token.startswith("-"):
            cursor += 1
            continue
        try:
            parts = parse_idf_component_line(line)
        except (ValueError, IndexError):
            return 0.0
        if first_token == "0" and len(parts) >= 9 and parts[8] == "0":
            raw_spec = safe_float(parts[7])
            spec = normalize_idf_bore_spec(raw_spec, bore_units or {})
            return spec if spec > 0 else 0.0
        return 0.0
    return 0.0


def same_point(a, b) -> bool:
    return a is not None and b is not None and all(abs(a[index] - b[index]) < 1e-3 for index in range(3))


def is_zero_length_segment(segment: dict) -> bool:
    return bool(segment.get("start") and segment.get("end") and same_point(segment.get("start"), segment.get("end")))


def get_external_segment_refs_from_segments(component_segments: list[dict], key_func) -> list[dict]:
    if not component_segments:
        return []
    if len(component_segments) == 1:
        segment = component_segments[0]
        return [
            {"point": segment.get("start"), "role": "start", "segment": segment},
            {"point": segment.get("end"), "role": "end", "segment": segment},
        ]

    non_zero_point_counts = {}
    zero_length_keys = set()
    for segment in component_segments:
        if is_zero_length_segment(segment):
            zero_length_keys.add(key_func(segment["start"]))
            continue
        for point in (segment.get("start"), segment.get("end")):
            if point:
                key = key_func(point)
                non_zero_point_counts[key] = non_zero_point_counts.get(key, 0) + 1

    refs = []
    seen_keys = set()
    for segment in component_segments:
        for role in ("start", "end"):
            point = segment.get(role)
            if not point:
                continue
            key = key_func(point)
            if key in seen_keys:
                continue
            non_zero_count = non_zero_point_counts.get(key, 0)
            is_external = non_zero_count == 1 or (key in zero_length_keys and non_zero_count <= 1)
            if is_external:
                refs.append({"point": point, "role": role, "segment": segment})
                seen_keys.add(key)
    return refs


def point_distance(a, b) -> float:
    if not a or not b:
        return 0.0
    return sum((a[index] - b[index]) ** 2 for index in range(3)) ** 0.5


def vector_between(start: list[float], end: list[float]) -> list[float]:
    return [end[index] - start[index] for index in range(3)]


def vector_dot(first: list[float], second: list[float]) -> float:
    return sum(first[index] * second[index] for index in range(3))


def vector_length(vector: list[float]) -> float:
    return sum(value * value for value in vector) ** 0.5


def point_along_axis(origin: list[float], axis: list[float], distance: float) -> list[float]:
    return [round(origin[index] + axis[index] * distance, 3) for index in range(3)]


def project_point_on_axis(point: list[float], origin: list[float], axis: list[float]) -> float:
    return vector_dot(vector_between(origin, point), axis)


def pipe_direction(component: dict) -> list[float] | None:
    start = component.get("start")
    end = component.get("end")
    if not start or not end:
        return None
    direction = vector_between(start, end)
    length = vector_length(direction)
    if length < 1e-6:
        return None
    return [value / length for value in direction]


def pipes_are_parallel(first: dict, second: dict) -> bool:
    first_direction = pipe_direction(first)
    second_direction = pipe_direction(second)
    if not first_direction or not second_direction:
        return False
    return abs(vector_dot(first_direction, second_direction)) >= 0.9999


def calculate_component_quantity(component: dict) -> float:
    if component.get("type") == "olet-marker":
        return 0
    if component.get("pipeOpeningWeldNoMaterial"):
        return 0
    if component.get("identifier") == 115 and component.get("boltQuantity") is not None:
        return safe_float(component.get("boltQuantity")) or 1
    if component.get("identifier") == 100 and component.get("start") and component.get("end"):
        return round(point_distance(component["start"], component["end"]) / 100000, 3)
    return 1


def direction_key(component: dict) -> str:
    start = component.get("start")
    end = component.get("end")
    if not start or not end:
        return ""
    vector = [end[index] - start[index] for index in range(3)]
    length = sum(value * value for value in vector) ** 0.5
    if length < 1e-6:
        return ""
    return ",".join(f"{round(value / length, 6):.6f}" for value in vector)


def same_pipe_run(previous: dict, current: dict) -> bool:
    return (
        previous.get("type") == "pipe"
        and current.get("type") == "pipe"
        and previous.get("spec") == current.get("spec")
        and same_point(previous.get("end"), current.get("start"))
        and direction_key(previous) == direction_key(current)
    )


def merge_components(sequence: list[dict], component_type: str, id_suffix: str) -> dict:
    first = sequence[0]
    last = sequence[-1]
    identifiers = []
    line_numbers = []
    source_component_ids = []
    raw_lines = []
    for component in sequence:
        identifiers.extend(component.get("identifiers") or [component["identifier"]])
        line_numbers.extend(component.get("lineNumbers") or [component["lineNumber"]])
        source_component_ids.extend(component.get("sourceComponentIds") or [component["id"]])
        raw_lines.extend(component.get("rawLines") or [component["raw"]])
    if component_type == "pipe":
        segments = [{
            "start": first.get("start"),
            "end": last.get("end"),
            "identifier": first["identifier"],
            "lineNumber": line_numbers[0],
            "lineNumbers": line_numbers,
            "type": component_type,
            "spec": first.get("spec", 0),
            "outerDiameterMm": first.get("outerDiameterMm", 0),
        }]
    else:
        segments = [
            {
                "start": component["start"],
                "end": component["end"],
                "identifier": component["identifier"],
                "lineNumber": component.get("lineNumber", 0),
                "lineNumbers": component.get("lineNumbers") or [component.get("lineNumber", 0)],
                "type": component_type,
                "spec": component.get("spec", 0),
                "outerDiameterMm": component.get("outerDiameterMm", 0),
            }
            for component in sequence
            if component.get("start") and component.get("end")
        ]
    merged = dict(first)
    if component_type == "pipe":
        material_source = max(
            sequence,
            key=lambda component: point_distance(component.get("start"), component.get("end"))
            if component.get("start") and component.get("end") else 0,
        )
        for key in ("materialIndex", "materialCode", "materialDescription", "unit", "skey"):
            if key in material_source:
                merged[key] = material_source[key]
    no_material_flag = any(component.get("noMaterialFlag") for component in sequence)
    pipe_opening_weld_no_material = any(component.get("pipeOpeningWeldNoMaterial") for component in sequence)
    merged.update({
        "id": f"{first['id']}-{id_suffix}-{last['lineNumber']}",
        "lineNumber": line_numbers[0],
        "lineNumbers": line_numbers,
        "identifier": identifiers[0],
        "identifiers": identifiers,
        "type": component_type,
        "start": first.get("start"),
        "end": last.get("end"),
        "sourceComponentIds": source_component_ids,
        "rawLines": raw_lines,
        "segments": segments,
        "noMaterialFlag": no_material_flag,
        "pipeOpeningWeldNoMaterial": pipe_opening_weld_no_material,
    })
    merged["quantity"] = calculate_component_quantity(merged)
    return merged


def merge_olet_components(sequence: list[dict]) -> dict | None:
    branch = next((
        component for component in sequence
        if component.get("identifier") == 41 and component.get("start") and component.get("end") and not same_point(component.get("start"), component.get("end"))
    ), None)
    if not branch:
        return None
    markers = [component for component in sequence if component.get("identifier") in {40, 42}]
    identifiers = [component["identifier"] for component in sequence]
    line_numbers = sorted(component["lineNumber"] for component in sequence)
    main_spec = next((component.get("spec", 0) for component in markers if component.get("spec", 0) > 0), 0)
    branch_spec = branch.get("spec", 0)
    merged = dict(branch)
    merged.update({
        "id": f"{branch['id']}-olet-{line_numbers[-1]}",
        "lineNumber": branch["lineNumber"],
        "lineNumbers": line_numbers,
        "identifier": 41,
        "identifiers": identifiers,
        "type": "olet",
        "start": branch.get("start"),
        "end": branch.get("end"),
        "sourceComponentIds": [component["id"] for component in sequence],
        "rawLines": [component["raw"] for component in sequence],
        "segments": [{
            "start": branch.get("start"),
            "end": branch.get("end"),
            "identifier": 41,
            "lineNumber": branch.get("lineNumber", 0),
            "lineNumbers": line_numbers,
            "type": "olet",
            "spec": branch.get("spec", 0),
            "outerDiameterMm": branch.get("outerDiameterMm", 0),
        }],
        "mainSpec": main_spec,
        "branchSpec": branch_spec,
        "displaySpec": f"{round(main_spec)}X{round(branch_spec)}" if main_spec > 0 and branch_spec > 0 and main_spec != branch_spec else str(branch_spec or ""),
        "noMaterialFlag": False,
        "pipeOpeningWeldNoMaterial": False,
        "quantity": 1,
    })
    return merged


def find_matching_elbow_end(components: list[dict], start_index: int, used_indexes: set[int]) -> int:
    start = components[start_index]
    if start.get("identifier") != 35 or not start.get("end"):
        return -1
    for index in range(start_index + 1, len(components)):
        candidate = components[index]
        if index not in used_indexes and candidate.get("identifier") == 36 and same_point(start.get("end"), candidate.get("start")):
            return index
    return -1


def point_keys_for_component(component: dict) -> list[str]:
    return [normalize_point_key(point) for point in (component.get("start"), component.get("end")) if point is not None]


def find_shared_tee_point(group: list[dict]) -> str:
    point_sets = [set(point_keys_for_component(component)) for component in group]
    if not point_sets:
        return ""
    for point_key in point_sets[0]:
        if all(point_key in item for item in point_sets[1:]):
            return point_key
    return ""


def find_matching_tee_parts(components: list[dict], start_index: int, used_indexes: set[int]) -> list[int] | None:
    current = components[start_index]
    if current.get("identifier") not in {45, 46, 47}:
        return None
    candidates = {45: [], 46: [], 47: []}
    for index in range(start_index + 1, len(components)):
        candidate = components[index]
        identifier = candidate.get("identifier")
        if index in used_indexes or identifier not in {45, 46, 47}:
            continue
        candidates[identifier].append(index)
    candidates[current["identifier"]].insert(0, start_index)
    for index45 in candidates[45]:
        for index46 in candidates[46]:
            for index47 in candidates[47]:
                indexes = [index45, index46, index47]
                if len(set(indexes)) != 3:
                    continue
                group = [components[group_index] for group_index in indexes]
                if find_shared_tee_point(group):
                    return indexes
    return None


def find_matching_olet_parts(components: list[dict], start_index: int, used_indexes: set[int]) -> list[int] | None:
    current = components[start_index]
    if current.get("identifier") not in {40, 41, 42} or not current.get("start"):
        return None
    point_key = normalize_point_key(current["start"])

    def is_matching_marker(candidate_index: int) -> bool:
        if candidate_index < 0 or candidate_index >= len(components) or candidate_index in used_indexes:
            return False
        candidate = components[candidate_index]
        return (
            candidate.get("identifier") in {40, 42}
            and candidate.get("start")
            and candidate.get("end")
            and same_point(candidate.get("start"), candidate.get("end"))
            and normalize_point_key(candidate["start"]) == point_key
        )

    def is_matching_branch(candidate_index: int) -> bool:
        if candidate_index < 0 or candidate_index >= len(components) or candidate_index in used_indexes:
            return False
        candidate = components[candidate_index]
        return (
            candidate.get("identifier") == 41
            and candidate.get("start")
            and candidate.get("end")
            and not same_point(candidate.get("start"), candidate.get("end"))
            and normalize_point_key(candidate["start"]) == point_key
        )

    if current.get("identifier") in {40, 42}:
        marker_indexes = []
        cursor = start_index
        while is_matching_marker(cursor):
            marker_indexes.append(cursor)
            cursor += 1
        if not is_matching_branch(cursor):
            return None
        return [cursor, *marker_indexes]

    if not is_matching_branch(start_index):
        return None
    branch_index = start_index
    marker_indexes = []
    cursor = start_index - 1
    while is_matching_marker(cursor):
        marker_indexes.insert(0, cursor)
        cursor -= 1
    if not marker_indexes:
        cursor = start_index + 1
        while is_matching_marker(cursor):
            marker_indexes.append(cursor)
            cursor += 1
    return [branch_index, *marker_indexes]


MULTI_LINE_COMPONENT_RULES = [
    {"identifiers": [60, 61, 62], "type": "teed-reducer", "suffix": "teed-reducer"},
    {"identifiers": [70, 71, 72], "type": "teed-elbow", "suffix": "teed-elbow"},
    {"identifiers": [85, 86, 87, 88], "type": "four-way-valve", "suffix": "four-way-valve"},
    {"identifiers": [80, 81, 82], "type": "three-way-valve", "suffix": "three-way-valve"},
    {"identifiers": [90, 91, 92, 93], "type": "instrument", "suffix": "instrument-4way"},
    {"identifiers": [90, 91, 93], "type": "instrument", "suffix": "instrument-3way"},
    {"identifiers": [90, 93], "type": "instrument", "suffix": "instrument"},
    {"identifiers": [75, 76], "type": "angle-valve", "suffix": "angle-valve"},
    {"identifiers": [95, 96], "type": "misc-component", "suffix": "misc"},
    {"identifiers": [132, 133], "type": "trap", "suffix": "trap"},
    {"identifiers": [136, 137], "type": "filter", "suffix": "filter"},
]


def find_matching_multi_line_component(components: list[dict], start_index: int, used_indexes: set[int]) -> dict | None:
    start = components[start_index]
    rules = [item for item in MULTI_LINE_COMPONENT_RULES if item["identifiers"][0] == start.get("identifier")]
    for rule in rules:
        chain_match = find_chained_component_match(components, start_index, used_indexes, rule)
        if chain_match:
            return chain_match
        shared_point_match = find_shared_point_component_match(components, start_index, used_indexes, rule)
        if shared_point_match:
            return shared_point_match
    return None


def find_chained_component_match(components: list[dict], start_index: int, used_indexes: set[int], rule: dict) -> dict | None:
    indexes = [start_index]
    previous = components[start_index]
    for identifier in rule["identifiers"][1:]:
        if not previous.get("end"):
            return None
        found_index = -1
        for index in range(start_index + 1, len(components)):
            candidate = components[index]
            if (
                index not in indexes
                and index not in used_indexes
                and candidate.get("identifier") == identifier
                and same_point(previous.get("end"), candidate.get("start"))
            ):
                found_index = index
                break
        if found_index < 0:
            return None
        indexes.append(found_index)
        previous = components[found_index]
    return {"rule": rule, "indexes": indexes}


def find_shared_point_component_match(components: list[dict], start_index: int, used_indexes: set[int], rule: dict) -> dict | None:
    candidate_lists = []
    for rule_index, identifier in enumerate(rule["identifiers"]):
        if rule_index == 0:
            candidate_lists.append([start_index])
        else:
            candidate_lists.append([
                index for index in range(start_index + 1, len(components))
                if index not in used_indexes and components[index].get("identifier") == identifier
            ])
    if any(not item for item in candidate_lists):
        return None

    picked = []

    def visit(rule_index: int) -> list[int] | None:
        if rule_index >= len(candidate_lists):
            group = [components[index] for index in picked]
            return list(picked) if find_shared_tee_point(group) else None
        for index in candidate_lists[rule_index]:
            if index in picked:
                continue
            picked.append(index)
            result = visit(rule_index + 1)
            if result:
                return result
            picked.pop()
        return None

    indexes = visit(0)
    return {"rule": rule, "indexes": indexes} if indexes else None


def normalize_components(components: list[dict], drawing_split_markers: list[dict] | None = None) -> list[dict]:
    drawing_split_markers = drawing_split_markers or []
    normalized = []
    used_indexes = set()
    index = 0
    while index < len(components):
        if index in used_indexes:
            index += 1
            continue
        current = components[index]

        olet_indexes = find_matching_olet_parts(components, index, used_indexes)
        if olet_indexes:
            for group_index in olet_indexes:
                if group_index != index:
                    used_indexes.add(group_index)
            merged_olet = merge_olet_components([components[group_index] for group_index in olet_indexes])
            if merged_olet:
                normalized.append(merged_olet)
                index += 1
                continue

        elbow_end_index = find_matching_elbow_end(components, index, used_indexes)
        if elbow_end_index >= 0:
            used_indexes.add(elbow_end_index)
            normalized.append(merge_components([current, components[elbow_end_index]], "elbow", "elbow"))
            index += 1
            continue

        tee_indexes = find_matching_tee_parts(components, index, used_indexes)
        if tee_indexes:
            for group_index in tee_indexes:
                if group_index != index:
                    used_indexes.add(group_index)
            normalized.append(merge_components([components[group_index] for group_index in tee_indexes], "branch", "tee"))
            index += 1
            continue

        multi_line_component = find_matching_multi_line_component(components, index, used_indexes)
        if multi_line_component:
            for group_index in multi_line_component["indexes"]:
                if group_index != index:
                    used_indexes.add(group_index)
            rule = multi_line_component["rule"]
            normalized.append(merge_components([components[group_index] for group_index in multi_line_component["indexes"]], rule["type"], rule["suffix"]))
            index += 1
            continue

        if current.get("type") == "pipe":
            run = [current]
            while (
                index + len(run) < len(components)
                and index + len(run) not in used_indexes
                and same_pipe_run(run[-1], components[index + len(run)])
                and not has_split_marker_between(run[-1], components[index + len(run)], drawing_split_markers)
            ):
                run.append(components[index + len(run)])
            if len(run) > 1:
                normalized.append(merge_components(run, "pipe", "pipe"))
                index += len(run)
                continue

        normalized.append(current)
        index += 1
    corrected = correct_pipes_extending_to_fitting_centers(normalized)
    corrected = correct_pipe_overrun_turnbacks(corrected)
    return merge_collinear_pipe_runs(corrected, drawing_split_markers)


def get_component_line_range(component: dict) -> dict:
    line_numbers = [
        item for item in (component.get("lineNumbers") or [component.get("lineNumber", 0)])
        if isinstance(item, (int, float))
    ]
    if not line_numbers:
        return {"min": 0, "max": 0}
    return {"min": min(line_numbers), "max": max(line_numbers)}


def has_split_marker_between(left: dict, right: dict, drawing_split_markers: list[dict]) -> bool:
    if not drawing_split_markers:
        return False
    left_range = get_component_line_range(left)
    right_range = get_component_line_range(right)
    lower = min(left_range["max"], right_range["max"])
    upper = max(left_range["min"], right_range["min"])
    return any(lower < marker.get("lineNumber", 0) < upper for marker in drawing_split_markers)


def correct_pipes_extending_to_fitting_centers(components: list[dict]) -> list[dict]:
    corrections = build_fitting_center_corrections(components)
    if not corrections:
        return components

    remove_ids = set()
    endpoint_updates: dict[str, dict] = {}
    for correction in corrections:
        bridge_pipe = correction["bridgePipe"]
        remove_ids.add(bridge_pipe["id"])
        for pipe in find_pipes_to_shorten_to_fitting_endpoint(components, correction):
            if pipe["id"] in remove_ids:
                continue
            endpoint_updates[pipe["id"]] = {
                "internalPoint": correction["internalPoint"],
                "externalPoint": correction["externalPoint"],
                "fittingId": correction["fitting"]["id"],
                "removedPipeId": bridge_pipe["id"],
            }

    corrected = []
    for component in components:
        if component.get("id") in remove_ids:
            continue
        update = endpoint_updates.get(component.get("id"))
        if update:
            component = apply_pipe_endpoint_correction(component, update)
        corrected.append(component)
    return corrected


def correct_pipe_overrun_turnbacks(components: list[dict]) -> list[dict]:
    pipes = [
        component for component in components
        if component.get("type") == "pipe" and component.get("start") and component.get("end")
    ]
    if len(pipes) < 2:
        return components

    endpoint_map: dict[str, list[dict]] = {}
    for pipe in pipes:
        for point in (pipe["start"], pipe["end"]):
            endpoint_map.setdefault(normalize_point_key(point), []).append(pipe)

    remove_ids = set()
    endpoint_updates: dict[str, dict] = {}
    for internal_key, connected_pipes in endpoint_map.items():
        if len(connected_pipes) < 2:
            continue
        internal_point = connected_pipes[0]["start"] if normalize_point_key(connected_pipes[0]["start"]) == internal_key else connected_pipes[0]["end"]
        candidates = []
        for bridge_pipe in connected_pipes:
            if bridge_pipe.get("id") in remove_ids:
                continue
            external_point = get_other_pipe_endpoint(bridge_pipe, internal_point)
            if not external_point or not has_non_pipe_component_at_point(components, external_point, ignore_ids={bridge_pipe["id"]}):
                continue
            external_to_internal = normalize_vector(vector_between(external_point, internal_point))
            if not external_to_internal:
                continue
            for long_pipe in connected_pipes:
                if long_pipe["id"] == bridge_pipe["id"] or long_pipe.get("id") in remove_ids:
                    continue
                far_point = get_other_pipe_endpoint(long_pipe, internal_point)
                if not far_point or same_point(far_point, external_point):
                    continue
                far_to_internal = normalize_vector(vector_between(far_point, internal_point))
                far_to_external = normalize_vector(vector_between(far_point, external_point))
                if not far_to_internal or not far_to_external:
                    continue
                if vector_dot(far_to_internal, external_to_internal) < 0.999:
                    continue
                if vector_dot(far_to_internal, far_to_external) < 0.999:
                    continue
                if point_to_segment_distance(external_point, far_point, internal_point) > 1e-3:
                    continue
                if point_distance(far_point, internal_point) <= point_distance(far_point, external_point) + 1e-3:
                    continue
                candidates.append({
                    "bridgePipe": bridge_pipe,
                    "longPipe": long_pipe,
                    "internalPoint": internal_point,
                    "externalPoint": external_point,
                    "overrunLength": point_distance(external_point, internal_point),
                })
        if not candidates:
            continue
        best = max(candidates, key=lambda item: point_distance(get_other_pipe_endpoint(item["longPipe"], item["internalPoint"]), item["internalPoint"]))
        remove_ids.add(best["bridgePipe"]["id"])
        endpoint_updates[best["longPipe"]["id"]] = {
            "internalPoint": best["internalPoint"],
            "externalPoint": best["externalPoint"],
            "fittingId": "",
            "removedPipeId": best["bridgePipe"]["id"],
            "correctionType": "pipe-overrun-turnback",
        }

    if not remove_ids and not endpoint_updates:
        return components
    corrected = []
    for component in components:
        if component.get("id") in remove_ids:
            continue
        update = endpoint_updates.get(component.get("id"))
        if update:
            component = apply_pipe_endpoint_correction(component, update)
        corrected.append(component)
    return corrected


def get_other_pipe_endpoint(pipe: dict, point: list[float]) -> list[float] | None:
    if same_point(pipe.get("start"), point):
        return pipe.get("end")
    if same_point(pipe.get("end"), point):
        return pipe.get("start")
    return None


def has_non_pipe_component_at_point(components: list[dict], point: list[float], ignore_ids: set[str] | None = None) -> bool:
    ignore_ids = ignore_ids or set()
    for component in components:
        if component.get("id") in ignore_ids or component.get("type") in {"pipe", "weld", "olet-marker"}:
            continue
        for component_point in iter_component_connection_points(component):
            if same_point(component_point, point):
                return True
    return False


def iter_component_connection_points(component: dict):
    segments = component.get("segments") or []
    if segments:
        for segment in segments:
            if segment.get("start"):
                yield segment["start"]
            if segment.get("end"):
                yield segment["end"]
        return
    if component.get("start"):
        yield component["start"]
    if component.get("end"):
        yield component["end"]


def build_fitting_center_corrections(components: list[dict]) -> list[dict]:
    pipes = [component for component in components if component.get("type") == "pipe" and component.get("start") and component.get("end")]
    corrections = []
    for fitting in components:
        if fitting.get("type") not in FITTING_CENTER_CORRECTION_TYPES:
            continue
        topology = get_fitting_center_topology(fitting)
        for pair in topology:
            bridge_pipe = find_pipe_between_points(pipes, pair["internalPoint"], pair["externalPoint"])
            if bridge_pipe:
                corrections.append({
                    "fitting": fitting,
                    "bridgePipe": bridge_pipe,
                    "internalPoint": pair["internalPoint"],
                    "externalPoint": pair["externalPoint"],
                })
    return corrections


def get_fitting_center_topology(fitting: dict) -> list[dict]:
    segments = [
        segment for segment in fitting.get("segments") or []
        if segment.get("start") and segment.get("end")
    ]
    if len(segments) < 2:
        return []
    point_counts = {}
    point_by_key = {}
    for segment in segments:
        for point in (segment["start"], segment["end"]):
            key = normalize_point_key(point)
            point_counts[key] = point_counts.get(key, 0) + 1
            point_by_key[key] = point
    internal_keys = {key for key, count in point_counts.items() if count > 1}
    if not internal_keys:
        return []
    topology = []
    seen_pairs = set()
    for segment in segments:
        start_key = normalize_point_key(segment["start"])
        end_key = normalize_point_key(segment["end"])
        if start_key in internal_keys and end_key not in internal_keys:
            pair_key = (start_key, end_key)
            if pair_key not in seen_pairs:
                topology.append({"internalPoint": point_by_key[start_key], "externalPoint": point_by_key[end_key]})
                seen_pairs.add(pair_key)
        elif end_key in internal_keys and start_key not in internal_keys:
            pair_key = (end_key, start_key)
            if pair_key not in seen_pairs:
                topology.append({"internalPoint": point_by_key[end_key], "externalPoint": point_by_key[start_key]})
                seen_pairs.add(pair_key)
    return topology


def find_pipe_between_points(pipes: list[dict], first_point: list[float], second_point: list[float]) -> dict | None:
    for pipe in pipes:
        if (
            same_point(pipe.get("start"), first_point) and same_point(pipe.get("end"), second_point)
        ) or (
            same_point(pipe.get("start"), second_point) and same_point(pipe.get("end"), first_point)
        ):
            return pipe
    return None


def find_pipes_to_shorten_to_fitting_endpoint(components: list[dict], correction: dict) -> list[dict]:
    internal_point = correction["internalPoint"]
    external_point = correction["externalPoint"]
    bridge_pipe_id = correction["bridgePipe"]["id"]
    fitting_id = correction["fitting"]["id"]
    external_to_internal = normalize_vector(vector_between(external_point, internal_point))
    if not external_to_internal:
        return []
    matches = []
    for component in components:
        if component.get("type") != "pipe" or component.get("id") in {bridge_pipe_id, fitting_id}:
            continue
        if not component.get("start") or not component.get("end"):
            continue
        if not (same_point(component["start"], internal_point) or same_point(component["end"], internal_point)):
            continue
        other_point = component["end"] if same_point(component["start"], internal_point) else component["start"]
        other_to_internal = normalize_vector(vector_between(other_point, internal_point))
        if other_to_internal and vector_dot(other_to_internal, external_to_internal) >= 0.999:
            matches.append(component)
    return matches


def apply_pipe_endpoint_correction(pipe: dict, update: dict) -> dict:
    corrected = copy.deepcopy(pipe)
    internal_point = update["internalPoint"]
    external_point = update["externalPoint"]
    if same_point(corrected.get("start"), internal_point):
        corrected["start"] = list(external_point)
    if same_point(corrected.get("end"), internal_point):
        corrected["end"] = list(external_point)
    corrected["segments"] = [{
        **(corrected.get("segments") or [{}])[0],
        "start": corrected.get("start"),
        "end": corrected.get("end"),
        "identifier": corrected.get("identifier", 100),
        "type": "pipe",
        "spec": corrected.get("spec", 0),
        "outerDiameterMm": corrected.get("outerDiameterMm", 0),
    }]
    corrected["quantity"] = calculate_component_quantity(corrected)
    corrected.setdefault("autoCorrections", []).append({
        "type": update.get("correctionType") or "pipe-fitting-center-overrun",
        "fittingId": update.get("fittingId", ""),
        "removedPipeId": update["removedPipeId"],
        "fromPoint": internal_point,
        "toPoint": external_point,
    })
    return corrected


def merge_collinear_pipe_runs(components: list[dict], drawing_split_markers: list[dict] | None = None) -> list[dict]:
    drawing_split_markers = drawing_split_markers or []
    result = []
    used_indexes = set()
    for index, current in enumerate(components):
        if index in used_indexes:
            continue
        if current.get("type") != "pipe":
            result.append(current)
            continue
        run = [current]
        used_indexes.add(index)
        tail = current
        while True:
            next_index = -1
            for candidate_index, candidate in enumerate(components):
                if (
                    candidate_index not in used_indexes
                    and same_pipe_run(tail, candidate)
                    and not has_split_marker_between(tail, candidate, drawing_split_markers)
                ):
                    next_index = candidate_index
                    break
            if next_index < 0:
                break
            used_indexes.add(next_index)
            run.append(components[next_index])
            tail = components[next_index]
        result.append(merge_components(run, "pipe", "pipe") if len(run) > 1 else current)
    return result


def build_viewer_model(model: dict) -> dict:
    nodes = {}
    segments = []
    symbol_components = []
    components = list(model["components"])
    connection_map = {}

    def add_node(point, component_id):
        if point is None:
            return
        key = normalize_point_key(point)
        nodes.setdefault(key, {"id": key, "point": point, "componentIds": []})
        nodes[key]["componentIds"].append(component_id)

    def remember_connection(point, component, role, segment=None):
        if point is None:
            return
        key = normalize_point_key(point)
        connection_map.setdefault(key, {"point": point, "refs": []})
        connection_map[key]["refs"].append({"component": component, "role": role, "segment": segment})

    def get_external_segment_refs(component_segments):
        return get_external_segment_refs_from_segments(component_segments, normalize_point_key)

    for component in components:
        component_segments = [] if component.get("type") == "olet-marker" else component.get("segments")
        if component.get("type") != "olet-marker" and not component_segments and component.get("start") and component.get("end"):
            component_segments = [{
                "start": component["start"],
                "end": component["end"],
                "type": component["type"],
            }]
        for ref in get_external_segment_refs(component_segments or []):
            remember_connection(ref["point"], component, ref["role"], ref["segment"])
        for segment in component_segments or []:
            add_node(segment.get("start"), component["id"])
            add_node(segment.get("end"), component["id"])
        for segment_index, segment in enumerate(component_segments or [], start=1):
            segments.append({
                "id": f"{component['id']}:{segment_index}",
                "componentId": component["id"],
                "lineNumber": segment.get("lineNumber") or component.get("lineNumber", 0),
                "lineNumbers": segment.get("lineNumbers") or component.get("lineNumbers") or [component.get("lineNumber", 0)],
                "start": segment["start"],
                "end": segment["end"],
                "type": segment.get("type", component["type"]),
                "spec": segment.get("spec", component["spec"]),
                "outerDiameterMm": segment.get("outerDiameterMm", component.get("outerDiameterMm", 0)),
                "skey": component["skey"],
            })
        if should_create_symbol(component):
            symbol_components.append(component)

    register_olet_main_pipe_connections(components, segments, connection_map, remember_connection)
    register_pipe_opening_connections(components, segments, remember_connection)

    node_specs = calculate_node_specs(components)
    apply_reducer_end_specs(components, connection_map, node_specs)

    auto_welds = create_auto_weld_components(connection_map, segments)
    for component in auto_welds:
        components.append(component)
        add_node(component.get("start"), component["id"])
        symbol_components.append(component)

    assign_weld_numbers(
        components,
        segments,
        default_pipeline_id=get_model_pipeline_id(model, "UNKNOWN"),
        default_pipeline_name=model.get("pipelineName") or model.get("fileName") or "UNKNOWN",
    )
    assign_symbol_directions(components, segments, connection_map)

    model["components"] = components
    model["units"] = "IDF coordinate units, rendered after automatic scaling"
    model["nodes"] = list(nodes.values())
    model["segments"] = segments
    model["symbolComponents"] = symbol_components
    return model


def assign_weld_numbers(
    components: list[dict],
    segments: list[dict] | None = None,
    default_pipeline_id: str = "UNKNOWN",
    default_pipeline_name: str = "UNKNOWN",
    options: ParserOptions | None = None,
) -> None:
    options = options or CURRENT_PARSE_OPTIONS
    component_by_id = {component["id"]: component for component in components}
    segments_by_pipeline = build_segments_by_pipeline(segments or [], component_by_id, default_pipeline_id)
    welds_by_pipeline = {}
    for component in components:
        if component.get("type") != "weld":
            continue
        if is_double_no_material_flag_weld(component, component_by_id):
            component["weldRawNo"] = 0
            component["weldNo"] = "0"
            component["excludeFromWeldTable"] = True
            component["doubleNoMaterialFlagWeld"] = True
            continue
        if component.get("pipeSplitWeld") and options.pipe_split_number_mode != PIPE_SPLIT_NUMBER_MODE_SEQUENCE:
            continue
        owner = infer_weld_owner_pipeline(component, component_by_id, default_pipeline_id, default_pipeline_name)
        component["weldOwnerPipelineId"] = owner["pipelineId"]
        component["weldOwnerPipelineName"] = owner["pipelineName"]
        component["weldOwnerUnitName"] = owner.get("unitName", "")
        welds_by_pipeline.setdefault(owner["pipelineId"], []).append(component)

    for pipeline_id, welds in welds_by_pipeline.items():
        ordered_welds = order_welds_by_pipeline_topology(
            pipeline_id,
            welds,
            components,
            segments_by_pipeline.get(pipeline_id, []),
            component_by_id,
            default_pipeline_id,
        )
        counters = {}
        for weld in ordered_welds:
            counter_key = get_weld_number_counter_key(weld, options)
            counters[counter_key] = counters.get(counter_key, 0) + 1
            weld["weldRawNo"] = counters[counter_key]
    if options.pipe_split_number_mode == PIPE_SPLIT_NUMBER_MODE_PIPE_END:
        assign_pipe_split_weld_numbers(components)
    apply_weld_number_format(components, options)


def is_double_no_material_flag_weld(weld: dict, component_by_id: dict) -> bool:
    connected = get_weld_connected_material_components(weld, component_by_id)
    if len(connected) < 2:
        return False
    return all(bool(component.get("noMaterialFlag")) for component in connected[:2])


def get_weld_number_counter_key(weld: dict, options: ParserOptions) -> str:
    mode = options.weld_type_number_mode
    weld_type = str(weld.get("weldType") or "").lower()
    if mode == WELD_TYPE_NUMBER_MODE_ALL and weld_type in NUMBERED_WELD_TYPES:
        return f"type:{weld_type}"
    if (
        mode == WELD_TYPE_NUMBER_MODE_CONFIGURED
        and weld_type in NUMBERED_WELD_TYPES
        and options.weld_prefix_by_type.get(weld_type)
    ):
        return f"type:{weld_type}"
    return "sequence"


def assign_pipe_split_weld_numbers(components: list[dict]) -> None:
    component_by_id = {component.get("id"): component for component in components if component.get("id")}
    pipe_components_by_group = {}
    split_welds_by_group = {}
    normal_welds = []
    for component in components:
        if component.get("type") == "pipe" and component.get("pipeGroupId"):
            pipe_components_by_group.setdefault(component["pipeGroupId"], []).append(component)
        elif component.get("type") == "weld" and component.get("pipeSplitWeld") and component.get("pipeGroupId"):
            split_welds_by_group.setdefault(component["pipeGroupId"], []).append(component)
        elif component.get("type") == "weld" and not component.get("excludeFromWeldTable"):
            normal_welds.append(component)

    for group_id, split_welds in split_welds_by_group.items():
        pipe_components = pipe_components_by_group.get(group_id, [])
        axis = get_pipe_group_axis(pipe_components)
        if not axis:
            for index, weld in enumerate(sorted(split_welds, key=lambda item: item.get("pipeSplitIndex", 0)), start=1):
                weld["weldRawNo"] = f"0/0-{index}"
            continue
        origin = axis["origin"]
        direction = axis["direction"]
        min_t = axis["minT"]
        max_t = axis["maxT"]
        group_component_ids = {component["id"] for component in pipe_components}
        boundary_welds = []
        for weld in normal_welds:
            if not set(weld.get("connectedComponentIds") or []) & group_component_ids:
                continue
            if not weld.get("start") or weld.get("weldRawNo") in (None, ""):
                continue
            if is_branch_root_weld(weld):
                continue
            t = project_point_on_axis(weld["start"], origin, direction)
            if min(abs(t - min_t), abs(t - max_t)) > GLOBAL_CONNECTION_TOLERANCE_UNITS * 2:
                continue
            boundary_welds.append({
                "t": t,
                "weldRawNo": weld.get("weldRawNo"),
                "pipelineId": weld.get("weldOwnerPipelineId") or weld.get("pipelineId") or "",
            })
        boundary_welds.sort(key=lambda item: item["t"])
        split_entries = [
            {
                "t": project_point_on_axis(weld["start"], origin, direction) if weld.get("start") else float(weld.get("pipeSplitIndex", 0)),
                "weld": weld,
            }
            for weld in split_welds
        ]
        split_entries.sort(key=lambda item: item["t"])
        pair_counts = {}
        for entry in split_entries:
            owner = infer_weld_owner_pipeline(entry["weld"], component_by_id, "", "")
            owner_pipeline_id = owner["pipelineId"]
            split_t = entry["t"]
            same_pipeline_boundaries = [
                item for item in boundary_welds
                if not owner_pipeline_id or item.get("pipelineId") == owner_pipeline_id
            ]
            left = next((item for item in reversed(same_pipeline_boundaries) if item["t"] < split_t - 1e-6), None)
            right = next((item for item in same_pipeline_boundaries if item["t"] > split_t + 1e-6), None)
            left_no = left["weldRawNo"] if left else 0
            right_no = right["weldRawNo"] if right else 0
            if left and right:
                number_prefix = f"{left_no}/{right_no}"
                pair_key = (str(left_no), str(right_no))
            else:
                number_prefix = str(left_no or right_no or 0)
                pair_key = (number_prefix,)
            pair_counts[pair_key] = pair_counts.get(pair_key, 0) + 1
            entry["weld"]["weldRawNo"] = f"{number_prefix}-{pair_counts[pair_key]}"
            entry["weld"]["weldOwnerPipelineId"] = owner["pipelineId"]
            entry["weld"]["weldOwnerPipelineName"] = owner["pipelineName"]
            entry["weld"]["weldOwnerUnitName"] = owner.get("unitName", "")


def apply_weld_number_format(components: list[dict], options: ParserOptions) -> None:
    for component in components:
        if component.get("type") != "weld":
            continue
        raw_no = component.get("weldRawNo", component.get("weldNo"))
        if raw_no in (None, ""):
            continue
        component["weldRawNo"] = raw_no
        component["weldNo"] = format_configured_weld_no(component, raw_no, options)


def format_configured_weld_no(weld: dict, raw_no, options: ParserOptions) -> str:
    text = str(raw_no)
    if weld.get("excludeFromWeldTable") or text == "0":
        return "0"
    prefix = options.weld_prefix_by_type.get(str(weld.get("weldType") or "").lower(), "")
    formatted = f"{prefix}{text}" if prefix else text
    location = str(weld.get("weldLocation") or "").lower()
    if location in {"shop", "prefab", "pre-fab"} and options.shop_weld_suffix:
        return f"{formatted}{options.shop_weld_suffix}"
    if location in {"field", "install", "site"} and options.field_weld_suffix:
        return f"{formatted}{options.field_weld_suffix}"
    return formatted


def get_pipe_group_axis(pipe_components: list[dict]) -> dict | None:
    first = next((component for component in pipe_components if component.get("start") and component.get("end")), None)
    if not first:
        return None
    direction = pipe_direction(first)
    if not direction:
        return None
    origin = first["start"]
    projections = [
        project_point_on_axis(point, origin, direction)
        for component in pipe_components
        for point in (component.get("start"), component.get("end"))
        if point
    ]
    if len(projections) < 2:
        return None
    return {
        "origin": origin,
        "direction": direction,
        "minT": min(projections),
        "maxT": max(projections),
    }


def infer_weld_owner_pipeline(weld: dict, component_by_id: dict, default_pipeline_id: str, default_pipeline_name: str) -> dict:
    candidates = {}
    for component_id in weld.get("connectedComponentIds") or []:
        component = component_by_id.get(component_id)
        if not component:
            continue
        pipeline_id = component.get("pipelineId") or default_pipeline_id
        candidates[pipeline_id] = {
            "pipelineName": component.get("pipelineName") or default_pipeline_name or pipeline_id,
            "unitName": component.get("unitName", ""),
        }

    if not candidates:
        pipeline_id = weld.get("pipelineId") if weld.get("pipelineId") != "cross-idf-connections" else default_pipeline_id
        pipeline_id = pipeline_id or default_pipeline_id
        return {
            "pipelineId": pipeline_id,
            "pipelineName": weld.get("pipelineName") or default_pipeline_name or pipeline_id,
            "unitName": weld.get("unitName", ""),
        }

    entries = sorted(candidates.items(), key=lambda item: str(item[0]))
    index = 0 if len(entries) == 1 else stable_hash(weld.get("id") or normalize_point_key(weld.get("start") or [0, 0, 0])) % len(entries)
    return {
        "pipelineId": entries[index][0],
        "pipelineName": entries[index][1]["pipelineName"],
        "unitName": entries[index][1]["unitName"],
    }


def build_segments_by_pipeline(segments: list[dict], component_by_id: dict, default_pipeline_id: str) -> dict[str, list[dict]]:
    grouped = {}
    for segment in segments:
        component = component_by_id.get(segment.get("componentId"))
        pipeline_id = segment.get("pipelineId") or (component.get("pipelineId") if component else "")
        pipeline_id = pipeline_id or default_pipeline_id
        grouped.setdefault(pipeline_id, []).append(segment)
    return grouped


def get_weld_number_sort_key(weld: dict, component_by_id: dict) -> tuple:
    point = weld.get("start") or [0, 0, 0]
    return (
        get_weld_branch_rank(weld, component_by_id),
        weld.get("lineNumber") if weld.get("lineNumber", 0) > 0 else 10**12,
        point[0] if len(point) > 0 else 0,
        point[1] if len(point) > 1 else 0,
        point[2] if len(point) > 2 else 0,
        str(weld.get("id", "")),
    )


def get_weld_branch_rank(weld: dict, component_by_id: dict) -> int:
    if weld.get("weldType") in {"olet", "seton"}:
        return 1
    connected = [
        component_by_id[component_id]
        for component_id in weld.get("connectedComponentIds") or []
        if component_id in component_by_id
    ]
    return 1 if any(component.get("type") in {"olet", "branch", "teed-reducer", "teed-elbow"} for component in connected) else 0


def is_branch_root_weld(weld: dict) -> bool:
    return weld.get("weldType") in {"olet", "seton"}


def should_record_weld_on_route(weld: dict, branch_entry_edge: dict | None) -> bool:
    if not is_branch_root_weld(weld):
        return True
    if not branch_entry_edge:
        return False
    edge_component_id = branch_entry_edge.get("componentId")
    if edge_component_id and edge_component_id in set(weld.get("connectedComponentIds") or []):
        return True
    return branch_entry_edge.get("componentType") in {"olet"}


def order_welds_by_pipeline_topology(
    pipeline_id: str,
    welds: list[dict],
    components: list[dict],
    segments: list[dict],
    component_by_id: dict,
    default_pipeline_id: str,
) -> list[dict]:
    graph = build_pipeline_weld_number_graph(pipeline_id, welds, components, segments, component_by_id, default_pipeline_id)
    if not graph["edges"]:
        return sorted(welds, key=lambda weld: get_weld_number_sort_key(weld, component_by_id))

    ordered_ids = []
    visited_weld_ids = set()
    visited_edge_ids = set()
    route_queue = []

    def record_node_welds(node_key: str, branch_entry_edge: dict | None = None) -> None:
        node_welds = graph["weldsByNode"].get(node_key, [])
        node_welds.sort(key=lambda weld: get_weld_number_sort_key(weld, component_by_id))
        for weld in node_welds:
            if not should_record_weld_on_route(weld, branch_entry_edge):
                continue
            weld_id = weld.get("id")
            if weld_id and weld_id not in visited_weld_ids:
                visited_weld_ids.add(weld_id)
                ordered_ids.append(weld_id)

    def enqueue_disconnected_routes() -> bool:
        next_start = find_next_topology_start(graph, visited_edge_ids)
        if not next_start:
            return False
        route_queue.append(next_start)
        return True

    route = find_next_topology_start(graph, visited_edge_ids)
    if route:
        route_queue.append(route)

    while route_queue:
        route = route_queue.pop(0)
        current_key = route["nodeKey"]
        previous_direction = route.get("previousDirection")
        previous_edge = route.get("previousEdge")
        forced_edge_id = route.get("edgeId")

        while True:
            candidates = [
                edge for edge in graph["adjacency"].get(current_key, [])
                if edge["id"] not in visited_edge_ids
            ]
            forced_edge = None
            if forced_edge_id:
                forced = next((edge for edge in candidates if edge["id"] == forced_edge_id), None)
                candidates = [forced] if forced else []
                forced_edge = forced
                forced_edge_id = None
            record_node_welds(current_key, forced_edge)
            if not candidates:
                break

            primary, branches = choose_primary_topology_edge(candidates, current_key, previous_direction, previous_edge)
            for branch in branches:
                route_queue.append({
                    "nodeKey": current_key,
                    "previousDirection": previous_direction,
                    "previousEdge": previous_edge,
                    "edgeId": branch["id"],
                })

            visited_edge_ids.add(primary["id"])
            previous_direction = edge_direction_from(primary, current_key)
            previous_edge = primary
            current_key = branch_other_node(primary, current_key)

        if not route_queue:
            enqueue_disconnected_routes()

    remaining = [
        weld for weld in welds
        if weld.get("id") not in visited_weld_ids
    ]
    remaining.sort(key=lambda weld: get_weld_number_sort_key(weld, component_by_id))
    ordered_by_id = {weld.get("id"): weld for weld in welds}
    return [ordered_by_id[weld_id] for weld_id in ordered_ids if weld_id in ordered_by_id] + remaining


def build_pipeline_weld_number_graph(
    pipeline_id: str,
    welds: list[dict],
    components: list[dict],
    segments: list[dict],
    component_by_id: dict,
    default_pipeline_id: str,
) -> dict:
    key_func = normalize_global_connection_key
    welds_by_node = {}
    weld_points = []
    weld_points_by_component_id = {}
    for weld in welds:
        point = weld.get("start")
        if not point:
            continue
        key = key_func(point)
        welds_by_node.setdefault(key, []).append(weld)
        weld_point = {
            "key": key,
            "point": point,
            "connectedComponentIds": set(weld.get("connectedComponentIds") or []),
        }
        weld_points.append(weld_point)
        for component_id in weld_point["connectedComponentIds"]:
            weld_points_by_component_id.setdefault(component_id, []).append(weld_point)

    relevant_segments = []
    for segment in segments:
        if not segment.get("start") or not segment.get("end"):
            continue
        if segment.get("type") in {"weld", "equipment", "olet-marker"}:
            continue
        component = component_by_id.get(segment.get("componentId"))
        if not component:
            continue
        segment_pipeline_id = segment.get("pipelineId") or component.get("pipelineId") or default_pipeline_id
        component_pipeline_id = component.get("pipelineId") or default_pipeline_id
        if segment_pipeline_id != pipeline_id and component_pipeline_id != pipeline_id:
            continue
        relevant_segments.append((segment, component))

    edges = []
    adjacency = {}
    for segment, component in relevant_segments:
        split_points = collect_segment_topology_points(
            segment,
            weld_points_by_component_id.get(segment.get("componentId"), []),
        )
        for index in range(len(split_points) - 1):
            first = split_points[index]
            second = split_points[index + 1]
            if first["key"] == second["key"]:
                continue
            edge = {
                "id": f"{segment.get('id') or segment.get('componentId')}:{index}",
                "componentId": segment.get("componentId"),
                "componentType": component.get("type") or segment.get("type", ""),
                "lineNumber": get_component_sort_line(component),
                "spec": segment.get("spec") or component.get("spec", 0),
                "startKey": first["key"],
                "endKey": second["key"],
                "start": first["point"],
                "end": second["point"],
            }
            edges.append(edge)
            adjacency.setdefault(edge["startKey"], []).append(edge)
            adjacency.setdefault(edge["endKey"], []).append(edge)

    return {
        "edges": edges,
        "adjacency": adjacency,
        "weldsByNode": welds_by_node,
    }


def collect_segment_topology_points(segment: dict, weld_points: list[dict]) -> list[dict]:
    start = segment["start"]
    end = segment["end"]
    component_id = segment.get("componentId")
    points = [
        {"key": normalize_global_connection_key(start), "point": start, "t": 0.0},
        {"key": normalize_global_connection_key(end), "point": end, "t": 1.0},
    ]
    for weld_point in weld_points:
        point = weld_point["point"]
        if component_id and component_id not in weld_point["connectedComponentIds"]:
            continue
        t = point_segment_parameter(point, start, end)
        if t is None or t <= 1e-6 or t >= 1 - 1e-6:
            continue
        if point_to_segment_distance(point, start, end) > GLOBAL_CONNECTION_TOLERANCE_UNITS:
            continue
        points.append({
            "key": weld_point["key"],
            "point": point,
            "t": t,
        })
    points.sort(key=lambda item: item["t"])

    deduped = []
    seen = set()
    for item in points:
        if item["key"] in seen:
            continue
        deduped.append(item)
        seen.add(item["key"])
    return deduped


def point_segment_parameter(point: list[float], start: list[float], end: list[float]) -> float | None:
    ab = [end[index] - start[index] for index in range(3)]
    ap = [point[index] - start[index] for index in range(3)]
    length_sq = sum(value * value for value in ab)
    if length_sq <= 1e-9:
        return None
    return sum(ap[index] * ab[index] for index in range(3)) / length_sq


def find_next_topology_start(graph: dict, visited_edge_ids: set[str]) -> dict | None:
    candidate_nodes = []
    for node_key, edges in graph["adjacency"].items():
        unvisited_edges = [edge for edge in edges if edge["id"] not in visited_edge_ids]
        if not unvisited_edges:
            continue
        degree_rank = 0 if len(unvisited_edges) == 1 else 1
        min_line = min(edge.get("lineNumber") or 10**12 for edge in unvisited_edges)
        max_spec = max(safe_float(edge.get("spec", 0)) for edge in unvisited_edges)
        candidate_nodes.append((degree_rank, min_line, -max_spec, node_key, unvisited_edges[0]))
    if not candidate_nodes:
        return None
    candidate_nodes.sort()
    node_key = candidate_nodes[0][3]
    return {"nodeKey": node_key, "previousDirection": None}


def choose_primary_topology_edge(
    candidates: list[dict],
    current_key: str,
    previous_direction: list[float] | None,
    previous_edge: dict | None = None,
) -> tuple[dict, list[dict]]:
    ranked = sorted(
        candidates,
        key=lambda edge: topology_edge_rank(edge, current_key, previous_direction, previous_edge),
    )
    return ranked[0], ranked[1:]


def topology_edge_rank(
    edge: dict,
    current_key: str,
    previous_direction: list[float] | None,
    previous_edge: dict | None = None,
) -> tuple:
    direction = edge_direction_from(edge, current_key)
    alignment = 0.0
    if previous_direction and direction:
        alignment = sum(previous_direction[index] * direction[index] for index in range(3))
    component_type = edge.get("componentType", "")
    previous_is_flanged_inline = (
        previous_edge and previous_edge.get("componentType") in TOPOLOGY_FLANGED_INLINE_COMPONENT_TYPES
    )
    flanged_inline_continuation_penalty = 0
    if previous_is_flanged_inline:
        flanged_inline_continuation_penalty = 1 if component_type in TOPOLOGY_BRANCH_COMPONENT_TYPES else 0
    branch_penalty = 1 if component_type in TOPOLOGY_BRANCH_COMPONENT_TYPES else 0
    return (
        -alignment,
        flanged_inline_continuation_penalty,
        branch_penalty,
        -safe_float(edge.get("spec", 0)),
        edge.get("lineNumber") or 10**12,
        str(edge.get("id", "")),
    )


def branch_other_node(edge: dict, current_key: str) -> str:
    return edge["endKey"] if edge["startKey"] == current_key else edge["startKey"]


def edge_direction_from(edge: dict, current_key: str) -> list[float] | None:
    if edge["startKey"] == current_key:
        start = edge["start"]
        end = edge["end"]
    else:
        start = edge["end"]
        end = edge["start"]
    return normalize_vector([end[index] - start[index] for index in range(3)])


def stable_hash(value) -> int:
    result = 0
    for char in str(value or ""):
        result = ((result * 31) + ord(char)) & 0xFFFFFFFF
    return result


def assign_symbol_directions(components: list[dict], segments: list[dict], connection_map: dict) -> None:
    direction_segments = [
        segment for segment in segments
        if segment.get("start") and segment.get("end") and segment.get("type") not in {"weld", "gasket"}
    ]
    for component in components:
        component_type = component.get("type")
        if component_type not in {"instrument", "weld"} or not component.get("start"):
            continue
        if component_type == "instrument" and (component.get("end") or component.get("segments")):
            continue
        connected_direction = find_connected_segment_direction(
            component["start"],
            connection_map,
            outward=component_type == "instrument",
        )
        if connected_direction:
            component["direction"] = connected_direction
            continue
        if component.get("end"):
            own_direction = normalize_vector([
                component["end"][index] - component["start"][index] for index in range(3)
            ])
            if own_direction:
                component["direction"] = own_direction
                continue
        nearest = find_nearest_segment_direction(component["start"], direction_segments)
        if nearest:
            component["direction"] = nearest


def find_connected_segment_direction(point: list[float], connection_map: dict, outward: bool = False) -> list[float] | None:
    connection = connection_map.get(normalize_point_key(point))
    if not connection:
        return None
    for ref in connection["refs"]:
        component = ref["component"]
        segment = ref.get("segment")
        if not segment or component.get("type") in {"weld", "gasket", "instrument"}:
            continue
        start_matches = same_point(segment.get("start"), point)
        end_matches = same_point(segment.get("end"), point)
        if start_matches or end_matches:
            other = segment["end"] if start_matches else segment["start"]
            direction = normalize_vector([
                (point[index] - other[index]) if outward else (other[index] - point[index])
                for index in range(3)
            ])
            if direction:
                return direction
    return None


def register_olet_main_pipe_connections(components: list[dict], segments: list[dict], connection_map: dict, remember_connection) -> None:
    component_by_id = {component["id"]: component for component in components}
    pipe_segments = [
        segment for segment in segments
        if segment.get("type") == "pipe" and segment.get("start") and segment.get("end")
    ]
    pipe_segment_index = build_segment_spatial_index(pipe_segments)
    for component in components:
        if component.get("type") != "olet" or not component.get("start"):
            continue
        key = normalize_point_key(component["start"])
        connection = connection_map.get(key)
        if connection and any(ref["component"].get("type") == "pipe" for ref in connection["refs"]):
            continue
        host_segment = next((
            segment for segment in query_segment_spatial_index(pipe_segment_index, component["start"])
            if segment.get("componentId") != component["id"]
            and point_to_segment_distance(component["start"], segment["start"], segment["end"]) <= 1e-3
        ), None)
        host_component = component_by_id.get(host_segment["componentId"]) if host_segment else None
        if host_component:
            remember_connection(component["start"], host_component, "middle", host_segment)


def register_pipe_opening_connections(components: list[dict], segments: list[dict], remember_connection) -> None:
    component_by_id = {component["id"]: component for component in components}
    pipe_segments = [
        segment for segment in segments
        if segment.get("type") == "pipe" and segment.get("start") and segment.get("end")
    ]
    pipe_segment_index = build_segment_spatial_index(pipe_segments)
    endpoint_refs = []
    for component in components:
        if component.get("type") != "pipe":
            continue
        component_segments = component.get("segments") or [{
            "start": component.get("start"),
            "end": component.get("end"),
            "type": component.get("type"),
            "spec": component.get("spec", 0),
        }]
        for segment in component_segments:
            if segment.get("start") and segment.get("end"):
                endpoint_refs.extend([
                    {"point": segment["start"], "component": component, "segment": segment, "role": "start"},
                    {"point": segment["end"], "component": component, "segment": segment, "role": "end"},
                ])
    for ref in endpoint_refs:
        point = ref["point"]
        branch_axis = get_ref_axis(ref)
        if not branch_axis:
            continue
        for host_segment in query_segment_spatial_index(pipe_segment_index, point):
            host_component = component_by_id.get(host_segment.get("componentId"))
            if not host_component or host_component["id"] == ref["component"]["id"]:
                continue
            host_axis = normalize_vector([
                host_segment["end"][index] - host_segment["start"][index]
                for index in range(3)
            ])
            if not host_axis:
                continue
            dot = abs(sum(host_axis[index] * branch_axis[index] for index in range(3)))
            if dot >= 0.85:
                continue
            if point_to_segment_distance(point, host_segment["start"], host_segment["end"]) > GLOBAL_CONNECTION_TOLERANCE_UNITS:
                continue
            if (
                point_distance(point, host_segment["start"]) <= GLOBAL_CONNECTION_TOLERANCE_UNITS
                or point_distance(point, host_segment["end"]) <= GLOBAL_CONNECTION_TOLERANCE_UNITS
            ):
                continue
            remember_connection(point, host_component, "middle", host_segment)


def find_nearest_segment_direction(point: list[float], segments: list[dict]) -> list[float] | None:
    best = None
    for segment in segments:
        direction = [segment["end"][index] - segment["start"][index] for index in range(3)]
        normalized = normalize_vector(direction)
        if not normalized:
            continue
        distance = point_to_segment_distance(point, segment["start"], segment["end"])
        if best is None or distance < best["distance"]:
            best = {"distance": distance, "direction": normalized}
    return best["direction"] if best else None


def normalize_vector(vector: list[float]) -> list[float] | None:
    length = sum(value * value for value in vector) ** 0.5
    if length <= 1e-6:
        return None
    return [value / length for value in vector]


def point_to_segment_distance(point: list[float], start: list[float], end: list[float]) -> float:
    ab = [end[index] - start[index] for index in range(3)]
    ap = [point[index] - start[index] for index in range(3)]
    length_sq = sum(value * value for value in ab)
    if length_sq <= 1e-9:
        return point_distance(point, start)
    t = max(0, min(1, sum(ap[index] * ab[index] for index in range(3)) / length_sq))
    projection = [start[index] + ab[index] * t for index in range(3)]
    return point_distance(point, projection)


def spatial_cell_key(point: list[float], cell_size: int = SPATIAL_INDEX_CELL_SIZE) -> tuple[int, int, int]:
    return tuple(math.floor(point[index] / cell_size) for index in range(3))


def build_segment_spatial_index(
    segments: list[dict],
    tolerance: float = GLOBAL_CONNECTION_TOLERANCE_UNITS,
    cell_size: int = SPATIAL_INDEX_CELL_SIZE,
) -> dict[tuple[int, int, int], list[dict]]:
    index = {}
    for segment in segments:
        start = segment.get("start")
        end = segment.get("end")
        if not start or not end:
            continue
        min_cell = spatial_cell_key([min(start[axis], end[axis]) - tolerance for axis in range(3)], cell_size)
        max_cell = spatial_cell_key([max(start[axis], end[axis]) + tolerance for axis in range(3)], cell_size)
        for x_cell in range(min_cell[0], max_cell[0] + 1):
            for y_cell in range(min_cell[1], max_cell[1] + 1):
                for z_cell in range(min_cell[2], max_cell[2] + 1):
                    index.setdefault((x_cell, y_cell, z_cell), []).append(segment)
    return index


def query_segment_spatial_index(
    index: dict[tuple[int, int, int], list[dict]],
    point: list[float],
    cell_size: int = SPATIAL_INDEX_CELL_SIZE,
) -> list[dict]:
    return index.get(spatial_cell_key(point, cell_size), [])


def calculate_node_specs(components: list[dict]) -> dict:
    outgoing = {}
    incoming = {}

    def add_spec(mapping, point, spec):
        if point is None or not spec or spec <= 0:
            return
        key = normalize_point_key(point)
        mapping.setdefault(key, []).append(spec)

    for component in components:
        start = component.get("start")
        end = component.get("end")
        if not start or not end or same_point(start, end):
            continue
        add_spec(outgoing, start, component.get("spec", 0))
        add_spec(incoming, end, component.get("spec", 0))

    node_specs = {}
    for key in set(outgoing) | set(incoming):
        if outgoing.get(key):
            node_specs[key] = max(outgoing[key])
        elif incoming.get(key):
            node_specs[key] = max(incoming[key])
    return node_specs


def get_connected_end_spec(connection_map: dict, node_specs: dict, component: dict, role: str) -> float:
    point = component.get("start") if role == "start" else component.get("end")
    if point is None:
        return component.get("spec", 0)
    point_key = normalize_point_key(point)
    node_spec = node_specs.get(point_key)
    if node_spec and node_spec > 0:
        return node_spec
    connection = connection_map.get(point_key)
    if not connection:
        return component.get("zerolineSpec") or component.get("spec", 0)
    for ref in connection["refs"]:
        ref_component = ref["component"]
        if (
            ref_component["id"] != component["id"]
            and ref_component.get("type") not in {"weld", "gasket", "equipment", "component"}
            and ref_component.get("spec")
        ):
            return ref_component["spec"]
    return component.get("zerolineSpec") or component.get("spec", 0)


def apply_reducer_end_specs(components: list[dict], connection_map: dict, node_specs: dict) -> None:
    for component in components:
        if component.get("type") != "reducer":
            continue
        component["startSpec"] = get_connected_end_spec(connection_map, node_specs, component, "start")
        component["endSpec"] = get_connected_end_spec(connection_map, node_specs, component, "end")
        if (
            component["startSpec"] == component["endSpec"]
            and component.get("zerolineSpec", 0) > 0
            and component["zerolineSpec"] != component.get("spec", 0)
        ):
            component["startSpec"] = component.get("spec", 0)
            component["endSpec"] = component["zerolineSpec"]
        component["startOuterDiameterMm"] = get_outer_diameter_mm(component["startSpec"])
        component["endOuterDiameterMm"] = get_outer_diameter_mm(component["endSpec"])


def should_create_symbol(component: dict) -> bool:
    if not component.get("start"):
        return False
    if component.get("type") == "olet-marker":
        return False
    custom_rendered_types = {"pipe", "elbow", "branch", "olet", "reducer", "cap", "flange", "valve", "teed-reducer", "teed-elbow", "angle-valve", "three-way-valve", "four-way-valve", "instrument", "misc-component", "trap", "filter"}
    if component.get("type") in custom_rendered_types and (component.get("end") or component.get("segments")):
        return False
    return component.get("type") != "component" or not component.get("end")


def is_connectable_for_auto_weld(component: dict) -> bool:
    return component and component.get("type") not in {"gasket", "weld", "equipment", "olet-marker"}


def is_collinear_pipe_connection(connectable_refs: list[dict]) -> bool:
    pipe_refs = [
        ref for ref in connectable_refs
        if ref["component"].get("type") == "pipe" and ref["component"].get("start") and ref["component"].get("end")
    ]
    if len(pipe_refs) < 2 or len(pipe_refs) != len(connectable_refs):
        return False
    first = pipe_refs[0]["component"]
    return all(are_mergeable_pipe_components(first, ref["component"]) for ref in pipe_refs[1:])


def are_mergeable_pipe_components(first: dict, second: dict) -> bool:
    if first.get("type") != "pipe" or second.get("type") != "pipe":
        return False
    if safe_float(first.get("spec")) != safe_float(second.get("spec")):
        return False
    if str(first.get("materialCode") or "") != str(second.get("materialCode") or ""):
        return False
    return pipes_are_parallel(first, second)


def get_connection_direction(ref: dict) -> list[float] | None:
    component = ref["component"]
    role = ref["role"]
    if component.get("type") == "reducer":
        return get_reducer_connection_direction(component)
    segments = component.get("segments") or []
    segment = ref.get("segment") or (segments[-1] if role == "end" and segments else segments[0] if role == "start" and segments else None)
    if segment:
        from_point = segment.get("start") if role == "end" else segment.get("end")
        to_point = segment.get("end") if role == "end" else segment.get("start")
    else:
        from_point = component.get("start") if role == "end" else component.get("end")
        to_point = component.get("end") if role == "end" else component.get("start")
    if not from_point or not to_point:
        return None
    vector = [to_point[index] - from_point[index] for index in range(3)]
    length = sum(value * value for value in vector) ** 0.5
    if length < 1e-6:
        return None
    return [value / length for value in vector]


def get_reducer_connection_direction(component: dict) -> list[float] | None:
    start = component.get("start")
    end = component.get("end")
    if not start or not end:
        return None
    delta = [end[index] - start[index] for index in range(3)]
    abs_delta = [abs(value) for value in delta]
    axis_index = abs_delta.index(max(abs_delta))
    direction = [0.0, 0.0, 0.0]
    direction[axis_index] = 1.0 if delta[axis_index] >= 0 else -1.0
    return direction


def get_connection_spec(ref: dict) -> float:
    component = ref["component"]
    role = ref["role"]
    if component.get("type") == "reducer":
        return component.get("startSpec" if role == "start" else "endSpec") or component.get("spec", 0)
    segments = component.get("segments") or []
    segment = ref.get("segment") or (segments[-1] if role == "end" and segments else segments[0] if role == "start" and segments else None)
    return segment.get("spec", component.get("spec", 0)) if segment else component.get("spec", 0)


def get_olet_weld_display(point: list[float], connectable_refs: list[dict], segments: list[dict]) -> dict | None:
    olet_ref = next((
        ref for ref in connectable_refs
        if ref["component"].get("type") == "olet" and ref.get("role") == "start"
    ), None)
    if not olet_ref or not point:
        return None
    olet_component = olet_ref["component"]
    if not olet_component.get("end"):
        return None
    direction = normalize_vector([
        olet_component["end"][index] - point[index] for index in range(3)
    ])
    if not direction:
        return None
    host_ref = next((ref for ref in connectable_refs if ref["component"].get("type") == "pipe"), None)
    host_spec = host_ref["component"].get("spec", 0) if host_ref else 0
    host_axis = get_connection_direction(host_ref) if host_ref else None
    if not host_spec and host_ref and host_ref.get("segment"):
        host_spec = host_ref["segment"].get("spec", 0)
    if not host_axis and host_ref and host_ref.get("segment"):
        segment = host_ref["segment"]
        host_axis = normalize_vector([
            segment["end"][index] - segment["start"][index] for index in range(3)
        ])
    if not host_spec:
        host_segment = next((
            segment for segment in segments
            if segment.get("type") == "pipe"
            and segment.get("start")
            and segment.get("end")
            and point_to_segment_distance(point, segment["start"], segment["end"]) <= 1e-3
        ), None)
        host_spec = host_segment.get("spec", 0) if host_segment else 0
        if host_segment:
            host_axis = normalize_vector([
                host_segment["end"][index] - host_segment["start"][index] for index in range(3)
            ])
    host_radius = get_outer_diameter_mm(host_spec) * IDF_UNITS_PER_MM / 2
    branch_spec = safe_float(olet_component.get("spec", 0))
    branch_od = get_outer_diameter_mm(branch_spec)
    branch_radius = branch_od * IDF_UNITS_PER_MM / 2
    curve_radius = max(branch_radius * 1.35, min(host_radius * 0.25, branch_radius * 1.8))
    if host_radius <= 0:
        return None
    display_start = [point[index] + direction[index] * host_radius for index in range(3)]
    return {
        "displayStart": display_start,
        "oletWeld": {
            "center": point,
            "hostAxis": host_axis,
            "branchAxis": direction,
            "hostRadius": host_radius,
            "branchRadius": branch_radius,
            "curveRadius": curve_radius,
        },
    }


def get_olet_display_start(point: list[float], connectable_refs: list[dict], segments: list[dict]) -> list[float] | None:
    display = get_olet_weld_display(point, connectable_refs, segments)
    return display.get("displayStart") if display else None


def get_ref_axis(ref: dict) -> list[float] | None:
    segment = ref.get("segment")
    if segment and segment.get("start") and segment.get("end"):
        return normalize_vector([
            segment["end"][index] - segment["start"][index]
            for index in range(3)
        ])
    component = ref["component"]
    if component.get("start") and component.get("end"):
        return normalize_vector([
            component["end"][index] - component["start"][index]
            for index in range(3)
        ])
    return None


def get_ref_direction_from_point(ref: dict, point: list[float]) -> list[float] | None:
    segment = ref.get("segment")
    start = segment.get("start") if segment else ref["component"].get("start")
    end = segment.get("end") if segment else ref["component"].get("end")
    if not start or not end:
        return None
    other = end if same_point(start, point) else start if same_point(end, point) else None
    if other is None:
        start_distance = point_distance(point, start)
        end_distance = point_distance(point, end)
        other = end if start_distance <= end_distance else start
    return normalize_vector([
        other[index] - point[index]
        for index in range(3)
    ])


def get_pipe_opening_weld_display(point: list[float], connectable_refs: list[dict]) -> dict | None:
    pipe_refs = [
        ref for ref in connectable_refs
        if ref["component"].get("type") == "pipe" and (ref.get("segment") or ref["component"].get("end"))
    ]
    if len(pipe_refs) < 2 or not point:
        return None
    ranked_refs = sorted(pipe_refs, key=lambda ref: get_connection_spec(ref) or 0, reverse=True)
    for host_ref in ranked_refs:
        host_axis = get_ref_axis(host_ref)
        host_spec = get_connection_spec(host_ref)
        host_radius = get_outer_diameter_mm(host_spec) * IDF_UNITS_PER_MM / 2
        if not host_axis or host_radius <= 0:
            continue
        for branch_ref in pipe_refs:
            if branch_ref is host_ref or branch_ref["component"]["id"] == host_ref["component"]["id"]:
                continue
            branch_axis = get_ref_direction_from_point(branch_ref, point)
            branch_spec = get_connection_spec(branch_ref)
            branch_radius = get_outer_diameter_mm(branch_spec) * IDF_UNITS_PER_MM / 2
            if not branch_axis or branch_radius <= 0:
                continue
            dot = abs(sum(host_axis[index] * branch_axis[index] for index in range(3)))
            if dot >= 0.85:
                continue
            display_start = [point[index] + branch_axis[index] * host_radius for index in range(3)]
            return {
                "displayStart": display_start,
                "branchRef": branch_ref,
                "oletWeld": {
                    "center": point,
                    "hostAxis": host_axis,
                    "branchAxis": branch_axis,
                    "hostRadius": host_radius,
                    "branchRadius": branch_radius,
                    "curveRadius": branch_radius,
                },
            }
    return None


def has_pipe_opening_connection(point: list[float], connectable_refs: list[dict]) -> bool:
    pipe_refs = [
        ref for ref in connectable_refs
        if ref["component"].get("type") == "pipe" and (ref.get("segment") or ref["component"].get("end"))
    ]
    if len(pipe_refs) < 2 or not point:
        return False
    for index, first_ref in enumerate(pipe_refs):
        first_axis = get_ref_axis(first_ref)
        if not first_axis:
            continue
        for second_ref in pipe_refs[index + 1:]:
            if second_ref["component"]["id"] == first_ref["component"]["id"]:
                continue
            second_axis = get_ref_axis(second_ref)
            if not second_axis:
                continue
            dot = abs(sum(first_axis[axis] * second_axis[axis] for axis in range(3)))
            if dot < 0.85:
                return True
    return False


def infer_component_weld_type(component: dict) -> str:
    skey = str(component.get("skey", "")).upper()
    if skey in PIPE_OPENING_WELD_SKEYS:
        return "seton"
    description_weld_type = infer_weld_type_from_material_description(component.get("materialDescription", ""))
    if description_weld_type:
        return description_weld_type
    if skey in EXPLICIT_SKEY_WELD_TYPES:
        return EXPLICIT_SKEY_WELD_TYPES[skey]
    suffix = skey[-2:] if len(skey) >= 2 else ""
    if suffix in END_CONDITION_WELD_TYPES:
        return END_CONDITION_WELD_TYPES[suffix]
    if skey in SLIP_ON_FLANGE_SKEYS:
        return "so"
    return "bw"


def infer_weld_type_from_material_description(description: str) -> str:
    tokens = [
        token.strip().upper()
        for token in MATERIAL_DESCRIPTION_WELD_TYPE_SEPARATORS_RE.split(str(description or ""))
        if token.strip()
    ]
    if not tokens:
        return ""
    matched_weld_types = []
    for token in tokens:
        weld_type = MATERIAL_DESCRIPTION_WELD_TYPE_TOKENS.get(token)
        if weld_type:
            matched_weld_types.append(weld_type)
    for phrase_tokens, weld_type in MATERIAL_DESCRIPTION_WELD_TYPE_PHRASES.items():
        phrase_length = len(phrase_tokens)
        for index in range(0, len(tokens) - phrase_length + 1):
            if tuple(tokens[index:index + phrase_length]) == phrase_tokens:
                matched_weld_types.append(weld_type)
    for weld_type in ("sw", "scw", "so", "bw"):
        if weld_type in matched_weld_types:
            return weld_type
    return ""


def infer_connection_weld_type(connectable_refs: list[dict], base_ref: dict, point: list[float] | None = None) -> str:
    if any(ref["component"].get("type") == "olet" and ref.get("role") == "start" for ref in connectable_refs):
        return "olet"
    if any(ref["component"].get("skey") in PIPE_OPENING_WELD_SKEYS for ref in connectable_refs):
        return "seton"
    if point and has_pipe_opening_connection(point, connectable_refs):
        return "seton"
    preferred_refs = [
        ref for ref in connectable_refs
        if ref["component"].get("type") == "olet" and ref.get("role") == "end"
    ]
    preferred_refs.extend(
        ref for ref in connectable_refs
        if ref["component"].get("type") not in {"pipe", "reducer"} and ref not in preferred_refs
    )
    preferred_refs.extend(ref for ref in connectable_refs if ref not in preferred_refs)
    candidates = [infer_component_weld_type(ref["component"]) for ref in preferred_refs]
    for weld_type in ("seton", "olet", "sw", "scw", "so"):
        if weld_type in candidates:
            return weld_type
    return next((weld_type for weld_type in candidates if weld_type), infer_component_weld_type(base_ref["component"]))


def create_auto_weld_components(connection_map: dict, segments: list[dict]) -> list[dict]:
    auto_welds = []
    for key, connection in connection_map.items():
        refs = connection["refs"]
        if any(ref["component"].get("type") == "gasket" for ref in refs):
            continue
        if any(ref["component"].get("type") == "weld" for ref in refs):
            continue
        connectable_refs = [ref for ref in refs if is_connectable_for_auto_weld(ref["component"])]
        component_ids = sorted({ref["component"]["id"] for ref in connectable_refs})
        if len(component_ids) < 2:
            continue
        if is_collinear_pipe_connection(connectable_refs):
            continue
        olet_start_refs = [
            ref for ref in connectable_refs
            if ref["component"].get("type") == "olet" and ref.get("role") == "start"
        ]
        if olet_start_refs:
            host_refs = [ref for ref in connectable_refs if ref["component"].get("type") == "pipe"]
            for olet_ref in olet_start_refs:
                local_refs = [olet_ref, *host_refs]
                local_component_ids = sorted({ref["component"]["id"] for ref in local_refs})
                if len(local_component_ids) < 2:
                    continue
                olet_display = get_olet_weld_display(connection["point"], local_refs, segments)
                weld_spec = get_connection_spec(olet_ref)
                auto_welds.append({
                    "id": f"auto-weld:{key}:olet:{olet_ref['component']['id']}",
                    "lineNumber": 0,
                    "identifier": 120,
                    "type": "weld",
                    "generated": True,
                    "start": connection["point"],
                    "displayStart": olet_display.get("displayStart") if olet_display else None,
                    "oletWeld": olet_display.get("oletWeld") if olet_display else None,
                    "end": None,
                    "spec": weld_spec,
                    "outerDiameterMm": get_outer_diameter_mm(weld_spec),
                    "materialIndex": 0,
                    "materialCode": "",
                    "materialDescription": "自动生成焊缝",
                    "skey": "WELD",
                    "weldType": "olet",
                    "connectedComponentIds": local_component_ids,
                    "direction": get_connection_direction(olet_ref),
                    "noMaterialFlag": False,
                    "pipeOpeningWeldNoMaterial": False,
                    "quantity": 1,
                    "raw": "",
                })
            continue
        base_ref = (
            next((ref for ref in connectable_refs if ref["component"].get("type") == "olet" and ref.get("role") == "start"), None)
            or next((ref for ref in connectable_refs if ref["component"].get("type") == "olet" and ref.get("role") == "end"), None)
            or next((ref for ref in connectable_refs if ref["component"].get("type") != "reducer"), None)
            or connectable_refs[0]
        )
        olet_display = get_olet_weld_display(connection["point"], connectable_refs, segments)
        opening_display = None if olet_display else get_pipe_opening_weld_display(connection["point"], connectable_refs)
        display_start = (
            olet_display.get("displayStart")
            if olet_display
            else opening_display.get("displayStart") if opening_display else None
        )
        weld_ref = opening_display.get("branchRef") if opening_display else base_ref
        weld_spec = get_connection_spec(weld_ref)
        weld_type = "seton" if opening_display else infer_connection_weld_type(connectable_refs, base_ref, connection["point"])
        auto_welds.append({
            "id": f"auto-weld:{key}",
            "lineNumber": 0,
            "identifier": 120,
            "type": "weld",
            "generated": True,
            "start": connection["point"],
            "displayStart": display_start,
            "oletWeld": olet_display.get("oletWeld") if olet_display else None,
            "end": None,
            "spec": weld_spec,
            "outerDiameterMm": get_outer_diameter_mm(weld_spec),
            "materialIndex": 0,
            "materialCode": "",
            "materialDescription": "自动生成焊缝",
            "skey": "WELD",
            "weldType": weld_type,
            "connectedComponentIds": component_ids,
            "direction": get_connection_direction(base_ref),
            "noMaterialFlag": False,
            "pipeOpeningWeldNoMaterial": False,
            "quantity": 1,
            "raw": "",
        })
    return auto_welds


def merge_models(models: list[dict], project_name: str) -> dict:
    nodes = {}
    materials = []
    components = []
    segments = []
    symbol_components = []
    pipelines = []
    drawing_split_markers = []

    for model in models:
        pipeline_id = get_model_pipeline_id(model, f"pipeline-{len(pipelines) + 1}")
        unit_name = model.get("unitName", "")
        pipelines.append({
            "pipelineId": pipeline_id,
            "unitName": unit_name,
            "pipelineName": model.get("pipelineName", "UNKNOWN"),
            "fileName": model.get("fileName", ""),
            "componentCount": len(model.get("components", [])),
            "segmentCount": len(model.get("segments", [])),
            "weldCount": sum(1 for component in model.get("components", []) if component.get("type") == "weld"),
            "drawingOptions": model.get("drawingOptions", {}),
            "drawingSplitMarkers": [
                {**marker, "unitName": unit_name, "pipelineId": pipeline_id, "pipelineName": model.get("pipelineName", "")}
                for marker in model.get("drawingSplitMarkers", [])
            ],
        })

        for material in model.get("materials", []):
            materials.append({**material, "unitName": unit_name, "pipelineId": pipeline_id})

        for component in model.get("components", []):
            components.append({
                **component,
                "unitName": component.get("unitName", unit_name),
                "pipelineId": pipeline_id,
                "pipelineName": model.get("pipelineName", ""),
            })

        for segment in model.get("segments", []):
            segments.append({**segment, "unitName": segment.get("unitName", unit_name), "pipelineId": pipeline_id})

        for component in model.get("symbolComponents", []):
            symbol_components.append({
                **component,
                "unitName": component.get("unitName", unit_name),
                "pipelineId": pipeline_id,
                "pipelineName": model.get("pipelineName", ""),
            })

        for node in model.get("nodes", []):
            key = normalize_point_key(node["point"])
            nodes.setdefault(key, {"id": key, "point": node["point"], "componentIds": []})
            nodes[key]["componentIds"].extend(node.get("componentIds", []))

        drawing_split_markers.extend(
            {**marker, "unitName": unit_name, "pipelineId": pipeline_id, "pipelineName": model.get("pipelineName", "")}
            for marker in model.get("drawingSplitMarkers", [])
        )

    existing_welds_by_key = {}
    for component in components:
        if component.get("type") == "weld" and component.get("start"):
            existing_welds_by_key.setdefault(normalize_global_connection_key(component["start"]), []).append(component)
    symbol_component_by_id = {
        component.get("id"): component
        for component in symbol_components
        if component.get("id")
    }
    global_connection_map = {}

    def remember_global_connection(point, component, role, segment=None):
        if point is None:
            return
        key = normalize_global_connection_key(point)
        global_connection_map.setdefault(key, {"point": point, "refs": []})
        global_connection_map[key]["refs"].append({"component": component, "role": role, "segment": segment})

    def get_global_external_segment_refs(component):
        component_segments = component.get("segments")
        if not component_segments and component.get("start") and component.get("end"):
            component_segments = [{
                "start": component["start"],
                "end": component["end"],
                "type": component["type"],
                "spec": component.get("spec", 0),
            }]
        return get_external_segment_refs_from_segments(component_segments, normalize_global_connection_key)

    for component in components:
        if component.get("type") == "weld":
            continue
        for ref in get_global_external_segment_refs(component):
            remember_global_connection(ref["point"], component, ref["role"], ref["segment"])

    register_pipe_opening_connections(components, segments, remember_global_connection)

    def find_existing_weld_for_auto_weld(key: str, auto_weld: dict) -> dict | None:
        candidates = existing_welds_by_key.get(key, [])
        auto_ids = set(auto_weld.get("connectedComponentIds") or [])
        if auto_ids:
            for candidate in candidates:
                candidate_ids = set(candidate.get("connectedComponentIds") or [])
                if candidate_ids and candidate_ids == auto_ids:
                    return candidate
        for candidate in candidates:
            if not candidate.get("connectedComponentIds") and not candidate.get("_autoConnectionMerged"):
                candidate["_autoConnectionMerged"] = True
                return candidate
        return None

    for component in create_auto_weld_components(global_connection_map, segments):
        key = normalize_global_connection_key(component["start"])
        existing_weld = find_existing_weld_for_auto_weld(key, component)
        if existing_weld:
            merged_ids = sorted(set(existing_weld.get("connectedComponentIds") or []) | set(component.get("connectedComponentIds") or []))
            if merged_ids == sorted(existing_weld.get("connectedComponentIds") or []):
                continue
            existing_weld["connectedComponentIds"] = merged_ids
            if component.get("weldType") == "seton":
                existing_weld["weldType"] = "seton"
                existing_weld["spec"] = component.get("spec")
                existing_weld["outerDiameterMm"] = component.get("outerDiameterMm")
                if component.get("displayStart"):
                    existing_weld["displayStart"] = component.get("displayStart")
                if component.get("direction"):
                    existing_weld["direction"] = component.get("direction")
            if component.get("oletWeld"):
                existing_weld["displayStart"] = component.get("displayStart")
                existing_weld["oletWeld"] = component.get("oletWeld")
                existing_weld["weldType"] = component.get("weldType")
                existing_weld["spec"] = component.get("spec")
                existing_weld["outerDiameterMm"] = component.get("outerDiameterMm")
                existing_weld["direction"] = component.get("direction")
            elif component.get("weldType") and component.get("weldType") != "bw" and existing_weld.get("weldType") in (None, "", "bw"):
                existing_weld["weldType"] = component.get("weldType")
                existing_weld["spec"] = component.get("spec")
                existing_weld["outerDiameterMm"] = component.get("outerDiameterMm")
                if component.get("direction"):
                    existing_weld["direction"] = component.get("direction")
            symbol_component = symbol_component_by_id.get(existing_weld.get("id"))
            if symbol_component:
                symbol_component.update({
                    "connectedComponentIds": existing_weld.get("connectedComponentIds"),
                    "displayStart": existing_weld.get("displayStart"),
                    "oletWeld": existing_weld.get("oletWeld"),
                    "weldType": existing_weld.get("weldType"),
                    "spec": existing_weld.get("spec"),
                    "outerDiameterMm": existing_weld.get("outerDiameterMm"),
                    "direction": existing_weld.get("direction"),
                })
            continue
        component["id"] = f"global-{component['id']}"
        component["pipelineId"] = "cross-idf-connections"
        component["pipelineName"] = project_name
        component["unitName"] = ""
        components.append(component)
        symbol_components.append(component)
        symbol_component_by_id[component["id"]] = component
        node_key = normalize_point_key(component["start"])
        nodes.setdefault(node_key, {"id": node_key, "point": component["start"], "componentIds": []})
        nodes[node_key]["componentIds"].append(component["id"])
        existing_welds_by_key.setdefault(key, []).append(component)

    for component in components:
        if component.get("type") == "weld":
            component.pop("_autoConnectionMerged", None)

    assign_weld_numbers(components, segments, default_pipeline_id=project_name, default_pipeline_name=project_name)
    weld_number_fields = {
        component["id"]: {
            "weldNo": component.get("weldNo"),
            "weldRawNo": component.get("weldRawNo"),
            "excludeFromWeldTable": component.get("excludeFromWeldTable"),
            "doubleNoMaterialFlagWeld": component.get("doubleNoMaterialFlagWeld"),
            "weldOwnerPipelineId": component.get("weldOwnerPipelineId"),
            "weldOwnerPipelineName": component.get("weldOwnerPipelineName"),
            "weldOwnerUnitName": component.get("weldOwnerUnitName"),
        }
        for component in components
        if component.get("type") == "weld"
    }
    for component in symbol_components:
        fields = weld_number_fields.get(component.get("id"))
        if fields:
            component.update(fields)

    return {
        "pipelineName": project_name,
        "fileName": f"{project_name}.json",
        "projectName": project_name,
        "sourceType": "idf-directory",
        "pipelineCount": len(pipelines),
        "pipelines": pipelines,
        "drawingOptionsByPipelineId": {
            (pipeline.get("pipelineId") or pipeline.get("fileName") or pipeline.get("pipelineName") or f"pipeline-{index + 1}"): pipeline.get("drawingOptions", {})
            for index, pipeline in enumerate(pipelines)
        },
        "materials": materials,
        "components": components,
        "units": "IDF coordinate units, rendered after automatic scaling",
        "nodes": list(nodes.values()),
        "segments": segments,
        "symbolComponents": symbol_components,
        "drawingSplitMarkers": drawing_split_markers,
    }


def component_unit(component: dict) -> str:
    return "米" if component.get("identifier") == 100 or component.get("type") == "pipe" else "个"


def component_spec_label(component: dict) -> str:
    material_spec_label = component.get("materialSpecLabel")
    if material_spec_label:
        return str(material_spec_label)
    display_spec = component.get("displaySpec")
    if display_spec:
        return str(display_spec)
    spec = component.get("spec", "")
    if isinstance(spec, float) and spec.is_integer():
        return str(int(spec))
    return str(spec or "")


def component_nps_label(component: dict) -> str:
    spec = component.get("spec", 0)
    normalized = round(safe_float(spec))
    if normalized <= 0:
        return ""
    value = PIPE_NPS_BY_DN.get(normalized)
    if value is None:
        if normalized > 2000:
            value = normalized / 25
        else:
            return ""
    return int(value) if float(value).is_integer() else value


def format_quantity(value) -> str:
    number = safe_float(value)
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f"{number:.6f}".rstrip("0").rstrip(".")


def material_quantity_for_table(component: dict) -> float:
    if component.get("noMaterialFlag"):
        return 0.0
    if component.get("pipeOpeningWeldNoMaterial"):
        return 0.0
    if component.get("localQuantity") is not None:
        return safe_float(component.get("localQuantity", 0))
    return safe_float(component.get("quantity", 0))


def material_quantity_for_weld_table(component: dict) -> float:
    if component.get("materialUniqueQuantity") is not None:
        return safe_float(component.get("materialUniqueQuantity"))
    return material_quantity_for_table(component)


def assign_material_unique_codes(model: dict) -> None:
    counters = {}
    material_fields_by_component_id = {}
    material_groups = {}
    for component in model.get("components", []):
        if not component.get("materialCode"):
            continue
        group_key = component.get("materialUniqueGroupId") or component.get("id")
        material_groups.setdefault(group_key, []).append(component)

    group_entries = []
    for group_key, components in material_groups.items():
        representative = min(components, key=lambda component: (
            component.get("unitName") or "",
            component.get("pipelineId") or "",
            component.get("pipelineName") or "",
            safe_float(component.get("materialIndex", 0)),
            get_component_sort_line(component),
            str(component.get("id", "")),
        ))
        group_entries.append((representative, components))

    group_entries.sort(key=lambda item: (
        item[0].get("unitName") or "",
        item[0].get("pipelineId") or "",
        item[0].get("pipelineName") or "",
        safe_float(item[0].get("materialIndex", 0)),
        get_component_sort_line(item[0]),
        str(item[0].get("id", "")),
    ))

    for representative, components in group_entries:
        material_code = str(representative.get("materialCode") or "").strip()
        if not material_code:
            continue
        counters[material_code] = counters.get(material_code, 0) + 1
        unique_code = f"{material_code}-{counters[material_code]}"
        group_quantity = round(sum(material_quantity_for_table(component) for component in components), 3)
        for component in components:
            component["materialUniqueCode"] = unique_code
            component["materialUniqueQuantity"] = component.get("materialUniqueQuantity", group_quantity)
            if component.get("id"):
                material_fields_by_component_id[component["id"]] = unique_code

    for component in model.get("symbolComponents", []):
        unique_code = material_fields_by_component_id.get(component.get("id"))
        if unique_code:
            component["materialUniqueCode"] = unique_code


def get_component_sort_line(component: dict) -> int:
    line_numbers = component.get("lineNumbers")
    if line_numbers:
        return min(int(value) for value in line_numbers)
    return int(component.get("lineNumber") or 0)


def build_material_table_rows(model: dict) -> list[dict]:
    grouped = {}
    for component in model.get("components", []):
        if component.get("type") in MATERIAL_TABLE_EXCLUDED_TYPES:
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
            component_spec_label(component),
            component.get("skey", ""),
            component_unit(component),
            bool(component.get("noMaterialFlag")),
            bool(component.get("pipeOpeningWeldNoMaterial")),
            component.get("materialIndex", 0),
        )
        entry = grouped.setdefault(key, {
            "单元号": unit_name,
            "管线号": pipeline_name,
            "材料描述": component.get("materialDescription", ""),
            "材料代码": component.get("materialCode", ""),
            "规格": component_spec_label(component),
            "record id": set(),
            "skey": component.get("skey", ""),
            "序号": component.get("materialIndex", 0),
            "数量": 0.0,
            "单位": component_unit(component),
            "不出料标识": "是" if component.get("noMaterialFlag") else "",
            "开口焊不计料": "是" if component.get("pipeOpeningWeldNoMaterial") else "",
            "_sortLine": get_component_sort_line(component),
        })
        for identifier in component.get("identifiers") or [component.get("identifier")]:
            if identifier is not None:
                entry["record id"].add(int(identifier))
        entry["数量"] += material_quantity_for_table(component)
        entry["_sortLine"] = min(entry["_sortLine"], get_component_sort_line(component))

    rows = []
    for entry in grouped.values():
        if abs(safe_float(entry["数量"])) <= 1e-9:
            continue
        rows.append({
            **{key: value for key, value in entry.items() if not key.startswith("_")},
            "record id": "/".join(str(identifier) for identifier in sorted(entry["record id"])),
            "数量": format_quantity(entry["数量"]),
        })
    return sorted(rows, key=lambda row: (
        row["单元号"],
        row["管线号"],
        safe_float(row["序号"]),
        row["材料描述"],
        row["材料代码"],
        row["规格"],
    ))


def get_weld_connected_material_components(weld: dict, component_by_id: dict) -> list[dict]:
    components = []
    for component_id in weld.get("connectedComponentIds") or []:
        component = component_by_id.get(component_id)
        if not component or component.get("type") in {"weld", "gasket", "equipment", "olet-marker"}:
            continue
        components.append(component)
    components.sort(key=lambda component: (
        1 if component.get("type") in {"olet", "branch", "teed-reducer", "teed-elbow"} else 0,
        get_component_sort_line(component),
        str(component.get("id", "")),
    ))
    return components[:2]


def infer_weld_row_type(weld: dict, materials: list[dict]) -> str:
    current_weld_type = str(weld.get("weldType") or "").lower()
    if weld.get("pipeSplitWeld"):
        return current_weld_type or "bw"
    if current_weld_type in {"olet", "seton"}:
        return current_weld_type
    material_weld_types = [
        infer_weld_type_from_material_description(component.get("materialDescription", ""))
        for component in materials
        if component
    ]
    if current_weld_type in {"", "bw"}:
        for weld_type in ("sw", "scw", "so"):
            if weld_type in material_weld_types:
                return weld_type
    return current_weld_type or next((weld_type for weld_type in material_weld_types if weld_type), "")


def build_weld_table_rows(model: dict) -> list[dict]:
    component_by_id = {component.get("id"): component for component in model.get("components", [])}
    rows = []
    for weld in model.get("components", []):
        if weld.get("type") != "weld":
            continue
        if weld.get("excludeFromWeldTable"):
            continue
        connected = get_weld_connected_material_components(weld, component_by_id)
        materials = connected + [{} for _ in range(max(0, 2 - len(connected)))]
        raw_weld_no = weld.get("weldRawNo", weld.get("weldNo", ""))
        weld_type = infer_weld_row_type(weld, materials[:2])
        row = {
            "单元号": weld.get("weldOwnerUnitName") or weld.get("unitName") or model.get("unitName", ""),
            "管线号": weld.get("weldOwnerPipelineName") or weld.get("pipelineName") or model.get("pipelineName", ""),
            "焊口号": format_weld_no_for_csv(weld.get("weldNo", raw_weld_no)),
            "_sortWeldNo": raw_weld_no,
            "公称直径": component_spec_label(weld),
            "寸径": component_nps_label(weld),
            "焊点坐标": format_weld_coordinate(weld),
            "焊接类型": weld_type,
        }
        for index, component in enumerate(materials[:2], start=1):
            row[f"材料描述{index}"] = component.get("materialDescription", "")
            row[f"材料代码{index}"] = component.get("materialCode", "")
            row[f"材料唯一码{index}"] = component.get("materialUniqueCode", "")
            row[f"数量{index}"] = format_quantity(material_quantity_for_weld_table(component)) if component else ""
            row[f"单位{index}"] = component_unit(component) if component else ""
        rows.append(row)
    return sorted(rows, key=lambda row: (
        row["单元号"],
        row["管线号"],
        weld_no_sort_value(row["_sortWeldNo"]),
        row["公称直径"],
    ))


def format_weld_coordinate(weld: dict) -> str:
    point = weld.get("start")
    if not point:
        return ""
    values = [
        round(safe_float(value) / IDF_UNITS_PER_MM, 2)
        for value in point[:3]
    ]
    return ",".join(f"{value:.2f}" for value in values)


def format_weld_no_for_csv(value) -> str:
    text = str(value or "")
    if re.match(r"^\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?-\d+|-\d+)$", text):
        return f" {text}"
    return text


def weld_no_sort_value(value) -> float:
    text = str(value or "")
    match = re.match(r"^(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)-(\d+)$", text)
    if match:
        left = safe_float(match.group(1))
        right = safe_float(match.group(2))
        index = safe_float(match.group(3))
        if right > left:
            return left + min(max(index, 1), 99) / 100
        return left + index / 100
    return safe_float(value)


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_derived_tables(model: dict, weld_table_output: Path | None, material_table_output: Path | None) -> None:
    if weld_table_output:
        weld_rows = build_weld_table_rows(model)
        write_csv(weld_table_output, weld_rows, [
            "单元号", "管线号", "焊口号", "公称直径", "寸径", "焊接类型",
            "材料描述1", "材料代码1", "材料唯一码1", "数量1", "单位1",
            "材料描述2", "材料代码2", "材料唯一码2", "数量2", "单位2", "焊点坐标",
        ])
        print(f"Wrote {weld_table_output} with {len(weld_rows)} weld rows")

    if material_table_output:
        material_rows = build_material_table_rows(model)
        write_csv(material_table_output, material_rows, [
            "单元号", "管线号", "材料描述", "材料代码", "规格", "record id", "skey", "序号", "数量", "单位", "不出料标识", "开口焊不计料",
        ])
        print(f"Wrote {material_table_output} with {len(material_rows)} material rows")


def find_idf_files(input_dir: Path) -> list[Path]:
    return sorted(
        [path for path in input_dir.rglob("*") if path.is_file() and path.suffix.lower() == ".idf"],
        key=lambda path: str(path).lower(),
    )


def preprocess_multi_pipeline_idfs(input_path: Path) -> list[dict]:
    if input_path.is_file():
        idf_files = [input_path] if input_path.suffix.lower() == ".idf" else []
        output_root = input_path.parent / "newidf"
    else:
        idf_files = find_idf_files(input_path)
        output_root = input_path / "newidf"
    results = []
    for idf_file in idf_files:
        split_result = split_multi_pipeline_idf(idf_file, output_root)
        if split_result:
            results.append(split_result)
    return results


def split_multi_pipeline_idf(path: Path, output_root: Path) -> dict | None:
    text = read_text(path)
    lines = text.splitlines()
    line_ending = detect_line_ending(text)
    pipeline_starts = [
        index for index, line in enumerate(lines)
        if line.lstrip().startswith("-6 ")
    ]
    if len(pipeline_starts) <= 1:
        return None
    last_999_index = find_last_terminal_999_index(lines)
    if last_999_index is None or last_999_index < pipeline_starts[-1]:
        raise SystemExit(f"Multi-pipeline IDF missing terminal 999 after last -6: {path}")

    header = lines[:pipeline_starts[0]]
    footer = lines[last_999_index + 1:]
    terminal_line = lines[last_999_index]
    unit_dir = output_root / safe_output_name(path.stem)
    unit_dir.mkdir(parents=True, exist_ok=True)
    written = []
    used_names = set()

    for index, start_line in enumerate(pipeline_starts):
        end_line = pipeline_starts[index + 1] if index + 1 < len(pipeline_starts) else last_999_index + 1
        body = lines[start_line:end_line]
        if not body:
            continue
        if not any(is_terminal_999_line(line) for line in body):
            body = [*body, terminal_line]
        pipeline_name = read_continuation_text(lines, start_line, "-6 ", 3) or f"{path.stem}-{index + 1}"
        file_stem = unique_split_file_stem(safe_output_name(pipeline_name), used_names)
        output_path = unit_dir / f"{file_stem}.idf"
        output_lines = [*header, *body, *footer]
        output_path.write_text(line_ending.join(output_lines) + line_ending, encoding="utf-8")
        written.append({
            "pipelineName": pipeline_name,
            "path": str(output_path),
        })

    return {
        "source": str(path),
        "outputDir": str(unit_dir),
        "pipelineCount": len(written),
        "files": written,
    }


def detect_line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def is_terminal_999_line(line: str) -> bool:
    stripped = line.lstrip()
    if not stripped.startswith("999"):
        return False
    return bool(re.match(r"^999(?:\s|,|$)", stripped))


def find_last_terminal_999_index(lines: list[str]) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        if is_terminal_999_line(lines[index]):
            return index
    return None


def unique_split_file_stem(base: str, used_names: set[str]) -> str:
    stem = base or "unnamed"
    candidate = stem
    suffix = 2
    while candidate.lower() in used_names:
        candidate = safe_output_name(f"{stem}_{suffix}")
        suffix += 1
    used_names.add(candidate.lower())
    return candidate


def print_split_preprocess_summary(results: list[dict]) -> None:
    total = sum(item.get("pipelineCount", 0) for item in results)
    print(f"检测到多管线合并 IDF，已执行拆分，本次不继续解析。拆分源文件 {len(results)} 个，生成管线 IDF {total} 个。")
    for result in results:
        print(f"- {result['source']} -> {result['outputDir']} ({result['pipelineCount']} files)")


def group_idf_files_by_unit(input_dir: Path) -> list[tuple[str, list[Path]]]:
    groups = {}
    for path in find_idf_files(input_dir):
        try:
            relative = path.relative_to(input_dir)
        except ValueError:
            relative = Path(path.name)
        unit_name = relative.parts[0] if len(relative.parts) > 1 else input_dir.name
        groups.setdefault(unit_name, []).append(path)
    return sorted(
        ((unit_name, sorted(paths, key=lambda item: str(item).lower())) for unit_name, paths in groups.items()),
        key=lambda item: item[0].lower(),
    )


def safe_output_name(value: str) -> str:
    name = re.sub(r"[^\w.\-()]+", "_", str(value or "").strip(), flags=re.UNICODE)
    name = re.sub(r"_+", "_", name).strip("._")
    return name[:120] or "unnamed"


def public_url_for_path(path: Path) -> str:
    parts = path.resolve().parts
    if "public" in parts:
        public_index = parts.index("public")
        return "/" + "/".join(parts[public_index + 1:]).replace("\\", "/")
    return path.as_posix()


def write_json(path: Path, model: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")


def prepare_project_models(models: list[dict], project_name: str) -> dict | None:
    apply_global_pipe_material_rules(models)
    assign_material_unique_codes({
        "components": [component for model in models for component in model.get("components", [])],
        "symbolComponents": [component for model in models for component in model.get("symbolComponents", [])],
    })
    global_model = merge_models(models, project_name)
    sync_global_welds_to_pipeline_models(global_model, models)
    return global_model


def apply_global_pipe_material_rules(models: list[dict]) -> None:
    groups = build_global_pipe_groups(models)
    replacements_by_component_id = {}
    split_welds_by_model_id = {}
    for group_index, group in enumerate(groups, start=1):
        group_id = f"pipe-group-{group_index}"
        build_pipe_group_material_sections(group, group_id, replacements_by_component_id, split_welds_by_model_id)

    if replacements_by_component_id:
        apply_pipe_replacements(models, replacements_by_component_id, split_welds_by_model_id)
    else:
        for model in models:
            if split_welds_by_model_id.get(id(model)):
                model.setdefault("components", []).extend(split_welds_by_model_id[id(model)])

    for model in models:
        rebuild_model_component_indexes(model)
        assign_weld_numbers(
            model.get("components", []),
            model.get("segments", []),
            default_pipeline_id=get_model_pipeline_id(model, "UNKNOWN"),
            default_pipeline_name=model.get("pipelineName") or model.get("fileName") or "UNKNOWN",
        )
        sync_symbol_weld_fields(model)


def build_global_pipe_groups(models: list[dict]) -> list[list[dict]]:
    entries = []
    endpoint_index = {}
    for model in models:
        for component in model.get("components", []):
            if component.get("type") != "pipe" or not component.get("start") or not component.get("end"):
                continue
            entry = {"model": model, "component": component}
            entries.append(entry)
            for point in (component.get("start"), component.get("end")):
                endpoint_index.setdefault(normalize_global_connection_key(point), []).append(entry)

    adjacency = {component_entry_key(entry): set() for entry in entries}
    entry_by_key = {component_entry_key(entry): entry for entry in entries}
    for shared_entries in endpoint_index.values():
        for first_index, first in enumerate(shared_entries):
            first_key = component_entry_key(first)
            for second in shared_entries[first_index + 1:]:
                second_key = component_entry_key(second)
                if first_key == second_key:
                    continue
                if are_mergeable_pipe_components(first["component"], second["component"]):
                    adjacency[first_key].add(second_key)
                    adjacency[second_key].add(first_key)

    groups = []
    visited = set()
    for entry_key in adjacency:
        if entry_key in visited:
            continue
        queue = [entry_key]
        visited.add(entry_key)
        group = []
        while queue:
            current_key = queue.pop(0)
            group.append(entry_by_key[current_key])
            for neighbor_key in adjacency[current_key]:
                if neighbor_key not in visited:
                    visited.add(neighbor_key)
                    queue.append(neighbor_key)
        groups.append(group)
    return groups


def component_entry_key(entry: dict) -> str:
    return f"{id(entry['model'])}:{entry['component'].get('id')}"


def calculate_pipe_split_distances(total_length_m: float, rule: str) -> list[float]:
    max_length = CURRENT_PARSE_OPTIONS.pipe_split_lengths_m.get(str(rule or "").upper())
    min_remainder = CURRENT_PARSE_OPTIONS.pipe_split_min_remainder_m
    if not max_length or total_length_m <= max_length + min_remainder - 1e-9:
        return []
    cut_count = int(total_length_m // max_length)
    remainder = total_length_m - cut_count * max_length
    if 0 < remainder < min_remainder - 1e-9:
        cut_count -= 1
    return [
        round(index * max_length, 6)
        for index in range(1, max(cut_count, 0) + 1)
        if index * max_length < total_length_m - 1e-9
    ]


def build_pipe_group_material_sections(
    group: list[dict],
    group_id: str,
    replacements_by_component_id: dict,
    split_welds_by_model_id: dict,
) -> None:
    representative = group[0]["component"]
    axis = pipe_direction(representative)
    if not axis:
        return
    origin = representative["start"]
    projections = [
        project_point_on_axis(point, origin, axis)
        for entry in group
        for point in (entry["component"].get("start"), entry["component"].get("end"))
        if point
    ]
    if len(projections) < 2:
        return
    min_t = min(projections)
    max_t = max(projections)
    total_length_m = max(0.0, (max_t - min_t) / IDF_UNITS_PER_OFFSET_METER)
    split_rule = get_pipe_split_rule(representative.get("materialCode", ""))
    cut_distances_m = calculate_pipe_split_distances(total_length_m, split_rule)
    cut_positions = [min_t + distance * IDF_UNITS_PER_OFFSET_METER for distance in cut_distances_m]

    if not cut_positions:
        group_quantity = round((max_t - min_t) / IDF_UNITS_PER_OFFSET_METER, 3)
        allocate_non_overlapping_pipe_quantities([entry["component"] for entry in group], origin, axis)
        for entry in group:
            component = entry["component"]
            component["quantity"] = calculate_component_quantity(component)
            component["pipeGroupId"] = group_id
            component["pipeGroupSplitRule"] = split_rule
            component["materialUniqueGroupId"] = group_id
            component["materialUniqueQuantity"] = group_quantity
        return

    section_bounds = [min_t, *cut_positions, max_t]
    parts_by_section_group = {}
    cut_part_ids_by_position = {round(cut_t, 3): [] for cut_t in cut_positions}
    cut_owner_model_by_position = {}

    for entry in group:
        component = entry["component"]
        start_t = project_point_on_axis(component["start"], origin, axis)
        end_t = project_point_on_axis(component["end"], origin, axis)
        forward = end_t >= start_t
        low_t, high_t = sorted([start_t, end_t])
        inner_cuts = [
            cut_t for cut_t in cut_positions
            if low_t + 1e-3 < cut_t < high_t - 1e-3
        ]
        ordered_positions = [start_t]
        ordered_positions.extend(sorted(inner_cuts, reverse=not forward))
        ordered_positions.append(end_t)

        parts = []
        for part_index in range(len(ordered_positions) - 1):
            part_start_t = ordered_positions[part_index]
            part_end_t = ordered_positions[part_index + 1]
            if abs(part_end_t - part_start_t) < 1e-3:
                continue
            midpoint_t = (part_start_t + part_end_t) / 2
            section_index = find_pipe_section_index(midpoint_t, section_bounds)
            section_group_id = f"{group_id}-section-{section_index + 1}"
            part = create_pipe_part_component(
                component,
                part_index + 1,
                point_along_axis(origin, axis, part_start_t),
                point_along_axis(origin, axis, part_end_t),
                group_id,
                section_group_id,
                section_index + 1,
                split_rule,
            )
            parts.append(part)
            parts_by_section_group.setdefault(section_group_id, []).append(part)
            for boundary_t in (part_start_t, part_end_t):
                boundary_key = round(boundary_t, 3)
                if boundary_key in cut_part_ids_by_position:
                    cut_part_ids_by_position[boundary_key].append(part["id"])
                    cut_owner_model_by_position.setdefault(boundary_key, entry["model"])
        if parts:
            replacements_by_component_id[component["id"]] = parts

    for section_group_id, parts in parts_by_section_group.items():
        section_index = int(parts[0].get("pipeGroupSectionIndex", 1)) - 1 if parts else 0
        section_quantity = round((section_bounds[section_index + 1] - section_bounds[section_index]) / IDF_UNITS_PER_OFFSET_METER, 3)
        allocate_non_overlapping_pipe_quantities(parts, origin, axis)
        for part in parts:
            part["quantity"] = calculate_component_quantity(part)
            part["materialUniqueQuantity"] = section_quantity

    for cut_index, cut_t in enumerate(cut_positions, start=1):
        cut_key = round(cut_t, 3)
        connected_ids = sorted(set(cut_part_ids_by_position.get(cut_key, [])))
        if len(connected_ids) < 2:
            continue
        owner_model = cut_owner_model_by_position.get(cut_key) or group[0]["model"]
        weld = create_pipe_split_weld(
            group_id,
            cut_index,
            point_along_axis(origin, axis, cut_t),
            axis,
            representative,
            connected_ids,
        )
        split_welds_by_model_id.setdefault(id(owner_model), []).append(weld)


def find_pipe_section_index(midpoint_t: float, section_bounds: list[float]) -> int:
    for index in range(len(section_bounds) - 1):
        if section_bounds[index] - 1e-6 <= midpoint_t <= section_bounds[index + 1] + 1e-6:
            return index
    return max(0, len(section_bounds) - 2)


def create_pipe_part_component(
    component: dict,
    part_index: int,
    start: list[float],
    end: list[float],
    group_id: str,
    section_group_id: str,
    section_index: int,
    split_rule: str,
) -> dict:
    part = copy.deepcopy(component)
    part["id"] = f"{component['id']}-part-{part_index}"
    part["start"] = start
    part["end"] = end
    part["pipeGroupId"] = group_id
    part["pipeGroupSplitRule"] = split_rule
    part["pipeGroupSectionIndex"] = section_index
    part["materialUniqueGroupId"] = section_group_id
    part["sourcePipeComponentId"] = component["id"]
    part["quantity"] = calculate_component_quantity(part)
    part["localQuantity"] = part["quantity"]
    part["segments"] = [{
        "start": start,
        "end": end,
        "identifier": part.get("identifier", 100),
        "type": "pipe",
        "spec": part.get("spec", 0),
        "outerDiameterMm": part.get("outerDiameterMm", 0),
    }]
    return part


def allocate_non_overlapping_pipe_quantities(parts: list[dict], origin: list[float], axis: list[float]) -> None:
    intervals = []
    for part in parts:
        if not part.get("start") or not part.get("end"):
            part["localQuantity"] = part.get("quantity", 0)
            continue
        start_t = project_point_on_axis(part["start"], origin, axis)
        end_t = project_point_on_axis(part["end"], origin, axis)
        low_t, high_t = sorted([start_t, end_t])
        intervals.append({
            "part": part,
            "low": low_t,
            "high": high_t,
            "line": get_component_sort_line(part),
            "id": str(part.get("id", "")),
        })
    intervals.sort(key=lambda item: (item["low"], item["high"], item["line"], item["id"]))
    covered: list[tuple[float, float]] = []
    for item in intervals:
        uncovered = calculate_uncovered_interval_length(item["low"], item["high"], covered)
        item["part"]["localQuantity"] = round(uncovered / IDF_UNITS_PER_OFFSET_METER, 3)
        covered = add_covered_interval(covered, item["low"], item["high"])


def calculate_uncovered_interval_length(low: float, high: float, covered: list[tuple[float, float]]) -> float:
    cursor = low
    total = 0.0
    for cover_low, cover_high in covered:
        if cover_high <= cursor + 1e-6:
            continue
        if cover_low >= high - 1e-6:
            break
        if cover_low > cursor:
            total += max(0.0, min(cover_low, high) - cursor)
        cursor = max(cursor, cover_high)
        if cursor >= high - 1e-6:
            break
    if cursor < high:
        total += high - cursor
    return max(0.0, total)


def add_covered_interval(covered: list[tuple[float, float]], low: float, high: float) -> list[tuple[float, float]]:
    intervals = sorted([*covered, (low, high)], key=lambda item: item[0])
    merged = []
    for current_low, current_high in intervals:
        if not merged or current_low > merged[-1][1] + 1e-6:
            merged.append([current_low, current_high])
        else:
            merged[-1][1] = max(merged[-1][1], current_high)
    return [(item[0], item[1]) for item in merged]


def create_pipe_split_weld(
    group_id: str,
    split_index: int,
    point: list[float],
    axis: list[float],
    representative: dict,
    connected_ids: list[str],
) -> dict:
    return {
        "id": f"pipe-split-weld:{group_id}:{split_index}",
        "lineNumber": 0,
        "identifier": 120,
        "type": "weld",
        "generated": True,
        "pipeSplitWeld": True,
        "pipeGroupId": group_id,
        "pipeSplitIndex": split_index,
        "start": point,
        "end": None,
        "spec": representative.get("spec", 0),
        "outerDiameterMm": representative.get("outerDiameterMm", 0),
        "materialIndex": 0,
        "materialCode": "",
        "materialDescription": "管子分段自动生成焊缝",
        "skey": "WELD",
        "weldType": "bw",
        "connectedComponentIds": connected_ids,
        "direction": axis,
        "noMaterialFlag": False,
        "pipeOpeningWeldNoMaterial": False,
        "quantity": 1,
        "raw": "",
    }


def apply_pipe_replacements(models: list[dict], replacements_by_component_id: dict, split_welds_by_model_id: dict) -> None:
    for model in models:
        new_components = []
        for component in model.get("components", []):
            if component.get("id") in replacements_by_component_id:
                new_components.extend(replacements_by_component_id[component["id"]])
                continue
            if component.get("type") == "weld":
                remap_weld_connected_pipe_parts(component, replacements_by_component_id)
            new_components.append(component)
        new_components.extend(split_welds_by_model_id.get(id(model), []))
        model["components"] = new_components


def remap_weld_connected_pipe_parts(weld: dict, replacements_by_component_id: dict) -> None:
    updated_ids = []
    for component_id in weld.get("connectedComponentIds") or []:
        replacements = replacements_by_component_id.get(component_id)
        if not replacements:
            updated_ids.append(component_id)
            continue
        replacement = select_pipe_part_for_point(replacements, weld.get("start"))
        if replacement:
            updated_ids.append(replacement["id"])
        else:
            updated_ids.extend(part["id"] for part in replacements)
    weld["connectedComponentIds"] = sorted(set(updated_ids))


def select_pipe_part_for_point(parts: list[dict], point: list[float] | None) -> dict | None:
    if not point:
        return parts[0] if parts else None
    ranked = []
    for part in parts:
        start = part.get("start")
        end = part.get("end")
        if not start or not end:
            continue
        distance = point_to_segment_distance(point, start, end)
        parameter = point_segment_parameter(point, start, end)
        endpoint_rank = 0 if same_point(point, start) or same_point(point, end) else 1
        inside_rank = 0 if parameter is not None and -1e-6 <= parameter <= 1 + 1e-6 else 1
        ranked.append((endpoint_rank, inside_rank, distance, part))
    if not ranked:
        return parts[0] if parts else None
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return ranked[0][3]


def rebuild_model_component_indexes(model: dict) -> None:
    ensure_unique_component_ids(model)
    nodes = {}
    segments = []
    symbol_components = []

    def add_node(point, component_id):
        if point is None:
            return
        key = normalize_point_key(point)
        nodes.setdefault(key, {"id": key, "point": point, "componentIds": []})
        nodes[key]["componentIds"].append(component_id)

    for component in model.get("components", []):
        component_segments = [] if component.get("type") in {"weld", "olet-marker"} else component.get("segments")
        if component.get("type") not in {"weld", "olet-marker"} and not component_segments and component.get("start") and component.get("end"):
            component_segments = [{
                "start": component["start"],
                "end": component["end"],
                "type": component["type"],
                "spec": component.get("spec", 0),
                "outerDiameterMm": component.get("outerDiameterMm", 0),
            }]
        for segment_index, segment in enumerate(component_segments or [], start=1):
            add_node(segment.get("start"), component["id"])
            add_node(segment.get("end"), component["id"])
            segments.append({
                "id": f"{component['id']}:{segment_index}",
                "componentId": component["id"],
                "start": segment["start"],
                "end": segment["end"],
                "type": segment.get("type", component["type"]),
                "spec": segment.get("spec", component.get("spec", 0)),
                "outerDiameterMm": segment.get("outerDiameterMm", component.get("outerDiameterMm", 0)),
                "skey": component.get("skey", ""),
            })
        if component.get("type") == "weld":
            add_node(component.get("start"), component["id"])
        if should_create_symbol(component):
            symbol_components.append(component)

    model["nodes"] = list(nodes.values())
    model["segments"] = segments
    model["symbolComponents"] = symbol_components


def ensure_unique_component_ids(model: dict) -> None:
    seen = {}
    for component in model.get("components", []):
        component_id = component.get("id")
        if not component_id:
            continue
        count = seen.get(component_id, 0) + 1
        seen[component_id] = count
        if count <= 1:
            continue
        component["id"] = f"{component_id}#dup{count}"


def sync_symbol_weld_fields(model: dict) -> None:
    weld_fields = {
        component.get("id"): {
            "weldNo": component.get("weldNo"),
            "weldRawNo": component.get("weldRawNo"),
            "excludeFromWeldTable": component.get("excludeFromWeldTable"),
            "doubleNoMaterialFlagWeld": component.get("doubleNoMaterialFlagWeld"),
            "weldOwnerPipelineId": component.get("weldOwnerPipelineId"),
            "weldOwnerPipelineName": component.get("weldOwnerPipelineName"),
            "weldOwnerUnitName": component.get("weldOwnerUnitName"),
            "connectedComponentIds": component.get("connectedComponentIds"),
            "weldType": component.get("weldType"),
            "spec": component.get("spec"),
            "outerDiameterMm": component.get("outerDiameterMm"),
            "direction": component.get("direction"),
            "displayStart": component.get("displayStart"),
            "oletWeld": component.get("oletWeld"),
            "pipeSplitWeld": component.get("pipeSplitWeld"),
            "pipeGroupId": component.get("pipeGroupId"),
            "pipeSplitIndex": component.get("pipeSplitIndex"),
        }
        for component in model.get("components", [])
        if component.get("type") == "weld"
    }
    for component in model.get("symbolComponents", []):
        fields = weld_fields.get(component.get("id"))
        if fields:
            component.update(fields)


def sync_global_welds_to_pipeline_models(global_model: dict, models: list[dict]) -> None:
    component_model_by_id = {
        component.get("id"): model
        for model in models
        for component in model.get("components", [])
        if component.get("id")
    }
    model_by_pipeline_id = {}
    for model in models:
        for key in (model.get("pipelineId"), model.get("fileName"), model.get("pipelineName")):
            if key:
                model_by_pipeline_id[key] = model
    source_weld_by_id = {
        component.get("id"): component
        for model in models
        for component in model.get("components", [])
        if component.get("type") == "weld" and component.get("id")
    }

    for weld in [component for component in global_model.get("components", []) if component.get("type") == "weld"]:
        existing = source_weld_by_id.get(weld.get("id"))
        if existing:
            existing.update(extract_weld_sync_fields(weld))
            continue
        target_model = model_by_pipeline_id.get(weld.get("weldOwnerPipelineId"))
        if target_model is None:
            target_model = next((
                component_model_by_id.get(component_id)
                for component_id in weld.get("connectedComponentIds") or []
                if component_model_by_id.get(component_id)
            ), None)
        if target_model is None:
            continue
        copied = copy.deepcopy(weld)
        copied["unitName"] = target_model.get("unitName", "")
        copied["pipelineId"] = get_model_pipeline_id(target_model, "")
        copied["pipelineName"] = target_model.get("pipelineName", "")
        target_model.setdefault("components", []).append(copied)
        source_weld_by_id[copied["id"]] = copied

    for model in models:
        rebuild_model_component_indexes(model)
        sync_symbol_weld_fields(model)


def extract_weld_sync_fields(weld: dict) -> dict:
    keys = [
        "connectedComponentIds", "weldType", "spec", "outerDiameterMm", "displayStart", "oletWeld",
        "direction", "weldNo", "weldOwnerPipelineId", "weldOwnerPipelineName",
        "weldOwnerUnitName", "weldRawNo", "excludeFromWeldTable", "doubleNoMaterialFlagWeld",
        "pipeSplitWeld", "pipeGroupId", "pipeSplitIndex",
    ]
    return {key: copy.deepcopy(weld.get(key)) for key in keys if key in weld}


def sync_weld_fields_from_global_model(model: dict, global_model: dict | None) -> None:
    if not global_model:
        return
    global_weld_by_id = {
        component.get("id"): component
        for component in global_model.get("components", [])
        if component.get("type") == "weld" and component.get("id")
    }
    for component in model.get("components", []):
        global_weld = global_weld_by_id.get(component.get("id"))
        if global_weld:
            component.update(extract_weld_sync_fields(global_weld))
    sync_symbol_weld_fields(model)


def write_unit_outputs(
    input_dir: Path,
    output_dir: Path,
    manifest_output: Path,
    project_name: str,
    weld_table_output: Path | None = None,
    material_table_output: Path | None = None,
) -> dict:
    grouped_files = group_idf_files_by_unit(input_dir)
    parsed_units = []
    all_pipeline_models = []
    for unit_name, idf_files in grouped_files:
        pipeline_models = [apply_model_unit_context(parse_idf(path), unit_name) for path in idf_files]
        parsed_units.append({"unitName": unit_name, "files": idf_files, "models": pipeline_models})
        all_pipeline_models.extend(pipeline_models)

    global_model = prepare_project_models(all_pipeline_models, project_name)
    write_derived_tables(global_model, weld_table_output, material_table_output)

    clear_unit_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "projectName": project_name,
        "sourceType": "idf-unit-directory",
        "unitCount": len(parsed_units),
        "pipelineCount": sum(len(unit["models"]) for unit in parsed_units),
        "units": [],
    }

    for unit in parsed_units:
        unit_name = unit["unitName"]
        unit_dir = output_dir / safe_output_name(unit_name)
        pipeline_dir = unit_dir / "pipelines"
        unit_model = merge_models(unit["models"], f"{project_name} {unit_name}")
        unit_model["unitName"] = unit_name
        sync_weld_fields_from_global_model(unit_model, global_model)
        unit_model_path = unit_dir / "unit.json"
        write_json(unit_model_path, unit_model)

        unit_entry = {
            "unitName": unit_name,
            "modelUrl": public_url_for_path(unit_model_path),
            "pipelineCount": len(unit["models"]),
            "componentCount": len(unit_model.get("components", [])),
            "weldCount": sum(1 for component in unit_model.get("components", []) if component.get("type") == "weld"),
            "pipelines": [],
        }
        for index, model in enumerate(unit["models"], start=1):
            pipeline_file = pipeline_dir / f"{index:04d}-{safe_output_name(model.get('pipelineName') or model.get('fileName'))}.json"
            sync_weld_fields_from_global_model(model, global_model)
            write_json(pipeline_file, model)
            unit_entry["pipelines"].append({
                "pipelineId": get_model_pipeline_id(model, ""),
                "unitName": unit_name,
                "pipelineName": model.get("pipelineName", ""),
                "fileName": model.get("fileName", ""),
                "modelUrl": public_url_for_path(pipeline_file),
                "componentCount": len(model.get("components", [])),
                "weldCount": sum(1 for component in model.get("components", []) if component.get("type") == "weld"),
            })
        manifest["units"].append(unit_entry)

    write_json(manifest_output, manifest)
    return manifest


def clear_unit_output_dir(output_dir: Path) -> None:
    if not output_dir.exists():
        return
    if output_dir.name != safe_output_name(output_dir.name) or not output_dir.name.endswith("-units"):
        raise ValueError(f"Refuse to clear unsafe unit output directory: {output_dir}")
    for child in output_dir.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("value must be greater than 0")
    return number


def non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("value must be greater than or equal to 0")
    return number


def build_parser_options(args) -> ParserOptions:
    return ParserOptions(
        pipe_split_lengths_m={
            "CS": args.cs_split_length,
            "SS": args.ss_split_length,
        },
        pipe_split_min_remainder_m=args.pipe_split_min_remainder,
        weld_prefix_by_type={
            "bw": args.weld_prefix_bw or "",
            "sw": args.weld_prefix_sw or "",
            "scw": args.weld_prefix_scw or "",
            "olet": args.weld_prefix_olet or "",
            "seton": args.weld_prefix_seton or "",
        },
        shop_weld_suffix=args.shop_weld_suffix or "",
        field_weld_suffix=args.field_weld_suffix or "",
        weld_type_number_mode=args.weld_type_number_mode,
        pipe_split_number_mode=args.pipe_split_number_mode,
    )


def set_current_parse_options(options: ParserOptions) -> None:
    global CURRENT_PARSE_OPTIONS
    CURRENT_PARSE_OPTIONS = options


def main():
    parser = argparse.ArgumentParser(description="Parse IDF centerline data to viewer JSON.")
    parser.add_argument("--input", help="Input .idf file path")
    parser.add_argument("--input-dir", help="Input directory containing .idf files")
    parser.add_argument("--output", help="Output JSON file path")
    parser.add_argument("--project-name", default="IDF Batch Model", help="Project name for --input-dir output")
    parser.add_argument("--weld-table-output", help="Output weld CSV path")
    parser.add_argument("--material-table-output", help="Output material CSV path")
    parser.add_argument("--unit-output-dir", help="Output directory for unit and pipeline JSON files")
    parser.add_argument("--manifest-output", help="Output manifest JSON path for unit mode")
    parser.add_argument("--no-derived-tables", action="store_true", help="Do not write weld/material CSV tables")
    parser.add_argument("--skip-multi-pipeline-split", action="store_true", help="Skip multi-pipeline IDF split preprocessing")
    parser.add_argument("--cs-split-length", type=positive_float, default=DEFAULT_PIPE_SPLIT_LENGTH_BY_RULE_M["CS"], help="Carbon steel pipe split length in meters")
    parser.add_argument("--ss-split-length", type=positive_float, default=DEFAULT_PIPE_SPLIT_LENGTH_BY_RULE_M["SS"], help="Stainless steel pipe split length in meters")
    parser.add_argument("--pipe-split-min-remainder", type=non_negative_float, default=DEFAULT_PIPE_SPLIT_MIN_REMAINDER_M, help="Minimum pipe split remainder length in meters")
    parser.add_argument("--weld-prefix-bw", default="", help="Weld number prefix for butt welds")
    parser.add_argument("--weld-prefix-sw", default="", help="Weld number prefix for socket welds")
    parser.add_argument("--weld-prefix-scw", default="", help="Weld number prefix for screwed welds")
    parser.add_argument("--weld-prefix-olet", default="", help="Weld number prefix for branch/olet welds")
    parser.add_argument("--weld-prefix-seton", default="", help="Weld number prefix for set-on/opening welds")
    parser.add_argument("--shop-weld-suffix", default="S", help="Configured suffix for shop welds; applied only when weldLocation is available")
    parser.add_argument("--field-weld-suffix", default="F", help="Configured suffix for field welds; applied only when weldLocation is available")
    parser.add_argument("--weld-type-number-mode", choices=[WELD_TYPE_NUMBER_MODE_ALL, WELD_TYPE_NUMBER_MODE_CONFIGURED, WELD_TYPE_NUMBER_MODE_NONE], default=WELD_TYPE_NUMBER_MODE_CONFIGURED, help="Whether weld numbers use per-type counters")
    parser.add_argument("--pipe-split-number-mode", choices=[PIPE_SPLIT_NUMBER_MODE_PIPE_END, PIPE_SPLIT_NUMBER_MODE_SEQUENCE], default=PIPE_SPLIT_NUMBER_MODE_PIPE_END, help="Pipe split weld numbering mode")
    args = parser.parse_args()
    set_current_parse_options(build_parser_options(args))

    output_path = Path(args.output) if args.output else None
    weld_table_output = Path(args.weld_table_output) if args.weld_table_output else (
        output_path.with_name(f"{output_path.stem}-weld-table.csv") if output_path else None
    )
    material_table_output = Path(args.material_table_output) if args.material_table_output else (
        output_path.with_name(f"{output_path.stem}-material-table.csv") if output_path else None
    )

    if args.input_dir:
        input_dir = Path(args.input_dir)
        if not args.skip_multi_pipeline_split:
            split_results = preprocess_multi_pipeline_idfs(input_dir)
            if split_results:
                print_split_preprocess_summary(split_results)
                return
        idf_files = find_idf_files(input_dir)
        if not idf_files:
            raise SystemExit(f"No .idf files found in {input_dir}")

        if args.unit_output_dir:
            unit_output_dir = Path(args.unit_output_dir)
            manifest_output = Path(args.manifest_output) if args.manifest_output else unit_output_dir.with_name(
                f"{unit_output_dir.name}-manifest.json"
            )
            manifest = write_unit_outputs(
                input_dir,
                unit_output_dir,
                manifest_output,
                args.project_name,
                None if args.no_derived_tables else weld_table_output,
                None if args.no_derived_tables else material_table_output,
            )
            print(
                f"Wrote {manifest_output} with {manifest['unitCount']} units, "
                f"{manifest['pipelineCount']} pipelines"
            )
            if output_path is None:
                return

        output_path.parent.mkdir(parents=True, exist_ok=True)
        models = [
            apply_model_unit_context(parse_idf(path), unit_name)
            for unit_name, unit_files in group_idf_files_by_unit(input_dir)
            for path in unit_files
        ]
        apply_global_pipe_material_rules(models)
        model = merge_models(models, args.project_name)
        assign_material_unique_codes(model)
        output_path.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"Wrote {output_path} with {len(model['pipelines'])} pipelines, "
            f"{len(model['components'])} components"
        )
        if not args.no_derived_tables:
            write_derived_tables(model, weld_table_output, material_table_output)
        return

    if not args.input:
        raise SystemExit("--input or --input-dir is required")
    if output_path is None:
        raise SystemExit("--output is required when parsing a single IDF file")

    input_path = Path(args.input)
    if not args.skip_multi_pipeline_split:
        split_results = preprocess_multi_pipeline_idfs(input_path)
        if split_results:
            print_split_preprocess_summary(split_results)
            return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    model = parse_idf(input_path)
    assign_material_unique_codes(model)
    output_path.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {output_path} with {len(model['components'])} components")
    if not args.no_derived_tables:
        write_derived_tables(model, weld_table_output, material_table_output)


if __name__ == "__main__":
    main()
