# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
The four catalog tools: read the product, look at a photo, read the store's existing
vocabulary, save the result.

Each callable here is referenced by dotted path in agent_meta's tool catalog and
invoked by the Alaiy OS executor's tool loop as ``handler(**tool_input)``. A
handler either:

  • returns JSON-serializable data (dict/list/str/…), which is sent back to the
    model as the tool_result, or
  • returns a dict with a "_content_blocks" key holding ready-made Anthropic
    content blocks — used here so the model can actually *see* the product
    photos (vision), not just read their URLs.

Raising is fine: the executor catches the exception and feeds it back to the
model as an errored tool_result. We still prefer to degrade gracefully (skip an
unreadable image, guard optional rows) so a single bad attachment does not sink
the whole enrichment.

The source of truth read here is the **Shopify Product Listing** DocType (its
`name` is the template item_code, autoname: field:item), NOT the Item — the
listing's own fields (title, description, price, variants) and its `images` child
table are all we look at. The agent does not edit the listing (or the Item behind
it) and does not publish to Shopify — that is the admin approval / connector step.

save_listing persists the finished enrichment into the Shopify Enriched Listing
DocType in "Needs Review" status, for the admin to edit and approve.
"""

import frappe

from alaiy_os_agent_shopify_listing.tools import images

# Cap how many photos we send to the model to keep token/latency cost bounded.
# Listing photos and variant photos are budgeted separately: a product with many
# variants must not crowd out the listing's own photos, and vice versa.
MAX_IMAGES = 5
MAX_VARIANT_IMAGES = 5

# The DocType the agent reads from. Its `name` is the template item_code
# (autoname: field:item), so a caller's item_code doubles as the listing name.
LISTING_DOCTYPE = "Shopify Product Listing"

# The DocType the agent writes to, for admin review.
ENRICHED_DOCTYPE = "Shopify Enriched Listing"


# ── listing photo access (also used by the image tools) ──────────────────────


def listing_image_rows(listing):
	"""The listing's images child rows, sorted by sort_order."""
	return sorted(
		listing.get("images") or [],
		key=lambda r: (r.get("sort_order") or 0),
	)


def listing_image_urls(listing):
	"""Every usable photo URL on the listing, in sort order."""
	return [row.get("image") for row in listing_image_rows(listing) if row.get("image")]


def primary_listing_image_url(listing):
	"""
	A stable URL for the listing's main photo, for an image tool that edits a real
	photo rather than inventing one. Prefers an ``Original`` (real) photo
	over an ``AI Enhanced`` render as the edit base; falls back to the first image
	row of any source. None if the listing has no usable photo.

	Note this is the File's own url (e.g. a '/files/...' path), which is not
	publicly fetchable on its own — resolve it through images.reference_source or
	images.public_image_url depending on whether you or the service reads it.
	"""
	rows = listing_image_rows(listing)
	originals = [r for r in rows if (r.get("source") or "").lower().startswith("original")]
	for row in (originals or rows):
		if row.get("image"):
			return row.get("image")
	return None


def get_listing(item_code):
	"""The Shopify Product Listing for `item_code`, or throw a useful message."""
	if not frappe.db.exists(LISTING_DOCTYPE, item_code):
		frappe.throw(
			f"No {LISTING_DOCTYPE} found for item_code '{item_code}'. "
			"Check the input or ask the admin to confirm the product has a listing."
		)
	return frappe.get_doc(LISTING_DOCTYPE, item_code)


def variant_image_map(listing):
	"""{item_variant: variant_image url} for enabled variants that have a photo."""
	return {
		v.get("item_variant"): v.get("variant_image")
		for v in (listing.get("variants") or [])
		if v.get("item_variant") and v.get("variant_image") and v.get("is_enabled")
	}


def _collect_variant_image_blocks(listing):
	"""
	Up to MAX_VARIANT_IMAGES photo blocks for the listing's variants, labelled with
	the variant's item code so the model can tie what it sees (colour, size chart,
	printed text) back to the exact variant it belongs to.
	"""
	blocks = []
	for item_variant, url in variant_image_map(listing).items():
		if len(blocks) >= MAX_VARIANT_IMAGES:
			break
		block = images.image_block_from_url(url)
		if block:
			blocks.append((f"{url} (variant {item_variant})", block))
	return blocks


