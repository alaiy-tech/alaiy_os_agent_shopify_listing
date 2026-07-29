# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
What the desk surfaces need to know about the listing agent.

The run-agent page and the Enrich buttons are shipped by this app and stay generic:
they do not hardcode an agent_id, and they render their per-request toggles from
whatever the agent's tools declare. This is the one endpoint they ask.
"""

import frappe


@frappe.whitelist()
def get_listing_agent():
	"""
	The listing agent, or None when an admin has switched it off in the Desk form —
	in which case the surfaces hide their buttons instead of offering a run that
	would throw.

	    {agent_id, agent_name, icon,
	     input_options: [{fieldname, label, description, default}]}
	"""
	from alaiy_os_agent_shopify_listing.agent_meta import build_agent_meta

	meta = build_agent_meta()
	if not frappe.db.get_value("OS Agent Registry", meta["agent_id"], "is_enabled"):
		return None

	return {
		"agent_id": meta["agent_id"],
		"agent_name": meta["agent_name"],
		"icon": meta["icon"],
		"input_options": meta["input_options"],
	}
