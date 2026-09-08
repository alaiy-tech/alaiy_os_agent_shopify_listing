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

import json
import re

import frappe

from alaiy_os_agent_shopify_listing import matrix
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

# The metafield namespace an approved enrichment publishes its attributes into
# (see ShopifyEnrichedListing._sync_attributes_as_metafields, which imports this
# so the two cannot drift). It is also the namespace read back as "what this
# product already says": a value here is one an approval would overwrite, which
# is exactly the set the agent must be shown before it writes.
ATTRIBUTE_NAMESPACE = "custom"

# A metafield value is evidence for the model, not a document — the one on this
# store that is genuinely long is a third-party app's JSON config blob, and it
# says nothing an enrichment needs. Cut rather than dropped, since the head of a
# long value is usually the part that matters.
MAX_METAFIELD_CHARS = 300


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


# ── published metafield access ───────────────────────────────────────────────
#
# The listing's metafields are the store's own record of this product, mirrored
# from Shopify. Until now nothing here read them, so the agent enriched every
# product as if it were blank and reported gaps the store had already filled.


# What a Shopify list metafield holds when nobody filled it in. Empty to a
# person, truthy to any `if value:` — so it has to be spelled out, or the model
# is told a product "has" a style whose value is the two characters "[]".
_EMPTY_METAFIELD_VALUES = ("", "[]", "{}", "[ ]", "null")


def metafield_text(value):
	"""
	One metafield's value as plain text, or None when it holds nothing.

	Shopify's `list.*` metafields are stored as their JSON, so the value arrives
	as the four characters `["Black"]`. Unwrapped to `Black`, because the model
	copies what it is shown into an attribute, and a value carrying brackets and
	quotes reaches a metafield and then a shopper's screen.
	"""
	text = _flatten(value)
	if text is None:
		return None
	text = text.strip()
	if text.startswith("[") and text.endswith("]"):
		try:
			text = _flatten(json.loads(text)).strip()
		except (json.JSONDecodeError, ValueError, AttributeError):
			pass
	if text in _EMPTY_METAFIELD_VALUES:
		return None
	if len(text) > MAX_METAFIELD_CHARS:
		text = text[:MAX_METAFIELD_CHARS] + "…"
	return text


def listing_metafields(listing):
	"""
	`{namespace: {key: value}}` for the listing, blanks dropped.

	Grouped by namespace rather than flattened, because a key is only unique
	within one: this store carries both `custom.style` ("Dress/Formal") and
	`uploadify_product.style` ("[]"), and a flat dict makes which one you get an
	accident of row order.
	"""
	grouped = {}
	for row in (listing.get("metafields") or []):
		key = row.get("key")
		text = metafield_text(row.get("value"))
		if not key or text is None:
			continue
		grouped.setdefault(row.get("namespace") or "", {})[key] = text
	return grouped


def published_attributes(listing):
	"""
	`{key: value}` this product already publishes as attributes.

	These are the values a reviewer sees in the grid and the ones an approval
	overwrites, so they are what "already answered" means everywhere below: the
	model is shown them so it verifies instead of re-deriving, and the
	mandatory-attribute check counts them as filled instead of sending the
	reviewer after a value the product has carried all along.
	"""
	return listing_metafields(listing).get(ATTRIBUTE_NAMESPACE, {})


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

	`published_attributes` and `other_metafields` are what the store already
	records about this product. They are here because without them the model
	enriches every product as if it were blank: it re-derives values the store
	already holds, and writes "Not provided in source data" for attributes that
	are published on the live product — which then reaches the reviewer as a
	chore that was already done. `published_attributes` is the namespace an
	approval overwrites, so it is both the model's evidence and the thing its
	output replaces; `other_metafields` is everything else, evidence only.

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
		"published_attributes": published_attributes(listing),
		"other_metafields": {
			namespace: values
			for namespace, values in listing_metafields(listing).items()
			if namespace != ATTRIBUTE_NAMESPACE
		},
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
		"tags": _distinct_values("Shopify Tag", "tag_name"),
	}


def _flatten(value):
	"""One attribute value as the plain text a grid cell (and Shopify) wants.

	The schema says these are strings, and they almost always are. A model that
	returns a list or an object anyway must not end up writing "['a', 'b']" into a
	metafield, so lists become a comma-separated line and anything else falls back
	to JSON rather than Python's repr.
	"""
	if value is None:
		return None
	if isinstance(value, str):
		return value
	if isinstance(value, (int, float, bool)):
		return str(value)
	if isinstance(value, (list, tuple)):
		return ", ".join(_flatten(v) or "" for v in value)
	return frappe.as_json(value)


