"""
Install / migrate plumbing for the Shopify listing agent:

  sync_agent_registry  -> (re)register it in OS Agent Registry
  sync_agent_sidebar   -> the "Agents" section + link on the OS workspace sidebar
  unregister           -> remove its registry row on uninstall

You should not need to edit this to add a customer. A customer app overrides the
agent by dropping a markdown file at `<app>/agents/shopify_listing.md`; see
agent_meta.py.

It runs from THIS app's after_install/after_migrate, which a plain `bench migrate`
reaches on every pass — so editing a customer's override file and migrating is
enough to reconcile it onto the site.

The agent engine, run history (OS Agent Run) and the LLM - tool loop all live in
alaiy_os; this app owns agent definitions and their tools, so there is no client,
scheduler or sync-log here (unlike a connector).
"""

import json

import frappe

# Fields written from the manifest on every reconcile. is_enabled is deliberately
# excluded: it is admin-controlled (toggled in the Desk form) and must survive
# migrates, so it is only ever set by the DocType default on the first insert.
_RUNTIME_FIELDS = {"is_enabled"}

# Manifest keys consumed by the desk surfaces (see api.py), not by the registry.
_NON_REGISTRY_FIELDS = {"agent_id", "tools", "input_options", "override_app"}


def sync_agent_registry():
	"""
	Reconcile alaiy_os's OS Agent Registry: upsert the one `shopify_listing` row, and
	drop any other listing-agent row this app owns. Called on install and every
	migrate. Idempotent.

	Because the agent_id is fixed, install order cannot produce a duplicate. The prune
	is what clears rows left by EARLIER naming (this app used to register
	`listing_enrichment`, and a forked app registered `nayaglobal_shopify_listing`),
	so a site that predates the rename converges on one agent without anyone deleting
	a row by hand.
	"""
	# alaiy_os may not be migrated yet on a fresh bench; bail and let our own next
	# migrate catch it.
	if not frappe.db.exists("DocType", "OS Agent Registry"):
		return

	from alaiy_os_agent_shopify_listing.agent_meta import build_agent_meta

	agent_meta = build_agent_meta()
	_upsert_agent(agent_meta)
	_prune_agents(keep=agent_meta["agent_id"])

	frappe.db.commit()


def _prune_agents(keep):
	"""
	Delete OS Agent Registry rows owned by this app other than `keep`.

	Ownership is decided by the tool handlers: a row is ours only if at least one
	handler dotted path points into this app's module namespace. That is what keeps
	this from ever touching another app's agent — note the trailing dot, without
	which a sibling app named `alaiy_os_agent_shopify_listing_*` would match too.

	force=True because every past OS Agent Run links to the agent; the default link
	check would refuse the delete. Those runs keep their agent id as recorded
	history, they just no longer point at a live row.
	"""
	prefix = "alaiy_os_agent_shopify_listing."

	for agent_id in frappe.get_all("OS Agent Registry", pluck="name"):
		if agent_id == keep:
			continue
		handlers = frappe.get_all(
			"OS Agent Tool",
			filters={"parenttype": "OS Agent Registry", "parent": agent_id},
			pluck="handler",
		)
		if not any(h and h.startswith(prefix) for h in handlers):
			continue

		frappe.delete_doc("OS Agent Registry", agent_id, force=True, ignore_permissions=True)
		print(f"removed superseded listing agent '{agent_id}' (now '{keep}')")


def _upsert_agent(agent_meta):
	agent_id = agent_meta["agent_id"]

	if frappe.db.exists("OS Agent Registry", agent_id):
		doc = frappe.get_doc("OS Agent Registry", agent_id)
	else:
		doc = frappe.new_doc("OS Agent Registry")
		doc.agent_id = agent_id

	for key, val in agent_meta.items():
		if key in _NON_REGISTRY_FIELDS or key in _RUNTIME_FIELDS:
			continue
		if key == "output_schema" and isinstance(val, dict):
			val = json.dumps(val, indent=1)
		doc.set(key, val)

	doc.set("tools", [
		{
			"tool_id": tool["tool_id"],
			"description": tool["description"],
			"handler": tool["handler"],
			"parameters_schema": (
				json.dumps(tool["parameters_schema"], indent=1)
				if isinstance(tool.get("parameters_schema"), dict)
				else tool.get("parameters_schema")
			),
			"connector": tool.get("connector"),
		}
		for tool in agent_meta.get("tools", [])
	])

	# save() inserts when new. The OS Agent Tool child controller validates each
	# handler dotted path here, so a broken tool fails at migrate, not mid-run.
	doc.save(ignore_permissions=True)


