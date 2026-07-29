app_name = "alaiy_os_agent_shopify_listing"
app_title = "Alaiy Os Agent Shopify Listing"
app_publisher = "Alaiy"
app_description = "The Shopify listing agent for AlaiyOS, overridable per customer with one markdown file"
app_email = "mail@alaiy.com"
app_license = "agpl-3.0"

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
# The agent engine, OS Agent Registry and OS Agent Run all live in alaiy_os.
# erpnext supplies the Item / Brand / Item Group the tools read.
required_apps = ["alaiy_os", "erpnext"]

# ---------------------------------------------------------------------------
# Desk surfaces
# ---------------------------------------------------------------------------
# Two entry points into the listing agent, both generic:
#   Item                    -> "Enrich" deep-links to the run-agent page with the
#                              item pre-selected (public/js/item_enrich.js).
#   Shopify Product Listing -> "Enrich Listing" runs the agent from the form and
#                              links to the result (public/js/listing_enrich.js).
# Both read the agent from api.get_listing_agent, so neither knows an
# agent_id and neither hardcodes a per-request toggle.
doctype_js = {
    "Item": "public/js/item_enrich.js",
    "Shopify Product Listing": "public/js/listing_enrich.js",
}

# ---------------------------------------------------------------------------
# Installation / migration
# ---------------------------------------------------------------------------
# sync_agent_registry() (re)registers the listing agent in alaiy_os's OS Agent
# Registry, with any customer override (<app>/agents/shopify_listing.md) appended to
# its prompt. It is idempotent, so it runs on both install (agent works immediately)
# and every migrate (edits to the prompt or the override reconcile onto the site).
# sync_agent_sidebar() adds the "Agents" section + run-agent link to alaiy_os's OS
# workspace sidebar. It runs after alaiy_os's own sidebar rebuild (this app
# migrates last, being downstream of alaiy_os), so the addition sticks.
after_install = [
    "alaiy_os_agent_shopify_listing.setup.install.sync_agent_registry",
    "alaiy_os_agent_shopify_listing.setup.install.sync_agent_sidebar",
]

after_migrate = [
    "alaiy_os_agent_shopify_listing.setup.install.sync_agent_registry",
    "alaiy_os_agent_shopify_listing.setup.install.sync_agent_sidebar",
]

# ---------------------------------------------------------------------------
# Uninstallation
# ---------------------------------------------------------------------------
# Remove the agent's OS Agent Registry row (OS Agent Run history is kept) and the
# sidebar entry.
before_uninstall = [
    "alaiy_os_agent_shopify_listing.setup.install.unregister",
    "alaiy_os_agent_shopify_listing.setup.install.unregister_sidebar",
]