def _collect_image_blocks(listing):
	"""
	Gather up to MAX_IMAGES photo blocks from a Shopify Product Listing's `images`
	child table, in sort order. Each row's `image` is an Attach Image URL (a
	stored File or an external URL). Labels carry the row's `source` so the model
	knows where each photo came from — for stores that distinguish a real photo
	from an already-enhanced render, that difference matters.
	"""
	blocks = []
	for row in listing_image_rows(listing):
		if len(blocks) >= MAX_IMAGES:
			break
		url = row.get("image")
		block = images.image_block_from_url(url)
		if block:
			label = f"{url} ({row.get('source')})" if row.get("source") else url
			blocks.append((label, block))
	return blocks


# ── tools ─────────────────────────────────────────────────────────────────────


def get_product(item_code):
	"""
	Return a Shopify Product Listing's data plus its product photos as vision
	content blocks. The listing's `name` is the template item_code, so the
	caller's item_code is used directly as the listing name. The model receives a
	text block of the structured data followed by one labelled image block per
	photo. Reads strictly from the listing — never the underlying Item.

	Both `image_urls` (all photos, in order) and `primary_image_url` (the best
	one to use as an edit base) are returned, so an image tool has whichever it
	needs without a second read.
	"""
	listing = get_listing(item_code)

	data = {
		"item_code": listing.item,
		"title": listing.get("listing_title"),
		"description": listing.get("listing_description"),
		"price": listing.get("listing_price"),
		"shopify_status": listing.get("sh_shopify_status"),
		"is_enabled": bool(listing.get("is_enabled")),
		"shopify_product_id": listing.get("sh_shopify_product_id"),
		"image_urls": listing_image_urls(listing),
		"primary_image_url": primary_listing_image_url(listing),
		"variants": [
			{
				"item_variant": v.get("item_variant"),
				"price": v.get("variant_price"),
				"is_enabled": bool(v.get("is_enabled")),
				"variant_image": v.get("variant_image"),
			}
			for v in (listing.get("variants") or [])
		],
	}

	labelled = _collect_image_blocks(listing)
	variant_labelled = _collect_variant_image_blocks(listing)
	data["image_count"] = len(labelled)
	data["variant_image_count"] = len(variant_labelled)

	blocks = [
		{
			"type": "text",
			"text": "Shopify Product Listing data (JSON):\n" + frappe.as_json(data),
		}
	]
	if labelled:
		blocks.append({
			"type": "text",
			"text": f"\n{len(labelled)} product photo(s) follow. They are your primary "
			"visual evidence — study them, including any text printed onto the image:",
		})
		for idx, (label, image_block) in enumerate(labelled, start=1):
			blocks.append({"type": "text", "text": f"Photo {idx}: {label}"})
			blocks.append(image_block)
	else:
		blocks.append({
			"type": "text",
			"text": "No usable product photos are on the listing. Enrich from the text "
			"only and flag every visually-determined attribute in needs_review.",
		})

	if variant_labelled:
		blocks.append({
			"type": "text",
			"text": f"\n{len(variant_labelled)} variant photo(s) follow, each labelled "
			"with its variant's item code. Use them to verify each variant's option "
			"values (colour, size, printed text) for the `variants` array:",
		})
		for idx, (label, image_block) in enumerate(variant_labelled, start=1):
			blocks.append({"type": "text", "text": f"Variant photo {idx}: {label}"})
			blocks.append(image_block)

	return {"_content_blocks": blocks}


def view_image(image_url):
	"""
	Fetch an external image URL and hand it back as a vision block, so the model
	can actually look at a product it only knows as a bare URL (no item_code /
	listing to read a photo from otherwise).
	"""
	return {
		"_content_blocks": [
			{"type": "text", "text": f"Reference image ({image_url}):"},
			images.fetch_image_block(image_url),
		]
	}


def _distinct_values(doctype, column, limit=2000):
	"""
	Distinct non-empty values of a column. Raw SQL rather than frappe.get_all because
	one caller reads a CHILD table (Shopify Product Metafield), which get_all refuses
	without a parent doctype.
	"""
	if not frappe.db.table_exists(doctype) or not frappe.db.has_column(doctype, column):
		return []
	rows = frappe.db.sql(
		f"select distinct `{column}` from `tab{doctype}` "
		f"where `{column}` is not null and `{column}` != '' limit {int(limit)}"
	)
	return sorted({(r[0] or "").strip() for r in rows if (r[0] or "").strip()})


