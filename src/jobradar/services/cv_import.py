"""Safe, local-only extraction and conservative suggestions from CV files.

The importer deliberately does not perform OCR, network requests, or language-model calls.
Every suggested value is backed by the exact source line that caused the suggestion, and the
caller must still let the user review and confirm the resulting profile.
"""

from __future__ import annotations

import re
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import PurePosixPath
from typing import Literal
from xml.etree import ElementTree

from pypdf import PdfReader
from pypdf.errors import PyPdfError

MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_PDF_PAGES = 80
MAX_DOCX_UNCOMPRESSED_BYTES = 40 * 1024 * 1024
MAX_DOCX_ENTRIES = 2_048
MAX_TEXT_CHARACTERS = 250_000
MAX_FILENAME_CHARACTERS = 180

PDF_CONTENT_TYPES = frozenset({"application/pdf", "application/x-pdf"})
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GENERIC_CONTENT_TYPES = frozenset({"", "application/octet-stream"})

_WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DOCX_MAIN_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)


class CVImportError(ValueError):
    """An expected, user-facing CV validation or extraction error."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SuggestionEvidence:
    """The exact local text fragment supporting one suggested profile value."""

    field: str
    value: str
    source_text: str


@dataclass(frozen=True, slots=True)
class WorkExperienceSuggestion:
    """A structured work-history entry inferred from a dated CV block."""

    period: str
    role: str | None
    employer: str | None
    source_text: str
    responsibilities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProfileSuggestions:
    """Locally inferred values that require explicit user confirmation."""

    full_name: str | None
    email: str | None
    phone: str | None
    current_title: str | None
    experience_years: int | None
    summary: str | None
    languages: tuple[str, ...]
    skills: tuple[str, ...]
    work_experience: tuple[WorkExperienceSuggestion, ...]
    education: tuple[str, ...]
    certifications: tuple[str, ...]
    evidence: tuple[SuggestionEvidence, ...]


@dataclass(frozen=True, slots=True)
class CVImportResult:
    """Validated document text plus transparent, structured profile suggestions."""

    filename: str
    document_type: Literal["pdf", "docx"]
    content_type: str
    text: str
    page_count: int | None
    profile: ProfileSuggestions
    warnings: tuple[str, ...] = ()


def sanitize_filename(filename: str) -> str:
    """Return a display/storage-safe basename without retaining an uploaded path."""

    normalized = unicodedata.normalize("NFKC", str(filename or ""))
    basename = normalized.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(
        character if character.isalnum() or character in {" ", ".", "_", "-", "(", ")"} else "_"
        for character in basename
        if character >= " " and character != "\x7f"
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = "lebenslauf"
    if len(cleaned) <= MAX_FILENAME_CHARACTERS:
        return cleaned

    dot_index = cleaned.rfind(".")
    suffix = cleaned[dot_index:] if dot_index > 0 and len(cleaned) - dot_index <= 12 else ""
    stem_limit = MAX_FILENAME_CHARACTERS - len(suffix)
    stem = cleaned[:stem_limit].rstrip(" .") or "lebenslauf"
    return f"{stem}{suffix}"


def import_cv(filename: str, content: bytes, content_type: str) -> CVImportResult:
    """Validate and extract a PDF or DOCX CV entirely in local memory.

    The function rejects mismatched formats before parsing, checks archive metadata before
    decompressing DOCX content, and fails rather than silently truncating extracted text.
    """

    if not isinstance(content, bytes):
        raise CVImportError(
            "Die Lebenslaufdatei muss als Bytes uebergeben werden.", code="bad_input"
        )
    if not content:
        raise CVImportError("Die Lebenslaufdatei ist leer.", code="empty_file")
    if len(content) > MAX_FILE_BYTES:
        raise CVImportError(
            f"Die Lebenslaufdatei ist groesser als {MAX_FILE_BYTES // (1024 * 1024)} MB.",
            code="file_too_large",
        )

    safe_filename = sanitize_filename(filename)
    declared_type = (content_type or "").split(";", 1)[0].strip().lower()
    document_type = _detect_document_type(safe_filename, content, declared_type)

    if document_type == "pdf":
        extracted_text, page_count = _extract_pdf(content)
        normalized_type = "application/pdf"
    else:
        extracted_text = _extract_docx(content)
        page_count = None
        normalized_type = DOCX_CONTENT_TYPE

    text = _normalize_and_validate_text(extracted_text)
    warnings: list[str] = []
    if not text:
        warnings.append(
            "Es wurde kein lesbarer Text gefunden. Ein gescannter Lebenslauf benoetigt spaeter OCR."
        )
    elif document_type == "pdf" and _extracted_text_score(text)[0] < max(8, page_count * 3):
        warnings.append(
            "Aus dem PDF wurde nur sehr wenig Text gelesen. Bei einem gescannten oder "
            "grafisch aufgebauten Lebenslauf pruefe bitte die erkannten Angaben besonders genau."
        )

    return CVImportResult(
        filename=_ensure_extension(safe_filename, document_type),
        document_type=document_type,
        content_type=normalized_type,
        text=text,
        page_count=page_count,
        profile=_suggest_profile(text),
        warnings=tuple(warnings),
    )


def _detect_document_type(
    filename: str, content: bytes, declared_type: str
) -> Literal["pdf", "docx"]:
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    extension_type: Literal["pdf", "docx"] | None = None
    if extension in {"pdf", "docx"}:
        extension_type = extension
    elif extension:
        raise CVImportError(
            "Nur Lebenslaeufe im PDF- oder DOCX-Format werden unterstuetzt.",
            code="unsupported_format",
        )

    declared_document_type: Literal["pdf", "docx"] | None
    if declared_type in PDF_CONTENT_TYPES:
        declared_document_type = "pdf"
    elif declared_type == DOCX_CONTENT_TYPE:
        declared_document_type = "docx"
    elif declared_type in GENERIC_CONTENT_TYPES:
        declared_document_type = None
    else:
        raise CVImportError(
            "Der gemeldete Dateityp ist weder PDF noch DOCX.", code="unsupported_content_type"
        )

    header = content[:1_024].lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    if header.startswith(b"%PDF-"):
        signature_type: Literal["pdf", "docx"] | None = "pdf"
    elif content.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        signature_type = "docx"
    else:
        signature_type = None

    claimed_types = {value for value in (extension_type, declared_document_type) if value}
    if len(claimed_types) > 1:
        raise CVImportError(
            "Dateiendung und gemeldeter Dateityp passen nicht zusammen.", code="format_mismatch"
        )
    expected_type = next(iter(claimed_types), signature_type)
    if expected_type is None:
        raise CVImportError(
            "Die Datei ist weder als PDF noch als DOCX erkennbar.", code="unsupported_format"
        )
    if signature_type != expected_type:
        raise CVImportError(
            "Der Dateiinhalt passt nicht zum angegebenen PDF- oder DOCX-Format.",
            code="invalid_signature",
        )
    return expected_type


def _ensure_extension(filename: str, document_type: Literal["pdf", "docx"]) -> str:
    if filename.lower().endswith(f".{document_type}"):
        return filename
    return f"{filename}.{document_type}"


def _extracted_text_score(value: str) -> tuple[int, int, int]:
    words = re.findall(r"[^\W\d_]{2,}", value, re.UNICODE)
    return len(words), len({word.casefold() for word in words}), len(value)


def _extract_pdf(content: bytes) -> tuple[str, int]:
    try:
        reader = PdfReader(BytesIO(content), strict=False)
        if reader.is_encrypted:
            raise CVImportError(
                "Passwortgeschuetzte PDF-Dateien koennen nicht eingelesen werden.",
                code="encrypted_pdf",
            )
        page_count = len(reader.pages)
        if page_count > MAX_PDF_PAGES:
            raise CVImportError(
                f"Das PDF hat mehr als {MAX_PDF_PAGES} Seiten.", code="too_many_pages"
            )

        parts: list[str] = []
        character_count = 0
        for page in reader.pages:
            plain_text = page.extract_text() or ""
            try:
                layout_text = page.extract_text(extraction_mode="layout") or ""
            except (TypeError, ValueError):
                layout_text = ""
            page_text = max(
                (plain_text, layout_text),
                key=_extracted_text_score,
            )
            character_count += len(page_text)
            if character_count > MAX_TEXT_CHARACTERS:
                raise CVImportError(
                    "Der aus dem PDF gelesene Text ist ungewoehnlich gross.",
                    code="text_too_large",
                )
            parts.append(page_text)
        return "\n\n".join(parts), page_count
    except CVImportError:
        raise
    except (PyPdfError, OSError, ValueError, TypeError, RecursionError) as exc:
        raise CVImportError(
            "Das PDF ist beschaedigt oder kann nicht sicher gelesen werden.", code="invalid_pdf"
        ) from exc


def _extract_docx(content: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            return _extract_docx_archive(archive)
    except CVImportError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError) as exc:
        raise CVImportError(
            "Die DOCX-Datei ist beschaedigt oder unvollstaendig.", code="invalid_docx"
        ) from exc


def _extract_docx_archive(archive: zipfile.ZipFile) -> str:
    infos = archive.infolist()
    if len(infos) > MAX_DOCX_ENTRIES:
        raise CVImportError(
            "Die DOCX-Datei enthaelt ungewoehnlich viele Bestandteile.",
            code="docx_too_many_entries",
        )

    total_uncompressed = 0
    names: set[str] = set()
    for info in infos:
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        first_part = path.parts[0] if path.parts else ""
        if (
            not name
            or "\x00" in name
            or path.is_absolute()
            or ".." in path.parts
            or ":" in first_part
            or name in names
        ):
            raise CVImportError(
                "Die DOCX-Datei enthaelt einen unsicheren Archivpfad.",
                code="unsafe_docx_archive",
            )
        names.add(name)
        if info.flag_bits & 0x1:
            raise CVImportError(
                "Passwortgeschuetzte DOCX-Dateien koennen nicht eingelesen werden.",
                code="encrypted_docx",
            )
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_DOCX_UNCOMPRESSED_BYTES:
            raise CVImportError(
                "Die entpackte DOCX-Datei ist ungewoehnlich gross.",
                code="docx_uncompressed_too_large",
            )

    if "[Content_Types].xml" not in names or "word/document.xml" not in names:
        raise CVImportError("Die Datei ist kein vollstaendiges DOCX-Dokument.", code="invalid_docx")

    remaining_read_budget = MAX_DOCX_UNCOMPRESSED_BYTES

    def read_entry(name: str) -> bytes:
        nonlocal remaining_read_budget
        data = _read_zip_entry(archive, name, max_bytes=remaining_read_budget)
        remaining_read_budget -= len(data)
        return data

    content_types = read_entry("[Content_Types].xml")
    if _DOCX_MAIN_CONTENT_TYPE.encode() not in content_types:
        raise CVImportError(
            "Das Archiv ist kein unterstuetztes Word-Dokument.", code="invalid_docx"
        )

    header_names = sorted(name for name in names if re.fullmatch(r"word/header\d+\.xml", name))
    footer_names = sorted(name for name in names if re.fullmatch(r"word/footer\d+\.xml", name))
    xml_names = [*header_names, "word/document.xml", *footer_names]
    paragraphs: list[str] = []
    for name in xml_names:
        paragraphs.extend(_paragraphs_from_word_xml(read_entry(name)))
    return "\n".join(paragraphs)


def _read_zip_entry(archive: zipfile.ZipFile, name: str, *, max_bytes: int) -> bytes:
    try:
        with archive.open(name) as source:
            data = source.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise CVImportError(
                "Die entpackte DOCX-Datei ist ungewoehnlich gross.",
                code="docx_uncompressed_too_large",
            )
        return data
    except CVImportError:
        raise
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise CVImportError(
            "Die DOCX-Datei ist beschaedigt oder kann nicht sicher gelesen werden.",
            code="invalid_docx",
        ) from exc


def _paragraphs_from_word_xml(xml_content: bytes) -> list[str]:
    if b"<!DOCTYPE" in xml_content or b"<!ENTITY" in xml_content:
        raise CVImportError(
            "Die DOCX-Datei enthaelt nicht erlaubte XML-Deklarationen.",
            code="unsafe_docx_xml",
        )
    try:
        root = ElementTree.fromstring(xml_content)
    except ElementTree.ParseError as exc:
        raise CVImportError(
            "Die DOCX-Datei enthaelt beschaedigte Textdaten.", code="invalid_docx"
        ) from exc

    paragraphs: list[str] = []
    text_tag = f"{_WORD_NAMESPACE}t"
    tab_tag = f"{_WORD_NAMESPACE}tab"
    break_tags = {f"{_WORD_NAMESPACE}br", f"{_WORD_NAMESPACE}cr"}
    for paragraph in root.iter(f"{_WORD_NAMESPACE}p"):
        chunks: list[str] = []
        for node in paragraph.iter():
            if node.tag == text_tag:
                chunks.append(node.text or "")
            elif node.tag == tab_tag:
                chunks.append("\t")
            elif node.tag in break_tags:
                chunks.append("\n")
        for line in "".join(chunks).splitlines() or [""]:
            if line.strip():
                paragraphs.append(line)
    return paragraphs


def _normalize_and_validate_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).replace("\x00", "")
    lines: list[str] = []
    blank_count = 0
    character_count = 0
    for raw_line in normalized.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"[\t\v\f ]+", " ", raw_line).strip()
        if not line:
            blank_count += 1
            if blank_count > 1 or not lines:
                continue
        else:
            blank_count = 0
        character_count += len(line) + 1
        if character_count > MAX_TEXT_CHARACTERS:
            raise CVImportError(
                "Der gelesene Lebenslauftext ist ungewoehnlich gross.", code="text_too_large"
            )
        lines.append(line)
    return "\n".join(lines).strip()


_EMAIL_PATTERN = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
_PHONE_PATTERN = re.compile(r"(?<!\w)(?:(?:\+|00)\d{1,3}[\s./()-]*)?(?:\d[\s./()-]*){7,15}")
_DATE_OF_BIRTH_PATTERN = re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b")

_LANGUAGE_TERMS = {
    "Deutsch": ("deutsch", "german"),
    "Englisch": ("englisch", "english"),
    "Franzoesisch": ("franzoesisch", "französisch", "french"),
    "Spanisch": ("spanisch", "spanish"),
    "Italienisch": ("italienisch", "italian"),
    "Tuerkisch": ("tuerkisch", "türkisch", "turkish"),
    "Polnisch": ("polnisch", "polish"),
    "Russisch": ("russisch", "russian"),
    "Arabisch": ("arabisch", "arabic"),
    "Portugiesisch": ("portugiesisch", "portuguese"),
    "Niederlaendisch": ("niederlaendisch", "niederländisch", "dutch"),
    "Chinesisch": ("chinesisch", "chinese", "mandarin"),
    "Japanisch": ("japanisch", "japanese"),
    "Koreanisch": ("koreanisch", "korean"),
    "Hindi": ("hindi",),
    "Ukrainisch": ("ukrainisch", "ukrainian"),
    "Tschechisch": ("tschechisch", "czech"),
    "Schwedisch": ("schwedisch", "swedish"),
    "Daenisch": ("daenisch", "dänisch", "danish"),
    "Norwegisch": ("norwegisch", "norwegian"),
}

_SKILL_TERMS = {
    "Microsoft Office": ("microsoft office", "ms office", "microsoft 365", "office 365"),
    "Excel": ("excel",),
    "PowerPoint": ("powerpoint",),
    "Word": ("microsoft word", "ms word"),
    "Outlook": ("outlook",),
    "Microsoft Teams": ("microsoft teams", "ms teams"),
    "SAP": ("sap",),
    "DATEV": ("datev",),
    "Salesforce": ("salesforce",),
    "Jira": ("jira",),
    "Confluence": ("confluence",),
    "Projektmanagement": ("projektmanagement", "project management", "project coordination"),
    "Eventmanagement": ("eventmanagement", "event management", "event coordination"),
    "Reisemanagement": (
        "reisemanagement",
        "reiseplanung",
        "travel management",
        "travel planning",
    ),
    "Terminmanagement": (
        "terminmanagement",
        "kalendersteuerung",
        "calendar management",
        "calendar coordination",
        "calendar scheduling",
    ),
    "Sitzungsorganisation": (
        "sitzungsorganisation",
        "meeting coordination",
        "board meeting coordination",
    ),
    "Stakeholdermanagement": (
        "stakeholdermanagement",
        "stakeholder management",
        "stakeholder relations",
    ),
    "Korrespondenz": ("korrespondenz", "correspondence"),
    "Protokollfuehrung": (
        "protokollfuehrung",
        "protokollführung",
        "minute taking",
        "meeting minutes",
    ),
    "Budgetplanung": ("budgetplanung", "budget planning"),
    "Controlling": ("controlling",),
    "Recruiting": ("recruiting",),
    "Ausgabenmanagement": (
        "ausgabenmanagement",
        "expense management",
        "expense reporting",
    ),
}

_SUMMARY_HEADINGS = {
    "profil",
    "kurzprofil",
    "profilübersicht",
    "profiluebersicht",
    "zusammenfassung",
    "profile",
    "professional profile",
    "professional summary",
    "executive summary",
    "personal statement",
    "about me",
}
_EXPERIENCE_HEADINGS = {
    "berufserfahrung",
    "beruflicher werdegang",
    "berufliche erfahrung",
    "berufspraxis",
    "work experience",
    "professional experience",
    "employment history",
    "career history",
    "work history",
}
_SKILL_HEADINGS = {
    "kenntnisse",
    "fähigkeiten",
    "faehigkeiten",
    "kompetenzen",
    "kernkompetenzen",
    "qualifikationen",
    "skills",
    "key skills",
    "core skills",
    "core competencies",
    "competencies",
    "areas of expertise",
    "expertise",
}
_TOOL_HEADINGS = {
    "edv-kenntnisse",
    "it-kenntnisse",
    "software",
    "tools",
    "technical skills",
    "digital skills",
    "systems",
}
_LANGUAGE_HEADINGS = {"sprachen", "sprachkenntnisse", "languages", "language skills"}
_EDUCATION_HEADINGS = {
    "ausbildung",
    "studium",
    "bildungsweg",
    "akademischer werdegang",
    "education",
    "academic background",
    "academic history",
}
_CERTIFICATION_HEADINGS = {
    "zertifikate",
    "zertifizierungen",
    "weiterbildungen",
    "fortbildungen",
    "certifications",
    "certificates",
    "professional development",
    "training",
    "courses",
}
_STOP_HEADINGS = {
    "kontakt",
    "persönliche daten",
    "persoenliche daten",
    "contact",
    "personal details",
    "interessen",
    "hobbys",
    "interests",
    "projects",
    "projekte",
    "awards",
    "auszeichnungen",
    "references",
    "referenzen",
    "volunteering",
    "ehrenamt",
}
_SECTION_ALIASES = {
    **{heading: "summary" for heading in _SUMMARY_HEADINGS},
    **{heading: "experience" for heading in _EXPERIENCE_HEADINGS},
    **{heading: "skills" for heading in _SKILL_HEADINGS},
    **{heading: "tools" for heading in _TOOL_HEADINGS},
    **{heading: "languages" for heading in _LANGUAGE_HEADINGS},
    **{heading: "education" for heading in _EDUCATION_HEADINGS},
    **{heading: "certifications" for heading in _CERTIFICATION_HEADINGS},
    **{heading: "other" for heading in _STOP_HEADINGS},
}
_SECTION_HEADINGS = frozenset(_SECTION_ALIASES)
_ROLE_TERMS = (
    "assistenz",
    "assistant",
    "referent",
    "manager",
    "coordinator",
    "koordinator",
    "sachbearbeit",
    "specialist",
    "consultant",
    "controller",
    "administrator",
    "secretary",
    "sekretär",
    "sekretaer",
    "chief of staff",
    "director",
    "head of",
    "team lead",
    "teamleit",
    "projektleit",
    "project lead",
    "office management",
    "operations",
    "analyst",
    "advisor",
    "officer",
)
_MONTH_NAMES = (
    "jan(?:uary|uar)?",
    "feb(?:ruary|ruar)?",
    "mar(?:ch)?",
    "märz",
    "maerz",
    "apr(?:il)?",
    "may",
    "mai",
    "jun(?:e|i)?",
    "jul(?:y|i)?",
    "aug(?:ust)?",
    "sep(?:t(?:ember)?)?",
    "okt(?:ober)?",
    "oct(?:ober)?",
    "nov(?:ember)?",
    "dec(?:ember)?",
    "dez(?:ember)?",
)
_MONTH_PATTERN = "(?:" + "|".join(_MONTH_NAMES) + ")"
_DATE_VALUE = (
    rf"(?:{_MONTH_PATTERN}\.?\s+)?(?:19|20)\d{{2}}"
    r"|(?:0?[1-9]|1[0-2])[./](?:19|20)\d{2}"
)
_PERIOD_PATTERN = re.compile(
    rf"(?P<period>(?:{_DATE_VALUE})\s*"
    rf"(?:[-\u2013\u2014]|bis|to|until)\s*"
    rf"(?:{_DATE_VALUE}|heute|aktuell|present|current|now)"
    rf"|(?:seit|since)\s+(?:{_DATE_VALUE}))",
    re.IGNORECASE,
)
_DATE_VALUE_PATTERN = re.compile(_DATE_VALUE, re.IGNORECASE)
_COMPANY_PATTERN = re.compile(
    r"\b(?:gmbh|ag|kg|se|ug|holding|gruppe|group|company|unternehmen|e\.v\.|"
    r"ltd|limited|llc|inc|incorporated|corp|corporation|plc|partners)\b",
    re.IGNORECASE,
)
_LIST_SEPARATOR = re.compile(r"\s*(?:[|•·;,]|\t)\s*")


def _suggest_profile(text: str) -> ProfileSuggestions:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    sections = _collect_sections(lines)
    evidence_groups: dict[tuple[str, str], list[str]] = {}

    def add_evidence(field: str, values: str | tuple[str, ...], source: str | None) -> None:
        if not source:
            return
        source_text = source.strip()[:2_000]
        candidates = (values,) if isinstance(values, str) else values
        target = evidence_groups.setdefault((field, source_text), [])
        known = {item.casefold() for item in target}
        for value in candidates:
            cleaned = str(value or "").strip()
            if cleaned and cleaned.casefold() not in known:
                target.append(cleaned)
                known.add(cleaned.casefold())

    full_name, name_source = _suggest_name(lines)
    add_evidence("full_name", full_name or "", name_source)
    email, email_source = _suggest_email(lines)
    add_evidence("email", email or "", email_source)
    phone, phone_source = _suggest_phone(lines)
    add_evidence("phone", phone or "", phone_source)

    work_experience = _suggest_work_experience(lines, sections)
    current_title, title_source = _suggest_current_title(
        lines,
        name_source=name_source,
        work_experience=work_experience,
    )
    add_evidence("current_title", current_title or "", title_source)

    experience_years = _experience_years(work_experience)
    if experience_years is not None:
        add_evidence(
            "experience_years",
            str(experience_years),
            " | ".join(item.period for item in work_experience),
        )

    summary_lines = sections["summary"]
    summary = _joined_section(summary_lines, maximum=3_000)
    if summary:
        add_evidence("summary", summary, " | ".join(summary_lines))

    languages, language_sources = _suggest_languages(lines, sections)
    for source, values in language_sources.items():
        add_evidence("languages", tuple(values), source)

    skills, skill_sources = _suggest_skills(lines, sections)
    for source, values in skill_sources.items():
        add_evidence("skills", tuple(values), source)

    education = _section_entries(sections["education"])
    for item in education:
        add_evidence("education", item, item)
    certifications = _section_entries(sections["certifications"])
    for item in certifications:
        add_evidence("certifications", item, item)

    for experience in work_experience:
        value = " · ".join(
            part for part in (experience.period, experience.role, experience.employer) if part
        )
        add_evidence("work_experience", value, experience.source_text)

    evidence = tuple(
        SuggestionEvidence(
            field=field,
            value=", ".join(values),
            source_text=source,
        )
        for (field, source), values in evidence_groups.items()
        if values
    )
    return ProfileSuggestions(
        full_name=full_name,
        email=email,
        phone=phone,
        current_title=current_title,
        experience_years=experience_years,
        summary=summary,
        languages=languages,
        skills=skills,
        work_experience=work_experience,
        education=education,
        certifications=certifications,
        evidence=evidence,
    )


def _heading_key(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).casefold().strip()
    folded = re.sub(r"^[#•·\-\s]+|[:#•·\-\s]+$", "", folded)
    return re.sub(r"\s+", " ", folded)


def _split_section_heading(line: str) -> tuple[str | None, str]:
    key = _heading_key(line)
    if key in _SECTION_ALIASES:
        return _SECTION_ALIASES[key], ""
    if ":" in line:
        prefix, remainder = line.split(":", 1)
        prefix_key = _heading_key(prefix)
        if prefix_key in _SECTION_ALIASES:
            return _SECTION_ALIASES[prefix_key], remainder.strip()
    return None, line


def _collect_sections(lines: list[str]) -> dict[str, list[str]]:
    sections = {
        "summary": [],
        "experience": [],
        "skills": [],
        "tools": [],
        "languages": [],
        "education": [],
        "certifications": [],
    }
    active: str | None = None
    for line in lines:
        category, remainder = _split_section_heading(line)
        if category is not None:
            active = category if category in sections else None
            if active and remainder:
                sections[active].append(remainder)
            continue
        if active:
            sections[active].append(line)
    return sections


def _suggest_name(lines: list[str]) -> tuple[str | None, str | None]:
    labelled_name = re.compile(
        r"^(?:name|full\s+name|vor-?\s*und\s*nachname)\s*:\s*(.+)$",
        re.IGNORECASE,
    )
    for line in lines[:20]:
        match = labelled_name.match(line)
        if match and _looks_like_name(match.group(1)):
            return match.group(1).strip(), line

    excluded = {
        "lebenslauf",
        "curriculum vitae",
        "resume",
        "résumé",
        "bewerbung",
        *_SECTION_HEADINGS,
    }
    for line in lines[:15]:
        candidate = line.strip(" ,;|")
        if _heading_key(candidate) in excluded:
            continue
        if "@" in candidate or _PHONE_PATTERN.search(candidate):
            continue
        if _looks_like_name(candidate):
            return candidate, line
    return None, None


def _looks_like_name(value: str) -> bool:
    words = value.split()
    if not 2 <= len(words) <= 5 or len(value) > 100:
        return False
    if any(term in value.casefold() for term in _ROLE_TERMS):
        return False
    for word in words:
        parts = word.replace("'", "-").split("-")
        if not all(part and part.isalpha() for part in parts):
            return False
        if not all(part[0].isupper() for part in parts):
            return False
    return True


def _suggest_email(lines: list[str]) -> tuple[str | None, str | None]:
    for line in lines:
        match = _EMAIL_PATTERN.search(line)
        if match:
            return match.group(0).lower(), line
    return None, None


def _suggest_phone(lines: list[str]) -> tuple[str | None, str | None]:
    labelled: list[tuple[str, str]] = []
    other: list[tuple[str, str]] = []
    for line in lines:
        if _DATE_OF_BIRTH_PATTERN.search(line) and re.search(r"geb|birth", line, re.IGNORECASE):
            continue
        for match in _PHONE_PATTERN.finditer(line):
            value = match.group(0).strip(" .,/()-")
            digits = re.sub(r"\D", "", value)
            if not 7 <= len(digits) <= 15:
                continue
            item = (value, line)
            if re.search(r"\b(?:tel|telefon|mobil|mobile|phone)\b", line, re.IGNORECASE):
                labelled.append(item)
            else:
                other.append(item)
    choices = labelled or other
    return choices[0] if choices else (None, None)


def _contains_term(value: str, term: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(term.casefold())}(?!\w)", value.casefold()))


def _append_unique(values: list[str], value: str) -> None:
    cleaned = re.sub(r"\s+", " ", value).strip(" •·|,;:-")
    if not cleaned:
        return
    folded = cleaned.casefold()
    if folded not in {item.casefold() for item in values}:
        values.append(cleaned)


def _suggest_languages(
    lines: list[str],
    sections: dict[str, list[str]],
) -> tuple[tuple[str, ...], dict[str, list[str]]]:
    values: list[str] = []
    sources: dict[str, list[str]] = {}
    candidates = sections["languages"] or lines
    for line in candidates:
        found: list[str] = []
        for language, terms in _LANGUAGE_TERMS.items():
            if any(_contains_term(line, term) for term in terms):
                _append_unique(values, language)
                _append_unique(found, language)
        if found:
            sources[line] = found
    return tuple(values), sources


def _suggest_skills(
    lines: list[str],
    sections: dict[str, list[str]],
) -> tuple[tuple[str, ...], dict[str, list[str]]]:
    values: list[str] = []
    sources: dict[str, list[str]] = {}
    section_lines = [*sections["skills"], *sections["tools"]]
    for line in section_lines:
        found: list[str] = []
        for candidate in _LIST_SEPARATOR.split(line):
            cleaned = re.sub(r"^[✓✔►▸▪●○\-\u2013\u2014]+\s*", "", candidate).strip()
            if not _looks_like_skill(cleaned):
                continue
            _append_unique(values, cleaned)
            _append_unique(found, cleaned)
        if found:
            sources[line] = found

    for skill, terms in _SKILL_TERMS.items():
        for line in lines:
            matched_term = next((term for term in terms if _contains_term(line, term)), None)
            if matched_term is None:
                continue
            already_present = any(
                matched_term.casefold() in value.casefold() for value in values
            )
            if not already_present:
                _append_unique(values, skill)
                sources.setdefault(line, [])
                _append_unique(sources[line], skill)
            break
    return tuple(values[:100]), sources


def _looks_like_skill(value: str) -> bool:
    if not value or len(value) > 120 or len(value.split()) > 12:
        return False
    if _PERIOD_PATTERN.search(value) or _EMAIL_PATTERN.search(value):
        return False
    if not re.search(r"[A-Za-zÄÖÜäöüß]", value):
        return False
    folded = value.casefold()
    return folded not in {
        "skills",
        "kenntnisse",
        "kompetenzen",
        "tools",
        "software",
        "advanced",
        "intermediate",
        "beginner",
        "fortgeschritten",
        "grundkenntnisse",
    }


def _section_entries(lines: list[str]) -> tuple[str, ...]:
    entries: list[str] = []
    for line in lines:
        cleaned = re.sub(r"^[✓✔►▸▪●○\-\u2013\u2014]+\s*", "", line).strip()
        if cleaned and len(cleaned) <= 500:
            _append_unique(entries, cleaned)
    return tuple(entries[:50])


def _joined_section(lines: list[str], *, maximum: int) -> str | None:
    cleaned = [
        re.sub(r"^[✓✔►▸▪●○\-\u2013\u2014]+\s*", "", line).strip()
        for line in lines
        if line.strip()
    ]
    value = " ".join(cleaned)
    return value[:maximum].strip() or None


def _suggest_current_title(
    lines: list[str],
    *,
    name_source: str | None,
    work_experience: tuple[WorkExperienceSuggestion, ...],
) -> tuple[str | None, str | None]:
    labelled = re.compile(
        r"^(?:aktuelle\s+tätigkeit|aktuelle\s+position|berufsbezeichnung|"
        r"current\s+role|current\s+position|job\s+title|title)\s*:\s*(.+)$",
        re.IGNORECASE,
    )
    for line in lines[:30]:
        match = labelled.match(line)
        if match:
            value = match.group(1).strip()
            if value:
                return value, line
    for line in lines[:20]:
        if line == name_source or "@" in line or _PHONE_PATTERN.search(line):
            continue
        if _split_section_heading(line)[0] is not None or _PERIOD_PATTERN.search(line):
            continue
        if any(term in line.casefold() for term in _ROLE_TERMS) and len(line) <= 150:
            return line.strip(" |,;:-"), line
    for experience in work_experience:
        if experience.role:
            return experience.role, experience.source_text
    return None, None


def _suggest_work_experience(
    lines: list[str],
    sections: dict[str, list[str]],
) -> tuple[WorkExperienceSuggestion, ...]:
    candidates = sections["experience"] or lines
    suggestions: list[WorkExperienceSuggestion] = []
    seen: set[tuple[str, str, str]] = set()
    for index, line in enumerate(candidates):
        match = _PERIOD_PATTERN.search(line)
        if match is None:
            continue
        context, identity_sources, identity_end = _experience_identity(candidates, index, match)
        has_role = any(term in context.casefold() for term in _ROLE_TERMS)
        if not sections["experience"] and not has_role:
            continue
        role, employer = _split_role_and_employer(context)
        if role is None and employer is None:
            continue
        responsibilities = _experience_responsibilities(
            candidates,
            start=max(index + 1, identity_end + 1),
        )
        period = match.group("period").strip()
        key = (period.casefold(), (role or "").casefold(), (employer or "").casefold())
        if key in seen:
            continue
        seen.add(key)
        source_parts = [line, *identity_sources, *responsibilities[:3]]
        source_text = " | ".join(dict.fromkeys(part for part in source_parts if part))[:2_000]
        suggestions.append(
            WorkExperienceSuggestion(
                period=period,
                role=role,
                employer=employer,
                source_text=source_text,
                responsibilities=responsibilities,
            )
        )
        if len(suggestions) >= 30:
            break
    suggestions.sort(key=lambda item: _period_sort_key(item.period), reverse=True)
    return tuple(suggestions)


def _experience_identity(
    lines: list[str],
    index: int,
    match: re.Match[str],
) -> tuple[str, list[str], int]:
    residue = re.sub(
        r"^[\s|,;:\u2013\u2014-]+|[\s|,;:\u2013\u2014-]+$",
        "",
        _PERIOD_PATTERN.sub("", lines[index]),
    )
    if residue:
        return residue, [], index

    sources: list[str] = []
    for cursor in range(index + 1, min(len(lines), index + 3)):
        candidate = lines[cursor]
        if _PERIOD_PATTERN.search(candidate) or not _looks_like_identity_line(candidate):
            break
        sources.append(candidate)
    if sources:
        return " | ".join(sources), sources, index + len(sources)

    for cursor in range(index - 1, max(-1, index - 3), -1):
        candidate = lines[cursor]
        if _PERIOD_PATTERN.search(candidate) or not _looks_like_identity_line(candidate):
            break
        sources.insert(0, candidate)
    return " | ".join(sources), sources, index


def _looks_like_identity_line(value: str) -> bool:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 180 or cleaned.endswith((".", ";")):
        return False
    if _EMAIL_PATTERN.search(cleaned) or _PHONE_PATTERN.search(cleaned):
        return False
    folded = cleaned.casefold()
    return bool(
        any(term in folded for term in _ROLE_TERMS)
        or _COMPANY_PATTERN.search(cleaned)
        or "|" in cleaned
        or re.search(r"\s+(?:bei|at)\s+", cleaned, re.IGNORECASE)
    )


def _experience_responsibilities(lines: list[str], *, start: int) -> tuple[str, ...]:
    values: list[str] = []
    total = 0
    for line in lines[start:]:
        if _PERIOD_PATTERN.search(line) or _split_section_heading(line)[0] is not None:
            break
        if _looks_like_identity_line(line):
            break
        cleaned = re.sub(r"^[✓✔►▸▪●○\-\u2013\u2014]+\s*", "", line).strip()
        if not cleaned or _EMAIL_PATTERN.search(cleaned):
            continue
        total += len(cleaned)
        if total > 2_000:
            break
        _append_unique(values, cleaned)
        if len(values) >= 15:
            break
    return tuple(values)


def _split_role_and_employer(context: str) -> tuple[str | None, str | None]:
    context = context.strip(" |,;:-\u2013\u2014")
    if not context:
        return None, None
    direct = re.split(
        r"\s+(?:bei|at)\s+|\s*@\s*",
        context,
        maxsplit=1,
        flags=re.IGNORECASE,
    )
    if len(direct) == 2:
        return direct[0].strip() or None, direct[1].strip() or None

    parts = [
        part.strip()
        for part in re.split(r"\s*[|\u2013\u2014]\s*", context)
        if part.strip()
    ]
    if len(parts) >= 2:
        role_index = next(
            (
                index
                for index, part in enumerate(parts[:3])
                if any(term in part.casefold() for term in _ROLE_TERMS)
            ),
            None,
        )
        company_index = next(
            (
                index
                for index, part in enumerate(parts[:3])
                if _COMPANY_PATTERN.search(part)
            ),
            None,
        )
        if role_index is not None:
            employer_index = company_index
            if employer_index is None:
                employer_index = next(
                    (index for index in range(min(3, len(parts))) if index != role_index),
                    None,
                )
            employer = parts[employer_index] if employer_index is not None else None
            return parts[role_index], employer
        if company_index is not None:
            role_index = next(
                (index for index in range(min(3, len(parts))) if index != company_index),
                None,
            )
            role = parts[role_index] if role_index is not None else None
            return role, parts[company_index]
        return parts[0], parts[1]

    if any(term in context.casefold() for term in _ROLE_TERMS):
        return context, None
    if _COMPANY_PATTERN.search(context):
        return None, context
    return context, None


def _period_sort_key(period: str) -> int:
    interval = _period_interval(period)
    return interval[0] if interval else -1


def _experience_years(
    work_experience: tuple[WorkExperienceSuggestion, ...],
) -> int | None:
    intervals = [
        interval
        for item in work_experience
        if (interval := _period_interval(item.period)) is not None
    ]
    if not intervals:
        return None
    intervals.sort()
    merged: list[list[int]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    months = sum(end - start + 1 for start, end in merged)
    return min(60, months // 12)


def _period_interval(period: str) -> tuple[int, int] | None:
    values = _DATE_VALUE_PATTERN.findall(period)
    if not values:
        return None
    start = _date_month(values[0], end=False)
    if start is None:
        return None
    folded = period.casefold()
    open_ended = any(
        term in folded for term in ("heute", "aktuell", "present", "current", "now")
    )
    if len(values) >= 2:
        end = _date_month(values[1], end=True)
    elif open_ended or folded.strip().startswith(("seit", "since")):
        now = datetime.now(UTC)
        end = now.year * 12 + now.month - 1
    else:
        end = _date_month(values[0], end=True)
    if end is None or end < start:
        return None
    return start, end


def _date_month(value: str, *, end: bool) -> int | None:
    year_match = re.search(r"(?:19|20)\d{2}", value)
    if year_match is None:
        return None
    year = int(year_match.group(0))
    numeric_month = re.search(r"\b(0?[1-9]|1[0-2])[./](?:19|20)\d{2}", value)
    if numeric_month:
        month = int(numeric_month.group(1))
    else:
        folded = value.casefold().replace(".", "")
        aliases = {
            1: ("jan", "january", "januar"),
            2: ("feb", "february", "februar"),
            3: ("mar", "march", "märz", "maerz"),
            4: ("apr", "april"),
            5: ("may", "mai"),
            6: ("jun", "june", "juni"),
            7: ("jul", "july", "juli"),
            8: ("aug", "august"),
            9: ("sep", "sept", "september"),
            10: ("oct", "october", "okt", "oktober"),
            11: ("nov", "november"),
            12: ("dec", "december", "dez", "dezember"),
        }
        month = next(
            (
                number
                for number, names in aliases.items()
                if any(re.search(rf"\b{re.escape(name)}\b", folded) for name in names)
            ),
            12 if end else 1,
        )
    return year * 12 + month - 1