def unregister():
	"""
	Remove the listing agent's OS Agent Registry row on uninstall. OS Agent Run
	history is intentionally left intact for audit.

	force=True is what makes that possible: every past OS Agent Run links to the
	agent, so the default link check refuses the delete and the whole uninstall dies
	on LinkExistsError the moment the agent has ever been run. The runs keep their
	agent id as recorded history; they simply no longer point at a live row.
	"""
	from alaiy_os_agent_shopify_listing.agent_meta import AGENT_ID

	if frappe.db.exists("OS Agent Registry", AGENT_ID):
		frappe.delete_doc("OS Agent Registry", AGENT_ID, force=True, ignore_permissions=True)
	frappe.db.commit()


# ── OS workspace sidebar entry ────────────────────────────────────────────────
# An "Agents" collapsible section with a link to the run-agent page, added to
# alaiy_os's main "OS" workspace sidebar. alaiy_os rebuilds that sidebar wholesale
# on every migrate; this app migrates AFTER alaiy_os (it depends on it via
# required_apps), so appending here survives the rebuild. Kept entirely in this
# app so the alaiy_os folder is untouched.

_AGENTS_SECTION_LABEL = "Agents"
_AGENT_PAGE = "run-agent"
_AGENT_ICON = "sparkles"
_AGENT_LABEL = "Shopify Listing"


def _os_sidebar_name():
	"""Name of the OS Workspace Sidebar doc. Imported from alaiy_os so it tracks
	any rename there; falls back to the known default."""
	try:
		from alaiy_os.constants.workspace import WORKSPACE_NAME
		return WORKSPACE_NAME
	except Exception:
		return "OS"


def _is_agents_item(item):
	return (
		(item.type == "Section Break" and item.label == _AGENTS_SECTION_LABEL)
		or (item.type == "Link" and item.link_type == "Page" and item.link_to == _AGENT_PAGE)
	)


def sync_agent_sidebar():
	"""
	Add (idempotently) the "Agents" section + run-agent link to the OS workspace
	sidebar. No-op until the Workspace Sidebar and the run-agent Page both exist.
	Called on install and every migrate.
	"""
	if not frappe.db.exists("DocType", "Workspace Sidebar"):
		return
	if not frappe.db.exists("Page", _AGENT_PAGE):
		return

	name = _os_sidebar_name()
	if not frappe.db.exists("Workspace Sidebar", name):
		return

	sidebar = frappe.get_doc("Workspace Sidebar", name)

	# Drop any previously-added copy first so re-runs never stack duplicates.
	kept = [it for it in sidebar.items if not _is_agents_item(it)]
	if len(kept) != len(sidebar.items):
		sidebar.set("items", kept)

	sidebar.append("items", {
		"type": "Section Break", "label": _AGENTS_SECTION_LABEL,
		"icon": _AGENT_ICON, "child": 0, "indent": 1,
	})
	sidebar.append("items", {
		"type": "Link", "link_type": "Page", "link_to": _AGENT_PAGE,
		"label": _AGENT_LABEL, "child": 1, "icon": _AGENT_ICON,
	})

	sidebar.flags.ignore_links = True
	sidebar.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_cache()


def unregister_sidebar():
	"""Remove the Agents section + run-agent link on uninstall."""
	if not frappe.db.exists("DocType", "Workspace Sidebar"):
		return
	name = _os_sidebar_name()
	if not frappe.db.exists("Workspace Sidebar", name):
		return
	sidebar = frappe.get_doc("Workspace Sidebar", name)
	kept = [it for it in sidebar.items if not _is_agents_item(it)]
	if len(kept) == len(sidebar.items):
		return
	sidebar.set("items", kept)
	sidebar.flags.ignore_links = True
	sidebar.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_cache()