def get_reference_values():
	"""
	The vocabulary already in use for the exact fields the agent fills, so it reuses
	established values instead of inventing near-duplicates.

	`category` is a Link to Shopify Category, so a near-duplicate there is not a
	variant spelling — it is a broken value. Product types and metafield keys are free
	text, where consistency is merely worth having.

	Categories are the ones already USED on listings, not every Shopify Category row.
	The Shopify taxonomy is thousands of deep paths ("Electronics / Print, Copy, Scan &
	Fax / Scanners / Drum Scanners"); dumping it would cost a fortune in tokens and
	bury the handful of categories this store actually sells in. A store selling
	watches does not need to be shown scanner accessories.

	Every lookup is guarded: these doctypes belong to the Shopify connector and may not
	be installed.
	"""
	return {
		"categories": _distinct_values(LISTING_DOCTYPE, "listing_category"),
		"product_types": _distinct_values(LISTING_DOCTYPE, "listing_product_type"),
		"attribute_keys": _distinct_values("Shopify Product Metafield", "key"),
	}


def save_listing(listing, item_code=None):
	"""
	Persist an enriched listing into the shared Shopify Enriched Listing DocType
	for admin review. Upserts by item_code (one row per product): re-running a
	listing agent on the same product updates the existing row instead of creating
	a duplicate.

	`listing` is the full enrichment object — the same shape the agent returns
	(schemas/output.json). `item_code` identifies the source product
	and is the upsert key; it falls back to listing["item_code"] if not passed
	separately. The row lands in "Needs Review" status so an admin edits/approves
	it before anything is published.

	Every field written here corresponds to a field on the Shopify Product Listing
	itself (title -> listing_title, description -> listing_description, category ->
	listing_category, product_type -> listing_product_type, attributes -> metafields,
	images -> images), plus the three review fields the approval step needs. The agent
	produces nothing else.

	List-valued fields (needs_review, notes) are flattened to one-per-line text for a
	readable Desk form, and the structured attributes plus the verbatim listing are
	stored as JSON — so nothing is lost even if the flattened fields drift from the
	schema.

	Image rows use one shape — {kind, item_variant, source_url, url, brief, note},
	each column optional — so a single child table serves both the tool that generates
	imagery and the one that reworks existing photos. A row with item_variant set is a
	variant's image (delivered to that variant's `variant_image` on approval); it
	shares the listing's image_status rather than having a lifecycle of its own. The
	per-variant observations land in variants_json, review material only.

	Returns {name, status, url} pointing at the new/updated record.
	"""
	item_code = item_code or (listing or {}).get("item_code")
	if not item_code:
		frappe.throw(
			"save_listing needs an item_code (pass it, or include it in the "
			"listing). This tool persists listings keyed to a product; a URL-only "
			"product has no record to write to — skip this tool and just return "
			"the JSON."
		)
	if not frappe.db.exists(LISTING_DOCTYPE, item_code):
		frappe.throw(
			f"No {LISTING_DOCTYPE} found for item_code '{item_code}'; cannot save the listing."
		)

	if frappe.db.exists(ENRICHED_DOCTYPE, item_code):
		doc = frappe.get_doc(ENRICHED_DOCTYPE, item_code)
	else:
		doc = frappe.new_doc(ENRICHED_DOCTYPE)
		doc.item_code = item_code

	doc.status = "Needs Review"
	doc.title = listing.get("title")
	doc.description = listing.get("description")
	doc.category = listing.get("category")
	doc.product_type = listing.get("product_type")
	doc.confidence = listing.get("confidence")

	# list-valued fields -> one item per line for a readable Desk form
	doc.needs_review = "\n".join(listing.get("needs_review") or [])
	doc.notes = "\n".join(listing.get("notes") or [])

	# structured attributes -> pretty JSON; whole payload kept verbatim for audit
	doc.attributes_json = frappe.as_json(listing.get("attributes") or {})
	doc.variants_json = frappe.as_json(listing.get("variants") or [])
	doc.output_json = frappe.as_json(listing)

	# rebuild the image child table from whatever the image tool produced
	doc.set("images", [])
	for img in (listing.get("images") or []):
		doc.append("images", {
			"kind": img.get("kind"),
			"item_variant": img.get("item_variant"),
			"source_url": img.get("source_url"),
			"url": img.get("url"),
			"brief": img.get("brief"),
			"note": img.get("note"),
		})

	# A row with no url is one the image step queued: the imagery is rendered after
	# this run finishes (see image_stage.py), so the listing is reviewable now and
	# says plainly that its pictures are still coming. Rows that already have a url
	# are ones the image step reused from an earlier run — nothing is owed for those,
	# so the listing is already Ready. Recomputed on every save, so a re-run that
	# queues fresh images resets a previous run's verdict.
	if any(not row.url for row in doc.images):
		doc.image_status = "Queued"
	elif doc.images:
		doc.image_status = "Ready"
	else:
		doc.image_status = "Not Required"
	doc.image_error = None

	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"name": doc.name,
		"status": doc.status,
		"url": f"/app/shopify-enriched-listing/{doc.name}",
	}