def _flag(doc, text):
	"""
	Add one line to the reviewer's queue.

	`needs_review` is how the admin learns what still needs a human, so anything
	we correct, clamp or blank on the way in says so here. Rebuilt from the
	agent's own list first, so re-running a product does not accumulate stale
	flags.
	"""
	doc.needs_review = "\n".join(filter(None, [doc.needs_review, text]))


def _select_value(doctype, fieldname, value):
	"""
	A Select field's value, or the closest legal one, plus whatever we rejected.

	The output schema declares these as enums, but it reaches the model as a
	Gemini function declaration, where an enum is advisory — so a model asked for
	`high` will sometimes answer "high, because the photos agree". That used to
	fail the whole save on a field nobody reviews, and the model's retry rebuilt
	the payload and silently dropped six attributes it had already got right. So
	take the leading token when it names a legal option and flag the rest: a
	listing is worth more than one tidy field, and `output_json` keeps the
	agent's exact words either way.

	Options are read from the DocType, never duplicated here, so editing the
	field cannot leave this behind.
	"""
	raw = _flatten(value)
	if not raw:
		return None, None

	field = frappe.get_meta(doctype).get_field(fieldname)
	allowed = [o.strip() for o in (field.options or "").split("\n") if o.strip()]
	if raw in allowed:
		return raw, None

	head = re.split(r"[^A-Za-z0-9_-]+", raw.strip(), maxsplit=1)[0].lower()
	for option in allowed:
		if option.lower() == head:
			return option, raw

	return None, raw


def _clamp_data(doctype, fieldname, value):
	"""
	A Data value cut to the column's width, and the length it was.

	Same reasoning as _select_value: `title` is Data(140) and a model that writes
	a 150-character title would otherwise fail the save and trigger the same
	lossy retry. Truncating and saying so costs a few words; retrying costs the
	attributes.
	"""
	raw = _flatten(value)
	if not raw:
		return raw, None

	limit = frappe.get_meta(doctype).get_field(fieldname).length or 140
	if len(raw) <= limit:
		return raw, None
	return raw[:limit], len(raw)


def _apply_category_profile(doc, listing):
	"""
	Settle which of the client's categories this is — it decides which attributes
	are mandatory, so nothing else can be checked until it is known.

	The agent declares it (the field and its values come from the installed
	matrix, see matrix.py). When it declares nothing usable we infer from the
	taxonomy path and product type and record that we guessed, because "the agent
	said Watches" and "we worked out Watches" are not the same claim. When even
	that fails we say so in the reviewer's queue rather than quietly enforcing an
	empty set of mandatory attributes.
	"""
	field = matrix.category_field()
	meta = frappe.get_meta(ENRICHED_DOCTYPE)
	if not field or not meta.has_field(field):
		return

	profiles = matrix.profiles()
	declared = _flatten(listing.get(field))

	if declared and declared in profiles:
		value, source = declared, "agent"
	else:
		if declared:
			_flag(doc, (
				f"{matrix.category_label()} (the agent answered {declared!r}, which is not "
				f"one of {', '.join(profiles)})"
			))
		value = matrix.resolve(
			category=doc.category, product_type=doc.product_type, title=doc.title
		)
		source = "inferred" if value else None

	doc.set(field, value)

	source_field = matrix.category_source_field()
	if source_field and meta.has_field(source_field):
		doc.set(source_field, source)

	if not value:
		_flag(doc, (
			f"{matrix.category_label()} (could not be determined, so the mandatory-attribute "
			"check did not run — set it before approving)"
		))


def _legal_value(text, legal, live):
	"""
	`text` held to the values the guideline allows, or None when it cannot be.

	Where the guideline names the values an attribute may take, those ARE the
	attribute -- "Branded Box" is not a longer way of saying Yes, it is a
	Jewelry answer given to a watch. So the value is resolved onto the list
	rather than stored beside it, three ways, in order of how much each assumes:

	  1. it is one of them in different clothes ("yes" -> "Yes")
	  2. it contains exactly one of them ("Tang buckle" -> "Tang"); two would be
	     ambiguous, and neither is then guessed at
	  3. the product already publishes a legal one, which outranks an illegal
	     answer from a run

	None means none of those applied, and the reviewer has to supply it -- which
	is a needs_review line, not a wrong value on a live product page.
	"""
	for value in legal:
		if matrix.same_value(value, text):
			return value

	contained = [value for value in legal if matrix.contains_value(text, value)]
	if len(contained) == 1:
		return contained[0]

	for value in legal:
		if live and matrix.same_value(value, live):
			return value

	return None


