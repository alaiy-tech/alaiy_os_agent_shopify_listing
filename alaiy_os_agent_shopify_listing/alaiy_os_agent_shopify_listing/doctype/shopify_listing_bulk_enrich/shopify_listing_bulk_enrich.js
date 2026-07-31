// The batch's actions. The work itself runs on Frappe workers (bulk.py), so this
// only starts/stops it and refreshes as progress events arrive.

frappe.ui.form.on("Shopify Listing Bulk Enrich", {
	refresh(frm) {
		if (frm.is_new()) return;

		const status = frm.doc.status;
		const in_flight = status === "Queued" || status === "Running";
		const rendering = status === "Generating Images";

		if (!in_flight) {
			frm.page.set_primary_action(
				status === "Draft" ? __("Start") : __("Run Again"),
				() => run_action(frm, "start", __("Queueing…"))
			);
		}

		if (in_flight) {
			frm.add_custom_button(__("Cancel Batch"), () =>
				run_action(frm, "cancel_batch", __("Cancelling…"))
			);
		}

		if (!in_flight && frm.doc.items.some((row) => ["Failed", "Cancelled"].includes(row.status))) {
			frm.add_custom_button(__("Retry Failed"), () =>
				run_action(frm, "retry_failed", __("Queueing…"))
			);
		}

		if (in_flight) {
			frm.dashboard.set_headline(
				__("{0} of {1} done — this page updates as products finish.", [
					frm.doc.items.filter((row) => !["Pending", "Running"].includes(row.status)).length,
					frm.doc.total_items,
				])
			);
			listen(frm);
		} else {
			// "Generating Images" closes from the image workers, so keep listening
			// for the bulk_enrich_done that flips it to its final status.
			if (rendering) listen(frm);
			show_image_progress(frm);
		}
	},
});

// The listings can be finished while their pictures are not: imagery is rendered
// after each run closes (image_stage.py), and the batch sits in "Generating
// Images" until it lands. Say how many are left rather than letting someone open
// a listing and think an image was lost.
function show_image_progress(frm) {
	if (frm.doc.status === "Draft") return;

	frappe
		.xcall("alaiy_os_agent_shopify_listing.api.get_bulk_status", { batch: frm.doc.name })
		.then((r) => {
			if (!r || !r.images_pending) return;
			frm.dashboard.set_headline(
				__("Listings done. {0} of them are still having images produced in the background.", [
					r.images_pending,
				])
			);
		});
}

// One product finishing (or the batch closing) is worth a reload — the rows are
// written straight to the database by the workers, not through this form.
function listen(frm) {
	if (frm.__bulk_listening) return;
	frm.__bulk_listening = true;
	const reload = frappe.utils.debounce(() => frm.reload_doc(), 1500);
	frappe.realtime.on("bulk_enrich_progress", reload);
	frappe.realtime.on("bulk_enrich_done", reload);
}

function run_action(frm, method, freeze_message) {
	frm.call({ doc: frm.doc, method: method, freeze: true, freeze_message: freeze_message }).then(() =>
		frm.reload_doc()
	);
}
