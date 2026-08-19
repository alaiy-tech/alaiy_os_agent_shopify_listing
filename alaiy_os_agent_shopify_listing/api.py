# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
What the desk surfaces need to know about the listing agent, plus bulk enrichment.

The run-agent page and the Enrich buttons are shipped by this app and stay generic:
they do not hardcode an agent_id, and they render their per-request toggles from
whatever the agent's tools declare. `get_listing_agent` is the one endpoint they ask.

`bulk_enrich` is the many-products entry point. It creates a Shopify Listing Bulk
Enrich and starts it; the products then run on Frappe workers, one OS Agent Run each
(see bulk.py). Poll `get_bulk_status`.

`enrich_listing_image` is the odd one out: the only endpoint here that runs a single
tool rather than a whole run. It retouches ONE photo of one product — for a per-photo
"Enrich" button — and changes nothing else about the listing. Like everything else
here it queues and returns; poll `get_listing_images`.
"""

import json

import frappe
from frappe.utils import cint, sbool

from alaiy_os_agent_shopify_listing.bulk import (
	BATCH_DOCTYPE,
	DEFAULT_BATCH_SIZE,
	ENRICHED_DOCTYPE,
	IMAGES_IN_FLIGHT,
)


def _flag(value):
	"""A checkbox argument as 0/1, however the caller expressed it.

	`cint` alone is not enough and fails silently, which is worse than throwing:
	frappe.call JSON-encodes any non-string argument, so a ticked box arrives as
	the string "true" — and cint("true") is 0, not 1. That turned every toggle
	from the Desk dialog off without a word. sbool resolves "true"/"false"/"1"/"0"
	first and passes anything else through for cint to handle.
	"""
	return cint(sbool(value))


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


@frappe.whitelist()
def bulk_enrich(item_codes, notes=None, batch_size=None, skip_enriched=0, **toggles):
	"""
	Enrich many products at once. Returns {batch, items, jobs}.

	`item_codes` are Shopify Product Listing names (a list, or a JSON array over
	REST). Extra keyword arguments are the agent's per-request toggles — whatever
	`get_listing_agent` reports in `input_options`, e.g. `generate_images` — so this
	signature does not name a tool either.

	The work happens on workers: poll `get_bulk_status`, or open the returned batch.
	"""
	if not frappe.has_permission("OS Agent Run", "create"):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if isinstance(item_codes, str):
		item_codes = json.loads(item_codes)
	if not item_codes:
		frappe.throw("bulk_enrich needs at least one item_code.")

	batch = frappe.new_doc(BATCH_DOCTYPE)
	batch.batch_size = cint(batch_size) or DEFAULT_BATCH_SIZE
	batch.skip_enriched = _flag(skip_enriched)
	batch.notes = notes
	for fieldname, value in toggles.items():
		# form_dict carries more than this signature declares (`cmd`), so only
		# arguments that are actually fields on the batch are honoured.
		if batch.meta.get_field(fieldname):
			batch.set(fieldname, _flag(value))
	for item_code in item_codes:
		batch.append("items", {"item_code": item_code})
	batch.insert()

	return batch.start()


@frappe.whitelist()
def enrich_listing_image(item_code, source_url, force=0):
	"""
	Retouch ONE photo of one product — the per-photo "Enrich" button.

	    POST {"item_code": "SH-123", "source_url": "https://cdn.../a.jpg"}
	    -> {item_code, source_url, image_status, queued, url, targets}

	Returns as soon as the work is queued. `url` is null when `queued` is true and
	the retouched photo lands on the listing minutes later — poll
	`get_listing_images` and watch for THAT ROW's url to fill in. Nothing else about
	the listing is touched: the enrichment writes one image row (and its variants'
	copies of the same photo), and no text, attribute or review field moves.

	`source_url` must be a photo this product actually has — one of the Shopify
	Product Listing's own images, or an enabled variant's `variant_image`. Anything
	else is refused, so this endpoint can retouch a catalog photo and nothing else;
	it is not a general "render me this url" service.

	A photo used in more than one place — the listing's own gallery and a variant's,
	or two variants sharing one shot — is rendered ONCE and written to every row that
	references it, each keeping its own `item_variant` so approval still routes it to
	the right variant. `targets` says how many rows this call will fill.

	Already retouched? Nothing is spent: the existing photo comes straight back with
	`queued: false`. Pass `force=1` to render it again anyway, which is the way to
	redo a result a reviewer was unhappy with.

	If the product has never been enriched there is no Shopify Enriched Listing to
	deliver into, so a Draft one is created holding just this image. Draft rather
	than Needs Review on purpose — retouching a photo is not a listing anyone asked
	a human to read yet, and it must not appear in the review queue as though it
	were.
	"""
	from alaiy_os_agent_shopify_listing import image_stage
	from alaiy_os_agent_shopify_listing.tools import handlers as base
	from alaiy_os_agent_shopify_listing.tools.image_generation import (
		already_enhanced,
		generate_product_images,
	)

	if not frappe.has_permission("OS Agent Run", "create"):
		# The same gate as a run, for the same reason: this spends real money at a
		# paid image service, so it is not open to every logged-in user.
		frappe.throw("Not permitted.", frappe.PermissionError)
	if not frappe.db.exists(base.LISTING_DOCTYPE, item_code):
		frappe.throw(f"No {base.LISTING_DOCTYPE} found for item_code '{item_code}'.")

	listing = frappe.get_doc(base.LISTING_DOCTYPE, item_code)
	listing.check_permission("read")

	# Which rows this photo fills: the listing's own gallery entry, plus every
	# enabled variant using the same shot. Settled here, from the product, so the
	# caller cannot name a photo the product does not have — and so a shared photo
	# reaches all of its rows off one render.
	targets = [
		{"source_url": source_url, "item_variant": None}
		for url in base.listing_image_urls(listing)
		if url == source_url
	]
	targets += [
		{"source_url": source_url, "item_variant": item_variant}
		for item_variant, url in base.variant_image_map(listing).items()
		if url == source_url
	]
	if not targets:
		frappe.throw(
			f"'{source_url}' is not a photo of {item_code}. Only this product's own "
			"images and its enabled variants' images can be enriched."
		)

	force = _flag(force)
	if not force:
		existing = already_enhanced(item_code).get(source_url)
		if existing:
			# Free, and idempotent: a second click on a photo that is already done
			# costs nothing and returns the same answer as the first.
			return {
				"item_code": item_code,
				"source_url": source_url,
				"image_status": frappe.db.get_value(
					ENRICHED_DOCTYPE, item_code, "image_status"
				),
				"queued": False,
				"url": existing,
				"targets": len(targets),
			}

	_ensure_enriched_listing(item_code, listing)

	# Empty whatever this photo's rows already hold, so the result replaces it rather
	# than being appended beside it — see image_stage.clear_rendered. Always, not just
	# when forcing: a seeded row holds the original photo, and a re-render holds the
	# previous result, and neither would be matched by the new url.
	image_stage.clear_rendered(item_code, source_url)

	# generate_images=True because asking for this endpoint IS the opt-in — the
	# toggle exists so that a *run* does not enhance images unless someone asked.
	# source_urls narrows the tool to this one photo; without it the tool would
	# (correctly, for a run) queue every photo the product has.
	generate_product_images(
		item_code=item_code,
		generate_images=True,
		source_urls=[source_url],
		force=force,
	)

	# So the poll has something truthful to say between now and the worker starting.
	# The job itself fires on this request's commit (enqueue_after_commit), so this
	# write and the queued work stand or fall together.
	frappe.db.set_value(
		ENRICHED_DOCTYPE, item_code, "image_status", "Queued", update_modified=False
	)

	return {
		"item_code": item_code,
		"source_url": source_url,
		"image_status": "Queued",
		"queued": True,
		"url": None,
		"targets": len(targets),
	}


@frappe.whitelist(methods=["POST"])
def revert_listing_image(item_code, source_url):
	"""Discard the retouched version of ONE photo — the per-photo "revert to
	original", the counterpart of enrich_listing_image.

	    POST {"item_code": "SH-123", "source_url": "https://cdn.../a.jpg"}
	    -> {item_code, source_url, reverted}

	This is a real revert, not a preview: the rows for this photo are emptied, and
	_sync_images publishes a row with no result as the photo it was made from. So
	approving the listing afterwards keeps the original, exactly as though this
	photo had never been enriched. Nothing else about the listing moves.

	Free and idempotent — reverting a photo that holds no result is a no-op that
	reports `reverted: 0` rather than throwing, so a double click costs nothing.

	The render itself is NOT unspent: the money went when the image was made. To
	get a retouched version back, call enrich_listing_image again, which will
	render afresh because there is no longer a result to hand back.
	"""
	from alaiy_os_agent_shopify_listing import image_stage
	from alaiy_os_agent_shopify_listing.tools import handlers as base

	if not frappe.db.exists(base.LISTING_DOCTYPE, item_code):
		frappe.throw(f"No {base.LISTING_DOCTYPE} found for item_code '{item_code}'.")
	if not frappe.db.exists(ENRICHED_DOCTYPE, item_code):
		# Never enriched, so there is nothing to take back. Not an error: the same
		# "already in the state you asked for" answer as reverting an empty row.
		return {"item_code": item_code, "source_url": source_url, "reverted": 0}

	# check_permission("write"), not "read": this changes what approval will
	# publish, so it needs the same rights as editing the enrichment by hand.
	frappe.get_doc(ENRICHED_DOCTYPE, item_code).check_permission("write")

	rows = frappe.get_all(
		"Shopify Enriched Listing Image",
		filters={"parent": item_code, "parenttype": ENRICHED_DOCTYPE, "source_url": source_url},
		fields=["name", "url"],
	)
	if not rows:
		frappe.throw(
			f"'{source_url}' has no enriched version on {item_code}, so there is "
			"nothing to revert."
		)

	filled = [row for row in rows if row.url]
	if filled:
		image_stage.clear_rendered(
			item_code,
			source_url,
			note="Reverted to the original photo by a reviewer.",
		)
		frappe.db.commit()

	return {"item_code": item_code, "source_url": source_url, "reverted": len(filled)}


def _ensure_enriched_listing(item_code, listing):
	"""The row stage two delivers into, seeded from the product if it has none.

	image_stage.run_step gives up when there is no Shopify Enriched Listing, so a
	product that has never been through the agent would render its photo and then
	have nowhere to put it.

	The new row is a COPY of what the product already says, not an empty shell. That
	matters because approval does not patch the listing, it overwrites it: it assigns
	every text field and rebuilds the images and metafields tables from this record
	(see ShopifyEnrichedListing._push_to_listing). Approving a blank record would
	therefore erase the product's title, description, category and every photo the
	image step did not happen to touch. Seeded, the same approval writes the
	product's own values back over themselves — a no-op — and the one retouched photo
	is the only thing that actually changes. Nothing here is invented: every value
	comes from the product itself.

	Draft rather than Needs Review: retouching a photo is not a listing anyone asked
	a human to read, and it must not join the review queue pretending otherwise.
	"""
	from alaiy_os_agent_shopify_listing.tools import handlers as base

	if frappe.db.exists(ENRICHED_DOCTYPE, item_code):
		return

	doc = frappe.new_doc(ENRICHED_DOCTYPE)
	doc.item_code = item_code
	doc.status = "Draft"
	doc.image_status = "Queued"

	# The listing -> enriched field mapping save_listing documents, read backwards.
	doc.title = listing.listing_title
	doc.description = listing.listing_description
	doc.category = listing.listing_category
	doc.product_type = listing.listing_product_type
	doc.seo_title = listing.get("listing_seo_title")
	doc.seo_description = listing.get("listing_seo_description")

	# Metafields are what the attributes table publishes back as, so they round-trip
	# through it. A product with none simply seeds none.
	for row in listing.get("metafields") or []:
		if row.key:
			doc.append("attributes", {"key": row.key, "value": row.value})

	# Every photo the product has, as a row that already holds it: `url` is the photo
	# itself, since nothing better exists yet, and `kind="hero"` is what _sync_images
	# maps back to source "Original" — so an untouched photo returns as the original
	# it is, not relabelled as something the agent made.
	for url in base.listing_image_urls(listing):
		doc.append("images", {"kind": "hero", "source_url": url, "url": url})
	for item_variant, url in base.variant_image_map(listing).items():
		doc.append("images", {
			"kind": "hero",
			"item_variant": item_variant,
			"source_url": url,
			"url": url,
		})

	doc.insert(ignore_permissions=True)


@frappe.whitelist()
def get_listing_images(item_code):
	"""
	One product's imagery and where it has got to — the poll for
	`enrich_listing_image`.

	    {item_code, image_status, image_error, image_tokens,
	     images: [{source_url, item_variant, url, note, kind}, ...]}

	**Watch the row, not the listing.** `image_status` is a property of the whole
	listing, and photo-by-photo enrichment puts several jobs in flight at once: the
	first to finish flips it to Ready while the others are still rendering. A caller
	waiting on one photo should poll until that photo's own row has a `url` (or a
	`note` explaining why it never will). `image_status` is for showing the listing's
	overall state, not for deciding one photo is done.

	Returns empty images and a null status for a product that has never been
	enriched, rather than throwing — "nothing here yet" is a normal answer for a UI
	asking about a product before anyone has enriched it.
	"""
	if not frappe.db.exists(ENRICHED_DOCTYPE, item_code):
		return {
			"item_code": item_code,
			"image_status": None,
			"image_error": None,
			"image_tokens": 0,
			"images": [],
		}

	doc = frappe.get_doc(ENRICHED_DOCTYPE, item_code)
	doc.check_permission("read")

	return {
		"item_code": item_code,
		"image_status": doc.image_status,
		"image_error": doc.image_error,
		"image_tokens": doc.image_tokens or 0,
		"images": [
			{
				"source_url": row.source_url,
				"item_variant": row.item_variant,
				"url": row.url,
				"note": row.note,
				"kind": row.kind,
			}
			for row in (doc.images or [])
		],
	}


@frappe.whitelist()
def approve_listings(names):
	"""
	Approve many enriched listings at once — the list view's "Approve" action.

	Each listing is approved through a normal document save, so the same
	on_update hook that fires for a one-at-a-time approval pushes each one to
	its Shopify Product Listing. Returns {approved, skipped, failed, errors}:
	already-approved rows are counted as skipped, and one bad listing does not
	stop the rest (its error is reported per name instead).
	"""
	if isinstance(names, str):
		names = json.loads(names)
	if not names:
		frappe.throw("approve_listings needs at least one listing name.")

	approved, skipped, errors = 0, 0, {}
	for name in names:
		doc = frappe.get_doc(ENRICHED_DOCTYPE, name)
		doc.check_permission("write")
		if doc.status == "Approved":
			skipped += 1
			continue
		try:
			doc.status = "Approved"
			doc.save()
			frappe.db.commit()
			approved += 1
		except Exception:
			frappe.db.rollback()
			errors[name] = str(frappe.get_traceback().splitlines()[-1])
			frappe.log_error(
				title=f"Bulk approve failed: {name}",
				message=frappe.get_traceback(),
			)

	return {
		"approved": approved,
		"skipped": skipped,
		"failed": len(errors),
		"errors": errors,
	}


@frappe.whitelist()
def get_bulk_status(batch):
	"""Progress of one bulk enrichment — the poll shape for a UI.

	Mirrors alaiy_os.api.agents.get_run, one level up: the batch's own state plus a
	row per product with the run to open for its output.

	Imagery is produced after each run closes (see image_stage.py): a batch whose
	runs are all done but whose images are still rendering reports status
	"Generating Images" until stage two settles — `images_pending` is how many are
	left, and each row carries its own `image_status`.
	"""
	doc = frappe.get_doc(BATCH_DOCTYPE, batch)
	doc.check_permission("read")

	image_states = _image_states([row.item_code for row in doc.items])
	return {
		"batch": doc.name,
		"status": doc.status,
		"total_items": doc.total_items,
		"succeeded": doc.succeeded,
		"failed": doc.failed,
		"skipped": doc.skipped,
		"images_pending": sum(1 for s in image_states.values() if s in IMAGES_IN_FLIGHT),
		"started_at": doc.started_at,
		"ended_at": doc.ended_at,
		"items": [
			{
				"item_code": row.item_code,
				"status": row.status,
				"run": row.run,
				"error": row.error,
				"image_status": image_states.get(row.item_code),
			}
			for row in doc.items
		],
	}


def _image_states(item_codes):
	"""{item_code: image_status} for whichever of these products has been enriched."""
	if not item_codes:
		return {}
	rows = frappe.get_all(
		ENRICHED_DOCTYPE,
		filters={"name": ("in", list(item_codes))},
		fields=["name", "image_status"],
	)
	return {row.name: row.image_status for row in rows}
