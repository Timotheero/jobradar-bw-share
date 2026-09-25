from __future__ import annotations

from jobradar.connectors.ba_jobsuche import BAJobsucheConnector
from jobradar.models import PreferenceProfile
from jobradar.services.preference_prompt import compile_preference_prompt
from jobradar.services.sync import query_plan_for_preferences


def _preferences() -> PreferenceProfile:
    return PreferenceProfile(
        employment_types=["Vollzeit"],
        contract_types=["unbefristet", "Direktanstellung"],
        preferred_states=["Baden-Württemberg"],
        include_germany_remote=True,
        commute_penalty_minutes=60,
        max_commute_minutes=75,
        onsite_weight=1.0,
        hybrid_weight=0.55,
        remote_weight=0.2,
        min_salary=None,
        target_salary=None,
        languages=["Deutsch", "Englisch"],
        primary_role_terms=["Assistenz der Geschäftsführung", "Executive Assistant"],
        additional_role_terms=[],
        extra_preferences={"workplace_preference": "onsite"},
    )


def test_compiles_state_change_and_marks_new_source_coverage() -> None:
    preferences = _preferences()

    plan = compile_preference_prompt(preferences, "Preferred regions: Bavaria")

    assert plan.valid
    assert plan.coverage_changed
    assert plan.values["preferred_states"] == ["Bayern"]
    assert [(change.field, change.before, change.after) for change in plan.changes] == [
        ("preferred_states", ["Baden-Württemberg"], ["Bayern"])
    ]


def test_compiles_extended_scoring_preferences_and_workplace_defaults() -> None:
    preferences = _preferences()
    prompt = """Preferred work model: Hybrid
Work-model weights: on-site 100%, hybrid 55%, remote 20%
Preferred industries: Technology, Healthcare
Preferred company sizes: 200-1000, 1000+
Maximum travel: 10%
Minimum annual salary: 60,000 EUR
Target annual salary: 70,000 EUR"""

    plan = compile_preference_prompt(preferences, prompt)

    assert plan.valid
    assert plan.values["extra_preferences"]["workplace_preference"] == "hybrid"
    assert (
        plan.values["onsite_weight"],
        plan.values["hybrid_weight"],
        plan.values["remote_weight"],
    ) == (0.75, 1.0, 0.35)
    assert plan.values["extra_preferences"]["industries"] == ["Technology", "Healthcare"]
    assert plan.values["extra_preferences"]["company_sizes"] == ["200-1000", "1000+"]
    assert plan.values["extra_preferences"]["max_travel_percent"] == 10
    assert plan.values["min_salary"] == 60_000
    assert plan.values["target_salary"] == 70_000


def test_rejects_unknown_state_and_unrecognized_instruction() -> None:
    preferences = _preferences()

    plan = compile_preference_prompt(
        preferences,
        "Preferred regions: Atlantis\nFind me something exciting",
    )

    assert not plan.valid
    assert {issue.code for issue in plan.issues} == {"invalid_state", "unknown_instruction"}


def test_query_plan_uses_structured_state_remote_and_role_preferences() -> None:
    preferences = _preferences()
    preferences.preferred_states = ["Bayern", "Hessen"]
    preferences.include_germany_remote = False
    preferences.primary_role_terms = ["Chief of Staff"]
    preferences.additional_role_terms = ["CEO Office"]

    plan = query_plan_for_preferences(preferences)
    assert plan.search_terms == ("Chief of Staff", "CEO Office")
    source = type("SourceLike", (), {"slug": BAJobsucheConnector.source_info.key})()
    queries = plan.queries_for(source)

    assert {query.location for query in queries} == {"Bayern", "Hessen"}
    assert "Chief of Staff" in {query.text for query in queries}
    assert all(not query.filters for query in queries)


def test_compiles_multiple_primary_and_additional_role_terms() -> None:
    preferences = _preferences()
    taxonomy = """Main role terms:
- Executive Assistant
- Chief of Staff

Additional titles:
- Executive Office Coordinator
- Executive Assistant"""

    plan = compile_preference_prompt(preferences, "", taxonomy)

    assert plan.valid
    assert plan.coverage_changed
    assert plan.values["primary_role_terms"] == ["Executive Assistant", "Chief of Staff"]
    assert plan.values["additional_role_terms"] == ["Executive Office Coordinator"]
    assert {change.field for change in plan.changes} == {
        "primary_role_terms",
        "additional_role_terms",
    }


def test_rejects_role_taxonomy_without_a_primary_term() -> None:
    preferences = _preferences()

    plan = compile_preference_prompt(
        preferences,
        "",
        "Main role terms:\n\nAdditional titles:\n- Office Manager",
    )

    assert not plan.valid
    assert [(issue.code, issue.source) for issue in plan.issues] == [
        ("missing_primary_role_terms", "role_taxonomy")
    ]
