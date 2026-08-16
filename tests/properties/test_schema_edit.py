"""Editing a drafted taxonomy: what gets published, and who is allowed to sign it.

A drafted schema is a proposal, and the reviewer's job is to decide on it. Until
now the only decisions available were yes and no, which meant a taxonomy that was
merely *too coarse* — the common case — could only be fixed by paying for a whole
new draft.

Two properties make an edit safe rather than merely possible:

* **The edited bytes are the published bytes.** A second artifact under the same
  kind must never win, and the drafter's original must stay separately
  answerable, or "what the model proposed" becomes unknowable after the first
  correction.
* **Writing is authoring.** Whoever last wrote the taxonomy cannot be the one who
  approves it. Otherwise a reviewer rewrites a schema and signs off their own
  rewrite, which is precisely the unilateral path to production that
  maker-cannot-be-checker exists to close.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from platform_core.db.engine import tenant_session
from platform_core.identity.principal import ActorType, Principal, RequestContext, Role
from platform_core.settings import get_settings
from workloads.graphrag.artifacts import (
    InvalidSchema,
    taxonomy_fit,
    validate_schema_yaml,
)
from workloads.onboarding import store

# `validate_schema_yaml` parses through the borrowed engine's `core.kg.schema`,
# so the four cases below need the external tree. They used to depend on it
# *silently*, via a settings default that pointed at one developer's home
# directory: green on that laptop, red on any other machine and in CI, with
# nothing in the test naming the requirement.
#
# Skipping is the honest outcome — the dependency is real and the engine is
# optional — but it is declared, and the reason is printed, so a permanently
# skipped case cannot be mistaken for a passing one.
_settings = get_settings()
needs_engine = pytest.mark.skipif(
    not (
        _settings.graphrag_enabled
        and _settings.graphrag_engine_root is not None
        and _settings.graphrag_engine_root.exists()
    ),
    reason=(
        "requires the canonical GraphRAG engine tree: set GRAPHRAG_ENABLED=true "
        "and GRAPHRAG_ENGINE_ROOT to a tree that exists"
    ),
)

DOMAIN = "edit-properties"
COLLECTION = "edit-properties"

DRAFTED_YAML = """
domain: edit-properties
version: 1
entity_types:
  - name: Component
    description: a physical component
edge_types:
  - name: PART_OF
    source: [Component]
    target: [Component]
"""

# The correction a reviewer would actually make: the entity types the instance
# table already named and the schema did not declare.
EDITED_YAML = """
domain: edit-properties
version: 1
entity_types:
  - name: Component
    description: a physical component
  - name: Alarm
    description: a fault code
  - name: Procedure
    description: a maintenance procedure
edge_types:
  - name: PART_OF
    source: [Component]
    target: [Component]
  - name: CAUSES
    source: [Component]
    target: [Alarm]
  - name: CLEARS
    source: [Procedure]
    target: [Alarm]
