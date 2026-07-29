// Run Agent — the Shopify listing agent. Implements docs/listing-agent-ui.md: the
// Auto/Manual mode toggle, the enrich flow (idle -> loading -> done), the loader,
// and the side-by-side Original/Generated comparison.
//
// Auto mode's enrich flow calls the real agent engine (alaiy_os.api.agents.
// run_agent/get_run) — it reads the selected Item's actual data and photos (or the
// given image_url) and the "Generated Listing" panel shows genuine AI output,
// polled until the run finishes. The "Original Listing" panel is populated from a
// plain frappe.db.get_value on the same Item, fetched alongside the run, so
// "Before" reflects what's actually in the DB rather than placeholder text. Manual
// mode stays a plain form with no agent involvement at all — that is by design,
// not a shortcut.
//
// The page is generic: it does not know an agent_id, and its per-request toggles are
// rendered from whatever the agent's tools declare (api.get_listing_agent ->
// input_options), each relayed into the run payload under its own fieldname. So
// "generate images" and "translate images" both appear here without this file
// knowing either exists.
//
// Any images the agent returns are shown as thumbnails in the Generated Listing
// panel — as a before/after pair when an existing photo was reworked, as a
// single captioned tile when it produced a new one. An "Enrich" button on the Item
// form (public/js/item_enrich.js) deep-links here with the item pre-selected via
// frappe.route_options.
//
// Colors/fonts/radii/shadows all come from this site's own OS Theme Settings
// tokens (--s-*, see run_agent.css) rather than the spec doc's literal hex
// values and Google Fonts — same design system as the rest of Alaiy OS.

const AGENT_METHOD = "alaiy_os_agent_shopify_listing.api.get_listing_agent";
const POLL_INTERVAL_MS = 2500;

// Kept at module scope so on_page_show (fired on every visit) can reach the
// instance built once in on_page_load.
let _ra_instance = null;

frappe.pages["run-agent"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: "Run Agent",
		single_column: true,
	});

	_ra_instance = new RunAgentPage(page);
};

// Frappe caches the page DOM: on_page_load runs once, on_page_show on every
// visit. The Item form's Enrich button sets frappe.route_options then routes
// here — pick that up on every show, not just the first build.
frappe.pages["run-agent"].on_page_show = function () {
	if (_ra_instance) _ra_instance.apply_route_options();
};

class RunAgentPage {
	constructor(page) {
		this.page = page;
		this.state = {
			mode: "auto", // "auto" | "manual"
			enrichState: "idle", // "idle" | "loading" | "done"
			// One entry per declared input option, keyed by its fieldname. Filled
			// once the agent lands.
			options: {},
			notesOpen: true,
			origCollapsed: false,
			genCollapsed: false,
		};
		this.agent = null;
		this.make();
	}

	// The agent decides the card's title and the toggles, so Auto mode is rendered
	// only once it has arrived. Manual mode involves no agent at all and is rendered
	// immediately.
	load_agent() {
		return frappe.xcall(AGENT_METHOD).catch(() => null);
	}

	init_options() {
		this.state.options = {};
		((this.agent && this.agent.input_options) || []).forEach((opt) => {
			this.state.options[opt.fieldname] = opt.default === undefined ? false : !!opt.default;
		});
	}

	make() {
		$(this.page.body).html(`
			<div class="ra-page">
				<div class="ra-toolbar">
					<div class="ra-mode-toggle">
						<button type="button" class="ra-mode-btn is-active" data-mode="auto">
							${frappe.utils.icon("sparkles", "xs")} <span>${__("Auto")}</span>
						</button>
						<button type="button" class="ra-mode-btn" data-mode="manual">
							${frappe.utils.icon("pencil", "xs")} <span>${__("Manual")}</span>
						</button>
					</div>
				</div>

				<div class="ra-mode-body" data-mode="auto"></div>
				<div class="ra-mode-body" data-mode="manual" style="display:none;"></div>
			</div>
		`);

		this.$root = $(this.page.body).find(".ra-page");
		this.$auto_body = this.$root.find('.ra-mode-body[data-mode="auto"]');
		this.$manual_body = this.$root.find('.ra-mode-body[data-mode="manual"]');

		this.render_manual();
		this.bind_mode_toggle();

		this.$auto_body.html(`<div class="ra-card frappe-card">${__("Loading…")}</div>`);
		this.load_agent().then((agent) => {
			this.agent = agent;
			this.init_options();
			this.render_auto();
		});
	}

