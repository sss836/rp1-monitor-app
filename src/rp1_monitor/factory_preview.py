"""Read-only original factory CSV preview. No device, database, upload or edits."""

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from .factory_bridge import CSV_COLUMNS, read_result_csv, source_id
from .platform_client import NativePlatformError

SIMULATED = re.compile(r"simulated|simulation|数据来源[：:]\s*模拟", re.IGNORECASE)


def preview_result_csv(path):
    path, raw, values = read_result_csv(path)
    rules = json.loads((Path(__file__).parent / "data/factory-report-rules.json").read_text(encoding="utf-8"))
    if [column["title"] for column in rules["columns"]] != list(CSV_COLUMNS):
        raise NativePlatformError("factory_csv_definition_mismatch")
    definitions = [row for row in rules["rows"] if row["level"] == "项目"]
    by_case = {row["case_code"]: row for row in definitions}
    children = {(row["case_code"], row["source_id"]) for row in rules["rows"] if row["level"] == "子项"}
    source = source_id(values[0][1])
    if any(row[1] != source for row in values):
        raise NativePlatformError("csv_source_or_record_identity_mismatch")
    serial = values[0][2]
    if (not serial or serial != serial.strip() or len(serial) > 200 or any(ord(char) < 32 for char in serial)
            or any(row[2] != serial for row in values)):
        raise NativePlatformError("csv_single_sn_required")
    model = values[0][3]
    if model not in {"24轴", "28轴"} or any(row[3] != model for row in values):
        raise NativePlatformError("csv_single_model_required")
    rows, identities = [], set()
    for index, row in enumerate(values):
        if (not row[0] or row[0] != row[0].strip() or len(row[0]) > 200 or any(ord(char) < 32 for char in row[0])
                or row[0] in identities):
            raise NativePlatformError("csv_record_identity_invalid_or_duplicate")
        identities.add(row[0])
        if (row[4] != "SYS" or row[7] not in by_case or row[6] not in {"项目", "子项"}
                or not row[9].strip() or len(row[9]) > 500
                or row[15] not in {"未测试", "通过", "不通过", "待复核", "中断", "不适用"}
                or (row[6] == "子项" and (row[7], row[8]) not in children)):
            raise NativePlatformError("csv_factory_project_mapping_invalid")
        fields = {column["field"]: value for column, value in zip(rules["columns"], row)}
        fields["original_check_id"] = fields.pop("source_id")
        rows.append({**fields, "index": index, "values": row, "evidence": None, "evidence_available": False})
    projects = []
    for definition in definitions:
        selected = [row for row in rows if row["case_code"] == definition["case_code"]]
        projects.append({"case_code": definition["case_code"], "name": definition["test_name"], "category": definition["category"],
                         "present": bool(selected), "row_indices": [row["index"] for row in selected],
                         "parent_indices": [row["index"] for row in selected if row["level"] == "项目"],
                         "child_indices": [row["index"] for row in selected if row["level"] == "子项"],
                         "row_count": len(selected), "verdict_counts": dict(Counter(row["verdict"] for row in selected))})
    simulated = bool(SIMULATED.search(raw.decode("utf-8-sig")))
    warnings = ["预览只读取原CSV；未核实、未上传、未改写本地结果。",
                "17列CSV未包含独立证据列；原证据仍在原本地目录，不能由本预览推断其已上传。"]
    if simulated:
        warnings.append("文件含模拟来源标识，不能按实机工厂成果批准。")
    else:
        warnings.append("文件未标模拟来源不等于实机来源，请核对真实来源与机器人映射。")
    return {"path": str(path), "filename": path.name, "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "source_id": source, "serial_number": serial, "model": model,
            "operators": list(dict.fromkeys(row["operator"] for row in rows if row["operator"])),
            "source_kind": "simulated" if simulated else "unknown", "simulated_marker": simulated,
            "columns": list(CSV_COLUMNS), "rows": rows, "projects": projects, "warnings": warnings}
