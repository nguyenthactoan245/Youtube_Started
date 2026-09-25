"""Read Footage Tracker XLSX values without executing formulas or external links."""
from __future__ import annotations

import json
import posixpath
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def read_tracker(path: Path) -> dict:
    path = Path(path)
    if path.suffix.lower() != ".xlsx":
        raise ValueError("Hãy chọn file .xlsx.")
    try:
        with zipfile.ZipFile(path) as archive:
            if sum(item.file_size for item in archive.infolist()) > 40_000_000:
                raise ValueError("File Excel quá lớn (giới hạn 40 MB sau giải nén).")
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            sheet = next((s for s in workbook.findall("m:sheets/m:sheet", NS)
                          if s.get("name") == "Footage Tracker"), None)
            if sheet is None:
                raise ValueError("Không tìm thấy sheet Footage Tracker.")
            relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            target = next(r.attrib["Target"] for r in relationships
                          if r.get("Id") == sheet.get(REL) and r.get("TargetMode") != "External")
            target = posixpath.normpath("xl/" + target) if not target.startswith("/") else target.lstrip("/")
            strings = []
            if "xl/sharedStrings.xml" in archive.namelist():
                strings = ["".join(t.text or "" for t in s.findall(".//m:t", NS))
                           for s in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("m:si", NS)]
            rows = []
            for row in ET.fromstring(archive.read(target)).findall("m:sheetData/m:row", NS):
                values = {}
                for cell in row.findall("m:c", NS):
                    letters = re.match(r"[A-Z]+", cell.get("r", ""))
                    if not letters:
                        raise ValueError("Địa chỉ ô Excel không hợp lệ.")
                    index = 0
                    for letter in letters[0]:
                        index = index * 26 + ord(letter) - 64
                    if index > 100:
                        raise ValueError("Sheet có quá nhiều cột (tối đa 100).")
                    value = cell.findtext("m:v", "", NS)
                    if cell.get("t") == "s":
                        value = strings[int(value)]
                    elif cell.get("t") == "inlineStr":
                        value = "".join(t.text or "" for t in cell.findall(".//m:t", NS))
                    values[index - 1] = value
                if any(values.values()):
                    rows.append([values.get(i, "") for i in range(max(values) + 1)])
                if len(rows) > 10001:
                    raise ValueError("Sheet vượt quá 10.000 cảnh.")
    except (zipfile.BadZipFile, KeyError, ET.ParseError, StopIteration, IndexError) as exc:
        raise ValueError("File Excel không hợp lệ hoặc bị hỏng.") from exc
    if not rows or rows[0][0].strip() != "Scene ID" or "Voice-over (original)" not in rows[0]:
        raise ValueError("Sheet cần có cột Scene ID và Voice-over (original).")
    headers = rows[0]
    if len(rows) < 2:
        raise ValueError("Sheet Footage Tracker chưa có cảnh nào.")
    if any(len(row) > len(headers) for row in rows[1:]):
        raise ValueError("Có dữ liệu nằm ngoài các cột tiêu đề.")
    return {"filename": path.name, "headers": headers,
            "rows": [row + [""] * (len(headers) - len(row)) for row in rows[1:]]}


def save_tracker(data: dict, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


def load_tracker(target: Path) -> dict | None:
    if not target.exists():
        return None
    data = json.loads(target.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or not isinstance(data.get("filename"), str)
            or not isinstance(data.get("headers"), list) or not data["headers"]
            or not isinstance(data.get("rows"), list)
            or not all(isinstance(h, str) for h in data["headers"])
            or not all(isinstance(r, list) and len(r) == len(data["headers"])
                       and all(isinstance(v, str) for v in r) for r in data["rows"])):
        raise ValueError("Dữ liệu Script đã lưu không hợp lệ.")
    return data