	bind_mode_toggle() {
		this.$root.find(".ra-mode-btn").on("click", (e) => {
			const mode = $(e.currentTarget).data("mode");
			if (mode === this.state.mode) return;
			this.state.mode = mode;
			this.$root.find(".ra-mode-btn").removeClass("is-active");
			$(e.currentTarget).addClass("is-active");
			this.$auto_body.toggle(mode === "auto");
			this.$manual_body.toggle(mode === "manual");
		});
	}

	// ── AUTO MODE ────────────────────────────────────────────────────────────

	// One toggle per option the agent's tools declare. The page never decides a
	// toggle's meaning — it relays the value under the tool's own fieldname and
	// the tool behind it enforces the rule.
	toggles_html() {
		const options = (this.agent && this.agent.input_options) || [];
		return options
			.map(
				(opt) => `
			<div class="ra-toggle-row">
				<label class="ra-toggle">
					<input type="checkbox" class="ra-option-toggle"
					       data-fieldname="${frappe.utils.escape_html(opt.fieldname)}"
					       ${this.state.options[opt.fieldname] ? "checked" : ""}>
					<span class="ra-toggle-track"><span class="ra-toggle-thumb"></span></span>
					<span class="ra-toggle-label">
						${frappe.utils.icon("image", "xs")} ${frappe.utils.escape_html(opt.label || opt.fieldname)}
					</span>
				</label>
				${
					opt.description
						? `<p class="ra-field-help">${frappe.utils.escape_html(opt.description)}</p>`
						: ""
				}
			</div>`
			)
			.join("");
	}

	render_auto() {
		if (!this.agent) {
			this.$auto_body.html(`
				<div class="ra-callout ra-callout--error">
					<div class="ra-callout-title">
						${frappe.utils.icon("triangle-alert", "sm")} ${__("No listing agent is available")}
					</div>
					<p>${__(
						"The listing agent is not registered or is disabled on this site. Check OS Agent Registry, or run a migrate to register it."
					)}</p>
				</div>
			`);
			return;
		}

		this.$auto_body.html(`
			<div class="ra-card ra-enrich-card frappe-card">
				<div class="ra-card-header">
					<span class="ra-icon-tile">${frappe.utils.icon(this.agent.icon || "sparkles", "md")}</span>
					<div class="ra-card-header-text">
						<h2>${frappe.utils.escape_html(this.agent.agent_name)}</h2>
						<p>${__("Fill in what you have, then generate a Shopify-ready listing.")}</p>
					</div>
				</div>

				<div class="ra-info-strip">
					${frappe.utils.icon("info", "xs")}
					<span>${__("Provide at least one: Item or Image URL.")}</span>
				</div>


				<div class="ra-bordered-block">
					<div class="ra-field-grid">
						<div class="ra-field-group">
							<label>${__("Item")}</label>
							<div class="ra-input-wrap" id="ra-item-code-wrap"></div>
							<p class="ra-field-help">${__("An existing product — reads its supplier data and photos.")}</p>
						</div>
						<div class="ra-field-group">
							<label>${__("Image URL")}</label>
							<div class="ra-input-wrap">
								<input type="text" class="form-control" id="ra-image-url" placeholder="https://…">
							</div>
							<p class="ra-field-help">${__("No Item yet? Paste a photo URL instead.")}</p>
						</div>
					</div>
				</div>

				<div class="ra-notes-accordion ${this.state.notesOpen ? "is-open" : ""}">
					<button type="button" class="ra-notes-toggle">
						<span>${__("Additional Notes")}</span>
						${frappe.utils.icon("chevron-down", "xs")}
					</button>
					<div class="ra-notes-body">
						<div class="ra-notes-body-inner">
							<label>${__("Notes for the agent")}</label>
							<textarea class="form-control" id="ra-notes" rows="3"></textarea>
							<p class="ra-field-help">${__("Optional — condition, provenance, or anything the data/photos won't capture.")}</p>
						</div>
					</div>
				</div>

				${this.toggles_html()}

				<div class="ra-enrich-actions">
					<button type="button" class="ra-enrich-btn">
						${frappe.utils.icon("sparkles", "xs")}
						<span class="ra-enrich-btn-label">${__("Enrich listing")}</span>
					</button>
				</div>
			</div>

			<div class="ra-flow-area"></div>
		`);

		this.$image_url = this.$auto_body.find("#ra-image-url");
		this.$notes = this.$auto_body.find("#ra-notes");
		this.$enrich_btn = this.$auto_body.find(".ra-enrich-btn");
		this.$flow_area = this.$auto_body.find(".ra-flow-area");

		this.$auto_body.find(".ra-option-toggle").on("change", (e) => {
			this.state.options[$(e.target).data("fieldname")] = e.target.checked;
		});


		// Real Link control (searchable dropdown against actual Item records
		// in the DB, with the standard Frappe "create a new Item" affordance)
		// rather than a plain text input the user would have to type an exact
		// item_code into blind.
		this.item_code_control = frappe.ui.form.make_control({
			df: {
				fieldtype: "Link",
				options: "Item",
				fieldname: "item_code",
				placeholder: __("Search for an item…"),
			},
			parent: this.$auto_body.find("#ra-item-code-wrap")[0],
			only_input: true,
			render_input: true,
		});
		this.item_code_control.refresh();

		this.$auto_body.find(".ra-notes-toggle").on("click", () => {
			this.state.notesOpen = !this.state.notesOpen;
			this.$auto_body.find(".ra-notes-accordion").toggleClass("is-open", this.state.notesOpen);
		});

		this.$enrich_btn.on("click", () => this.on_enrich_click());

		// Pick up an item pre-selected by the Item form's Enrich button on the
		// very first build (on_page_show handles subsequent visits).
		this.apply_route_options();
	}

