# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Stage two: the images, rendered after the listing text is already saved.

The agent's run is stage one. It reads the product, writes the listing, and — when
an image toggle is on — decides *what* imagery it wants: the briefs for a generated
set, or the photos to translate. It does not wait for that imagery. The image tool
returns placeholders with ``url: None`` and queues the work here, so a run finishes
in the ~35s its LLM turns actually take instead of the ~5 minutes the image API
takes.

Why split at all: the two halves have opposite shapes. Stage one is LLM-bound and
short; stage two is minutes of pure HTTP waiting on a paid image service. Sharing
one worker pool means a single image product holds a slot that could have cleared
nine text listings, and it delays the listing text — the part a human actually
reviews — behind imagery they will look at later.

The handoff is deliberately not a protocol the model can corrupt. The tool enqueues
the job itself with ``enqueue_after_commit=True``, so it fires on ``save_listing``'s
commit — the enriched listing is guaranteed to exist by then — and is dropped
entirely if the run fails first, because ``db.rollback()`` resets the after-commit
callbacks. Everything stage two needs travels in the job's own arguments; nothing is
reconstructed by guessing at saved rows.
"""

import frappe

ENRICHED_DOCTYPE = "Shopify Enriched Listing"

# Stage two runs on its own queue when the bench defines one, because the whole
# point is that image work cannot starve anything else. Falls back to `long` so the
# app still works on a stock bench — see the README on adding a dedicated queue.
QUEUE_KEY = "listing_image_queue"
DEFAULT_QUEUE = "long"

# Generous: a five-shot set is five sequential-worst-case image renders plus the
# re-hosting of each result.
JOB_TIMEOUT = 1800

# The two steps stage two knows how to render, mapped to the module that owns each.
GENERATE = "generate"
TRANSLATE = "translate"


def image_queue():
	"""The queue stage two runs on, falling back to `long` if it isn't configured.

	A custom queue only exists if the bench declares it under `workers` in
	common_site_config.json — Frappe validates the name and raises otherwise. That
	raise would happen inside the agent's run and fail an otherwise good
	enrichment, so a queue named here but never provisioned degrades to `long`
	instead of taking the listing down with it.
	"""
	from frappe.utils.background_jobs import get_queues_timeout

	queue = frappe.conf.get(QUEUE_KEY)
	if queue and queue in get_queues_timeout():
		return queue
	if queue:
		frappe.log_error(
			title="Shopify listing images: unknown queue",
			message=(
				f"'{queue}' is set as {QUEUE_KEY} but is not declared under `workers` "
				f"in common_site_config.json; falling back to '{DEFAULT_QUEUE}'."
			),
		)
	return DEFAULT_QUEUE


def queue_step(item_code, step, work):
	"""Queue this product's image work, to run once the listing itself is saved.

	Called by the image tools mid-run. `work` is everything the renderer needs —
	the resolved reference photo and briefs, or the resolved photo urls — so stage
	two never has to re-derive intent from what the model chose to write down.
	"""
	frappe.enqueue(
		"alaiy_os_agent_shopify_listing.image_stage.run_step",
		queue=image_queue(),
		timeout=JOB_TIMEOUT,
		# One image job per product. A re-run that queues again while the first is
		# still waiting replaces nothing and adds nothing.
		job_id=f"listing-images::{item_code}",
		deduplicate=True,
		item_code=item_code,
		step=step,
		work=work,
		# Fires on save_listing's commit; discarded if the run rolls back first.
		enqueue_after_commit=True,
	)


def run_step(item_code, step, work):
	"""Worker entry point: render this product's images and patch them in."""
	if not frappe.db.exists(ENRICHED_DOCTYPE, item_code):
		# The only way here is a run that saved its listing and had it deleted
		# before this job ran. Nothing to patch, and nothing worth failing over.
		_nudge_batches(item_code)
		return

	_set_state(item_code, "Running", None)

	try:
		result = _render(step, item_code, work)
	except Exception as exc:
		# The service is down, unconfigured, or refused the whole product. The
		# listing text is already saved and reviewable — only the imagery is lost.
		frappe.log_error(title=f"Listing images {item_code}: {step} failed")
		_set_state(item_code, "Failed", _summary(str(exc)))
		_publish(item_code, "Failed")
		_nudge_batches(item_code)
		return

	rendered = result["images"]
	produced = _apply(item_code, rendered)
	failed = len(rendered) - produced

	if not produced:
		status = "Failed" if rendered else "Ready"
	elif failed:
		status = "Partial"
	else:
		status = "Ready"

	_set_state(
		item_code,
		status,
		_first_note(rendered) if failed else None,
		# The image spend happens out here now, after the Run is closed, so it is
		# recorded against the listing instead of vanishing from the accounting.
		tokens=result.get("image_tokens") or 0,
	)
	_publish(item_code, status)
	_nudge_batches(item_code)