def _flag_missing_mandatory(doc, published):
	"""
	The check the prompt describes and nothing used to enforce: every mandatory
	attribute is either filled by this run, already published on the product, or
	named in `needs_review`.

	`published` counts as filled because the check asks whether the product will
	have the attribute, not whether this particular run produced it. Reading it
	as "this run must fill it" is what put Brand Packaging, Papers, Clasp Type
	and Complications in one watch's review queue while the live product carried
	a value for all four.

	Deliberately appends instead of refusing. Refusing means the model rebuilds
	the payload, and rebuilding the payload is precisely what lost six good
	attributes in the run that prompted this. A listing that reaches a reviewer
	with gaps it can see beats a listing that lost the parts it got right.
	"""
	profile = doc.get(matrix.category_field() or "")
	required = matrix.mandatory(profile, product_type=doc.product_type)
	if not required:
		return

	filled = {row.key for row in doc.attributes if row.key and (row.value or "").strip()}
	filled |= set(published or {})
	flagged = set(matrix.match_keys((doc.needs_review or "").splitlines()))

	for key, label in required:
		if key not in filled and key not in flagged:
			_flag(doc, (
				f"{label} (mandatory for {profile}; the agent put it in neither "
				"attributes nor needs_review)"
			))


def _drop_inapplicable_flags(doc):
	"""
	Remove `needs_review` lines about attributes this category does not have.

	`needs_review` is a queue of work for a person, and there is no work in
	"Weight (not applicable - omit field)" on a watch -- the guideline says a
	watch has no weight attribute, so nobody has to go and find one. A run that
	writes such a line has misread the guideline, which the dropped-attribute
	report below already says once.
	"""
	allowed = matrix.applicable(doc.get(matrix.category_field() or ""))
	if allowed is None:
		return

	kept = []
	for line in (doc.needs_review or "").splitlines():
		keys = matrix.match_keys([line])
		if keys and all(key not in allowed for key in keys):
			continue
		kept.append(line)
	doc.needs_review = "\n".join(kept)


