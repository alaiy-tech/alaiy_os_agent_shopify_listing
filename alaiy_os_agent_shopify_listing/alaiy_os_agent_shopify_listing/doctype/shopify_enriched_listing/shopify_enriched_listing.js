// Renders the images child table as thumbnails, so a reviewer can see what a
// run actually produced without clicking through a URL per row. The child table
// itself is still there (collapsed) for editing the raw values.
//
// One renderer serves both image tools, because the child table carries both
// shapes they can produce:
//
//   • source_url AND url  -> a before/after pair (a reworked supplier photo,
//                            e.g. Nayaglobal's Chinese-to-English translation)
//   • url only            -> a single tile captioned with kind/brief
//                            (a generated editorial shot, e.g. The Solist's)
//   • no url              -> a placeholder carrying the row's note, so a failed
//                            image is visible rather than silently missing
//
// So neither tool needs its own form script; each just fills the columns that
// apply to what it produces.

frappe.ui.form.on("Shopify Enriched Listing", {
	refresh(frm) {
		render_image_preview(frm);
	},
});

const THUMB = 190;

function pane(url, label, placeholder) {
	const caption = label
		? `<div style="font-size:11px;margin-top:4px;color:var(--text-muted);">${frappe.utils.escape_html(
				label
		  )}</div>`
		: "";

	if (!url) {
		return `
			<div style="width:${THUMB}px;">
				<div style="width:100%;height:${THUMB}px;border:1px dashed var(--border-color);border-radius:4px;
				            display:flex;align-items:center;justify-content:center;text-align:center;
				            color:var(--text-muted);font-size:12px;padding:8px;">
					${frappe.utils.escape_html(placeholder || "no image")}
				</div>
				${caption}
			</div>`;
	}

	const safe = frappe.utils.escape_html(url);
	return `
		<div style="width:${THUMB}px;">
			<a href="${safe}" target="_blank" rel="noopener">
				<img src="${safe}" loading="lazy"
				     style="width:100%;height:${THUMB}px;object-fit:contain;background:var(--control-bg);
				            border:1px solid var(--border-color);border-radius:4px;" />
			</a>
			${caption}
		</div>`;
}

function cap(s) {
	return s ? String(s).charAt(0).toUpperCase() + String(s).slice(1) : "";
}

function side_text(row) {
	// A note is only worth surfacing when the image is missing — otherwise the
	// thumbnail speaks for itself. The brief, where a tool records one, is
	// what the reviewer needs to judge a generated shot against.
	const parts = [];
	if (!row.url && row.note) parts.push(row.note);
	if (row.brief) parts.push(row.brief);
	if (!parts.length) return "";
	return `<div style="font-size:12px;color:var(--text-muted);white-space:pre-wrap;
	                    word-break:break-word;">${frappe.utils.escape_html(parts.join("\n\n"))}</div>`;
}

function render_image_preview(frm) {
	const field = frm.get_field("images_preview");
	if (!field) return;

	const wrapper = field.$wrapper;
	wrapper.empty();

	const rows = frm.doc.images || [];
	if (!rows.length) {
		wrapper.html(
			`<div style="color:var(--text-muted);font-size:12px;">${__(
				"No images on this listing."
			)}</div>`
		);
		return;
	}

	const blocks = rows
		.map((row, idx) => {
			const paired = !!row.source_url;
			let result_label = row.kind ? cap(row.kind) : paired ? __("Result") : __("Generated");
			if (row.item_variant) {
				// A variant's image: say whose, since it lands on that variant's
				// variant_image on approval rather than in the listing's images.
				result_label = `${result_label} — ${__("Variant")} ${row.item_variant}`;
			}

			const panes = paired
				? `${pane(row.source_url, __("Source"), "")}
				   <div style="padding-top:${THUMB / 2 - 10}px;color:var(--text-muted);font-size:16px;">&rarr;</div>
				   ${pane(row.url, result_label, __("not produced"))}`
				: pane(row.url, result_label, __("not produced"));

			return `
				<div style="display:flex;gap:12px;align-items:flex-start;margin-bottom:14px;
				            padding-bottom:14px;border-bottom:1px solid var(--border-color);">
					<div style="width:22px;padding-top:${THUMB / 2 - 8}px;color:var(--text-muted);font-size:12px;">
						${idx + 1}
					</div>
					${panes}
					<div style="flex:1;min-width:0;padding-top:2px;">${side_text(row)}</div>
				</div>`;
		})
		.join("");

	wrapper.html(`<div style="margin-top:8px;">${blocks}</div>`);
}
