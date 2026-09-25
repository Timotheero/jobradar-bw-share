# ruff: noqa: E501
"""Small, dependency-free presentation translations for the web interface.

German values remain canonical in persistence and service boundaries.  This
module translates only at the presentation boundary so existing application
statuses, filters, feedback, and integrations keep their stable values.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from fastapi import Request

from .config import get_settings

SUPPORTED_LOCALES = ("de", "en")


ENGLISH_TRANSLATIONS: dict[str, str] = {
    # Shared shell and navigation
    "Zum Inhalt springen": "Skip to content",
    "GF-Jobradar – Startseite": "GF Jobradar – home",
    "Assistenz- und Stabsrollen": "Executive support and staff roles",
    "Crawling deaktiviert": "Crawling disabled",
    "Crawling aktiv": "Crawling active",
    "Jobportale: bereit": "Job portals: ready",
    "Jobportale: pausiert": "Job portals: paused",
    "Jobportale: Crawl läuft": "Job portals: crawling",
    "Jobportale: eingeschränkt": "Job portals: limited",
    "Jobportale: Fehler": "Job portals: error",
    "Jobportale: nicht geprüft": "Job portals: not checked",
    "Firmenportale: bereit": "Company sites: ready",
    "Firmenportale: pausiert": "Company sites: paused",
    "Firmenportale: Crawl läuft": "Company sites: crawling",
    "Firmenportale: eingeschränkt": "Company sites: limited",
    "Firmenportale: Fehler": "Company sites: error",
    "Firmenportale: nicht geprüft": "Company sites: not checked",
    "Zuletzt abgerufen": "Last fetched",
    "Stelle": "Job",
    "API-Abrufe aktiv": "API calls active",
    "API-Abrufe deaktiviert": "API calls disabled",
    "APIs: bereit": "APIs: ready",
    "APIs: pausiert": "APIs: paused",
    "API-Abruf läuft": "API retrieval running",
    "APIs: Fehler": "APIs: error",
    "APIs: nicht geprüft": "APIs: not checked",
    "gefunden": "found",
    "neu/24 h": "new/24h",
    "ChatGPT verbunden": "ChatGPT connected",
    "ChatGPT nicht verbunden": "ChatGPT not connected",
    "ChatGPT: nicht geprüft": "ChatGPT: not checked",
    "Navigation öffnen": "Open navigation",
    "Hauptnavigation": "Main navigation",
    "Übersicht": "Overview",
    "Stellen": "Jobs",
    "Bewerbungen": "Applications",
    "Mein Profil": "My profile",
    "Quellen": "Sources",
    "Einstellungen": "Settings",
    "Privater Bereich": "Private area",
    "Zugriff über deinen privaten SSH-Tunnel": "Access through your private SSH tunnel",
    "Sprache": "Language",
    "Deutsch": "German",
    "Englisch": "English",
    # Dashboard
    "Guten Tag": "Hello",
    "Dein Jobradar auf einen Blick": "Your Jobradar at a glance",
    "Deine besten Treffer und wichtigsten Kennzahlen auf einen Blick.": (
        "Your best matches and key metrics at a glance."
    ),
    "Alle Stellen ansehen": "View all jobs",
    "Zusammenfassung": "Summary",
    "Neue Treffer": "New matches",
    "seit deinem letzten Besuch": "since your last visit",
    "Starke Treffer": "Strong matches",
    "mit mindestens 80 % Aufgabennähe": "with at least 80% role relevance",
    "Vorgemerkt": "Saved",
    "für deine engere Auswahl": "for your shortlist",
    "noch nicht abgeschlossen": "not yet closed",
    "Für dich ausgewählt": "Selected for you",
    "Beste neue Treffer": "Best new matches",
    "Alle anzeigen": "View all",
    "Neu": "New",
    "Noch keine Treffer": "No matches yet",
    "Nach dem ersten Suchlauf erscheinen hier die passendsten Stellen.": (
        "The most suitable jobs will appear here after the first search."
    ),
    # Job search
    "Stellensuche": "Job search",
    "Alle Stellen": "All jobs",
    "Passende Stellen": "Priority jobs",
    "Beste Treffer": "Best matches",
    "Passende Stellentitel": "Matching job titles",
    "Öffne eine Stelle für Beschreibung, Arbeitgeber, Standort und Bewertung.": (
        "Open a job to see its description, employer, location, and assessment."
    ),
    "Inhalte und Aufgaben zählen stärker als der Stellentitel.": (
        "Responsibilities and content carry more weight than the job title."
    ),
    "Ansicht": "View",
    "Stellen filtern": "Filter jobs",
    "Stellen durchsuchen": "Search jobs",
    "Titel, Unternehmen oder Aufgabe": "Title, company, or responsibility",
    "Arbeitsort": "Work location",
    "Alle Modelle": "All models",
    "Präsenz": "On-site",
    "Hybrid": "Hybrid",
    "Remote": "Remote",
    "Arbeitszeit": "Working hours",
    "Alle": "All",
    "Vollzeit": "Full-time",
    "Teilzeit": "Part-time",
    "Vertragsart": "Contract type",
    "Unbefristet": "Permanent",
    "unbefristet": "Permanent",
    "Befristet": "Fixed-term",
    "befristet": "Fixed-term",
    "Direktanstellung": "Direct employment",
    "unbefristete Direktanstellung": "Permanent direct employment",
    "Unbefristete Direktanstellung": "Permanent direct employment",
    "befristete Direktanstellung": "Fixed-term direct employment",
    "Befristete Direktanstellung": "Fixed-term direct employment",
    "Aufgabennähe": "Role relevance",
    "ab 60 %": "60% and above",
    "ab 70 %": "70% and above",
    "ab 80 %": "80% and above",
    "Rollenart": "Role category",
    "Alle Rollenarten": "All role categories",
    "Enge Assistenzrollen": "Core executive-assistance roles",
    "Strategisch erweitert": "Strategically broadened",
    "Treffer anzeigen": "Show matches",
    "Suchen": "Search",
    "Weitere Filter": "More filters",
    "Filter anwenden": "Apply filters",
    "Filter zurücksetzen": "Reset filters",
    "{total} Stellen gefunden": "{total} jobs found",
    "Sortieren nach": "Sort by",
    "Beste Passung": "Best match",
    "Neueste zuerst": "Newest first",
    "Weitere Stellen werden geladen": "Loading more jobs",
    "Weitere Stellen laden": "Load more jobs",
    "Kürzeste Fahrtzeit": "Shortest commute",
    "Bewertungen": "Scores",
    "Arbeitgeber": "Employer",
    "Aufgabe": "Role",
    "Du": "You",
    "Details ansehen": "View details",
    "Warum angezeigt": "Why it is shown",
    "Stelle öffnen": "Open job",
    "Keine Stellen passen zu dieser Auswahl": "No jobs match this selection",
    "Setze die Filter zurück oder öffne alle gespeicherten Stellen.": (
        "Reset the filters or open all stored jobs."
    ),
    "Noch keine Stellen im Radar": "No jobs in the radar yet",
    "Quellenstatus ansehen": "View source status",
    "Nach dem ersten Suchlauf erscheinen hier Titel, Arbeitgeber, Standort und Beschreibung.": (
        "Titles, employers, locations, and descriptions will appear here after the first search."
    ),
    "Präsenz in Baden-Württemberg": "On-site in Baden-Württemberg",
    "Deine bevorzugten Stellen vor Ort.": "Your preferred on-site jobs.",
    "Hybrid in Baden-Württemberg": "Hybrid in Baden-Württemberg",
    "Eine Mischung aus Büro und mobilem Arbeiten.": "A mix of office and remote work.",
    "Deutschlandweit Remote": "Remote throughout Germany",
    "Vollständig entfernte Stellen, bewusst getrennt dargestellt.": (
        "Fully remote jobs, intentionally shown separately."
    ),
    "Arbeitsmodell noch nicht angegeben": "Work model not provided yet",
    "Diese Angabe fehlt in der Originalanzeige.": (
        "This information is missing from the original posting."
    ),
    # Job details and application pipeline
    "Brotkrümelnavigation": "Breadcrumb",
    "Details": "Details",
    "Stellendetails": "Job details",
    "Vormerken": "Save",
    "Original ansehen": "View original",
    "Originalanzeige öffnen": "Open original posting",
    "(öffnet neues Fenster)": "(opens in a new window)",
    "Bewerbungsstatus": "Application status",
    "Notiz": "Note",
    "Optional": "Optional",
    "Status speichern": "Save status",
    "Deine Auswertung": "Your assessment",
    "Warum diese Stelle passt": "Why this job may fit",
    "{score} Prozent Aufgabennähe": "{score} percent role relevance",
    "Aufgaben einer GF-Assistenz": "Executive-assistance responsibilities",
    "Kandidatenpassung": "Candidate fit",
    "{score} Prozent Kandidatenpassung": "{score} percent candidate fit",
    "Dein Profil und deine Wünsche": "Your profile and preferences",
    "Passungsgründe": "Reasons for the match",
    "Für diese Stelle liegt noch keine ausführliche Bewertung vor.": (
        "No detailed assessment is available for this job yet."
    ),
    "Darauf solltest du achten": "Points to consider",
    "Stellenbeschreibung": "Job description",
    "Wichtigste Angaben": "Key information",
    "Vergütung": "Compensation",
    "Originalinhalt": "Original content",
    "Das Wichtigste zuerst": "What matters most",
    "Kurze Zusammenfassung": "Short summary",
    "Automatisch mit {model} · {effort}": "Automatically generated with {model} · {effort}",
    "Mittel": "Medium",
    "Die kurze Zusammenfassung wird automatisch erstellt.": (
        "The short summary is being generated automatically."
    ),
    "Noch zu klären": "Still to clarify",
    "Originaltext anzeigen": "Show original text",
    "Vollständige Stellenbeschreibung in lesbarer Formatierung": (
        "Full job description in readable formatting"
    ),
    "Die automatische Zusammenfassung ist derzeit nicht verfügbar.": (
        "The automatic summary is currently unavailable."
    ),
    "Die automatische Zusammenfassung konnte nicht erstellt werden.": (
        "The automatic summary could not be generated."
    ),
    "Einordnung": "Assessment",
    "Bewertung der Stelle": "Job assessment",
    "Meine Auswahl": "My selection",
    "Status und Notiz speichern": "Save status and note",
    "Stellendaten": "Job information",
    "Herkunft der Anzeige": "Posting source",
    "Auf einen Blick": "At a glance",
    "Arbeitsmodell": "Work model",
    "Vertrag": "Contract",
    "Fahrtzeit": "Commute time",
    "Veröffentlicht": "Published",
    "Diese Anzeige wurde über folgende Quellen gefunden:": (
        "This posting was found through the following sources:"
    ),
    "Noch keine Quelle": "No source yet",
    "Der Import wurde noch nicht gestartet.": "The import has not started yet.",
    "Zuletzt gesehen: {date}": "Last seen: {date}",
    "Merkliste": "Saved",
    "Nicht passend": "Not a fit",
    "Bewerbung geplant": "Application planned",
    "Beworben": "Applied",
    "Gespräch": "Interview",
    "Angebot": "Offer",
    "Abgeschlossen": "Closed",
    "Dein Fortschritt": "Your progress",
    "Entwürfe erstellen, unabhängig prüfen und vor jeder weiteren Aktion bearbeiten.": (
        "Create drafts, review them independently, and edit them before any further action."
    ),
    "Stellen auswählen": "Select jobs",
    "Nur Entwürfe": "Drafts only",
    "Bewerbungsentwürfe vorbereiten": "Prepare application drafts",
    "Codex formuliert einen Lebenslaufentwurf und ein Anschreiben. Eine getrennte HR-Prüfung bewertet beide Texte. Es wird nichts versendet.": (
        "Codex drafts a CV and cover letter. A separate HR review evaluates both texts. "
        "Nothing is submitted."
    ),
    "Mindest-Aufgabennähe": "Minimum role relevance",
    "Mindest-Kandidatenpassung": "Minimum candidate fit",
    "Maximale Entwürfe": "Maximum drafts",
    "Entwürfe ab Mindestwerten erstellen": "Create drafts above thresholds",
    "Bewerbungsentwürfe": "Application drafts",
    "Bearbeitbare Texte mit unabhängiger HR-Rückmeldung.": (
        "Editable documents with independent HR feedback."
    ),
    "Version {revision}": "Revision {revision}",
    "HR-Prüfung bestanden": "HR review passed",
    "Überarbeitung empfohlen": "Revision recommended",
    "Erneute HR-Prüfung erforderlich": "HR review required again",
    "Entwurf": "Draft",
    "Noch keine Bewerbungsentwürfe": "No application drafts yet",
    "Wähle Stellen aus oder starte einen Lauf über die Mindestwerte.": (
        "Select jobs or start a run using the score thresholds."
    ),
    "Entwürfe für Auswahl erstellen": "Create drafts for selection",
    "Für Bewerbungsentwurf auswählen": "Select for application draft",
    "Bewerbungsentwurf": "Application draft",
    "Zur Entwurfsübersicht": "Back to draft overview",
    "Kein Versand": "No submission",
    "Diese Seite speichert und prüft nur lokale Entwürfe. Es werden keine Formulare ausgefüllt, Dateien hochgeladen oder Nachrichten versendet.": (
        "This page only stores and reviews local drafts. It does not fill forms, upload "
        "files, or send messages."
    ),
    "Bearbeitbar": "Editable",
    "Unterlagen": "Documents",
    "Lebenslaufentwurf": "CV draft",
    "Motivationsschreiben": "Cover letter",
    "Speichern setzt die bisherige HR-Prüfung zurück.": (
        "Saving resets the previous HR review."
    ),
    "Entwurf speichern": "Save draft",
    "Unabhängige Prüfung": "Independent review",
    "HR-Rückmeldung": "HR feedback",
    "Relevanz": "Relevance",
    "Belegbarkeit": "Evidence",
    "Klarheit": "Clarity",
    "Motivation": "Motivation",
    "Nicht belegte Aussagen": "Unsupported claims",
    "Muss überarbeitet werden": "Must be revised",
    "Optionale Verbesserungen": "Optional improvements",
    "Prüfung erforderlich": "Review required",
    "Der Entwurf wurde bearbeitet und muss erneut unabhängig geprüft werden.": (
        "The draft was edited and must be reviewed independently again."
    ),
    "HR-Prüfung erneut ausführen": "Run HR review again",
    "Offene Fragen": "Open questions",
    "Verwendete Profilbelege": "Profile evidence used",
    "Vor der Bewerbung": "Before applying",
    "Verfügbarkeit der Stellen": "Job availability",
    "Jobradar prüft die Originalanzeige, bevor eine Bewerbung geplant oder ein Entwurf erstellt wird.": (
        "Jobradar checks the original posting before an application is planned or a draft "
        "is created."
    ),
    "Verfügbarkeit prüfen": "Check availability",
    "Zuletzt geprüft: {date}": "Last checked: {date}",
    "Nicht mehr verfügbar": "No longer available",
    "Anzeige nicht mehr verfügbar": "Posting no longer available",
    "Verfügbarkeit zuletzt nicht bestätigt": "Availability not confirmed on the last check",
    "Verfügbarkeit konnte zuletzt nicht bestätigt werden": (
        "Availability could not be confirmed on the last check"
    ),
    "Prüfung nicht möglich": "Unable to check",
    "Die Quelle war nicht erreichbar oder externe Abrufe sind deaktiviert. Bestätige ausdrücklich, wenn du trotzdem fortfahren möchtest.": (
        "The source could not be reached or external retrieval is disabled. Explicitly "
        "confirm if you still want to continue."
    ),
    "Trotz fehlender Bestätigung fortfahren": "Continue without confirmation",
    "Diese Stellen wurden nicht fortgesetzt.": "These jobs were not continued.",
    "Abbrechen und zurück": "Cancel and go back",
    "Zurück": "Back",
    "Crawling": "Crawling",
    "Der Bewerbungsentwurf wurde nicht gefunden.": "The application draft was not found.",
    "Codex ist für Bewerbungsentwürfe nicht eingerichtet.": (
        "Codex is not configured for application drafts."
    ),
    "Codex ist für die HR-Prüfung nicht eingerichtet.": (
        "Codex is not configured for the HR review."
    ),
    "Der Codex-Lauf für die Bewerbungsentwürfe ist fehlgeschlagen.": (
        "The Codex run for the application drafts failed."
    ),
    "Die HR-Prüfung ist fehlgeschlagen.": "The HR review failed.",
    "Neue Entwurfsversion gespeichert. Die bisherige HR-Prüfung wurde zurückgesetzt.": (
        "New draft revision saved. The previous HR review was reset."
    ),
    "Keine Änderungen am Entwurf.": "No changes to the draft.",
    "Die unabhängige HR-Prüfung wurde aktualisiert.": (
        "The independent HR review was updated."
    ),
    "Noch keine Stelle": "No job yet",
    "{score} % Aufgabe": "{score}% role relevance",
    # Profile
    "Persönliche Grundlage": "Personal foundation",
    "Damit der Jobradar erkennt, welche Stellen fachlich und persönlich zu dir passen.": (
        "Help Jobradar identify jobs that fit your experience and preferences."
    ),
    "Profilbereiche": "Profile sections",
    "Qualifikationen": "Qualifications",
    "Stellenwünsche": "Job preferences",
    "Objektive Angaben": "Objective information",
    "Erfahrung und Fähigkeiten": "Experience and skills",
    "{completion} % vollständig": "{completion}% complete",
    "Name": "Name",
    "Aktuelle Tätigkeit": "Current role",
    "Profilzusammenfassung": "Profile summary",
    "Ausbildung": "Education",
    "Zertifikate": "Certifications",
    "E-Mail": "Email",
    "Telefon": "Phone",
    "Wohnort für Pendelzeiten": "Home location for commute times",
    "Startadressen für Pendelzeiten": "Starting addresses for commute times",
    "Startadresse {number}": "Starting address {number}",
    "Adresse hinzufügen": "Add address",
    "Adresse entfernen": "Remove address",
    "Du kannst mehrere Startadressen angeben. Die erste ist die Hauptadresse. Alle bleiben lokal und werden nicht an OpenAI gesendet.": (
        "You can add multiple starting addresses. The first is the primary address. "
        "All stay local and are not sent to OpenAI."
    ),
    "Es können höchstens 10 Startadressen gespeichert werden.": (
        "You can save up to 10 starting addresses."
    ),
    "Eine Startadresse darf höchstens 500 Zeichen lang sein.": (
        "A starting address can contain up to 500 characters."
    ),
    "Startadressen müssen als Liste übergeben werden.": (
        "Starting addresses must be provided as a list."
    ),
    "Jede Startadresse muss Text sein.": "Every starting address must be text.",
    "Bleibt lokal und wird nicht an OpenAI gesendet.": (
        "Stays local and is not sent to OpenAI."
    ),
    "Berufserfahrung": "Professional experience",
    "Jahre": "years",
    "Sprachen": "Languages",
    "z. B. Deutsch, Englisch": "e.g. German, English",
    "Wichtige Fähigkeiten": "Key skills",
    "z. B. Terminorganisation, Korrespondenz, Projektkoordination": (
        "e.g. scheduling, correspondence, project coordination"
    ),
    "Lebenslauf hinzufügen": "Add résumé",
    "Eingelesen: {filename}": "Imported: {filename}",
    "Akzeptiert": "Accepted",
    "Der Lebenslauf wurde erfolgreich eingelesen.": (
        "The résumé was imported successfully."
    ),
    "Erneut hochladen": "Re-upload",
    "Datei ausgewählt": "File selected",
    "{filename} ist ausgewählt. Speichere dein Profil, um die Datei einzulesen.": (
        "{filename} is selected. Save your profile to import the file."
    ),
    "Andere Datei auswählen": "Choose another file",
    "PDF oder DOCX. Erkannte Angaben werden dir vor der Übernahme gezeigt.": (
        "PDF or DOCX. Detected information is shown before you accept it."
    ),
    "Datei auswählen": "Choose file",
    "Erkannte Angaben und Belege prüfen": "Review detected information and evidence",
    "Deine Wünsche": "Your preferences",
    "Welche Stelle suchst du?": "What kind of job are you looking for?",
    "Meine Stellenwünsche als Text": "My job preferences as text",
    "Rollenbegriffe für die Stellensuche": "Role terms for job search",
    "Hauptbegriffe": "Main role terms",
    "Zusätzliche Titel": "Additional titles",
    "Rollenbegriffe": "Role terms",
    "Hauptbegriffe bestimmen die Rollenfamilien. Alle zusätzlichen Titel werden ebenfalls verbindlich in der Quellensuche und lokalen Rollenbewertung verwendet.": (
        "Main terms define the role families. Every additional title is also used as a binding "
        "term in source searches and local role scoring."
    ),
    "Rollenbegriffe auf den zuletzt bestätigten Stand zurückgesetzt.": (
        "Role terms reset to the last approved state."
    ),
    "Ähnliche Begriffe mit KI generieren": "Generate similar terms with AI",
    "Mehrere Hauptbegriffe sind möglich. Füge jeden Begriff als eigene Zeile mit Bindestrich unter der passenden Überschrift ein.": (
        "You can use multiple main terms. Add each term on its own hyphenated line under the "
        "appropriate heading."
    ),
    "Diese Überschrift für Rollenbegriffe wurde nicht erkannt.": (
        "This role-term heading was not recognized."
    ),
    "Rollenbegriffe müssen als Aufzählung mit einem Bindestrich beginnen.": (
        "Role terms must be hyphenated list items."
    ),
    "Mindestens ein Hauptbegriff ist erforderlich.": "At least one main role term is required.",
    "Es sind höchstens 10 Hauptbegriffe und 50 zusätzliche Titel erlaubt.": (
        "At most 10 main role terms and 50 additional titles are allowed."
    ),
    "Die Rollenbegriffe müssen vor der KI-Erweiterung korrigiert werden.": (
        "Correct the role terms before asking AI to expand them."
    ),
    "Die KI-Erweiterung ist derzeit nicht verfügbar.": (
        "AI expansion is currently unavailable."
    ),
    "{count} ähnliche Rollentitel wurden ergänzt. Prüfe sie und wende die Änderung anschließend an.": (
        "{count} similar role titles were added. Review them, then apply the change."
    ),
    "Die KI hat keine neuen Rollentitel außerhalb der bereits gespeicherten Begriffe gefunden.": (
        "AI found no new role titles beyond the terms already saved."
    ),
    "Alle bekannten Einstellungen sind bereits zusammengefasst. Du kannst den Text frei ergänzen oder ersetzen. Er bleibt lokal gespeichert; für den lokalen Job-Match-Score gelten die strukturierten Felder auf dieser Seite.": (
        "All known settings are already summarized. You can freely add to or replace the text. "
        "It is stored locally; the structured fields on this page determine the local Job Match score."
    ),
    "Ja": "Yes",
    "Nein": "No",
    "Arbeitszeit: {value}": "Employment scope: {value}",
    "Vertragsarten: {value}": "Contract types: {value}",
    "Bevorzugte Regionen: {value}": "Preferred regions: {value}",
    "Deutschlandweite Remote-Stellen einbeziehen: {value}": (
        "Include Germany-wide remote jobs: {value}"
    ),
    "Bevorzugtes Arbeitsmodell: {value}": "Preferred work model: {value}",
    "Gewichtung der Arbeitsmodelle: Präsenz {onsite} %, Hybrid {hybrid} %, Remote {remote} %": (
        "Work-model weights: on-site {onsite}%, hybrid {hybrid}%, remote {remote}%"
    ),
    "Fahrtzeit: volle Wertung bis {soft} Minuten; maximal {maximum} Minuten einfach.": (
        "Commute: full score through {soft} minutes; maximum {maximum} minutes one way."
    ),
    "Mindestgehalt pro Jahr: {value}": "Minimum annual salary: {value}",
    "Zielgehalt pro Jahr: {value}": "Target annual salary: {value}",
    "Sprachen: {value}": "Languages: {value}",
    "Zusätzliche Rollenbegriffe: {value}": "Additional role terms: {value}",
    "Weitere gespeicherte Präferenz ({key}): {value}": (
        "Other saved preference ({key}): {value}"
    ),
    "Bevorzugte Branchen: {value}": "Preferred industries: {value}",
    "Bevorzugte Unternehmensgrößen: {value}": "Preferred company sizes: {value}",
    "Maximale Reisetätigkeit: {value}": "Maximum travel: {value}",
    "Bearbeite die erzeugten Zeilen oder ergänze klare Wünsche. Jobradar zeigt dir vor dem Anwenden genau, welche strukturierten Einstellungen daraus entstehen.": (
        "Edit the generated lines or add clear preferences. Before applying anything, "
        "Jobradar shows exactly which structured settings it recognized."
    ),
    "Änderungen wirken sich auf deine Ergebnisse aus": "Changes affect your results",
    "Job-Match-Scores, Rangfolge, sichtbare Empfehlungen und die zukünftige Suchabdeckung können sich ändern. Prüfe die erkannten Einstellungen vor dem Anwenden.": (
        "Job Match scores, ranking, visible recommendations, and future search coverage "
        "can change. Review the recognized settings before applying them."
    ),
    "Zurücksetzen": "Reset",
    "Auf gespeicherten Stand zurücksetzen": "Reset to saved state",
    "Auf den zuletzt bestätigten Stand zurückgesetzt.": (
        "Reset to the last approved state."
    ),
    "Letzte gespeicherte Änderung rückgängig machen": "Undo last saved change",
    "Schritt 1 · Suchrollen": "Step 1 · Search roles",
    "Schritt 2 · Weitere Stellenwünsche": "Step 2 · Other job preferences",
    "Schritt 3": "Step 3",
    "Weiter: prüfen und speichern": "Continue: review and save",
    "Prüfen und speichern": "Review and save",
    "Kontrolliere die erkannten Änderungen. Gespeichert wird erst mit dem nächsten Klick.": (
        "Check the recognized changes. Nothing is saved until the next click."
    ),
    "Noch nicht gespeichert": "Not saved yet",
    "Geprüfte Änderungen speichern": "Save reviewed changes",
    "Speichern und Quellen aktualisieren": "Save and refresh sources",
    "Bitte korrigieren": "Please correct",
    "Letzte Eingabe rückgängig machen": "Undo latest edit",
    "Letzte Eingabe zurückgenommen.": "Latest edit undone.",
    "Änderungen prüfen": "Review changes",
    "Vor dem Anwenden": "Before applying",
    "Erkannte Änderungen prüfen": "Review recognized changes",
    "Noch nicht anwendbar": "Not ready to apply",
    "Zeile {number}": "Line {number}",
    "Keine Einstellungsänderung erkannt": "No settings change recognized",
    "Passe mindestens einen Wert im Text an und prüfe ihn erneut.": (
        "Change at least one value in the text and review it again."
    ),
    "Neuer Quellenabruf": "New source refresh",
    "Neue Suche erforderlich": "New search required",
    "Diese Änderung verschiebt oder erweitert die Suchabdeckung. Vorhandene Ergebnisse werden sofort neu bewertet. Vollständige neue Ergebnisse erscheinen erst nach einem Quellenabruf, der externe APIs oder Crawler kontaktieren kann und Zeit benötigt.": (
        "This change moves or expands search coverage. Existing results are reassessed "
        "immediately. Complete new results appear only after a source refresh, which may "
        "contact external APIs or crawlers and takes time."
    ),
    "Crawling ist derzeit gesperrt. Du kannst die Einstellungen trotzdem anwenden und den Quellenabruf später starten.": (
        "Crawling is currently locked. You can still apply the settings and start the "
        "source refresh later."
    ),
    "Einstellungen anwenden": "Apply settings",
    "Anwenden und Quellen aktualisieren": "Apply and refresh sources",
    "Letzte Änderung rückgängig machen": "Undo last change",
    "Vertragsarten": "Contract types",
    "Bevorzugte Regionen": "Preferred regions",
    "Deutschlandweite Remote-Stellen": "Germany-wide remote jobs",
    "Gewichtung Präsenz": "On-site weight",
    "Gewichtung Hybrid": "Hybrid weight",
    "Gewichtung Remote": "Remote weight",
    "Mindestgehalt pro Jahr": "Minimum annual salary",
    "Zielgehalt pro Jahr": "Target annual salary",
    "Zusätzliche Rollenbegriffe": "Additional role terms",
    "Bevorzugte Branchen": "Preferred industries",
    "Bevorzugte Unternehmensgrößen": "Preferred company sizes",
    "Maximale Reisetätigkeit": "Maximum travel",
    "{minutes} Minuten": "{minutes} minutes",
    "Diese Zeile wurde nicht verstanden und wird nicht angewendet.": (
        "This line was not understood and will not be applied."
    ),
    "Mindestens eine Region ist kein unterstütztes deutsches Bundesland.": (
        "At least one region is not a supported German state."
    ),
    "Bitte Ja oder Nein angeben.": "Enter Yes or No.",
    "Bitte Präsenz, Hybrid oder Remote angeben.": "Enter on-site, hybrid, or remote.",
    "Bitte genau drei Gewichtungen zwischen 0 und 100 Prozent angeben.": (
        "Enter exactly three weights between 0 and 100 percent."
    ),
    "Die Fahrtzeit braucht zwei gültige, aufsteigende Minutenwerte.": (
        "Commute requires two valid ascending minute values."
    ),
    "Das Gehalt ist ungültig.": "The salary is invalid.",
    "Der Prozentwert muss zwischen 0 und 100 liegen.": (
        "The percentage must be between 0 and 100."
    ),
    "Das Zielgehalt darf nicht unter dem Mindestgehalt liegen.": (
        "Target salary cannot be below minimum salary."
    ),
    "Die maximale Fahrtzeit darf nicht unter der Abwertungsgrenze liegen.": (
        "Maximum commute cannot be below the score-reduction threshold."
    ),
    "Es können höchstens fünf Bundesländer gleichzeitig gesucht werden.": (
        "At most five German states can be searched at once."
    ),
    "Eine Präferenzliste ist zu lang.": "A preference list is too long.",
    "Präferenzen angewendet, Ergebnisse neu bewertet und Quellen aktualisiert.": (
        "Preferences applied, results reassessed, and sources refreshed."
    ),
    "Präferenzen angewendet. Der Quellenabruf blieb durch die Crawling-Sperre blockiert.": (
        "Preferences applied. The source refresh remained blocked by the crawling lock."
    ),
    "Präferenzen angewendet und die Neubewertung der vorhandenen Ergebnisse angefordert.": (
        "Preferences applied and reassessment of existing results requested."
    ),
    "Letzte Präferenzänderung rückgängig gemacht. Für vollständige Abdeckung ist ein neuer Quellenabruf nötig.": (
        "Last preference change undone. A new source refresh is required for complete coverage."
    ),
    "Letzte Präferenzänderung rückgängig gemacht.": "Last preference change undone.",
    "Präsenz in {regions}": "On-site in {regions}",
    "Hybrid in {regions}": "Hybrid in {regions}",
    "Branchenpräferenz": "Industry preference",
    "Die Branche entspricht deiner Präferenz.": "The industry matches your preference.",
    "Die Branche weicht von deiner Präferenz ab.": (
        "The industry differs from your preference."
    ),
    "Unternehmensgröße": "Company size",
    "Die Unternehmensgröße entspricht deiner Präferenz.": (
        "The company size matches your preference."
    ),
    "Die Unternehmensgröße weicht von deiner Präferenz ab.": (
        "The company size differs from your preference."
    ),
    "Reisetätigkeit": "Travel",
    "Die Reisetätigkeit liegt innerhalb deiner Grenze.": (
        "Travel is within your configured limit."
    ),
    "Die Reisetätigkeit überschreitet deine Grenze.": (
        "Travel exceeds your configured limit."
    ),
    "Bevorzugtes Arbeitsmodell": "Preferred work model",
    "Präsenz bevorzugt": "Prefer on-site",
    "Deine klare Priorität": "Your clear priority",
    "Teilweise im Büro": "Partly in the office",
    "Nur getrennt anzeigen": "Show separately only",
    "Abwertung der Fahrtzeit ab": "Reduce score for commute times above",
    "Minuten": "minutes",
    "Maximale einfache Fahrtzeit": "Maximum one-way commute",
    "Änderungen führen später zu einer Neubewertung deiner Stellen.": (
        "Changes will trigger a reassessment of your jobs."
    ),
    "Profil speichern": "Save profile",
    "Profil bestätigen": "Confirm profile",
    "Prüfe die erkannten Angaben oben. Erst nach deiner Bestätigung wird dieses Profil für die Kandidatenpassung verwendet.": (
        "Review the detected information above. This profile is used for candidate fit only after you confirm it."
    ),
    "Angaben sind geprüft": "I have reviewed the information",
    "Lebenslauf lokal eingelesen. Bitte prüfe und bestätige die Angaben.": (
        "Résumé imported locally. Please review and confirm the information."
    ),
    "Profil bestätigt und zur Bewertung freigegeben.": (
        "Profile confirmed and approved for assessment."
    ),
    "Profil gespeichert.": "Profile saved.",
    # Sources
    "Datenabdeckung": "Data coverage",
    "Kostenlose Schnittstellen zuerst, lokales Crawling ergänzend.": (
        "Free interfaces first, supplemented by local crawling."
    ),
    "Jetzt suchen": "Search now",
    "Hier verwaltest du Datenquellen und startest bei Bedarf genau einen manuellen Abruf.": (
        "Manage data sources here and start exactly one manual refresh when needed."
    ),
    "Einmal aktualisieren": "Refresh once",
    "API-Abrufe sind deaktiviert": "API retrieval is disabled",
    "Die kostenlosen APIs und Feeds werden erst abgerufen, wenn der Serverzugriff und der API-Schalter in den Einstellungen aktiv sind.": (
        "Free APIs and feeds are fetched only when server access and the API switch in "
        "Settings are enabled."
    ),
    "API-Abrufe sind freigegeben.": "API retrieval is enabled.",
    "Öffentliche APIs und Feeds dürfen automatisch und manuell aktualisiert werden.": (
        "Public APIs and feeds may be refreshed automatically or manually."
    ),
    "Firecrawl ist gesperrt.": "Firecrawl is disabled.",
    "Webseiten werden erst abgerufen, wenn die Server- und Anwendungsfreigaben für Firecrawl aktiv sind.": (
        "Websites are fetched only when Firecrawl is allowed by both the server and the "
        "application setting."
    ),
    "Aktive Quellen": "Active sources",
    "von {total} eingerichtet": "of {total} configured",
    "Letzter Suchlauf": "Last search",
    "Gespeicherte Stellen": "Stored jobs",
    "quellenübergreifend bereinigt": "deduplicated across sources",
    "Eingerichtete Dienste": "Configured services",
    "Quellenstatus": "Source status",
    "Quellenarten": "Source types",
    "API und Feeds": "APIs and feeds",
    "API-Quellen": "API sources",
    "Firecrawl ist freigegeben.": "Firecrawl is enabled.",
    "Firmenportale werden von ihren Startseiten gecrawlt; Jobportale werden innerhalb der "
    "freigegebenen Domains nach passenden Stellen durchsucht.": (
        "Company sites are crawled from their starting pages; job portals are searched for "
        "matching jobs within the approved domains."
    ),
    "Crawler-Status": "Crawler status",
    "Dauerbetrieb": "Continuous operation",
    "Live-Crawler steuern": "Control live crawler",
    "Dauerhaften Live-Crawler aktivieren": "Enable continuous live crawler",
    "Steuert alle konfigurierten Jobportale und Unternehmensseiten. Ein bereits laufender Durchgang wird sicher beendet.": (
        "Controls all configured job portals and company sites. A run already in progress "
        "finishes safely."
    ),
    "Crawler-Einstellung speichern": "Save crawler setting",
    "Crawler-Einstellung wurde gespeichert.": "Crawler setting saved.",
    "Noch kein Crawler eingerichtet.": "No crawler configured yet.",
    "Crawl-Ziele": "Crawl targets",
    "Haupt-Jobportale und konkrete Unternehmensseiten werden getrennt geführt. BA Jobsuche bleibt eine API-Quelle und wird nicht gecrawlt.": (
        "Major job portals and specific company sites are managed separately. BA Jobsuche "
        "remains an API source and is not crawled."
    ),
    "Zielart": "Target type",
    "Jobportal": "Job portal",
    "Unternehmensseite": "Company site",
    "Crawl-Ziele filtern": "Filter crawl targets",
    "Jobportale": "Job portals",
    "Unternehmensseiten": "Company sites",
    "Noch keine Crawl-Ziele konfiguriert.": "No crawl targets configured yet.",
    "Keine Crawl-Ziele in diesem Filter.": "No crawl targets in this filter.",
    "Unbekannte Crawl-Zielart.": "Unknown crawl target type.",
    "Quelle": "Source",
    "Verfahren": "Method",
    "Status": "Status",
    "Letzte Prüfung": "Last checked",
    "Gefundene Stellen": "Jobs found",
    "Zuletzt gecrawlt": "Last crawled",
    "Im letzten Crawl gefunden": "Found in last crawl",
    "Keine API- oder Feed-Quellen konfiguriert.": (
        "No API or feed sources are configured."
    ),
    "Direkte Arbeitgeberquellen": "Direct employer sources",
    "Öffentlichen Arbeitgeber-Feed ergänzen": "Add public employer feed",
    "Trage die Board-Kennung oder Karriere-URL eines Arbeitgebers ein. Unterstützt werden Greenhouse, Lever, Ashby, SmartRecruiters, Personio und weitere öffentliche Karriereplattformen. Das Speichern ruft noch keine Daten ab.": (
        "Enter an employer's board identifier or careers URL. Greenhouse, Lever, Ashby, "
        "SmartRecruiters, Personio, and other public career platforms are supported. "
        "Saving does not fetch any data."
    ),
    "Anbieter": "Provider",
    "Board-Kennung oder Karriere-URL": "Board identifier or careers URL",
    "Arbeitgeber (optional)": "Employer (optional)",
    "Feed speichern": "Save feed",
    "Greenhouse – konfigurierte Arbeitgeber": "Greenhouse – configured employers",
    "Lever – konfigurierte Arbeitgeber": "Lever – configured employers",
    "Ashby – konfigurierte Arbeitgeber": "Ashby – configured employers",
    "SmartRecruiters – konfigurierte Arbeitgeber": (
        "SmartRecruiters – configured employers"
    ),
    "Personio – konfigurierte Arbeitgeber": "Personio – configured employers",
    "JOIN – konfigurierte Arbeitgeber": "JOIN – configured employers",
    "softgarden – konfigurierte Arbeitgeber": "softgarden – configured employers",
    "SuccessFactors – konfigurierte Arbeitgeber": "SuccessFactors – configured employers",
    "Workday – konfigurierte Arbeitgeber": "Workday – configured employers",
    "Workable – konfigurierte Arbeitgeber": "Workable – configured employers",
    "Recruitee – konfigurierte Arbeitgeber": "Recruitee – configured employers",
    "Teamtailor – konfigurierte Arbeitgeber": "Teamtailor – configured employers",
    "iCIMS – konfigurierte Arbeitgeber": "iCIMS – configured employers",
    "Oracle Recruiting Cloud – konfigurierte Arbeitgeber": (
        "Oracle Recruiting Cloud – configured employers"
    ),
    "Phenom – konfigurierte Arbeitgeber": "Phenom – configured employers",
    "Radancy – konfigurierte Arbeitgeber": "Radancy – configured employers",
    "BeeSite – konfigurierte Arbeitgeber": "BeeSite – configured employers",
    "Bitte eine Workday-Karriere-URL mit Tenant und Site angeben.": (
        "Enter a Workday careers URL containing a tenant and site."
    ),
    "Lokaler Crawler": "Local crawler",
    "Firecrawl-Ziel ergänzen": "Add Firecrawl target",
    "Trage eine Karriere- oder Stellenübersichtsseite ein. Das Speichern selbst ruft die Seite noch nicht ab. Für den späteren Abruf ist die separate Firecrawl-Freigabe unter Einstellungen erforderlich.": (
        "Enter a careers or job-listing page. Saving does not fetch it. A later request "
        "requires the separate Firecrawl permission in Settings."
    ),
    "Startadresse": "Starting URL",
    "Ziel speichern": "Save target",
    "So arbeitet die Quellensuche": "How source search works",
    "Der Jobradar nutzt kostenlose APIs und Feeds, wo immer sie verfügbar sind. Der lokale Firecrawl-Server ergänzt fehlende Karriereportale. Doppelte Anzeigen werden zusammengeführt, ihre Herkunft bleibt sichtbar.": (
        "Jobradar uses free APIs and feeds wherever available. The local Firecrawl server supplements missing careers portals. Duplicate postings are merged while their sources remain visible."
    ),
    "Pausiert": "Paused",
    "Bereit": "Ready",
    "Fehler": "Error",
    "Läuft": "Running",
    "Noch nicht geprüft": "Not checked yet",
    "Ohne Treffer": "No results",
    "Eingeschränkt": "Limited",
    "Extraktion unzureichend": "Insufficient extraction",
    "Blockiert": "Blocked",
    "Rate-Limit": "Rate limited",
    "Letzter Crawlerlauf fehlgeschlagen.": "The latest crawler run failed.",
    "Letzter Crawlerlauf teilweise fehlgeschlagen.": (
        "The latest crawler run partially failed."
    ),
    "Erfolgreich": "Successful",
    "Teilerfolg": "Partially successful",
    "0 Stellen übernommen": "0 jobs accepted",
    "0 Stellen gefunden": "0 jobs found",
    "Technischer Fehler": "Technical error",
    "Kein Crawl gespeichert": "No crawl stored",
    "Exakte technische Fehler": "Exact technical errors",
    "Kein Laufzeitstatus gespeichert.": "No runtime status stored.",
    "Crawlerlauf wird gerade ausgeführt.": "A crawler run is currently in progress.",
    "Letzter Crawlerlauf erfolgreich abgeschlossen.": (
        "The latest crawler run completed successfully."
    ),
    "Crawler ist deaktiviert.": "The crawler is disabled.",
    "Noch kein abgeschlossener Crawlerlauf gespeichert.": (
        "No completed crawler run has been stored yet."
    ),
    "Keine genaue Fehlermeldung gespeichert.": "No exact error message was stored.",
    "Noch kein abgeschlossener Crawl für dieses Ziel gespeichert.": (
        "No completed crawl has been stored for this target yet."
    ),
    (
        "Dokumente geprüft: {documents} · Stellen übernommen: {records} · "
        "Seiten verworfen: {rejected} · Duplikate übersprungen: {duplicates} · "
        "Durch Suchfilter ausgeschlossen: {filtered} · Technische Fehler: {errors}"
    ): (
        "Documents checked: {documents} · Jobs accepted: {records} · "
        "Pages rejected: {rejected} · Duplicates skipped: {duplicates} · "
        "Excluded by search filters: {filtered} · Technical errors: {errors}"
    ),
    "Der Crawler ist freigegeben, wurde aber noch nicht erfolgreich ausgeführt.": (
        "The crawler is enabled but has not completed successfully yet."
    ),
    "Vorbereitet": "Prepared",
    "Offizielle Quelle": "Official source",
    "Zusätzliche Quelle": "Additional source",
    "Kostenlose API": "Free API",
    "Gefundene Quelle": "Discovered source",
    "Lokale Beispieldaten": "Local sample data",
    "Noch nie": "Never",
    "Noch nicht gestartet": "Not started yet",
    "Automatische API-Abrufe aktiv": "Automatic API retrieval active",
    "Manueller Suchlauf wurde abgeschlossen.": "Manual search completed.",
    "Firecrawl-Ziel wurde gespeichert.": "Firecrawl target saved.",
    "Arbeitgeber-Feed wurde gespeichert.": "Employer feed saved.",
    "Unbekannter Feed-Anbieter.": "Unknown feed provider.",
    "Bitte eine gültige Board-Kennung oder Anbieter-URL angeben.": (
        "Enter a valid board identifier or provider URL."
    ),
    "Der Arbeitgebername ist zu lang.": "The employer name is too long.",
    "Feed-Quelle fehlt.": "The feed source is unavailable.",
    "Zu viele Arbeitgeber-Feeds.": "Too many employer feeds.",
    # Settings
    "Dein System": "Your system",
    "Alle wichtigen Schalter verständlich an einem Ort.": (
        "All important controls explained in one place."
    ),
    "Hier steuerst du nur Verbindungen, Automatik und Benachrichtigungen. Stellenfilter stehen ausschließlich in der Stellensuche.": (
        "This page controls only connections, automation, and notifications. Job filters are available only in the job search."
    ),
    "ChatGPT-Verbindung": "ChatGPT connection",
    "Für ausführliche Passungsbegründungen und Zusammenfassungen.": (
        "For detailed match explanations and summaries."
    ),
    "Verbunden": "Connected",
    "Noch nicht verbunden": "Not connected yet",
    "ChatGPT-Anmeldung öffnen": "Open ChatGPT sign-in",
    "Öffne": "Open",
    "und gib diesen Code ein:": "and enter this code:",
    "Öffne {url} und gib diesen Code ein:": "Open {url} and enter this code:",
    "Der Verbindungscode bleibt verborgen, bis du ihn benötigst.": (
        "The connection code stays hidden until you need it."
    ),
    "Verbindungscode anzeigen": "Show connection code",
    "Verbindungscode ausblenden": "Hide connection code",
    "ChatGPT ist serverseitig deaktiviert": "ChatGPT is disabled on the server",
    "Aktiviere": "Enable",
    "Starte Jobradar anschließend neu. Bis dahin werden keine Profildaten an den Codex App Server gesendet.": (
        "Then restart Jobradar. Until then, no profile data is sent to the Codex App Server."
    ),
    "Mit ChatGPT verbinden": "Connect ChatGPT",
    "Die Anmeldung nutzt einen Gerätecode und deinen ChatGPT-Plan.": (
        "Sign-in uses a device code and your ChatGPT plan."
    ),
    "ChatGPT-Verbindung prüfen": "Check ChatGPT connection",
    "Die gespeicherte ChatGPT-Anmeldung ist abgelaufen oder derzeit nicht erreichbar. Verbinde dein Konto erneut.": (
        "The saved ChatGPT sign-in has expired or is currently unavailable. Reconnect your account."
    ),
    "Erneut mit ChatGPT verbinden": "Reconnect ChatGPT",
    "Der Codex App Server ist mit deinem ChatGPT-Konto verbunden. Vor jedem Batch wird das gemeldete Kontingent geprüft.": (
        "The Codex App Server is connected to your ChatGPT account. Reported quota is checked before every batch."
    ),
    "Automatische API-Abrufe": "Automatic API retrieval",
    "Kostenlose APIs und Arbeitgeber-Feeds benötigen keine eigene Crawl-Freigabe. Dieser Schalter steuert automatische und manuelle Aktualisierungen.": (
        "Free APIs and employer feeds need no separate crawling permission. This switch "
        "controls automatic and manual refreshes."
    ),
    "Aktiv": "Active",
    "Deaktiviert": "Disabled",
    "API- und Feed-Abrufe aktivieren": "Enable API and feed retrieval",
    "Automatische Ausführung standardmäßig alle sechs Stunden": (
        "Automatic execution every six hours by default"
    ),
    "Quellen prüfen oder einmal manuell aktualisieren": (
        "Review sources or refresh once manually"
    ),
    "Der Server-Schalter CRAWLING_ENABLED ist noch aus. Externe Quellen können nicht abgerufen werden.": (
        "The server switch CRAWLING_ENABLED is off. External sources cannot be fetched."
    ),
    "Firecrawl-Crawling": "Firecrawl crawling",
    "Webseitenabrufe benötigen zusätzlich zu den API-Abrufen eine eigene ausdrückliche Freigabe.": (
        "Website retrieval requires its own explicit permission in addition to API access."
    ),
    "Webseiten mit Firecrawl abrufen": "Fetch websites with Firecrawl",
    "Standardmäßig ausgeschaltet und unabhängig zu bestätigen": (
        "Off by default and confirmed separately"
    ),
    "Der Server-Schalter FIRECRAWL_ENABLED ist noch aus. Die Oberfläche kann ihn nicht umgehen.": (
        "The server switch FIRECRAWL_ENABLED is off. The interface cannot bypass it."
    ),
    "Standardansicht": "Default view",
    "Welche Stellen dir zuerst angezeigt werden.": "Which jobs are shown first.",
    "Nur Vollzeit anzeigen": "Show full-time jobs only",
    "Andere Arbeitszeiten bleiben unter „Alle Stellen“ verfügbar.": (
        "Other working-hour models remain available under “All jobs”."
    ),
    "Unbefristete Direktanstellung bevorzugen": "Prefer permanent direct employment",
    "Andere Vertragsformen bleiben filterbar.": "Other contract types remain filterable.",
    "Strategisch erweiterte Rollen getrennt zeigen": (
        "Show strategically broadened roles separately"
    ),
    "Zum Beispiel Geschäftsführungsreferent oder Chief of Staff.": (
        "For example, executive office advisor or Chief of Staff."
    ),
    "Benachrichtigungen": "Notifications",
    "Auf Wunsch Hinweise zu besonders guten Treffern.": (
        "Optional alerts for particularly strong matches."
    ),
    "Benachrichtigungen aktivieren": "Enable notifications",
    "Bleibt standardmäßig ausgeschaltet.": "Remains off by default.",
    "Einstellungen werden nur in deinem System gespeichert.": (
        "Settings are stored only in your system."
    ),
    "Einstellungen speichern": "Save settings",
    "Einstellungen gespeichert.": "Settings saved.",
    # Empty states, dates, and fallbacks
    "Stelle nicht gefunden": "Job not found",
    "Diese Stelle wurde nicht gefunden": "This job was not found",
    "Vielleicht wurde sie entfernt oder noch nicht importiert.": (
        "It may have been removed or may not have been imported yet."
    ),
    "Zur Stellenübersicht": "Back to jobs",
    "Datum unbekannt": "Date unknown",
    "Heute veröffentlicht": "Published today",
    "Gestern veröffentlicht": "Published yesterday",
    "Vor {days} Tagen veröffentlicht": "Published {days} days ago",
    "Veröffentlicht am {date}": "Published on {date}",
    "Die vollständige Beschreibung ist noch nicht verfügbar.": (
        "The full description is not available yet."
    ),
    "Arbeitgeber nicht angegeben": "Employer not provided",
    "Arbeitsort nicht angegeben": "Work location not provided",
    "Nicht angegeben": "Not provided",
    "Vertrag nicht angegeben": "Contract not provided",
    "Quelle unbekannt": "Unknown source",
    "Noch nicht berechnet": "Not calculated yet",
    # Built-in score explanations shown with imported jobs
    "Direkte Unterstützung der Geschäftsführung": "Direct support for executive management",
    "Die Tätigkeitsbeschreibung enthält passende Aufgaben.": (
        "The job description contains relevant responsibilities."
    ),
    "Für diesen Aufgabenbereich wurde kein eindeutiger Hinweis gefunden.": (
        "No clear evidence was found for this responsibility area."
    ),
    "Der Titel unterstützt die Einordnung; Aufgaben bleiben stärker gewichtet.": (
        "The title supports the classification; responsibilities remain weighted more heavily."
    ),
    "Der Titel allein liefert keinen Treffer.": "The title alone does not provide a match.",
    "Die Anzeige scheint eine fachfremde Assistenzrolle zu beschreiben.": (
        "The posting appears to describe an unrelated assistant role."
    ),
    "Anforderungen oder bestätigte Profilkompetenzen fehlen.": (
        "Requirements or confirmed profile skills are missing."
    ),
    "Die Arbeitszeit passt.": "The working hours match.",
    "Die Arbeitszeit weicht ab.": "The working hours differ.",
    "Die Vertragsart passt.": "The contract type matches.",
    "Die Vertragsart weicht ab.": "The contract type differs.",
    "Die Sprache ist im Profil vorhanden.": "The language is present in the profile.",
    "Die Sprache fehlt im Profil.": "The language is missing from the profile.",
    "Die Punktzahl folgt den sichtbaren Präsenz-/Hybrid-/Remote-Gewichten.": (
        "The score follows the visible on-site, hybrid, and remote weights."
    ),
    "Abwertung beginnt am eingestellten Schwellenwert.": (
        "The reduction begins at the configured threshold."
    ),
    "Keine lokale Fahrtzeit vorhanden; daher keine Abwertung.": (
        "No local commute time is available, so no reduction is applied."
    ),
    "Bewertung anhand der eingestellten Mindest- und Zielwerte.": (
        "Assessment based on the configured minimum and target values."
    ),
    "Keine belastbare Gehaltsangabe; deshalb keine Abwertung.": (
        "No reliable salary information is available, so no reduction is applied."
    ),
    "Der neutrale Startwert bleibt sichtbar, bis Angaben vorliegen.": (
        "The neutral starting score remains visible until information is available."
    ),
    # HTTP and operational messages
    "Ungültiger Anfrageursprung": "Invalid request origin",
    "Bitte eine gültige HTTP(S)-Adresse angeben.": "Please enter a valid HTTP(S) URL.",
    "Quellenmodul fehlt.": "The sources module is unavailable.",
    "Firecrawl-Quelle fehlt.": "The Firecrawl source is unavailable.",
    "Zu viele Firecrawl-Ziele.": "Too many Firecrawl targets.",
    "Codex-Adapter fehlt.": "The Codex adapter is unavailable.",
    "Codex-Adapter ist nicht eingerichtet.": "The Codex adapter is not configured.",
    # Local résumé import validation
    "Die Lebenslaufdatei muss als Bytes uebergeben werden.": (
        "The résumé file must be provided as bytes."
    ),
    "Die Lebenslaufdatei ist leer.": "The résumé file is empty.",
    "Es wurde kein lesbarer Text gefunden. Ein gescannter Lebenslauf benoetigt spaeter OCR.": (
        "No readable text was found. A scanned résumé will require OCR later."
    ),
    (
        "Aus dem PDF wurde nur sehr wenig Text gelesen. Bei einem gescannten oder grafisch "
        "aufgebauten Lebenslauf pruefe bitte die erkannten Angaben besonders genau."
    ): (
        "Only a small amount of text could be read from the PDF. For a scanned or "
        "graphically designed résumé, review the detected information especially carefully."
    ),
    "Nur Lebenslaeufe im PDF- oder DOCX-Format werden unterstuetzt.": (
        "Only résumés in PDF or DOCX format are supported."
    ),
    "Der gemeldete Dateityp ist weder PDF noch DOCX.": (
        "The reported file type is neither PDF nor DOCX."
    ),
    "Dateiendung und gemeldeter Dateityp passen nicht zusammen.": (
        "The file extension and reported file type do not match."
    ),
    "Die Datei ist weder als PDF noch als DOCX erkennbar.": (
        "The file cannot be identified as either PDF or DOCX."
    ),
    "Der Dateiinhalt passt nicht zum angegebenen PDF- oder DOCX-Format.": (
        "The file content does not match the specified PDF or DOCX format."
    ),
    "Passwortgeschuetzte PDF-Dateien koennen nicht eingelesen werden.": (
        "Password-protected PDF files cannot be imported."
    ),
    "Der aus dem PDF gelesene Text ist ungewoehnlich gross.": (
        "The text extracted from the PDF is unusually large."
    ),
    "Das PDF ist beschaedigt oder kann nicht sicher gelesen werden.": (
        "The PDF is damaged or cannot be read safely."
    ),
    "Die DOCX-Datei ist beschaedigt oder unvollstaendig.": (
        "The DOCX file is damaged or incomplete."
    ),
    "Die DOCX-Datei enthaelt ungewoehnlich viele Bestandteile.": (
        "The DOCX file contains an unusually large number of components."
    ),
    "Die DOCX-Datei enthaelt einen unsicheren Archivpfad.": (
        "The DOCX file contains an unsafe archive path."
    ),
    "Passwortgeschuetzte DOCX-Dateien koennen nicht eingelesen werden.": (
        "Password-protected DOCX files cannot be imported."
    ),
    "Die entpackte DOCX-Datei ist ungewoehnlich gross.": (
        "The unpacked DOCX file is unusually large."
    ),
    "Die Datei ist kein vollstaendiges DOCX-Dokument.": (
        "The file is not a complete DOCX document."
    ),
    "Das Archiv ist kein unterstuetztes Word-Dokument.": (
        "The archive is not a supported Word document."
    ),
    "Die DOCX-Datei ist beschaedigt oder kann nicht sicher gelesen werden.": (
        "The DOCX file is damaged or cannot be read safely."
    ),
    "Die DOCX-Datei enthaelt nicht erlaubte XML-Deklarationen.": (
        "The DOCX file contains disallowed XML declarations."
    ),
    "Die DOCX-Datei enthaelt beschaedigte Textdaten.": (
        "The DOCX file contains damaged text data."
    ),
    "Der gelesene Lebenslauftext ist ungewoehnlich gross.": (
        "The extracted résumé text is unusually large."
    ),
    # Runtime lock and optional Codex messages
    "Synchronisierung ist gesperrt.": "Synchronization is blocked.",
    "Serverfreigabe CRAWLING_ENABLED ist deaktiviert.": (
        "The server permission CRAWLING_ENABLED is disabled."
    ),
    "Der Anwendungsschalter fuer API- und Feed-Abrufe ist deaktiviert.": (
        "The application switch for API and feed retrieval is disabled."
    ),
    "API- und Feed-Abrufe bleiben gesperrt: CRAWLING_ENABLED ist auf dem Server nicht aktiviert.": (
        "API and feed retrieval remains blocked because CRAWLING_ENABLED is not active "
        "on the server."
    ),
    "API- und Feed-Abrufe sind gesperrt. Serverfreigabe und Anwendungsschalter müssen aktiv sein.": (
        "API and feed retrieval is blocked. Both the server permission and application "
        "switch must be active."
    ),
    "Firecrawl bleibt gesperrt: CRAWLING_ENABLED ist auf dem Server nicht aktiviert.": (
        "Firecrawl remains blocked because CRAWLING_ENABLED is not active on the server."
    ),
    "Firecrawl bleibt gesperrt: FIRECRAWL_ENABLED ist auf dem Server nicht aktiviert.": (
        "Firecrawl remains blocked because FIRECRAWL_ENABLED is not active on the server."
    ),
    "Die separate Firecrawl-Freigabe in den Einstellungen ist deaktiviert.": (
        "The separate Firecrawl permission in Settings is disabled."
    ),
    "Der konfigurierte Mindestrest des ChatGPT-Kontingents wurde erreicht.": (
        "The configured minimum remaining ChatGPT quota has been reached."
    ),
    "Der Codex-Lauf endete ohne Analyseergebnis.": (
        "The Codex run ended without an analysis result."
    ),
    "Die Codex-Anbindung ist gesperrt. CODEX_ENABLED muss bewusst aktiviert werden.": (
        "The Codex connection is blocked. CODEX_ENABLED must be enabled explicitly."
    ),
    "CODEX_COMMAND ist leer.": "CODEX_COMMAND is empty.",
    "Der Codex App Server ist nicht installiert oder nicht startbar.": (
        "The Codex App Server is not installed or cannot be started."
    ),
    "Der Codex App Server wurde beendet.": "The Codex App Server stopped.",
    "Der Codex App Server laeuft nicht.": "The Codex App Server is not running.",
    "Keine rechtzeitige Antwort vom Codex App Server.": (
        "The Codex App Server did not respond in time."
    ),
    "Verbindung zum Codex App Server verloren.": "Connection to the Codex App Server was lost.",
    "Das Analyseergebnis ist kein gueltiges JSON.": "The analysis result is not valid JSON.",
    "Das Analyseergebnis hat nicht die erwartete Form.": (
        "The analysis result does not have the expected shape."
    ),
    "Ein Analyseergebnis ist ungueltig.": "An analysis result is invalid.",
    "Das Analyseergebnis enthaelt eine unbekannte Stellen-ID.": (
        "The analysis result contains an unknown job ID."
    ),
    "Das Analyseergebnis enthaelt eine unbekannte Kategorie.": (
        "The analysis result contains an unknown category."
    ),
    "Das Analyseergebnis ist unvollstaendig.": "The analysis result is incomplete.",
    "Ein numerischer Analysewert fehlt.": "A numeric analysis value is missing.",
    "Vorhandene Entscheidungen waren fuer diese Stelle nicht aehnlich genug; deshalb bleibt das Praeferenzsignal neutral.": (
        "Existing decisions were not similar enough to this job, so the preference signal remains neutral."
    ),
    "Noch keine unterstuetzten Merken-/Ablehnen-Entscheidungen vorhanden.": (
        "No supported save or reject decisions are available yet."
    ),
    # Command-line interface
    "Jobradar BW verwalten": "Manage Jobradar BW",
    "Datenbanktabellen anlegen": "Create database tables",
    "Lokale Beispieldaten anlegen": "Create local sample data",
    "Sicheren Laufzeitstatus ausgeben": "Show safe runtime status",
    "Webanwendung starten": "Start the web application",
    "Einmalige Synchronisierung anfordern": "Request a one-time synchronization",
    "Datenbank ist bereit.": "The database is ready.",
    "{count} Beispieldatensaetze wurden angelegt oder aktualisiert.": (
        "{count} sample records were created or updated."
    ),
    "{count} Quellenlaeufe abgeschlossen.": "{count} source runs completed.",
}


_ENGLISH_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^(\d+) von (\d+) erkannten Anforderungen passen\.$"),
        r"\1 of \2 detected requirements match.",
    ),
    (re.compile(r"^(\d+) Minuten$"), r"\1 minutes"),
    (re.compile(r"^([\d.]+) EUR/Jahr$"), r"\1 EUR/year"),
    (
        re.compile(r"^Die Lebenslaufdatei ist groesser als (\d+) MB\.$"),
        r"The résumé file is larger than \1 MB.",
    ),
    (re.compile(r"^Das PDF hat mehr als (\d+) Seiten\.$"), r"The PDF has more than \1 pages."),
    (
        re.compile(r"^Serverfreigabe ([A-Z_]+) ist deaktiviert\.$"),
        r"The server permission \1 is disabled.",
    ),
    (
        re.compile(r"^Zeitueberschreitung bei (.+)\.$"),
        r"Timed out while running \1.",
    ),
    (
        re.compile(r"^Unerwartete Antwort auf (.+)\.$"),
        r"Unexpected response to \1.",
    ),
    (
        re.compile(r"^Der App Server lieferte keine (.+)-ID\.$"),
        r"The App Server did not return a \1 ID.",
    ),
)


def normalize_locale(value: str | None, *, fallback: str = "de") -> str:
    """Return one supported two-letter locale without trusting arbitrary input."""

    token = (value or "").strip().casefold().replace("_", "-").split("-", 1)[0]
    if token in SUPPORTED_LOCALES:
        return token
    fallback_token = fallback.strip().casefold().replace("_", "-").split("-", 1)[0]
    return fallback_token if fallback_token in SUPPORTED_LOCALES else "de"


def locale_from_request(request: Request) -> str:
    """Resolve the locale from the signed session, then the deployment default."""

    session = request.scope.get("session")
    selected = session.get("locale") if isinstance(session, Mapping) else None
    runtime = getattr(request.app.state, "settings", None) or get_settings()
    default = str(getattr(runtime, "default_locale", "de"))
    return normalize_locale(str(selected) if selected else None, fallback=default)


def translate(message: str, locale: str, **values: Any) -> str:
    """Translate a known presentation message and interpolate trusted placeholders."""

    translated = ENGLISH_TRANSLATIONS.get(message, message) if locale == "en" else message
    return translated.format(**values) if values else translated


def translate_text(value: str | None, locale: str) -> str:
    """Translate known persisted explanations while preserving source-authored text."""

    if not value:
        return ""
    if locale != "en":
        return value
    direct = ENGLISH_TRANSLATIONS.get(value)
    if direct is not None:
        return direct
    for pattern, replacement in _ENGLISH_PATTERNS:
        if pattern.fullmatch(value):
            return pattern.sub(replacement, value)
    return value


__all__ = [
    "ENGLISH_TRANSLATIONS",
    "SUPPORTED_LOCALES",
    "locale_from_request",
    "normalize_locale",
    "translate",
    "translate_text",
]
