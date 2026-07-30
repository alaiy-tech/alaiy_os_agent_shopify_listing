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
"""

import json

import frappe
from frappe.utils import cint, sbool

from alaiy_os_agent_shopify_listing.bulk import BATCH_DOCTYPE, DEFAULT_BATCH_SIZE, ENRICHED_DOCTYPE

# Image states that mean stage two still owes this listing its pictures.
IMAGES_IN_FLIGHT = ("Queued", "Running")


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

	`status` covers the agent runs only. Imagery is produced after each run closes
	(see image_stage.py), so a Completed batch can still have images rendering —
	`images_pending` is how many, and each row carries its own `image_status`.
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