def _nudge_batches(item_code):
	"""Tell bulk enrichment this product's imagery is settled — a batch whose runs
	are all done parks in "Generating Images" and waits for exactly this (see
	bulk._finalize). Never allowed to fail the job: the images themselves are
	already applied by the time this runs."""
	try:
		from alaiy_os_agent_shopify_listing.bulk import finalize_images

		finalize_images(item_code)
	except Exception:
		frappe.log_error(title=f"Listing images {item_code}: batch nudge failed")


def _render(step, item_code, work):
	"""Dispatch to the module that owns this step. Returns the finished image rows."""
	if step == GENERATE:
		from alaiy_os_agent_shopify_listing.tools.image_generation import render_generated

		return render_generated(item_code, work)

	if step == TRANSLATE:
		from alaiy_os_agent_shopify_listing.tools.image_translation import render_translated

		return render_translated(item_code, work)

	frappe.throw(f"Unknown image step '{step}'.")


def _apply(item_code, rendered):
	"""Write the rendered urls onto the listing's image rows. Returns how many worked.

	Rows are matched to what stage one already wrote — by `kind` for a generated
	set, by `source_url` for translated photos — and appended if it is not there, so
	a rendered image is never silently thrown away. A translated photo arrives as one
	entry PER USE (see render_translated), so the listing's row and each variant's
	row are written from their own entry, carrying their own `item_variant`.

	Appending is not an edge case for translation: the rows are rebuilt by
	save_listing from what the MODEL wrote down, and a model that dropped an entry —
	or its item_variant — would otherwise cost that variant its picture. Here the
	plan the tool queued wins, and the listing ends up with the rows it should have
	regardless of what the model reported.
	"""
	doc = frappe.get_doc(ENRICHED_DOCTYPE, item_code)
	existing = list(doc.images or [])
	produced = 0

	for image in rendered:
		rows = _match(existing, image)
		if not rows:
			row = doc.append("images", {"item_variant": image.get("item_variant")})
			existing.append(row)
			rows = [row]

		for row in rows:
			row.kind = image.get("kind") or row.kind
			row.source_url = image.get("source_url") or row.source_url
			row.brief = image.get("brief") or row.brief
			row.url = image.get("url")
			row.note = image.get("note")
		if image.get("url"):
			produced += 1

	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return produced


def _match(rows, image):
	"""The row(s) this rendered image belongs in: the placeholders stage one wrote
	for it, plus any row that already holds exactly this result.

	A rendered image that names its variant — every translated entry does, with None
	for a listing-level one — is only ever matched against rows for that same
	variant. Without that, a variant's photo and the listing's copy of the same photo
	would match each other and the wrong picture could reach `variant_image`.

	Matching a row that already holds this url is what makes re-delivery a no-op: a
	job queued only to reconcile rows an earlier run produced must not append a
	second copy of every one of them.
	"""
	if "item_variant" in image:
		want = image.get("item_variant") or None
		rows = [row for row in rows if (row.get("item_variant") or None) == want]

	for key in ("source_url", "kind"):
		value = image.get(key)
		if not value:
			continue
		matched = [
			row
			for row in rows
			if row.get(key) == value and (not row.url or row.url == image.get("url"))
		]
		if matched:
			return matched
	return []


def _set_state(item_code, status, error, tokens=None):
	"""Image state is written straight to the database, never through the document.

	Loading a fresh doc to save one field would fight with `_apply`'s save, and the
	reviewer's own edits to the listing must not be clobbered by a background job.
	"""
	values = {"image_status": status, "image_error": error}
	if tokens:
		values["image_tokens"] = tokens
	frappe.db.set_value(ENRICHED_DOCTYPE, item_code, values, update_modified=False)
	frappe.db.commit()


def _publish(item_code, status):
	"""So an open listing form can show the images arriving without polling."""
	frappe.publish_realtime(
		"listing_images_done",
		{"item_code": item_code, "status": status},
		doctype=ENRICHED_DOCTYPE,
		docname=item_code,
	)


def _first_note(rendered):
	for image in rendered:
		if not image.get("url") and image.get("note"):
			return _summary(image["note"])
	return None


def _summary(text):
	return (text or "").strip()[:500] or None
