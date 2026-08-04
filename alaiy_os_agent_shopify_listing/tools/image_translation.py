# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
The `translate_product_images` tool: supplier photos with their printed Chinese
text rendered into English.

One of the two image steps this app ships. It does not generate new imagery — that
is `generate_product_images`, a deliberately different capability. Both are always
registered; the agent's prompt is what decides which it calls — see agent_meta.py.

The translation itself goes through Alaiy OS core's `ai_client` seam
(`llm.translate_image`), the same seam the agent's text turns use. This app holds
no vendor credential: on a managed bench the call is served by the billing
service, which owns the alphashop key and meters the spend. A BYOK bench has no
translation provider and the tool reports that rather than half-working.

Each translated photo is stored as its own public File, so the original supplier
photo is never overwritten and a bad translation is always recoverable.

Two halves, split across two stages. `translate_product_images` runs inside the
agent's run: it decides whether translation happens at all, and queues it. The
actual work is `render_translated`, which runs later on the image queue — see
image_stage.py for why.
"""

from concurrent.futures import ThreadPoolExecutor

import frappe
from alaiy_os.engine import llm

from alaiy_os_agent_shopify_listing import image_stage
from alaiy_os_agent_shopify_listing.tools import handlers as base
from alaiy_os_agent_shopify_listing.tools import images

# Every photo a product has is translated — the listing's own and each enabled
# variant's. There is deliberately no per-product cap: a variant whose photo was
# left untranslated is a variant that ships with Chinese text on it, which is worse
# than the cost of translating it. Spend is bounded by gate 3 instead (a photo is
# never translated twice) and by the toggle being off by default.

# How many photos go through the service at once — see render_translated. This is a
# paid third-party API, and firing every photo of every product in a batch at it
# simultaneously is a good way to get throttled.
_RENDER_CONCURRENCY = 4

# What stage one puts on an image row that stage two has not produced yet. It is
# read by a human on the Desk form, so it says what is happening, not "queued".
_QUEUED_NOTE = "Being translated in the background; the image will appear here when ready."

# What goes on a photo an earlier run already translated. Also read by a human, so
# it explains why this one has a url when its siblings do not.
_REUSED_NOTE = "Translated on an earlier run; reused rather than translated again."

def translate_product_images(item_code=None, image_urls=None, translate_images=False):
	"""
	Queue a product's supplier photos to have their printed Chinese text rendered
	into English, and return immediately. Returns
	{"images": [{source_url, url, note}, ...]} — copy that list verbatim into the
	final `images` array.

	`url` comes back null: the photos are translated after this run finishes, by
	image_stage.run_step, and attached to the listing then. That is by design — the
	translation service takes minutes, and holding the run open for it would block
	a worker that could be enriching other products. The work itself is
	render_translated() below, which goes through core's `ai_client` seam
	(llm.translate_image) and re-hosts each result.

	EVERY photo is translated: the listing's own PLUS each enabled variant's
	`variant_image`, under the one toggle, with no per-product cap. A URL shared by
	the listing and a variant (or by two variants) is paid for once — one translation
	fills every row that references it.

	Which photo belongs to which variant is settled HERE, in code, and travels with
	the queued job: stage two writes the rows from that plan, not from the `images`
	array the model returns. So a variant's translated photo reaches its variant even
	if the model drops the entry, or drops its `item_variant`, on the way out. The
	entries returned below are the same plan, for the model to report — they are its
	copy of the truth, not the truth itself.

	A row's `item_variant` is what routes the result to that variant's
	`variant_image` when an admin approves the listing (see
	ShopifyEnrichedListing._sync_variant_images); nothing is written to the Shopify
	Product Listing before that approval.

	The exception is a URL-only product: it has no listing record for stage two to
	deliver into, so its photos are translated inline and come back with real urls.

	Whether we translate at all is decided HERE, deterministically — not left to the
	model's judgement — by two gates:

	  1. There must be at least one photo to translate. For an item_code run those
	     are the listing's own photos (read from the Shopify Product Listing's images
	     table); for a URL-only product they are image_urls. No photos → empty list,
	     nothing done. This gate is airtight: it holds regardless of what the model
	     passes.
	  2. Translation is opt-in per request: translate_images must be true. When
	     photos exist but the toggle is off, we return empty too.
	  3. A photo already translated on an earlier run is never translated again —
	     its existing result is returned as-is. This is per photo, not per product,
	     so a listing that gained a photo only pays for the new one. A photo that
	     FAILED has no url and so is not "already translated": it is retried.

	Per-image failures degrade rather than raise: that entry comes back with
	url=None and a note, and the remaining photos still process. If the service
	isn't configured at all (and both gates pass), the model is told via the thrown
	message not to retry and to fall back to null placeholders instead of stalling
	the rest of the listing.
	"""
	# ── Gate 1 (airtight): resolve the photos; no photos → nothing to do.
	# Targets are (source_url, item_variant) pairs: the listing's own photos first
	# (item_variant None), then each enabled variant's photo tagged with its
	# variant. All of them are translated; the order is just what a reviewer expects
	# to see first on the listing form.
	targets = []
	if item_code and frappe.db.exists(base.LISTING_DOCTYPE, item_code):
		listing = frappe.get_doc(base.LISTING_DOCTYPE, item_code)
		targets = [
			{"source_url": url, "item_variant": None}
			for url in base.listing_image_urls(listing)
		]
		targets += [
			{"source_url": url, "item_variant": item_variant}
			for item_variant, url in base.variant_image_map(listing).items()
		]
	if not targets and image_urls:
		targets = [{"source_url": u, "item_variant": None} for u in image_urls if u]
	if not targets:
		return {
			"images": [],
			"note": "This product has no photos, so nothing was translated.",
		}

	# ── Gate 2: image translation is opt-in per request.
	if not translate_images:
		return {
			"images": [],
			"note": (
				"The product has photos, but the translate_images toggle is off, so "
				"no images were translated."
			),
		}

	# Checked while the model is still listening, rather than leaving it to
	# discover a misconfigured site minutes later in the background. Only the
	# capability is checked, not a credential — the credential lives off-bench now.
	if not llm.image_client().image_support().get("translate"):
		frappe.throw(
			"Image translation is not available on this site (the active AI client "
			"cannot translate images). Do NOT retry; return each image with url=null "
			"so the team can translate it manually."
		)

	# A URL-only product has no Shopify Enriched Listing for stage two to patch, so
	# there is nowhere to deliver the results later — translate inline, as before.
	if not item_code:
		urls = [t["source_url"] for t in targets]
		return {"images": render_translated(None, {"urls": urls})["images"]}

	# ── Gate 3: never pay to translate the same photo twice. Per photo URL, not per
	# target: a URL shared by the listing and a variant is queued once, and stage two
	# fills every row that references it.
	done = _already_translated(item_code)
	todo = []
	for target in targets:
		url = target["source_url"]
		if url not in done and url not in todo:
			todo.append(url)

	# `targets` is the plan stage two delivers against: every use of every photo —
	# the listing's own and each variant's — with the url already known for it. It is
	# queued EVEN WHEN `todo` is empty (everything was translated on an earlier run),
	# because writing those rows back onto the listing is the job's other half, and
	# reconciling them costs nothing when there is nothing left to translate.
	plan = [dict(target, url=done.get(target["source_url"])) for target in targets]
	image_stage.queue_step(item_code, image_stage.TRANSLATE, {"urls": todo, "targets": plan})

	# The same plan, for the model to report. Every entry is returned — including the
	# ones reused from an earlier run — because save_listing rebuilds the image table
	# from what this run reports; a translation left out here would disappear from the
	# listing the model returns, even though stage two will still deliver it.
	result = {
		"images": [
			{
				"source_url": entry["source_url"],
				"item_variant": entry["item_variant"],
				"url": entry["url"],
				"note": _REUSED_NOTE if entry["url"] else _QUEUED_NOTE,
			}
			for entry in plan
		]
	}

	variants = sum(1 for entry in plan if entry["item_variant"])
	notes = []
	if todo:
		notes.append(
			f"{len(todo)} photo(s) queued for translation. They are being processed in "
			"the background and will be attached to this listing when they are ready — "
			"this is normal and is NOT a failure. Copy these entries verbatim "
			"(including each entry's item_variant), leave url as null, and do NOT "
			"record them in needs_review."
		)
	reused = sum(1 for entry in plan if entry["url"])
	if reused:
		notes.append(
			f"{reused} entr(ies) were translated on an earlier run and are reused "
			"as-is, with their existing url. Copy them verbatim too."
		)
	if variants:
		notes.append(
			f"{variants} of these entries are variant photos, each carrying the "
			"item_variant it belongs to."
		)
	if notes:
		result["note"] = " ".join(notes)

	return result


def _already_translated(item_code):
	"""{source_url: url} for photos a previous run already translated.

	"Already done" means the listing holds a translated image for that exact source
	photo. A photo that failed has no url, so it is absent from this map and gets
	another attempt — which is the retry behaviour we want without a flag for it.
	"""
	if not frappe.db.exists(base.ENRICHED_DOCTYPE, item_code):
		return {}

	rows = frappe.get_all(
		"Shopify Enriched Listing Image",
		filters={"parent": item_code, "parenttype": base.ENRICHED_DOCTYPE},
		fields=["source_url", "url"],
	)
	return {row.source_url: row.url for row in rows if row.source_url and row.url}


def render_translated(item_code, work):
	"""Translate the queued photos. Stage two's worker — see image_stage.py.

	`work` holds the photos to translate (`urls`) and, for an item_code run, the
	plan they were queued for (`targets`: every use of every photo as
	{source_url, item_variant, url-already-known}).

	Returns {"images": [{source_url, item_variant, url, note}, ...],
	"image_tokens": 0} — ONE ENTRY PER USE, not per photo. A photo the listing and
	two variants share is translated once and comes back as three entries, so
	image_stage._apply writes it onto all three rows and each variant's row carries
	its own item_variant. A photo that failed comes back with url=None and a note, so
	one bad photo costs one photo rather than the whole product.

	Without `targets` (a URL-only product) it falls back to one entry per photo,
	which is all that shape has.

	The photos go through concurrently. Each is an independent call to a service
	that fetches, rewrites and returns an image — slow, and slow in parallel just
	as well.
	"""
	urls = work.get("urls") or []
	targets = work.get("targets")

	# Only a job with something left to translate needs the service. A job queued
	# purely to write an earlier run's results back onto the listing must not fail
	# because the capability has since gone away.
	client = llm.image_client() if urls else None
	if urls and not client.image_support().get("translate"):
		frappe.throw("Image translation is not available on this site.")

	# Both resolved on THIS thread, before the pool starts: the client reads site
	# config and the Frappe hook registry, and expanding a local File path needs
	# the site context. Neither works inside a worker thread — the client instance
	# is thread-safe by contract once built.
	sources = [(url, images.public_image_url(url)) for url in urls]

	results = []
	if sources:
		with ThreadPoolExecutor(max_workers=min(_RENDER_CONCURRENCY, len(sources))) as pool:
			results = list(pool.map(lambda pair: _try_translate(client, pair[1]), sources))

	# What this run produced, per photo. The fan-out onto each use of the photo
	# happens below, so a shared photo is stored once here.
	fresh = {}
	for (url, _public), (payload, error) in zip(sources, results, strict=True):
		if error:
			# Logged here rather than in the worker thread: frappe.log_error needs
			# the request context that only this thread has.
			frappe.log_error(
				title="Shopify listing: image translation failed",
				message=f"{item_code} / {url}\n{error}",
			)
			fresh[url] = {"url": None, "note": f"Translation failed: {error}"[:200]}
			continue

		out_bytes, out_media_type = payload
		# Saving writes a File row, so it stays on this thread too.
		fresh[url] = {
			"url": images.save_public_image(
				"listing-translated", out_bytes, out_media_type, default_ext=".jpg"
			),
			"note": None,
		}

	if targets is None:
		return {
			"images": [{"source_url": url, **fresh[url]} for url, _public in sources],
			"image_tokens": 0,
		}

	out = []
	for target in targets:
		source_url = target["source_url"]
		if source_url in fresh:
			produced = fresh[source_url]
		else:
			# Nothing was owed for this photo: an earlier run already translated it,
			# and the url came along in the plan.
			produced = {"url": target.get("url"), "note": _REUSED_NOTE if target.get("url") else None}
		out.append({
			"source_url": source_url,
			"item_variant": target.get("item_variant"),
			**produced,
		})

	return {"images": out, "image_tokens": 0}


def _try_translate(client, public_url):
	"""One photo, in a worker thread. Returns (payload, error) — never raises.

	Both the translate call and the fetch of its result are pure HTTP, so both
	belong here; nothing in this function touches Frappe, which has no context in
	a worker thread. The client was built on the calling thread and is thread-safe
	by contract. Re-hosting the bytes is the caller's job.
	"""
	try:
		# The provider fetches the image itself, so hand it an absolute URL.
		result = client.translate_image(public_url)
		# Re-host the result: the provider's URL is theirs and may expire, and we
		# want the reviewed listing to keep working regardless.
		return images.fetch_image_bytes(result["translated_url"]), None
	except Exception as exc:
		return None, str(exc)
