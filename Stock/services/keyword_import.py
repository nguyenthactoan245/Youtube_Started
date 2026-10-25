from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree


WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CHAPTER_HEADING = re.compile(r"^(?:INTRO|OUTRO|PART\s+\d+)\b", re.IGNORECASE)


def parse_keyword_docx(path: str | Path) -> list[tuple[str, str]]:
    """Read chapter titles and their keyword paragraphs from a Word document."""
    source = Path(path)
    if source.suffix.lower() != ".docx":
        raise ValueError("Chỉ hỗ trợ file Word có định dạng .docx.")
    try:
        with zipfile.ZipFile(source) as archive:
            document = archive.read("word/document.xml")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ValueError("Không thể đọc file Word đã chọn.") from exc

    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise ValueError("Nội dung file Word không hợp lệ.") from exc

    namespace = {"w": WORD_NAMESPACE}
    paragraphs = [
        "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace)).strip()
        for paragraph in root.findall(".//w:body/w:p", namespace)
    ]
    chapters: list[tuple[str, str]] = []
    title: str | None = None
    keyword_parts: list[str] = []
    for text in paragraphs:
        if not text:
            continue
        if CHAPTER_HEADING.match(text):
            if title is not None:
                chapters.append((title, " ".join(keyword_parts).strip()))
            title = text
            keyword_parts = []
        elif title is not None:
            keyword_parts.append(text)
    if title is not None:
        chapters.append((title, " ".join(keyword_parts).strip()))

    if not chapters:
        raise ValueError("Không tìm thấy tiêu đề INTRO, PART hoặc OUTRO trong file Word.")
    missing = [chapter_title for chapter_title, keyword in chapters if not keyword]
    if missing:
        raise ValueError(f"Chapter không có keyword: {missing[0]}")
    return chapters
