"""Guards for scripts/seed_skills.py -- the repo-to-store skill deploy path."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import seed_skills  # noqa: E402

SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"


def _all_skill_dirs():
    """Shipped skills plus any local ones this deployment happens to have.
    Local skills are gitignored, so on a fresh checkout this is just the
    shipped set -- the test must pass either way."""
    dirs = list(seed_skills.SHIPPED_SKILLS)
    local = SKILLS_ROOT / seed_skills.LOCAL_SKILLS_DIR
    if local.is_dir():
        dirs += [f"{seed_skills.LOCAL_SKILLS_DIR}/{d.name}" for d in sorted(local.iterdir())
                 if (d / "SKILL.md").is_file()]
    return dirs


def test_every_seed_target_exists_with_frontmatter():
    for rel_dir in _all_skill_dirs():
        md = SKILLS_ROOT / rel_dir / "SKILL.md"
        assert md.is_file(), f"{rel_dir}: SKILL.md missing"
        desc = seed_skills._frontmatter_description(md.read_text())
        assert len(desc) > 40, f"{rel_dir}: description too thin to drive skill selection"
        assert len(desc) <= 1024, f"{rel_dir}: description over the spec's 1024-char cap"


def test_skill_names_match_directories():
    # The agent-skills spec requires frontmatter name == parent directory name;
    # load_skills_summary also keys the manifest on it.
    import re
    for rel_dir in _all_skill_dirs():
        md = (SKILLS_ROOT / rel_dir / "SKILL.md").read_text()
        m = re.search(r"^name:\s*(\S+)", md, re.MULTILINE)
        assert m, f"{rel_dir}: no name in frontmatter"
        assert m.group(1) == Path(rel_dir).name, f"{rel_dir}: name/dir mismatch"


def test_folded_yaml_description_collapses_to_one_line():
    text = "---\nname: x\ndescription: >\n  line one\n  line two\n---\nbody"
    assert seed_skills._frontmatter_description(text) == "line one line two"


def test_shipped_skills_carry_no_deployment_specific_knowledge():
    """A public checkout must ship only generic skills -- domain knowledge
    about someone's own repos belongs in skills/local/ (gitignored)."""
    assert seed_skills.SHIPPED_SKILLS == ["vendor/webapp-testing"]
    for rel_dir in seed_skills.SHIPPED_SKILLS:
        assert (SKILLS_ROOT / rel_dir / "SKILL.md").is_file()


def test_local_skills_are_discovered_not_hardcoded(tmp_path):
    """Adding a skill must not require editing a tracked file."""
    root = tmp_path / "skills"
    (root / "local" / "my-skill").mkdir(parents=True)
    (root / "local" / "my-skill" / "SKILL.md").write_text("---\nname: my-skill\n---\n")
    (root / "vendor" / "webapp-testing").mkdir(parents=True)
    (root / "vendor" / "webapp-testing" / "SKILL.md").write_text("---\nname: webapp-testing\n---\n")
    targets = seed_skills._seed_targets(root, ["a", "b"])
    assert targets["local/my-skill"] == ["a", "b"], "unscoped local skills go everywhere"
    assert targets["vendor/webapp-testing"] == ["a", "b"]


def test_local_targets_file_scopes_a_skill_to_named_projects(tmp_path):
    root = tmp_path / "skills"
    (root / "local" / "scoped").mkdir(parents=True)
    (root / "local" / "scoped" / "SKILL.md").write_text("---\nname: scoped\n---\n")
    (root / "local" / "targets.json").write_text('{"_comment": "x", "scoped": ["only-this"]}')
    targets = seed_skills._seed_targets(root, ["a", "b"])
    assert targets["local/scoped"] == ["only-this"]


def test_provenance_and_license_never_seeded():
    assert "PROVENANCE.md" in seed_skills.SKIP_FILES
    assert "LICENSE.txt" in seed_skills.SKIP_FILES


def test_skill_bodies_stay_within_progressive_disclosure_budget():
    # spec guidance: keep SKILL.md under ~5k tokens / 500 lines. Chars/4 is a
    # rough token proxy; the authored skills sit far under it on purpose.
    for rel_dir in _all_skill_dirs():
        text = (SKILLS_ROOT / rel_dir / "SKILL.md").read_text()
        assert len(text) / 4 < 5000, f"{rel_dir}: SKILL.md likely over 5k tokens"
        assert text.count("\n") < 500, f"{rel_dir}: SKILL.md over 500 lines"
