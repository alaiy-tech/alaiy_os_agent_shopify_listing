// "Approve" in the Shopify Enriched Listing list view: tick any number of
// listings and approve them in one go. Each approval runs server-side through a
// normal document save, so the same on_update hook that serves a one-at-a-time
// approval pushes every listing to its Shopify Product Listing
// (api.approve_listings).

frappe.listview_settings["Shopify Enriched Listing"] = {
	onload(listview) {
		listview.page.add_actions_menu_item(__("Approve"), () => approve_selected(listview), false);
	},
};

function approve_selected(listview) {
	const names = listview.get_checked_items(true);
	if (!names.length) {
		frappe.msgprint({
			title: __("Nothing selected"),
			message: __("Tick the listings you want to approve, then choose Approve again."),
			indicator: "orange",
		});
		return;
	}

	frappe.confirm(
		__(
			"Approve {0} listing(s)? Each one is pushed to its Shopify Product Listing immediately.",
			[names.length]
		),
		() => {
			frappe.call({
				method: "alaiy_os_agent_shopify_listing.api.approve_listings",
				args: { names: names },
				freeze: true,
				freeze_message: __("Approving…"),
				callback: (r) => {
					const res = r.message || {};
					const parts = [__("{0} approved", [res.approved || 0])];
					if (res.skipped) parts.push(__("{0} already approved", [res.skipped]));
					if (res.failed) parts.push(__("{0} failed", [res.failed]));

					frappe.show_alert({
						message: parts.join(", "),
						indicator: res.failed ? "orange" : "green",
					});

					// A failure names its listing so the admin can open it, fix, and
					// re-approve — the successes have already gone through.
					if (res.failed) {
						const rows = Object.entries(res.errors)
							.map(([name, err]) => `<li><b>${frappe.utils.escape_html(name)}</b>: ${frappe.utils.escape_html(err)}</li>`)
							.join("");
						frappe.msgprint({
							title: __("Some approvals failed"),
							message: `<ul>${rows}</ul>`,
							indicator: "orange",
						});
					}
					listview.refresh();
				},
			});
		}
	);
}