	// Prefill from frappe.route_options (set by public/js/item_enrich.js before
	// routing here). Switches to Auto mode and selects the item; the user still
	// clicks Enrich themselves.
	//
	// Auto mode renders only once the agent has loaded, so a route option can
	// land before the Item control exists. It is stashed rather than dropped, and
	// render_auto replays it — otherwise arriving from the Item form would silently
	// lose the item on a cold page.
	apply_route_options() {
		const opts = frappe.route_options;
		if (opts && opts.item_code) {
			frappe.route_options = null;
			this.pending_item_code = opts.item_code;
		}
		if (!this.pending_item_code) return;

		if (this.state.mode !== "auto") {
			this.$root.find('.ra-mode-btn[data-mode="auto"]').trigger("click");
		}
		if (this.item_code_control) {
			this.item_code_control.set_value(this.pending_item_code);
			this.pending_item_code = null;
		}
	}

	on_enrich_click() {
		if (this.state.enrichState === "loading") return;

		if (this.state.enrichState === "done") {
			// "Hide generated listing" — collapses straight back to idle;
			// clicking Enrich again starts a fresh loading pass (see spec
			// section 6: "Enrich click (done) -> back to idle").
			this.state.enrichState = "idle";
			this.$flow_area.removeClass("is-visible").empty();
			this.set_enrich_button_idle();
			return;
		}

		const item_code = this.item_code_control.get_value() || "";
		const image_url = (this.$image_url.val() || "").trim();
		if (!item_code && !image_url) {
			frappe.msgprint({
				title: __("Missing Input"),
				message: __("Provide at least an Item or an Image URL before running the agent."),
				indicator: "orange",
			});
			return;
		}

		const notes = (this.$notes.val() || "").trim();
		const inputs = { item_code, image_url };
		if (notes) inputs.notes = notes;
		// Each declared toggle goes in under its own fieldname; the agent's prompt
		// and the tool behind it decide what to do with it.
		Object.assign(inputs, this.state.options);

		this.start_enrich(inputs);
	}

	start_enrich(inputs) {
		this.state.enrichState = "loading";
		this.current_run = null;
		this.original_item = null;
		this.$enrich_btn.prop("disabled", true).addClass("is-loading");
		this.$enrich_btn.html(`${this.spinner(15)} <span class="ra-enrich-btn-label">${__("Enriching...")}</span>`);
		this.render_loader();

		// Fetched alongside the run (not awaited before starting it) so the
		// "Original Listing" panel has real data by the time the run finishes
		// — this doesn't block/slow the actual enrichment. Read from the Shopify
		// Product Listing, the same doctype the agent reads and writes back onto, so
		// Before/After compare the same fields.
		if (inputs.item_code) {
			frappe.db
				.get_value("Shopify Product Listing", inputs.item_code, [
					"listing_title",
					"listing_description",
					"listing_category",
					"listing_product_type",
				])
				.then((r) => {
					this.original_item = (r && r.message) || null;
				});
		}

		frappe.call({
			method: "alaiy_os.api.agents.run_agent",
			args: { agent: this.agent.agent_id, payload: inputs },
			callback: (r) => {
				if (!r.message || !r.message.run) {
					this.render_run_error(__("The agent did not return a run id."), null);
					return;
				}
				this.current_run = r.message.run;
				this.poll(inputs);
			},
			error: () => {
				// frappe.call already shows its own error dialog for server
				// exceptions (e.g. permission denial) — just reset our state.
				this.render_run_error(__("Could not start the agent. See the error dialog for details."), null);
			},
		});
	}

