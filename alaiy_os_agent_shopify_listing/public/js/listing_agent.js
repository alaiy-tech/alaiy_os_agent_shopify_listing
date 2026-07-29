// What every desk surface needs before it can run the listing agent: the agent
// itself, and the dialog fields its tools declare.
//
// Shared so the single-product form (listing_enrich.js) and the list-view bulk
// action (listing_bulk_enrich.js) can't drift on which toggles they offer or what
// the payload looks like. Nothing here names an agent_id or a tool.

frappe.provide("alaiy.listing_agent");

const AGENT_METHOD = "alaiy_os_agent_shopify_listing.api.get_listing_agent";

// Cached for the session: list and form refreshes fire often, and the agent only
// changes on a migrate or when an admin disables it — both a reload away.
let _agent_promise = null;

// The agent, or null when it is disabled — callers hide their entry point rather
// than offer a run that would throw.
alaiy.listing_agent.get = function () {
	if (!_agent_promise) {
		_agent_promise = frappe.xcall(AGENT_METHOD).catch(() => null);
	}
	return _agent_promise;
};

// One field per input option the agent's tools declare, plus the free-text notes
// every surface offers.
alaiy.listing_agent.option_fields = function (agent) {
	const fields = (agent.input_options || []).map((opt) => ({
		fieldname: opt.fieldname,
		fieldtype: opt.fieldtype || "Check",
		label: __(opt.label || opt.fieldname),
		description: opt.description ? __(opt.description) : undefined,
		default: opt.default === undefined ? 0 : opt.default,
	}));

	fields.push({
		fieldname: "notes",
		fieldtype: "Small Text",
		label: __("Notes for the agent"),
		description: __(
			"Optional — condition, provenance, or anything the listing data and photos won't capture."
		),
	});

	return fields;
};

// Dialog values -> the arguments the agent takes. Checks always go through so the
// agent sees an explicit false; everything else only when the admin filled it in.
alaiy.listing_agent.options_from = function (fields, values) {
	const options = {};
	fields.forEach((f) => {
		if (f.fieldtype === "Check") {
			options[f.fieldname] = !!values[f.fieldname];
		} else if (values[f.fieldname]) {
			options[f.fieldname] = values[f.fieldname];
		}
	});
	return options;
};
