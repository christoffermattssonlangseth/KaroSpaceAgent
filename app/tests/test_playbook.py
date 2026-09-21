"""The playbook composes from a stage registry, preserves the decision content,
and carries the schema-only verify stage. These are structural checks — they pin
the shape of the skill library, not the exact prose (which is expected to evolve).
"""

from karospace_agent import playbook, prompt


def test_system_prompt_composes_from_the_stage_registry():
    sp = playbook.build_system_prompt()
    # Every workflow stage's body appears, in registry order.
    last = -1
    for _key, body in playbook.WORKFLOW_STAGES:
        idx = sp.find(body.strip("\n").splitlines()[0])
        assert idx != -1, "a stage went missing from the composed prompt"
        assert idx > last, "stages are out of order"
        last = idx
    # Framing pieces are present.
    assert "Data-handling rule (non-negotiable)" in sp
    assert "by pipeline stage" in sp  # the grouped toolbox
    assert sp.rstrip().endswith("never fabricate a\nsuccess.")  # FINISH last


def test_prompt_module_reexports_match_the_playbook():
    assert prompt.SYSTEM_PROMPT == playbook.build_system_prompt()
    assert prompt.CHAT_ADDENDUM == playbook.CHAT_ADDENDUM


def test_verify_stage_is_registered_and_schema_only():
    keys = [k for k, _ in playbook.WORKFLOW_STAGES]
    assert "verify" in keys
    assert keys[-1] == "verify", "verify runs last, just before Finish"
    v = playbook.VERIFY
    # It must reason from the schema, never pull new data across the boundary.
    assert "crosses no new information" in v
    assert "boundary held" in v
    # It names our concrete failure modes, not generic advice.
    for trap in ("single-section trap", "Failure A", "Failure B", "pseudobulk"):
        assert trap in v


def test_toolbox_groups_every_tool_by_stage():
    from karospace_agent import tools

    tb = playbook.TOOLBOX
    for name in tools.TOOL_NAMES:
        assert name in tb, f"{name} is missing from the stage-grouped toolbox"
    # Grouped, not a flat list: the stage labels are present.
    for stage in ("Acquire", "Inspect", "Enrich", "Export", "Deliver"):
        assert stage in tb