	poll(inputs) {
		if (!this.current_run) return;
		const run_at_request_time = this.current_run;

		frappe.call({
			method: "alaiy_os.api.agents.get_run",
			args: { run: run_at_request_time },
			callback: (r) => {
				// A fresh Enrich click may have started a newer run while this
				// poll was in flight — a stale response landing here must not
				// clobber it.
				if (this.current_run !== run_at_request_time) return;
				const data = r.message;
				if (!data) return;
				if (data.status === "Success") {
					this.on_run_succeeded(inputs, data);
				} else if (data.status === "Failed") {
					this.render_run_error(__("The agent run failed."), data);
				} else {
					setTimeout(() => this.poll(inputs), POLL_INTERVAL_MS);
				}
			},
			error: () => setTimeout(() => this.poll(inputs), POLL_INTERVAL_MS),
		});
	}

	on_run_succeeded(inputs, data) {
		let listing;
		try {
			listing = JSON.parse(data.output || "{}");
		} catch (e) {
			this.render_run_error(__("The agent's output could not be parsed."), data);
			return;
		}
		this.state.enrichState = "done";
		this.state.origCollapsed = false;
		this.state.genCollapsed = false;
		this.set_enrich_button_done();
		this.render_comparison(inputs, listing);
	}

	render_run_error(message, data) {
		this.state.enrichState = "idle";
		this.set_enrich_button_idle();
		const details = data && data.error ? data.error : null;
		this.$flow_area.removeClass("is-visible").html(`
			<div class="ra-callout ra-callout--error">
				<div class="ra-callout-title">
					${frappe.utils.icon("triangle-alert", "sm")} ${frappe.utils.escape_html(message)}
				</div>
				${
					details
						? `<details class="ra-error-details">
							<summary>${__("Technical details")}</summary>
							<pre>${frappe.utils.escape_html(details)}</pre>
						</details>`
						: ""
				}
			</div>
		`);
		requestAnimationFrame(() => this.$flow_area.addClass("is-visible"));
	}

	// Inline SVG ring spinner. Used instead of a bordered <div> circle because
	// the theme forces `* { border-radius: var(--s-radius-sm) }` (0 on sharp
	// themes), which squares a CSS circle; an SVG <circle> keeps its shape
	// regardless. Themed via CSS (see .ra-spin-*), respects reduced-motion.
	spinner(size) {
		return `<svg class="ra-spin" viewBox="0 0 50 50" style="width:${size}px;height:${size}px;" aria-hidden="true">
			<circle class="ra-spin-track" cx="25" cy="25" r="20"></circle>
			<circle class="ra-spin-arc" cx="25" cy="25" r="20"></circle>
		</svg>`;
	}

	set_enrich_button_idle() {
		this.$enrich_btn
			.prop("disabled", false)
			.removeClass("is-loading is-done")
			.html(`${frappe.utils.icon("sparkles", "xs")} <span class="ra-enrich-btn-label">${__("Enrich listing")}</span>`);
	}

	set_enrich_button_done() {
		this.$enrich_btn
			.prop("disabled", false)
			.removeClass("is-loading")
			.addClass("is-done")
			.html(`<span class="ra-enrich-btn-label">${__("Hide generated listing")}</span>`);
	}

	render_loader() {
		// Any toggle being on means there is an image step to wait for as well.
		const sub = Object.values(this.state.options).some(Boolean)
			? __("Reading product data and photos, working on images")
			: __("Reading product data and photos");
		this.$flow_area.removeClass("is-visible").html(`
			<div class="ra-loader-card">
				${this.spinner(44)}
				<div class="ra-loader-label">${__("Enriching listing…")}</div>
				<div class="ra-loader-sublabel">${sub}</div>
				<div class="ra-skeleton-lines">
					<div class="ra-skeleton-line" style="animation-delay:0s;"></div>
					<div class="ra-skeleton-line" style="animation-delay:.2s;"></div>
					<div class="ra-skeleton-line" style="animation-delay:.4s;"></div>
				</div>
			</div>
		`);
		requestAnimationFrame(() => this.$flow_area.addClass("is-visible"));
	}

