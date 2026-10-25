import shutil
import unittest
import uuid
import zipfile
from pathlib import Path

from services.database import VideoDatabase
from services.keyword_import import parse_keyword_docx


def write_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(
        f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>' for text in paragraphs
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body>{body}</w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


class KeywordImportTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parent / ".test-work" / "keyword-import"
        self.root = test_root / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)

    def test_parse_keyword_docx_preserves_title_and_collects_keywords(self):
        source = self.root / "keywords.docx"
        write_docx(source, [
            "FRANCE – STOCK FOOTAGE KEYWORDS",
            "INTRO – THE FRANCE YOU NEVER KNEW",
            "Paris aerial, French Alps",
            "PART 1 – THE FRENCH ALPS",
            "Mont Blanc drone, Chamonix valley",
            "OUTRO – BEYOND THE POSTCARDS",
            "France cinematic ending",
        ])
        self.assertEqual(parse_keyword_docx(source), [
            ("INTRO – THE FRANCE YOU NEVER KNEW", "Paris aerial, French Alps"),
            ("PART 1 – THE FRENCH ALPS", "Mont Blanc drone, Chamonix valley"),
            ("OUTRO – BEYOND THE POSTCARDS", "France cinematic ending"),
        ])

    def test_database_import_creates_then_updates_without_duplicates(self):
        database = VideoDatabase(self.root / "stock.db")
        project_id = database.create_project("France")
        chapters = [("INTRO – FRANCE", "Paris, Alps"), ("PART 1 – ALPS", "Mont Blanc")]
        self.assertEqual(database.import_keyword_chapters(project_id, chapters), (2, 0))
        self.assertEqual(database.import_keyword_chapters(
            project_id, [("INTRO – FRANCE", "Paris updated")]), (0, 1))
        rows = database.list_chapters(project_id)
        self.assertEqual([row["title"] for row in rows],
                         ["INTRO – FRANCE", "PART 1 – ALPS"])
        self.assertEqual(rows[0]["keyword"], "Paris updated")

    def test_parse_rejects_document_without_chapter_headings(self):
        source = self.root / "keywords.docx"
        write_docx(source, ["FRANCE", "Paris aerial"])
        with self.assertRaisesRegex(ValueError, "Không tìm thấy"):
            parse_keyword_docx(source)


if __name__ == "__main__":
    unittest.main()
