"""Frontend routing (agent/frontend_route.py): which coder and planning seat
a request gets. Category beats paths beats keywords; the operator's choice
beats everything; every decision carries a reason."""

import pytest

from agent.frontend_route import CODER_ROLE, classify_frontend, keyword_hits, named_paths, normalize_override


def test_operator_override_beats_everything():
    d = classify_frontend("rewrite src/core/bot.js for speed", category="performance", override="frontend")
    assert d.route == "frontend" and d.reason == "operator's choice"
    d = classify_frontend("polish the dashboard layout and theme", category="ui-styling", override="general")
    assert d.route == "general"


def test_ui_styling_category_routes_frontend():
    d = classify_frontend("tighten the positions table", category="ui-styling")
    assert d.is_frontend and d.reason == "category ui-styling"


def test_a_clear_frontend_majority_routes_frontend_even_for_a_feature():
    text = "Add a Notional column: frontend/src/pages/OpenPositionsPage.tsx, frontend/src/utils/format.ts, frontend/src/utils/positions.ts and a note in docs/README.md"
    d = classify_frontend(text, category="feature")
    assert d.is_frontend
    assert d.reason == "3 of 3 named files are frontend"


def test_any_named_backend_path_routes_general_whatever_the_count():
    """2026-09-09: a Prisma schema change with API, mapper and import edits
    named 19 frontend files against 17 backend ones and went to the frontend
    seat on the majority vote. The migration was the whole risk."""
    text = ("Products in several categories: change apps/api/prisma/schema.prisma, apps/api/src/catalog/products/products.service.ts, "
            "apps/api/src/catalog/mappers/product.mapper.ts, then apps/admin/src/pages/ProductEditPage.tsx, "
            "apps/admin/src/components/CategoryMultiSelect.tsx, apps/storefront/src/pages/ShopPage.tsx, HomePage.tsx, ProductDetailPage.tsx")
    d = classify_frontend(text, category="feature")
    assert d.route == "general"
    assert d.reason.startswith("backend work named:") and "schema.prisma" in d.reason


def test_backend_keywords_route_general_even_with_frontend_paths():
    d = classify_frontend("Add a migration so the ShopPage.tsx filter can read the new column", category="feature")
    assert d.route == "general" and "migration" in d.reason


def test_a_slim_frontend_majority_is_not_enough():
    text = "Touch frontend/src/a.tsx, frontend/src/b.tsx and lib/util.js, lib/other.js"  # 2 of 4
    d = classify_frontend(text, category="feature")
    assert d.route == "general" and d.reason.startswith("mixed:")


def test_mostly_backend_paths_stay_general():
    text = "Fix src/core/bot.js and src/strategies/strata.js; adjust the badge in frontend/src/components/Sidebar.tsx"
    d = classify_frontend(text, category="bug-fix")
    assert d.route == "general"


def test_ui_styling_category_still_wins_over_a_backend_mention():
    # The classifier read the whole goal; a passing mention of an endpoint in a styling task does not demote it.
    assert classify_frontend("restyle the settings page; the endpoint stays as is", category="ui-styling").is_frontend


def test_two_keywords_route_frontend_one_does_not():
    assert classify_frontend("make the sidebar responsive on mobile").is_frontend
    d = classify_frontend("fix the chart's wrong numbers after a restart")
    assert d.route == "general" and d.reason == "no frontend signal"


def test_keyword_match_is_whole_word():
    assert "ui" not in keyword_hits("rebuild the guidance module")
    assert "page" not in keyword_hits("pagination in the API")


def test_named_paths_split_by_location_and_extension():
    fe, other = named_paths("touch frontend/src/App.tsx, src/core/bot.js, styles/app.css and docs/README.md")
    assert fe == ["frontend/src/App.tsx", "styles/app.css"]
    assert other == ["src/core/bot.js"]


@pytest.mark.parametrize("value,expected", [("frontend", "frontend"), ("general", "general"), ("auto", None), (None, None), ("bogus", None)])
def test_normalize_override(value, expected):
    assert normalize_override(value) == expected


def test_roles_are_the_router_aliases():
    assert CODER_ROLE == {"frontend": "agent-coder-frontend", "general": "agent-coder"}
