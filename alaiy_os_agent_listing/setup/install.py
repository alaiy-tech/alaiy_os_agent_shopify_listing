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