	render_comparison(inputs, listing) {
		const esc = (v) => frappe.utils.escape_html(v == null ? "" : String(v));
		const orig = this.original_item || {};
		const orig_title = orig.item_name || inputs.item_code || "";

		const confidence_color = { high: "green", medium: "orange", low: "red" }[listing.confidence] || "gray";
		const category_html = listing.category
			? `<span class="indicator-pill blue">${esc(listing.category)}</span>`
			: "";
		const confidence_html = listing.confidence
			? `<span class="indicator-pill ${confidence_color}">${esc(listing.confidence)} ${__("confidence")}</span>`
			: "";

		const needs_review = listing.needs_review || [];
		const needs_review_html = needs_review.length
			? `<div class="ra-callout ra-callout--warning">
				<div class="ra-callout-title">
					${frappe.utils.icon("triangle-alert", "xs")} ${__("Needs review before publishing")}
				</div>
				<ul>${needs_review.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>
			</div>`
			: "";

		// The attributes object is open, so render whatever is actually in it rather
		// than a fixed list of rows.
		const attr_rows = (obj) => {
			const entries = Object.entries(obj || {}).filter(([, v]) => v !== "" && v != null);
			if (!entries.length) return `<li><span>—</span><span></span></li>`;
			return entries
				.map(
					([k, v]) =>
						`<li><span>${esc(k.replace(/_/g, " "))}</span><span>${esc(v)}</span></li>`
				)
				.join("");
		};

		// Images the image step returned — empty when the agent has none, when
		// the toggle was off, or when the product had no photos. A tile whose result
		// URL is null (not configured, or the call failed) is shown as a placeholder
		// rather than hidden, so a missing image is visible.
		//
		// Both shapes the image tools produce render here: `source_url` present means
		// an existing photo was reworked, so the source is shown alongside the
		// result; otherwise it is a single tile.
		const cap = (s) => (s ? String(s).charAt(0).toUpperCase() + String(s).slice(1) : "");
		const images = (listing.images || []).filter(Boolean);
		const tile = (url, label) =>
			`<div class="ra-image-tile">
				${
					url
						? `<a href="${esc(url)}" target="_blank" rel="noopener"><img src="${esc(url)}" alt="${esc(label)}" loading="lazy"></a>`
						: `<div class="ra-image-placeholder">${frappe.utils.icon("image", "sm")}</div>`
				}
				<div class="ra-image-kind">${esc(label)}</div>
			</div>`;
		const images_html = images.length
			? `<div class="ra-field-block">
					<div class="control-label">${__("Images")}</div>
					<div class="ra-images-grid">
						${images
							.map((im) => {
								const out = im.url || null;
								const label = im.kind ? cap(im.kind) : __("Result");
								return im.source_url
									? tile(im.source_url, __("Source")) + tile(out, label)
									: tile(out, label);
							})
							.join("")}
					</div>
				</div>`
			: "";

		this.$flow_area.removeClass("is-visible").html(`
			<div class="ra-comparison-grid">
				<div class="ra-compare-card ra-compare-card--original ${this.state.origCollapsed ? "is-collapsed" : ""}">
					<div class="ra-compare-header">
						<span class="ra-icon-tile ra-icon-tile--muted">${frappe.utils.icon("file-text", "sm")}</span>
						<div class="ra-card-header-text">
							<h3>${__("Original Listing")}</h3>
						</div>
						<span class="indicator-pill gray">${__("Before")}</span>
						<button type="button" class="ra-collapse-btn" data-target="original">
							${frappe.utils.icon("chevron-down", "xs")}
						</button>
					</div>
					<div class="ra-compare-body">
						<div class="ra-field-block">
							<div class="control-label">${__("Title")}</div>
							<div class="ra-static-value">${orig_title ? esc(orig_title) : "—"}</div>
						</div>
						<div class="ra-field-block">
							<div class="control-label">${__("Description")}</div>
							<div class="ra-static-value">${
								orig.listing_description ? esc(strip_html(orig.listing_description)) : "—"
							}</div>
						</div>
						<div class="ra-field-block">
							<div class="control-label">${__("Category")}</div>
							<div class="ra-static-value">${orig.listing_category ? esc(orig.listing_category) : "—"}</div>
						</div>
						<div class="ra-field-block">
							<div class="control-label">${__("Product Type")}</div>
							<div class="ra-static-value">${
								orig.listing_product_type ? esc(orig.listing_product_type) : "—"
							}</div>
						</div>
					</div>
				</div>

				<div class="ra-compare-card ra-compare-card--generated ${this.state.genCollapsed ? "is-collapsed" : ""}">
					<div class="ra-compare-header">
						<span class="ra-icon-tile">${frappe.utils.icon("sparkles", "sm")}</span>
						<div class="ra-card-header-text">
							<h3>${__("Generated Listing")}</h3>
						</div>
						<span class="indicator-pill green">${__("Success")}</span>
						<button type="button" class="ra-collapse-btn" data-target="generated">
							${frappe.utils.icon("chevron-down", "xs")}
						</button>
					</div>
					<div class="ra-compare-body">
						<div class="ra-listing-badges">${category_html}${confidence_html}</div>

						${needs_review_html}

						<div class="ra-field-block">
							<div class="control-label">${__("Title")}</div>
							<input type="text" class="form-control" value="${esc(listing.title)}" placeholder="${__("Generated title")}">
						</div>
						<div class="ra-field-block">
							<div class="control-label">${__("Description")}</div>
							<textarea class="form-control" rows="4" placeholder="${__("Generated description")}">${esc(listing.description)}</textarea>
						</div>
						<div class="ra-field-block">
							<div class="control-label">${__("Product Type")}</div>
							<input type="text" class="form-control" value="${esc(listing.product_type)}" placeholder="${__("Detected product type")}">
						</div>
						<div class="ra-field-block">
							<div class="control-label">${__("Attributes")}</div>
							<ul class="ra-attr-list">${attr_rows(listing.attributes)}</ul>
						</div>

						${images_html}
					</div>
				</div>
			</div>
		`);

		this.$flow_area.find(".ra-collapse-btn").on("click", (e) => {
			const target = $(e.currentTarget).data("target");
			const key = target === "original" ? "origCollapsed" : "genCollapsed";
			this.state[key] = !this.state[key];
			$(e.currentTarget)
				.closest(".ra-compare-card")
				.toggleClass("is-collapsed", this.state[key]);
		});

		requestAnimationFrame(() => this.$flow_area.addClass("is-visible"));
	}

