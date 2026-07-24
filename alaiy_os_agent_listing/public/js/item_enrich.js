// Adds an "Enrich" button to the Item form that deep-links to the Listing
// Enrichment agent (the run-agent page) with this Item pre-selected. The page
// reads frappe.route_options on show (see run_agent.js#apply_route_options)
// and fills the Item dropdown, so the user lands ready to click "Enrich".
//
// Loaded via doctype_js in this app's hooks.py — no change to the Item doctype
// or to alaiy_os itself.

frappe.ui.form.on("Item", {
	refresh(frm) {
		// Nothing to enrich until the Item is actually saved (the agent looks
		// it up by name / item_code).
		if (frm.is_new()) return;

		frm.add_custom_button(__("Enrich"), () => {
			frappe.route_options = { item_code: frm.doc.name };
			frappe.set_route("run-agent");
		});
	},
});
