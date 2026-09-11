"""Every agent-* alias the router config defines must be a managed role, or
the Models page silently hides it (2026-09-09: agent-coder-frontend and
agent-planning-chat-frontend were pinned in config.yaml and described in
ROLE_REQUIREMENTS but missing from MANAGED_ROLES, so the operator could not
change them). The rule: all roles on the Models page, all changeable."""

import yaml

from agent.model_config import MANAGED_ROLES, ROLE_REQUIREMENTS
from agent.tools.model_rates import LLM_ROUTER_CONFIG_PATH


def _config_agent_aliases() -> set[str]:
    cfg = yaml.safe_load(LLM_ROUTER_CONFIG_PATH.read_text())
    return {e["model_name"] for e in cfg.get("model_list", []) if str(e.get("model_name", "")).startswith("agent-")}


def test_every_agent_alias_in_config_is_a_managed_role():
    missing = _config_agent_aliases() - set(MANAGED_ROLES)
    assert not missing, f"aliases invisible on the Models page: {sorted(missing)}"


def test_every_role_with_requirements_is_managed():
    missing = set(ROLE_REQUIREMENTS) - set(MANAGED_ROLES)
    assert not missing, sorted(missing)


def test_frontend_seats_are_managed_with_readable_labels():
    assert MANAGED_ROLES["agent-coder-frontend"] == "Coder (Frontend)"
    assert MANAGED_ROLES["agent-planning-chat-frontend"] == "Planning Chat (Frontend)"


def test_readme_names_every_managed_role_alias():
    """The Models tab lists every agent-* pin. If the README's role list
    omits one, an operator reading the docs cannot find it to repin -- the
    same class of silence that hid agent-coder-frontend from the page
    itself (see the module docstring)."""
    from pathlib import Path

    readme = Path("README.md").read_text()
    missing = [alias for alias in MANAGED_ROLES if f"`{alias}`" not in readme]
    assert not missing, f"README does not mention managed role(s): {missing}"