	// ── MANUAL MODE ──────────────────────────────────────────────────────────

	render_manual() {
		this.$manual_body.html(`
			<div class="ra-card frappe-card">
				<div class="ra-card-header">
					<span class="ra-icon-tile">${frappe.utils.icon("pencil", "md")}</span>
					<div class="ra-card-header-text">
						<h2>${__("Manual Listing")}</h2>
						<p>${__("Enter every field yourself — nothing is generated by the agent.")}</p>
					</div>
				</div>

				<div class="ra-section-title">${__("Basics")}</div>
				<div class="ra-bordered-block">
					<div class="ra-field-grid">
						<div class="ra-field-group"><label>${__("SKU / Item")}</label><input type="text" class="form-control"></div>
						<div class="ra-field-group"><label>${__("Product Type")}</label><input type="text" class="form-control"></div>
						<div class="ra-field-group ra-field-group--full"><label>${__("Title")}</label><input type="text" class="form-control"></div>
						<div class="ra-field-group"><label>${__("Category")}</label><input type="text" class="form-control"></div>
					</div>
				</div>

				<div class="ra-section-title">${__("Attributes")}</div>
				<div class="ra-bordered-block">
					<div class="ra-field-grid">
						<div class="ra-field-group"><label>${__("Material")}</label><input type="text" class="form-control"></div>
						<div class="ra-field-group"><label>${__("Color")}</label><input type="text" class="form-control"></div>
						<div class="ra-field-group"><label>${__("Dimensions")}</label><input type="text" class="form-control"></div>
						<div class="ra-field-group"><label>${__("Weight")}</label><input type="text" class="form-control"></div>
					</div>
				</div>

				<div class="ra-section-title">${__("Content")}</div>
				<div class="ra-bordered-block">
					<div class="ra-field-group ra-field-group--full">
						<label>${__("Description")}</label>
						<textarea class="form-control" rows="5"></textarea>
					</div>
				</div>

				<div class="ra-enrich-actions">
					<button type="button" class="ra-enrich-btn">
						<span class="ra-enrich-btn-label">${__("Save listing")}</span>
						${frappe.utils.icon("arrow-right", "xs")}
					</button>
				</div>
			</div>
		`);
	}
}
