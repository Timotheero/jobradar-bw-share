from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, nodes

from jobradar.i18n import (
    ENGLISH_TRANSLATIONS,
    normalize_locale,
    translate,
    translate_text,
)

TEMPLATE_DIR = Path(__file__).parents[1] / "src" / "jobradar" / "templates"


def test_every_literal_template_message_has_an_english_translation() -> None:
    environment = Environment(autoescape=True)
    messages: set[str] = set()
    for path in TEMPLATE_DIR.glob("*.html"):
        parsed = environment.parse(path.read_text(encoding="utf-8"))
        for call in parsed.find_all(nodes.Call):
            if not isinstance(call.node, nodes.Name) or call.node.name != "t":
                continue
            if call.args and isinstance(call.args[0], nodes.Const):
                messages.add(str(call.args[0].value))

    missing = sorted(message for message in messages if message not in ENGLISH_TRANSLATIONS)
    assert missing == []


def test_locale_normalization_and_runtime_text_translation() -> None:
    assert normalize_locale("en-US") == "en"
    assert normalize_locale("DE_de") == "de"
    assert normalize_locale("unsupported", fallback="en") == "en"
    assert translate("Kandidatenpassung", "en") == "Candidate fit"
    assert translate_text("3 von 5 erkannten Anforderungen passen.", "en") == (
        "3 of 5 detected requirements match."
    )
    assert translate_text("unbefristete Direktanstellung", "en") == (
        "Permanent direct employment"
    )
    assert translate_text("Source-authored text", "en") == "Source-authored text"
