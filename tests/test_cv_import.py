from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from docx import Document
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from jobradar.services import cv_import
from jobradar.services.cv_import import CVImportError, import_cv


def _pdf_with_text(text_lines: list[str]) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)  # noqa: SLF001 - small local test fixture
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
    )
    commands = ["BT", "/F1 12 Tf", "72 780 Td"]
    for index, line in enumerate(text_lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if index:
            commands.append("0 -18 Td")
        commands.append(f"({escaped}) Tj")
    commands.append("ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)  # noqa: SLF001
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _docx_with_lines(lines: list[str]) -> bytes:
    document = Document()
    for line in lines:
        document.add_paragraph(line)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def test_imports_pdf_and_builds_evidence_backed_suggestions() -> None:
    content = _pdf_with_text(
        [
            "Max Mustermann",
            "max.mustermann@example.com",
            "Telefon: +49 711 1234567",
            "Berufserfahrung",
            "2021 - heute Assistenz der Geschaeftsfuehrung bei Muster GmbH",
            "Kenntnisse: Microsoft Office, Excel, SAP",
            "Sprachen: Deutsch, Englisch",
        ]
    )

    result = import_cv("../privat/Lebenslauf.pdf", content, "application/pdf")

    assert result.filename == "Lebenslauf.pdf"
    assert result.document_type == "pdf"
    assert result.page_count == 1
    assert result.profile.full_name == "Max Mustermann"
    assert result.profile.email == "max.mustermann@example.com"
    assert result.profile.phone == "+49 711 1234567"
    assert result.profile.languages == ("Deutsch", "Englisch")
    assert {"Microsoft Office", "Excel", "SAP"} <= set(result.profile.skills)
    assert result.profile.work_experience[0].role == "Assistenz der Geschaeftsfuehrung"
    assert result.profile.work_experience[0].employer == "Muster GmbH"
    assert any(item.field == "email" and item.source_text for item in result.profile.evidence)


def test_imports_docx_and_sanitizes_uploaded_filename() -> None:
    content = _docx_with_lines(
        [
            "Erika Musterfrau",
            "erika@example.de",
            "Work Experience",
            "2019 - 2024 Office Manager at Beispiel AG",
            "Skills: PowerPoint, Salesforce",
        ]
    )

    result = import_cv(
        "../../geheim\\Mein Lebenslauf?.docx",
        content,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert result.filename == "Mein Lebenslauf_.docx"
    assert result.document_type == "docx"
    assert result.page_count is None
    assert result.profile.full_name == "Erika Musterfrau"
    assert result.profile.email == "erika@example.de"
    assert result.profile.work_experience[0].role == "Office Manager"
    assert result.profile.work_experience[0].employer == "Beispiel AG"


def test_imports_structured_english_cv_sections_without_duplicate_evidence() -> None:
    content = _docx_with_lines(
        [
            "JANE DOE",
            "EXECUTIVE ASSISTANT",
            "jane.doe@example.com | +49 711 1234567",
            "PROFESSIONAL SUMMARY",
            "Bilingual executive support professional with 10 years of C-suite experience.",
            "CORE COMPETENCIES",
            "Executive calendar management | Board meeting coordination | Stakeholder management",
            "PROFESSIONAL EXPERIENCE",
            "Jan 2021 - Dec 2025 | Senior Executive Assistant | Acme Ltd",
            "Managed complex calendars for the CEO and CFO across three time zones.",
            "Coordinated board meetings, agendas and executive presentations.",
            "Mar 2016 - Dec 2020 | Executive Assistant | Example Corporation",
            "Organised international travel and processed expense reports.",
            "EDUCATION",
            "Bachelor of Arts in Business Administration | University of London | 2015",
            "CERTIFICATIONS",
            "Certified Administrative Professional | 2024",
            "LANGUAGES",
            "English - Native | German - B2",
            "TECHNICAL SKILLS",
            "Microsoft 365 | Excel | PowerPoint | Salesforce",
        ]
    )

    result = import_cv("english-cv.docx", content, cv_import.DOCX_CONTENT_TYPE)

    assert result.profile.full_name == "JANE DOE"
    assert result.profile.current_title == "EXECUTIVE ASSISTANT"
    assert result.profile.experience_years == 9
    assert result.profile.summary == (
        "Bilingual executive support professional with 10 years of C-suite experience."
    )
    assert result.profile.languages == ("Deutsch", "Englisch")
    assert {
        "Executive calendar management",
        "Board meeting coordination",
        "Stakeholder management",
        "Microsoft 365",
        "Excel",
        "PowerPoint",
        "Salesforce",
    } <= set(result.profile.skills)
    assert len(result.profile.work_experience) == 2
    assert result.profile.work_experience[0].role == "Senior Executive Assistant"
    assert result.profile.work_experience[0].employer == "Acme Ltd"
    assert result.profile.work_experience[0].responsibilities == (
        "Managed complex calendars for the CEO and CFO across three time zones.",
        "Coordinated board meetings, agendas and executive presentations.",
    )
    assert result.profile.education == (
        "Bachelor of Arts in Business Administration | University of London | 2015",
    )
    assert result.profile.certifications == ("Certified Administrative Professional | 2024",)
    evidence_keys = [
        (item.field, item.source_text.casefold()) for item in result.profile.evidence
    ]
    assert len(evidence_keys) == len(set(evidence_keys))


def test_pdf_uses_more_complete_plain_extraction_when_layout_is_sparse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        def extract_text(self, extraction_mode: str | None = None) -> str:
            if extraction_mode == "layout":
                return "Jane"
            return "Jane Doe\nExecutive Assistant\nSkills: Excel, PowerPoint"

    class FakeReader:
        is_encrypted = False
        pages = [FakePage()]

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    monkeypatch.setattr(cv_import, "PdfReader", FakeReader)

    result = import_cv("resume.pdf", b"%PDF-placeholder", "application/pdf")

    assert "Executive Assistant" in result.text
    assert result.profile.full_name == "Jane Doe"
    assert result.profile.skills == ("Excel", "PowerPoint")

def test_rejects_content_that_is_not_the_claimed_format() -> None:
    with pytest.raises(CVImportError, match="Dateiinhalt") as error:
        import_cv("lebenslauf.pdf", b"not a pdf", "application/pdf")

    assert error.value.code == "invalid_signature"


def test_rejects_file_size_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cv_import, "MAX_FILE_BYTES", 8)

    with pytest.raises(CVImportError, match="groesser") as error:
        import_cv("lebenslauf.pdf", b"%PDF-12345", "application/pdf")

    assert error.value.code == "file_too_large"


def test_rejects_pdf_page_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cv_import, "MAX_PDF_PAGES", 0)

    with pytest.raises(CVImportError, match="mehr als 0 Seiten") as error:
        import_cv("lebenslauf.pdf", _pdf_with_text(["Max Muster"]), "application/pdf")

    assert error.value.code == "too_many_pages"


def test_rejects_extracted_text_character_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cv_import, "MAX_TEXT_CHARACTERS", 10)

    with pytest.raises(CVImportError, match="Lebenslauftext") as error:
        import_cv(
            "lebenslauf.docx",
            _docx_with_lines(["Dieser Lebenslauftext ist zu lang"]),
            cv_import.DOCX_CONTENT_TYPE,
        )

    assert error.value.code == "text_too_large"


def test_rejects_docx_zip_bomb_by_declared_uncompressed_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cv_import, "MAX_DOCX_UNCOMPRESSED_BYTES", 1_000)
    content_types = (
        '<?xml version="1.0"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.'
        'document.main+xml"/></Types>'
    )
    oversized_document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{'A' * 2_000}</w:t></w:r></w:p></w:body></w:document>"
    )
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", oversized_document)

    with pytest.raises(CVImportError, match="entpackte DOCX") as error:
        import_cv("lebenslauf.docx", output.getvalue(), cv_import.DOCX_CONTENT_TYPE)

    assert error.value.code == "docx_uncompressed_too_large"


def test_rejects_unsafe_internal_docx_path() -> None:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("../word/document.xml", "unsafe")

    with pytest.raises(CVImportError, match="unsicheren Archivpfad") as error:
        import_cv("lebenslauf.docx", output.getvalue(), cv_import.DOCX_CONTENT_TYPE)

    assert error.value.code == "unsafe_docx_archive"
