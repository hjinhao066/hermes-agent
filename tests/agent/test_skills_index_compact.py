"""Focused tests for the opt-in compact skill-index mode (``skills.prompt_index_mode``).

Scope: the two knobs added by the lean-upgrade branch — ``skills.prompt_index_mode``
(``full`` default / ``compact``) and ``skills.prompt_full_categories`` (the allowlist) —
and the single renderer they feed, ``agent.prompt_builder.build_skills_system_prompt``.

Deliberately self-contained: it does NOT import ``tests/agent/test_prompt_builder.py``,
whose module-level POSIX-only ``@pytest.mark.skipif(os.geteuid() == 0, ...)`` raises
``AttributeError`` at collection time on Windows and would abort the whole file.

Every case runs against a throwaway ``HERMES_HOME`` (never the live home) and clears both
the in-process prompt cache and the raw-config cache between config rewrites, so a stale
cache entry can never make a comparison pass vacuously.

Cases
  1  absent / ``full`` / unknown mode render byte-identical full output
  2  compact mode preserves the identical skill-name and category sets
  3  allowlisted categories keep descriptions; every other category is names-only
  4  an explicit ``full_categories`` argument equals the config-driven result
  5  ``skills_list`` / ``skill_view`` discovery metadata stays full under compact
  6  context-files block + Telegram platform hint + prefill resolution are byte-identical
  7  the compact knob is confined to the skills-index path (bounded prefill evidence)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.prompt_builder import (
    build_context_files_prompt,
    build_skills_system_prompt,
    clear_skills_system_prompt_cache,
)
from agent.skill_utils import (
    SKILLS_INDEX_MODE_COMPACT,
    SKILLS_INDEX_MODE_FULL,
    _raw_config_cache_clear,
    skills_index_full_categories,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── fixtures / helpers ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """Throwaway HERMES_HOME + cwd, with both prompt and config caches cleared on entry/exit."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    _clear_caches()
    yield tmp_path
    _clear_caches()


def _clear_caches() -> None:
    clear_skills_system_prompt_cache(clear_snapshot=True)
    _raw_config_cache_clear()


def _write_config(home: Path, body: str | None) -> None:
    """(Re)write ``config.yaml`` (or remove it for the absent-key case), then drop caches."""
    path = home / "config.yaml"
    if body is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(body, encoding="utf-8")
    _clear_caches()


def _mk_skill(home: Path, category: str, name: str, description: str) -> None:
    d = home / "skills" / category / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody for {name}.\n",
        encoding="utf-8",
    )


def _fixture_skills(home: Path) -> None:
    _mk_skill(home, "catA", "alpha", "Alpha does A")
    _mk_skill(home, "catA", "beta", "Beta does B")
    _mk_skill(home, "catB", "gamma", "Gamma does C")
    _mk_skill(home, "nested", "deep", "Deep thing")


COMPACT_ALLOW_CATA = (
    "skills:\n"
    "  prompt_index_mode: compact\n"
    "  prompt_full_categories:\n"
    "    - catA\n"
)


def _parse_index(text: str) -> tuple[list[str], list[str]]:
    """(names, categories) from a rendered index, mirroring the renderer's own layout."""
    names: list[str] = []
    categories: list[str] = []
    for line in text.splitlines():
        if line.startswith("    - "):
            names.append(line[6:].split(":", 1)[0].strip())
        elif line.startswith("  ") and not line.startswith("    - "):
            head = line.strip()
            if "[names only]:" in head:
                categories.append(head.split(" [names only]:", 1)[0].strip())
                names.extend(n.strip() for n in head.split("[names only]:", 1)[1].split(",") if n.strip())
            else:
                categories.append(head.split(":", 1)[0].strip())
    return names, categories


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── 1. default / absent / unknown mode is the full behaviour ─────────────────


def test_default_full_and_unknown_modes_render_identical_full_index(_isolated_home):
    home = _isolated_home
    _fixture_skills(home)

    _write_config(home, None)  # keys absent entirely
    absent = build_skills_system_prompt()
    assert skills_index_full_categories() is None

    _write_config(home, f"skills:\n  prompt_index_mode: {SKILLS_INDEX_MODE_FULL}\n")
    explicit_full = build_skills_system_prompt()
    assert skills_index_full_categories() is None

    _write_config(home, "skills:\n  prompt_index_mode: fulll\n")  # typo must not slim a prompt
    unknown = build_skills_system_prompt()
    assert skills_index_full_categories() is None

    _write_config(home, COMPACT_ALLOW_CATA)

    explicit_none = build_skills_system_prompt(full_categories=None)

    assert absent == explicit_full, "absent mode must equal an explicit prompt_index_mode: full"
    assert absent == unknown, "an unknown mode value must fall back to the default full render"
    assert absent == explicit_none, "the default must equal an explicit full_categories=None call"
    # And it really is the full render, not an empty/degraded one.
    assert "Alpha does A" in absent and "Gamma does C" in absent
    assert "[names only]" not in absent