"""

# What the drafter's classifier produced: two entities it could place, three it
# could not. The `raw:` prefix is the engine's own marker for that.
INSTANCE_TABLE = {
    "instances": [
        {"canonical": "PMP-3", "entity_type": "Component", "raw_type": "pump"},
        {"canonical": "FLT-9", "entity_type": "Component", "raw_type": "filter"},
        {"canonical": "ALM-401", "entity_type": "raw:alarm", "raw_type": "alarm"},
        {"canonical": "ALM-512", "entity_type": "raw:alarm", "raw_type": "alarm"},
        {"canonical": "PR-100", "entity_type": "raw:procedure", "raw_type": "procedure"},
    ]
}


def _principal(tenant, subject: str, role: Role) -> Principal:
    with tenant_session(tenant) as s:
        pid = s.execute(
            text(
                "INSERT INTO principal (tenant_id, subject, actor_type, roles) "
                "VALUES (:t, :s, 'human', :r) "
                "ON CONFLICT (tenant_id, subject) DO UPDATE SET roles = EXCLUDED.roles "
                "RETURNING id"
            ),
            {"t": tenant.id, "s": subject, "r": [str(role)]},
        ).scalar_one()
    return Principal(
        id=pid, tenant=tenant, subject=subject,
        roles=frozenset({role}), actor_type=ActorType.HUMAN,
    )


@pytest.fixture
def author(tenant_a) -> RequestContext:
    return RequestContext(principal=_principal(tenant_a, "author@acme.example", Role.OWNER))


@pytest.fixture
def reviewer(tenant_a) -> RequestContext:
    return RequestContext(
        principal=_principal(tenant_a, "reviewer@acme.example", Role.REVIEWER)
    )


@pytest.fixture
def third_party(tenant_a) -> RequestContext:
    """A second approver, for the case where the reviewer has become the author."""
    return RequestContext(
        principal=_principal(tenant_a, "second-reviewer@acme.example", Role.REVIEWER)
    )


@pytest.fixture
def session_id(author) -> uuid.UUID:
    """A draft_ready session with the artifacts a real draft would have left."""
    with tenant_session(author.tenant) as s:
        sid = store.create(s, author, domain=DOMAIN, collection=COLLECTION)
        s.execute(
            text(
                "UPDATE onboarding_session SET status = 'draft_ready' WHERE id = :i"
            ),
            {"i": sid},
        )
    store.put_artifact(author, sid, "schema", "schema", {"yaml": DRAFTED_YAML})
    store.put_artifact(author, sid, "instance_table", "instance_table", INSTANCE_TABLE)
    return sid


# ── what gets published ───────────────────────────────────────────────────


def test_the_edited_taxonomy_is_the_one_that_gets_published(
    author, session_id, record_evidence
):
    """``artifacts_for`` feeds ``materialize``; it must return the edit."""
    before = store.artifacts_for(author, session_id)
    assert "Alarm" not in before["schema"]["yaml"], "precondition: the draft is the coarse one"

    store.edit_schema(author, session_id, EDITED_YAML)

    after = store.artifacts_for(author, session_id)
    assert after["schema"]["yaml"] == EDITED_YAML
    assert "Alarm" in after["schema"]["yaml"]

    record_evidence(
        "schema_edit_is_what_publishes", holds=True,
        detail="the artifact materialize reads resolves to the edited taxonomy",
    )


def test_the_drafters_original_stays_answerable(author, session_id, record_evidence):
    """Two edits must not lose the model's proposal.

    Retaining "the previous version" would keep the reviewer's first attempt and
    silently discard the drafter's — so after two corrections nobody could say
    what the model actually produced, which is the thing the provenance is for.
    """
    store.edit_schema(author, session_id, EDITED_YAML)
    store.edit_schema(author, session_id, EDITED_YAML.replace("version: 1", "version: 2"))

    with tenant_session(author.tenant) as s:
        kept = s.execute(
            text(
                "SELECT payload->>'yaml' FROM onboarding_artifact "
                "WHERE session_id = :s AND kind = 'schema' AND name = :n"
            ),
            {"s": session_id, "n": store.DRAFTED_SCHEMA_NAME},
        ).scalar_one()

    assert kept == DRAFTED_YAML, "the retained original must be the drafter's, not an edit"

    record_evidence(
        "schema_edit_retains_the_original", holds=True,
        detail="the drafted taxonomy survives repeated correction",
    )


def test_a_sibling_artifact_never_becomes_the_published_schema(
    author, session_id, record_evidence
):
    """Resolution is by ``name = kind``, not by kind alone.

    The earlier version keyed only on kind, so any second row under that kind
    overwrote the real one in whatever order the rows came back — the published
    taxonomy would have depended on a query's ordering. Nothing had a second row
    until edits introduced one, which is what made this latent rather than
    theoretical.
    """
    store.edit_schema(author, session_id, EDITED_YAML)

    with tenant_session(author.tenant) as s:
        rows = s.execute(
            text(
                "SELECT count(*) FROM onboarding_artifact "
                "WHERE session_id = :s AND kind = 'schema'"
            ),
            {"s": session_id},
        ).scalar_one()
    assert rows == 2, "precondition: a sibling must exist or this proves nothing"

    resolved = store.artifacts_for(author, session_id)["schema"]["yaml"]
    assert resolved == EDITED_YAML

    record_evidence(
        "schema_resolution_is_deterministic", holds=True,
        detail="with two rows of kind 'schema', the one named 'schema' is the one used",
    )


# ── who may sign it ───────────────────────────────────────────────────────


def test_the_editor_cannot_approve_their_own_edit(
    author, reviewer, third_party, session_id, record_evidence
):
    """Writing is authoring, so the writer is not eligible to check.

    Asserted at both layers, like every other maker/checker rule here: the
    application refuses with a message, and the CHECK constraint refuses even a
    direct UPDATE that bypasses it entirely.
    """
    store.edit_schema(reviewer, session_id, EDITED_YAML)

    with pytest.raises(PermissionError):
        store.approve(reviewer, session_id)

    # The drafter was already forbidden; that must still hold after an edit.
    with pytest.raises(PermissionError):
        store.approve(author, session_id)

    with tenant_session(author.tenant) as s, pytest.raises(IntegrityError):
        s.execute(
            text(
                "UPDATE onboarding_session SET approved_by = :by, approved_at = now() "
                "WHERE id = :i"
            ),
            {"by": reviewer.principal.id, "i": session_id},
        )

    # And someone who wrote nothing can.
    assert store.approve(third_party, session_id) == "approved"

    record_evidence(
        "schema_editor_is_not_the_approver", holds=True,
        detail="the editor is refused by the capability path and by a CHECK constraint",
    )


def test_a_taxonomy_cannot_be_edited_once_approved(
    author, reviewer, session_id, record_evidence
):
    """Otherwise the published bytes are not the bytes anyone approved."""
    store.approve(reviewer, session_id)

    with pytest.raises(ValueError):
        store.edit_schema(author, session_id, EDITED_YAML)

    record_evidence(
        "approved_taxonomy_is_frozen", holds=True,
        detail="an edit after approval is refused, so approval means the published content",
    )


# ── the report that makes the edit informed ───────────────────────────────


@needs_engine
def test_an_unparseable_taxonomy_is_refused_before_it_is_stored():
    """The failure mode of accepting one is silent, which is why this is a 400.

    ``KnowledgeGraph`` falls back to the engine's own domain lookup, which does
    not know an onboarded domain, so ``build_graph`` catches the error and builds
    with no artifacts: entities, no edges, no complaint.
    """
    for bad in ("", "   ", "not a mapping"):
        with pytest.raises(InvalidSchema):
            validate_schema_yaml(bad)


@needs_engine
def test_the_fit_report_names_what_the_schema_fails_to_declare(record_evidence):
    """The signal that was missing entirely, computable with no corpus and no LLM.

    An entity the schema does not declare is typed ``raw:`` by the classifier, and
    ``EdgeType.accepts`` requires both endpoints to be declared — so every
    relation touching one is discarded at build time, after approval, silently.
    """
    drafted = validate_schema_yaml(DRAFTED_YAML)
    fit = taxonomy_fit(drafted["entity_types"], INSTANCE_TABLE)

    assert fit["instances"] == 5, "precondition: the table must be non-empty"
    assert fit["instances_unclassified"] == 3
    assert fit["unclassified_share"] == 0.6
    named = {s["type"]: s["instances"] for s in fit["suggested_entity_types"]}
    assert named == {"alarm": 2, "procedure": 1}

    # Declaring the missing types is **not** sufficient on its own, and the
    # report must keep saying so. The instance table still types these entities
    # `raw:alarm`, `KnowledgeGraph` admits them as nodes carrying that literal
    # string, and `EdgeType.accepts` compares it against the declared types — so
    # the edges stay discarded. A report that went to zero here would tell a
    # reviewer they had fixed something they had not.
    edited = validate_schema_yaml(EDITED_YAML)
    still = taxonomy_fit(edited["entity_types"], INSTANCE_TABLE)
    assert still["instances_unclassified"] == 3, (
        "a schema edit alone must not read as a fix — the types live in the "
        "instance table, which the drafter derived under the old schema"
    )

    record_evidence(
        "taxonomy_fit_is_reported_before_approval", holds=True,
        detail=(
            "unclassified instances are counted, the missing types named, and a "
            "schema-only edit is not reported as resolving them"
        ),
    )


@needs_engine
def test_retyping_is_what_actually_clears_the_unclassified_instances(
    author, session_id, record_evidence
):
    """The other half of the edit, and the half that changes the graph.

    Asserted end to end through the store rather than on ``taxonomy_fit`` alone,
    because the property is that the *stored* instance table changes — that is
    what ``materialize`` writes and what the graph builds from.
    """
    before = taxonomy_fit(
        validate_schema_yaml(DRAFTED_YAML)["entity_types"], INSTANCE_TABLE
    )
    assert before["instances_unclassified"] == 3, "precondition: there must be work to do"

    result = store.edit_schema(
        author, session_id, EDITED_YAML,
        retype={"raw:alarm": "Alarm", "procedure": "Procedure"},
    )
    assert result["instances_retyped"] == 3

    artifacts = store.artifacts_for(author, session_id)
    after = taxonomy_fit(
        validate_schema_yaml(artifacts["schema"]["yaml"])["entity_types"],
        artifacts["instance_table"],
    )
    assert after["instances_unclassified"] == 0
    assert after["suggested_entity_types"] == []

    # The drafter's table is still answerable, exactly as the schema's is.
    with tenant_session(author.tenant) as s:
        original = s.execute(
            text(
                "SELECT payload FROM onboarding_artifact "
                "WHERE session_id = :s AND kind = 'instance_table' AND name = :n"
            ),
            {"s": session_id, "n": store.drafted_name("instance_table")},
        ).scalar_one()
    assert [i["entity_type"] for i in original["instances"]].count("raw:alarm") == 2

    record_evidence(
        "retyping_clears_unclassified_instances", holds=True,
        detail="the stored instance table adopts the declared types; the original is retained",
    )


@needs_engine
def test_an_edge_type_with_undeclared_endpoints_is_reported():
    """A different failure from the one above, and it is checkable from YAML alone."""
    orphaned = DRAFTED_YAML.replace("source: [Component]", "source: [Ghost]")
    report = validate_schema_yaml(orphaned)
    assert report["unreachable_edge_types"] == [
        {"edge_type": "PART_OF", "undeclared_endpoint_types": ["Ghost"]}
    ]
    assert validate_schema_yaml(DRAFTED_YAML)["unreachable_edge_types"] == []


@pytest.fixture(autouse=True)
def _sweep(tenant_a):
    """Remove sessions this module created. Its domain is its own namespace."""
    yield
    with tenant_session(tenant_a) as s:
        s.execute(
            text("DELETE FROM onboarding_session WHERE domain = :d"), {"d": DOMAIN}
        )


def test_the_fixture_matches_what_a_real_draft_writes(author, session_id):
    """Guards the fixture itself.

    Every property above is measured against these rows. If a future change to
    the drafter stops writing a `schema`/`schema` artifact, this file would keep
    passing while testing a shape nothing produces.
    """
    counts = store.artifact_counts(author, session_id)
    assert counts.get("schema") == 1 and counts.get("instance_table") == 1
    assert json.loads(json.dumps(INSTANCE_TABLE))["instances"], "the table must be non-empty"
