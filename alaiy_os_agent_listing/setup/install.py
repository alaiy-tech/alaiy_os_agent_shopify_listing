"""
Install / migrate plumbing shared by every Alaiy OS agent:

  sync_agent_registry  -> (re)register in OS Agent Registry (install + migrate)
  unregister           -> remove the registry row on uninstall

This file is generic - you should not need to edit it when building a new
agent. Define the agent in agent_meta.py instead.

The agent engine, run history (OS Agent Run) and the LLM - tool loop all live
in alaiy_os; this app only owns one agent's definition, so there is no client,
scheduler or sync-log here (unlike a connector).
"""

import json

import frappe

# Fields written from the manifest on every reconcile. is_enabled is
# deliberately excluded: it is admin-controlled (toggled in the Desk form) and
# must survive migrates, so it is only ever set by the DocType default on the
# first insert.
_RUNTIME_FIELDS = {"is_enabled"}


def sync_agent_registry():
	"""
	Register or update this agent's row (and its tool child rows) in alaiy_os's
	OS Agent Registry. Called from hooks.py on install and every migrate.
	Idempotent.
	"""
	# alaiy_os may not be migrated yet on a fresh bench; bail and let our own
	# next migrate catch it.
	if not frappe.db.exists("DocType", "OS Agent Registry"):
		return

	from alaiy_os_agent_listing.agent_meta import agent_meta

	agent_id = agent_meta["agent_id"]

	if frappe.db.exists("OS Agent Registry", agent_id):
		doc = frappe.get_doc("OS Agent Registry", agent_id)
	else:
		doc = frappe.new_doc("OS Agent Registry")
		doc.agent_id = agent_id

	output_schema = agent_meta.get("output_schema")
	if isinstance(output_schema, dict):
		output_schema = json.dumps(output_schema, indent=1)

	scalar = {
		"agent_name": agent_meta["agent_name"],
		"description": agent_meta.get("description"),
		"icon": agent_meta.get("icon"),
		"page": agent_meta.get("page"),
		"settings_doctype": agent_meta.get("settings_doctype"),
		"model": agent_meta.get("model", "claude-sonnet-5"),
		"max_turns": agent_meta.get("max_turns", 8),
		"system_prompt": agent_meta["system_prompt"],
		"output_format": agent_meta.get("output_format", "Text"),
		"output_schema": output_schema,
	}
	for key, val in scalar.items():
		if key not in _RUNTIME_FIELDS:
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
	# handler dotted path here, so a broken manifest fails at install, not
	# mid-run.
	doc.save(ignore_permissions=True)
	frappe.db.commit()


def unregister():
	"""
	Remove this agent's OS Agent Registry row on uninstall. OS Agent Run history
	is intentionally left intact for audit.
	"""
	from alaiy_os_agent_listing.agent_meta import agent_meta

	agent_id = agent_meta["agent_id"]
	if frappe.db.exists("OS Agent Registry", agent_id):
		frappe.delete_doc("OS Agent Registry", agent_id, ignore_permissions=True)
		frappe.db.commit()


# ── OS workspace sidebar entry ────────────────────────────────────────────────
# An "Agents" collapsible section with a "Listing Enrichment" link, added to
# alaiy_os's main "OS" workspace sidebar. alaiy_os rebuilds that sidebar
# wholesale on every migrate; this app migrates AFTER alaiy_os (it depends on
# it via required_apps), so appending here survives the rebuild. Kept entirely
# in this app so the alaiy_os folder is untouched.

_AGENTS_SECTION_LABEL = "Agents"
_AGENT_PAGE = "run-agent"
_AGENT_SIDEBAR_LABEL = "Listing Enrichment"
_AGENT_ICON = "sparkles"


def _os_sidebar_name():
	"""Name of the OS Workspace Sidebar doc. Imported from alaiy_os so it
	tracks any rename there; falls back to the known default."""
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
	Add (idempotently) the "Agents" section + "Listing Enrichment" link to the
	OS workspace sidebar. No-op until the Workspace Sidebar and the run-agent
	Page both exist. Called on install and every migrate.
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
		"label": _AGENT_SIDEBAR_LABEL, "child": 1, "icon": _AGENT_ICON,
	})

	sidebar.flags.ignore_links = True
	sidebar.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_cache()


def unregister_sidebar():
	"""Remove the Agents section + Listing Enrichment link on uninstall."""
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