def _annotate_published_flags(doc, published):
	"""
	Mark the agent's own `needs_review` lines that the product already answers.

	The agent flags what it could not see, and it cannot see a metafield it was
	never shown -- so before `get_product` carried them, a flag like "Papers"
	arrived on a product publishing `papers: Yes`. Feeding the model those values
	is the real fix; this covers the run that flags one anyway.

	Annotated, not dropped. The agent had a reason to flag it, and "the agent
	could not confirm the papers, and the product claims Yes" is a discrepancy
	worth a reviewer's glance -- just not an empty field to go and fill.
	"""
	lines = (doc.needs_review or "").splitlines()
	if not lines or not published:
		return

	annotated = []
	for line in lines:
		answered = [key for key in matrix.match_keys([line]) if key in published]
		if len(answered) == 1:
			# The line is usually just the label, so naming the attribute again
			# would read "Papers — ... publishes Papers: Yes".
			line = f"{line} — already published as {published[answered[0]]}"
		elif answered:
			values = "; ".join(f"{matrix.label(key)}: {published[key]}" for key in answered)
			line = f"{line} — already published: {values}"
		annotated.append(line)

	doc.needs_review = "\n".join(annotated)


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
	listing_category, product_type -> listing_product_type, seo_title ->
	listing_seo_title, seo_description -> listing_seo_description, attributes ->
	metafields, images -> images) or on the Item (shopify_tags -> sh_shopify_tags —
	tags are Item-level, not a listing field), plus the three review fields the
	approval step needs. The agent produces nothing else.

	List-valued fields (needs_review, notes) are flattened to one-per-line text for a
	readable Desk form. The structured parts — attributes and per-variant
	observations — are written BOTH as child tables, which is what the Desk form
	shows and what an admin edits, AND verbatim as JSON, which is the audit copy. The
	whole payload is kept as JSON too, so nothing is lost even if the flattened
	fields drift from the schema.

	A variant row links to its variant Item; a variant the model named that is not an
	Item is skipped rather than allowed to fail the save, since variants_json holds
	it either way.

	Image rows use one shape — {kind, item_variant, source_url, url, brief, note},
	each column optional — so a single child table serves both image steps, and rows
	written by older versions of either keep rendering. (`kind` and `brief` are what
	the retired five-shot generator wrote; nothing produces them now.) A row with
	item_variant set is a
	variant's image (delivered to that variant's `variant_image` on approval); it
	shares the listing's image_status rather than having a lifecycle of its own. The
	per-variant observations land in the `variants` table, review material only.

	Nothing the model sends is allowed to fail this save if the listing can be
	written at all. A value the DocType would reject is clamped and the reviewer
	told (see _select_value, _clamp_data); a value that is really a note about
	not having the value is blanked and flagged (matrix.is_placeholder); and any
	mandatory attribute left out of both `attributes` and `needs_review` is
	appended to `needs_review` (_flag_missing_mandatory). An attribute the
	product already publishes is none of those things — it is filled, and saying
	otherwise sends the reviewer after a value that is already live. All of it exists
	because the alternative — refusing the call — makes the model rebuild the
	payload, and a rebuilt payload silently drops attributes it had already got
	right. `output_json` keeps the agent's own words regardless, so a clamp
	loses nothing but the reviewer's time.

	Which attributes are mandatory is the client's business, not this app's: it
	arrives through the `listing_attribute_matrix` hook. See matrix.py.

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

	# What the product already publishes. Read once and used three times below —
	# an attribute the store answers is not a placeholder to report, not a gap to
	# flag, and not a chore for the reviewer.
	published = published_attributes(get_listing(item_code))

	if frappe.db.exists(ENRICHED_DOCTYPE, item_code):
		doc = frappe.get_doc(ENRICHED_DOCTYPE, item_code)
	else:
		doc = frappe.new_doc(ENRICHED_DOCTYPE)
		doc.item_code = item_code

	doc.status = "Needs Review"
	doc.description = listing.get("description")
	doc.category = listing.get("category")
	doc.product_type = listing.get("product_type")
	doc.seo_title = listing.get("seo_title")
	doc.seo_description = listing.get("seo_description")

	# Both of these are clamped rather than trusted, and both for the same
	# reason: a value the DocType would reject fails the whole save, and the
	# model's retry rebuilds the payload from scratch and loses attributes.
	doc.title, overlong = _clamp_data(ENRICHED_DOCTYPE, "title", listing.get("title"))
	doc.confidence, bad_confidence = _select_value(
		ENRICHED_DOCTYPE, "confidence", listing.get("confidence")
	)

	# list-valued fields -> one item per line for a readable Desk form. Set before
	# anything calls _flag, since every flag below appends to this.
	doc.needs_review = "\n".join(_flatten(t) or "" for t in (listing.get("needs_review") or []))
	doc.notes = "\n".join(_flatten(t) or "" for t in (listing.get("notes") or []))
	doc.shopify_tags = "\n".join(_flatten(t) or "" for t in (listing.get("shopify_tags") or []))

	_annotate_published_flags(doc, published)

	if overlong:
		_flag(doc, f"Title (the agent wrote {overlong} characters; it was cut to fit)")
	if bad_confidence:
		_flag(doc, (
			f"Confidence (the agent answered {bad_confidence[:80]!r} instead of one word; "
			"its full wording is in the raw output)"
		))

	_apply_category_profile(doc, listing)

	# After the profile is settled, since which attributes a category has none of
	# is the whole question.
	_drop_inapplicable_flags(doc)

	# structured attributes -> pretty JSON; whole payload kept verbatim for audit
	doc.attributes_json = frappe.as_json(listing.get("attributes") or {})
	doc.variants_json = frappe.as_json(listing.get("variants") or [])
	doc.output_json = frappe.as_json(listing)

	# The same two things again, as child tables — a reviewer reads and edits rows,
	# not a JSON blob, exactly as they do on the Shopify Product Listing itself. The
	# attributes table is the one approval publishes from (see
	# ShopifyEnrichedListing._sync_attributes_as_metafields), so an edit made there
	# reaches Shopify; the JSON fields beside them stay the agent's own words.
	doc.set("attributes", [])
	# What the agent already told the reviewer about, so a placeholder for a field
	# it also listed in needs_review does not report the same field twice.
	spoken_for = set(matrix.match_keys((doc.needs_review or "").splitlines()))
	# The keys this category is allowed to carry at all, or None when the
	# client's guideline has no opinion.
	profile = doc.get(matrix.category_field() or "")
	allowed = matrix.applicable(profile)
	for key, value in (listing.get("attributes") or {}).items():
		if not key:
			continue
		if allowed is not None and key not in allowed:
			# The guideline says this attribute does not exist for this
			# category -- a watch has no earring back. Kept out of the table
			# rather than stored: a row here publishes as a metafield on
			# approval, and `back_type: "Not applicable"` on a wristwatch is a
			# field of noise on a live product page. The reviewer is told, since
			# the agent filling it at all means it worked from the wrong set --
			# but only once, and not on top of the agent's own line about it.
			if key not in spoken_for:
				_flag(doc, (
					f"{matrix.label(key)} (does not apply to {profile}; the agent "
					"filled it anyway, so it was dropped)"
				))
			continue
		text = _flatten(value)
		if text is not None and not text.strip():
			# An empty string is the model declining to answer, not an answer.
			# Written as a row it reads as "considered and left blank", and the
			# reviewer cannot tell it apart from a value someone cleared.
			text = None
		if matrix.is_placeholder(text):
			# "Not provided in source data, will be determined upon manual
			# review." is a note about the absence of a measurement, not a
			# measurement. Left in place it counts as filled, hides the gap from
			# the completeness check below -- and, because
			# _sync_attributes_as_metafields only skips falsy values, publishes
			# to the live storefront as a metafield when the listing is approved.
			# The row stays so the reviewer sees an empty cell to fill, and
			# attributes_json/output_json keep the agent's words verbatim.
			# Not when the product publishes the value: the agent wrote a note
			# about not knowing something the store does know, which is a gap in
			# what it was shown, not a gap in the product.
			if key not in spoken_for and key not in published:
				_flag(doc, f"{matrix.label(key)} (the agent gave a placeholder, not a value)")
			text = None
		if text is None:
			# No value, so no row. The reviewer's grid is driven by the client's
			# attribute matrix, not by which rows happen to exist, so a blank
			# row adds nothing a reviewer can act on -- and the completeness
			# pass below names it in needs_review if it is mandatory.
			continue

		live = published.get(key)
		if live and text != live and matrix.same_value(text, live):
			# The same value in different clothes -- "18K rose gold" for the
			# store's "18K Rose Gold". Stored as the store spells it, because
			# the difference is not one a reviewer can act on: shown as a change
			# it demands a decision between two identical values, and approved
			# it rewrites a live metafield to say what it already said. The
			# store's spelling wins by being the one already published.
			text = live

		legal = matrix.allowed_values(profile, key)
		if legal:
			resolved = _legal_value(text, legal, live)
			if resolved is None:
				# Nothing legal to store, so nothing is stored. Left in as the
				# agent wrote it, the value publishes to a live product page as
				# an answer the guideline does not recognise.
				_flag(doc, (
					f"{matrix.label(key)} (the guideline allows only "
					f"{' or '.join(legal)} for {profile}; the agent wrote "
					f"{text!r}, so the field was left empty)"
				))
				continue
			# Resolved silently when it resolves. A value corrected onto the
			# guideline is not a change anyone needs to review -- reported, it
			# fills the reviewer's queue with work that is already done.
			text = resolved

		doc.append("attributes", {"key": key, "value": text})

	doc.set("variants", [])
	for variant in (listing.get("variants") or []):
		item_variant = variant.get("item_variant")
		# The row links to the variant Item, so a code that is not one would fail the
		# save and lose an otherwise good enrichment. variants_json keeps the whole
		# thing regardless, so nothing is actually lost by skipping the row.
		if not item_variant or not frappe.db.exists("Item", item_variant):
			continue
		doc.append("variants", {
			"item_variant": item_variant,
			"observed": "\n".join(
				f"{name}: {_flatten(value)}"
				for name, value in (variant.get("observed") or {}).items()
			),
			"suggestions": "\n".join(variant.get("suggestions") or []),
			"notes": variant.get("notes"),
		})

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

	_flag_missing_mandatory(doc, published)

	try:
		doc.save(ignore_permissions=True)
	except frappe.exceptions.ValidationError as exc:
		# The clamps above cover the cases we have actually seen. This is the
		# backstop for the rest of the class, and its whole job is to stop the
		# model treating "one field was wrong" as "write the listing again":
		# that is what turned a bad `confidence` string into six lost attributes.
		frappe.db.rollback()
		frappe.throw(
			"The listing was NOT saved. One field was rejected: "
			f"{frappe.utils.strip_html(str(exc)).strip()}. Call save_listing again "
			"with the IDENTICAL `listing` object and ONLY that field changed. Do "
			"not rebuild the payload, do not re-derive or shorten `attributes`, "
			"and never drop an attribute you already extracted. If you cannot "
			"produce a valid value, send an empty string for that field and put "
			"its name in `needs_review`."
		)

	frappe.db.commit()

	return {
		"name": doc.name,
		"status": doc.status,
		"url": f"/app/shopify-enriched-listing/{doc.name}",
	}