# ── 2. compact preserves names and categories exactly ────────────────────────


def test_compact_preserves_identical_names_and_categories(_isolated_home):
    home = _isolated_home
    _fixture_skills(home)

    _write_config(home, None)
    full = build_skills_system_prompt(full_categories=None)
    _write_config(home, COMPACT_ALLOW_CATA)
    compact = build_skills_system_prompt()

    assert skills_index_full_categories() == frozenset({"catA"})

    full_names, full_cats = _parse_index(full)
    comp_names, comp_cats = _parse_index(compact)

    assert sorted(full_names) == sorted(comp_names), "no skill name may disappear in compact mode"
    assert sorted(full_cats) == sorted(comp_cats), "no category may disappear in compact mode"
    assert len(comp_names) == len(set(comp_names)), "compact must not duplicate a name"
    assert len(comp_names) == 4  # alpha, beta, gamma, deep


# ── 3. allowlist keeps descriptions; everything else is names-only ───────────


def test_allowlisted_descriptions_kept_nonallowlisted_names_only(_isolated_home):
    home = _isolated_home
    _fixture_skills(home)
    _write_config(home, COMPACT_ALLOW_CATA)
    compact = build_skills_system_prompt()

    # catA is allowlisted: header + per-skill description lines survive verbatim.
    assert "  catA:" in compact
    assert "    - alpha: Alpha does A" in compact
    assert "    - beta: Beta does B" in compact
    # catB is not allowlisted: name stays, description leaves.
    assert "  catB [names only]: gamma" in compact
    assert "gamma" in compact
    assert "Gamma does C" not in compact
    # nested category follows its top-level parent.
    assert "  nested [names only]: deep" in compact
    assert "Deep thing" not in compact
    # the explanatory note appears exactly once, and only because something was demoted.
    assert compact.count("descriptions omitted from this session's") == 1


def test_nested_allowlist_entry_keeps_only_that_subtree(_isolated_home):
    home = _isolated_home
    _mk_skill(home, "mlops/training", "fine-tune", "Fine-tune models")
    _mk_skill(home, "mlops/serving", "deploy", "Deploy models")
    _mk_skill(home, "other", "misc", "Misc thing")
    _write_config(
        home,
        "skills:\n  prompt_index_mode: compact\n  prompt_full_categories:\n    - mlops/training\n",
    )
    compact = build_skills_system_prompt()

    assert "    - fine-tune: Fine-tune models" in compact          # named nested path kept
    assert "  mlops/serving [names only]: deploy" in compact       # sibling under same parent demoted
    assert "Deploy models" not in compact
    assert "  other [names only]: misc" in compact


def test_empty_allowlist_yields_names_only_index_but_no_hidden_names(_isolated_home):
    home = _isolated_home
    _fixture_skills(home)
    _write_config(home, "skills:\n  prompt_index_mode: compact\n  prompt_full_categories: []\n")
    compact = build_skills_system_prompt()

    assert skills_index_full_categories() == frozenset()
    names, categories = _parse_index(compact)
    assert sorted(names) == ["alpha", "beta", "deep", "gamma"]
    for category in categories:
        assert f"  {category} [names only]:" in compact


# ── 4. explicit argument == config-driven result ─────────────────────────────


def test_explicit_full_categories_argument_equals_config_driven_result(_isolated_home):
    home = _isolated_home
    _fixture_skills(home)

    _write_config(home, COMPACT_ALLOW_CATA)
    config_driven = build_skills_system_prompt()
    explicit = build_skills_system_prompt(full_categories=frozenset({"catA"}))

    assert explicit == config_driven, "an explicit full_categories must match the config-driven render"
    assert _sha(explicit) == _sha(config_driven)

    # Control: a different allowlist must NOT collide with either of the above.
    different = build_skills_system_prompt(full_categories=frozenset({"catB"}))
    assert different != config_driven
    assert "    - gamma: Gamma does C" in different
    assert "Alpha does A" not in different

    # Control: None (full) must not collide with the compact render.
    assert build_skills_system_prompt(full_categories=None) != config_driven


# ── 5. discovery tools stay full ─────────────────────────────────────────────


def test_discovery_tools_remain_full_metadata_under_compact(_isolated_home):
    home = _isolated_home
    _fixture_skills(home)

    from tools.skills_tool import skill_view, skills_list

    _write_config(home, None)
    list_full = skills_list()
    view_full = skill_view("alpha")

    _write_config(home, COMPACT_ALLOW_CATA)
    list_compact = skills_list()
    view_compact = skill_view("alpha")

    assert list_compact == list_full, "skills_list must be byte-identical across index modes"
    assert view_compact == view_full, "skill_view must be byte-identical across index modes"

    payload = json.loads(list_compact)
    assert payload["success"] is True
    assert payload["count"] == 4
    described = {s["name"]: s["description"] for s in payload["skills"]}
    # The category that the prompt index demoted still exposes its description here.
    assert described["gamma"] == "Gamma does C"
    assert all(described.values()), "every discovery entry must still carry its description"

    view = json.loads(view_compact)
    assert view["success"] is True and view["description"] == "Alpha does A"


# ── 6. other always-injected blocks are byte-identical across modes ──────────


def test_context_files_telegram_hint_and_prefill_are_byte_identical(_isolated_home):
    home = _isolated_home
    _fixture_skills(home)
    (home / "AGENTS.md").write_text("# Project\n\nContext body for the bench home.\n", encoding="utf-8")

    from agent.system_prompt import platform_hint
    from hermes_cli.cli_config_load import _resolve_prefill_messages_file

    full_cfg = {"skills": {"prompt_index_mode": SKILLS_INDEX_MODE_FULL, "prompt_full_categories": ["catA"]}}
    compact_cfg = {"skills": {"prompt_index_mode": SKILLS_INDEX_MODE_COMPACT, "prompt_full_categories": ["catA"]}}

    _write_config(home, None)
    ctx_full = build_context_files_prompt(
        cwd=str(home), skip_soul=True, context_length=None,
        allow_install_tree_fallback=True, home_override=home)
    hint_full = platform_hint(SimpleNamespace(platform="telegram"))

    _write_config(home, COMPACT_ALLOW_CATA)
    ctx_compact = build_context_files_prompt(
        cwd=str(home), skip_soul=True, context_length=None,
        allow_install_tree_fallback=True, home_override=home)
    hint_compact = platform_hint(SimpleNamespace(platform="telegram"))

    assert ctx_full, "context-file block should be non-empty in this fixture"
    assert _sha(ctx_compact) == _sha(ctx_full) == _sha(ctx_full)
    assert ctx_compact == ctx_full, "context files must be byte-identical across index modes"

    assert hint_full, "the telegram platform hint should be non-empty"
    assert hint_compact == hint_full, "the Telegram formatter hint must be byte-identical across index modes"

    # Prefill resolution is a pure config read; adding the skills knobs must not perturb it.
    assert _resolve_prefill_messages_file(full_cfg) == _resolve_prefill_messages_file(compact_cfg)


# ── 7. bounded prefill-prefix evidence ───────────────────────────────────────


def test_compact_knob_is_confined_to_the_skills_index_path(_isolated_home):
    """The full three-tier system prompt (what actually gets prefilled) cannot be assembled
    in-process here: ``_identity_parts`` needs agent attributes beyond the offline harness
    stub (the measurement harness records the same gap as ``full_prompt_error:
    AttributeError: ... has no attribute 'load_soul_identity'``). So the prefill/stability
    claim is established structurally instead of by driving a live agent:
      (a) nothing outside the skills-index path even reads the two knobs;
      (b) the skills index feeds the *volatile* tier, not the reusable prefill prefix.
    """
    knob_owner = {}
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith(("tests/", "optional-", "scripts/", "evals/", "website/", "apps/", "node_modules/")):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover
            continue
        if "prompt_index_mode" in text or "prompt_full_categories" in text or "skills_index_full_categories" in text:
            knob_owner[rel] = text

    assert set(knob_owner) == {
        "agent/skill_utils.py",        # the only reader of the two config keys
        "agent/prompt_builder.py",     # the only consumer (the skills-index renderer)
        "hermes_cli/config_defaults.py",  # declares the shipped defaults; never reads them
    }, (
        "the compact knob must be read only by agent/skill_utils.py and consumed only by "
        "agent/prompt_builder.py (hermes_cli/config_defaults.py may only declare the defaults); "
        f"found: {sorted(knob_owner)}"
    )

    sp_src = (REPO_ROOT / "agent" / "system_prompt.py").read_text(encoding="utf-8")
    assert "volatile_parts: List[str] = [skills_prompt," in sp_src, (
        "the skills index must stay in the volatile tier so the stable+context prefill prefix "
        "is untouched by an index-mode change"
    )
    assert sp_src.count("_skills_prompt(") == 2, (
        "_skills_prompt should be defined once and called once (the volatile-tier assembly)"
    )
