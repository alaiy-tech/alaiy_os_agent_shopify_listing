# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
The `generate_product_images` tool: every product photo, retouched.

One of the two image steps this app ships. Both are always registered; the agent's
prompt is what decides which it calls — see agent_meta.py.

What this step is, and what it deliberately is not. It takes each photo the product
already has and returns a cleaner version of that same photo — even lighting, true
colour, no dust or glare, a tidy background. It does NOT compose new shots. An
earlier version of this tool asked the model for a five-shot editorial set (hero,
detail, angle, lifestyle, scale) built by editing one reference photo; handed a
photo plus a rich brief, an image model re-renders an idealised lookalike rather
than editing the real piece, and a luxury resale catalog cannot ship a photo of
something the customer will not receive. So the briefs are gone: there is no shot
vocabulary, nothing for the model to compose, and one fixed retouch instruction
(enhance_prompt) that every photo goes through unchanged.

That also means the model no longer chooses what imagery exists. It calls the tool;
the tool processes whatever photos the product has.

The rendering itself goes through Alaiy OS core's `ai_client` seam
(`llm.generate_image`), the same seam the agent's text turns use. This app holds
no provider credential and names no image model: on a managed bench the call is
served by the billing service, which owns the key and meters the spend; on a BYOK
bench core's default client uses that site's own `openrouter_api_key`.

Each enhanced photo is stored as its own public File, so the original is never
overwritten and a bad result is always recoverable.

Two halves, split across two stages. `generate_product_images` runs inside the
agent's run: it decides whether enhancement happens at all, and queues it. The
actual rendering is `render_generated`, which runs later on the image queue — see
image_stage.py for why.
"""

import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor

import frappe
from alaiy_os.engine import llm

from alaiy_os_agent_shopify_listing import image_stage
from alaiy_os_agent_shopify_listing.tools import handlers as base
from alaiy_os_agent_shopify_listing.tools import image_style, images

# Every photo a product has is enhanced — the listing's own and each enabled
# variant's. There is deliberately no per-product cap: a photo left unenhanced is a
# photo that sits next to enhanced ones on the same listing, looking worse by
# comparison. Spend is bounded by gate 3 instead (a photo is never enhanced twice)
# and by the toggle being off by default.

# How many photos are rendered at once — see render_generated. This is a paid image
# service, and firing every photo of every product in a batch at it simultaneously
# is a good way to get throttled.
_RENDER_CONCURRENCY = 4

# What stage one puts on an image row that stage two has not produced yet. It is
# read by a human on the Desk form, so it says what is happening, not "queued".
_QUEUED_NOTE = "Being enhanced in the background; the image will appear here when ready."

# What goes on a photo an earlier run already enhanced. Also read by a human, so it
# explains why this one has a url when its siblings do not.
_REUSED_NOTE = "Enhanced on an earlier run; reused rather than enhanced again."

# What must not change, in the terms a jewelry and watch catalog cares about — a
# moved stone, a straightened link or a polished-out scratch turns a listing photo
# into a misrepresentation. This half is the same whatever the site's house style is.
_PRESERVE = (
	"Retouch this product photograph. Return the SAME photograph, cleaned up — not "
	"a new image of the product, and not a restyled or re-rendered one.\n\n"
	"Leave the product itself untouched: the same shape and proportions, the same "
	"material and metal colour, the same stones in the same cut, count, size, "
	"placement and setting, the same dial, hands, markers, links, clasp, "
	"engravings and hallmarks, and the same marks of age, wear and patina. Keep "
	"the original camera angle, pose, framing, crop and aspect ratio. Do not "
	"repair, polish out, straighten, beautify or complete any part of the piece, "
	"and do not add reflections, props, hands, models, text or watermarks."
)

# The retouching that IS wanted, listed specifically rather than as "make it
# better", which an image model reads as licence to restyle. The material-by-
# material half comes from the client's photography guidelines: a warm yellow gold,
# a cool-but-not-grey white gold and a pink-but-not-lurid rose gold are the three
# ways this catalog is most often got wrong, and "correct the white balance" alone
# does not say which way to err.
_IMPROVE = (
	"Improve only the photography: even out the lighting and remove harsh glare "
	"and blown highlights, correct the white balance and colour cast so the metal "
	"and stones read true to the original, recover detail lost in shadow, sharpen "
	"detail that is genuinely there, reduce noise and compression artefacts, and "
	"remove dust, lint, fingerprints and smudges.\n\n"
	"Hold each material to its own character. Yellow gold keeps its warmth and "
	"saturation without going orange; white gold reads cool without turning grey "
	"or blue; rose gold keeps its pink hue, neither washed out nor oversaturated. "
	"Diamonds and colourless stones keep their fire and brilliance and stay "
	"well-defined against their settings, without being flattened or blown out; "
	"coloured stones stay bright and true. Pearls keep a satin lustre, not a gloss. "
	"Let polished metal carry specular highlights that read as depth, and keep a "
	"brushed finish visibly brushed. Deepen shadows on the piece only enough to "
	"give it dimension. The result must look polished but believable: an "
	"over-edited photograph is a worse result than an unedited one."
)

# Appended when the site has a house style (see tools/image_style.py). The ground,
# the margin and the shadow are composited in code afterwards, to an exact colour
# and an exact geometry, so what is wanted from the model here is the OPPOSITE of a
# finished catalog shot: the piece, alone, on nothing. A model that helpfully adds
# its own soft shadow or grey sweep does not just duplicate that work, it defeats
# it — the compositor separates the product from the ground by flooding in from the
# edge of the frame, and a gradient or a cast shadow is what makes that refuse to
# run at all (and the photo comes back unfinished).
_ISOLATE = (
	"Place the retouched product on a completely empty, uniform, pure white "
	"background. The background must be a single flat white with no shadow, no "
	"reflection, no gradient, no vignette, no surface, no horizon line and no "
	"texture of any kind, edge to edge. Do not add a drop shadow or a contact "
	"shadow beneath the product — one is applied later. Keep the product itself "
	"fully inside the frame and do not crop into it."
)

_CLOSING = "If a change would alter what the customer actually receives, do not make it."


def enhance_prompt(style=None):
	"""The whole retouch instruction, identical for every photo of every product.

	It is assembled here rather than written by the model because the failure this
	tool exists to avoid is the model describing the product well enough that the
	image service re-renders it: the photograph is the only description of the
	product that is allowed to matter.

	`style` is the site's house style, or None. Its only effect on the prompt is to
	ask for the product on an empty ground instead of a tidied one — everything
	measurable about the house look (the exact background, the margin, the shadow)
	is composited afterwards in image_style.apply_finish, because an image model
	cannot hold an exact hex or a consistent margin across a catalog.

	With no style this returns exactly the instruction this tool has always sent,
	so a site that contributes no guidelines is unaffected by any of it.
	"""
	if not style:
		return "\n\n".join([
			_PRESERVE,
			_IMPROVE
			+ "\n\nAlso clean up the background — even out its tone or replace a "
			"cluttered one with a plain neutral studio surface.",
			_CLOSING,
		])

	return "\n\n".join([_PRESERVE, _IMPROVE, _ISOLATE, _CLOSING])


def generate_product_images(
	item_code=None,
	image_urls=None,
	generate_images=False,
	source_urls=None,
	force=False,
	reference_image_url=None,
	**_retired,
):
	"""
	Queue a product's photos to be retouched, and return immediately. Returns
	{"images": [{source_url, item_variant, url, note}, ...]} — copy that list
	verbatim into the final `images` array.

	`url` comes back null: the photos are enhanced after this run finishes, by
	image_stage.run_step, and attached to the listing then. That is by design — a
	set of images takes minutes, and holding the run open for it would block a
	worker that could be enriching other products. The rendering itself is
	render_generated() below, which goes through core's `ai_client` seam
	(llm.generate_image) and saves each result as its own public File.

	EVERY photo is enhanced: the listing's own PLUS each enabled variant's
	`variant_image`, under the one toggle, with no per-product cap. A URL shared by
	the listing and a variant (or by two variants) is paid for once — one render
	fills every row that references it.

	Which photo belongs to which variant is settled HERE, in code, and travels with
	the queued job: stage two writes the rows from that plan, not from the `images`
	array the model returns. So a variant's enhanced photo reaches its variant even
	if the model drops the entry, or drops its `item_variant`, on the way out. The
	entries returned below are the same plan, for the model to report — its copy of
	the truth, not the truth itself.

	A row's `item_variant` is what routes the result to that variant's
	`variant_image` when an admin approves the listing (see
	ShopifyEnrichedListing._sync_variant_images); nothing is written to the Shopify
	Product Listing before that approval.

	The exception is a URL-only product: it has no listing record for stage two to
	deliver into, so its photos are enhanced inline and come back with real urls.

	Whether we enhance at all is decided HERE, deterministically — not left to the
	model's judgement — by three gates:

	  1. There must be at least one existing photo. For an item_code run those are
	     the listing's own photos (read from the Shopify Product Listing's images
	     table) plus its enabled variants'; for a URL-only product they are
	     image_urls. No photos → empty list, nothing done. This gate is airtight: it
	     holds regardless of what the model passes, and it is why this agent can
	     never produce imagery for a product it has no photograph of.
	  2. Enhancement is opt-in per request: generate_images must be true. When
	     photos exist but the toggle is off, we return empty too.
	  3. A photo already enhanced on an earlier run is never enhanced again — its
	     existing result is returned as-is. This is per photo, not per product, so a
	     listing that gained a photo only pays for the new one. A photo that FAILED
	     has no url and so is not "already enhanced": it is retried.

	Per-image failures degrade rather than raise: that entry comes back with
	url=None and a note, and the remaining photos still process. If the site's AI
	client cannot generate images at all (and both gates pass), the model is told
	via the thrown message not to retry and to fall back to url=null placeholders
	instead of stalling the rest of the listing.

	`source_urls` and `force` are for the per-photo endpoint
	(api.enrich_listing_image), not for the model. They are deliberately absent from
	the tool's declared schema in agent_meta.py, so a run can neither narrow the work
	nor bypass gate 3 — an agent enriching a product always covers all of its photos,
	and never pays twice. `source_urls` narrows the resolved targets to those photos
	(every use of each still travels, so a shared photo still fills every row it
	belongs to); `force` skips the gate 3 lookup so a photo a reviewer was unhappy
	with can be rendered again.

	`reference_image_url` and `**_retired` are compatibility, not API. This tool used
	to take `briefs` and a single `reference_image_url`; a site whose OS Agent
	Registry has not been re-synced since still advertises those, and a model will
	occasionally reach for them from habit either way. Retired arguments are ignored
	(there are no briefs to honour), and a lone reference url is treated as the
	URL-only product's one photo rather than dropped.
	"""
	image_urls = list(image_urls or [])
	if reference_image_url and reference_image_url not in image_urls:
		image_urls.append(reference_image_url)

	# ── Gate 1 (airtight): resolve the photos; no photos → nothing to do.
	# Targets are (source_url, item_variant) pairs: the listing's own photos first
	# (item_variant None), then each enabled variant's photo tagged with its
	# variant. All of them are enhanced; the order is just what a reviewer expects
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

	# Narrow to the photos a caller named, AFTER gate 1 resolved them from the
	# product — so a named photo still has to be one this product actually has, and
	# every use of it still travels. Filtering here rather than at the source keeps
	# gate 1 airtight: nothing a caller passes can introduce a photo.
	if source_urls is not None:
		wanted = {url for url in source_urls if url}
		narrowed = [target for target in targets if target["source_url"] in wanted]
		if targets and not narrowed:
			return {
				"images": [],
				"note": (
					"None of the requested photos belong to this product, so nothing "
					"was enhanced."
				),
			}
		targets = narrowed

	if not targets:
		return {
			"images": [],
			"note": (
				"This product has no photos, so nothing was enhanced — this agent "
				"only ever improves an existing photograph, and never creates "
				"product imagery from scratch."
			),
		}

	# ── Gate 2: image enhancement is opt-in per request.
	if not generate_images:
		return {
			"images": [],
			"note": (
				"The product has photos, but the generate_images toggle is off, so "
				"no images were enhanced."
			),
		}

	# Checked here, while the model is still listening, rather than leaving it to
	# discover a misconfigured site minutes later in the background. Only the
	# capability is checked, not a credential — the credential lives off-bench now,
	# behind the seam, so this app has nothing to inspect.
	#
	# Skipped entirely when the site's house style has retouching off: that path
	# never calls the image service, and refusing the work because a service it
	# will not use is unavailable would take enhancement down on exactly the sites
	# that need nothing from it.
	style = image_style.load()
	if retouch_wanted(style) and not llm.image_client().image_support().get("generate"):
		frappe.throw(
			"Image enhancement is not available on this site (the active AI client "
			"cannot generate images). Do NOT retry; return each image with url=null "
			"so the team can retouch it manually."
		)

	# A URL-only product has no Shopify Enriched Listing for stage two to patch, so
	# there is nowhere to deliver the images later — render them inline.
	if not item_code:
		urls = [t["source_url"] for t in targets]
		result = render_generated(None, {"urls": urls})
		return {
			"images": result["images"],
			# The executor's "_usage" convention, so image cost lands in the Run.
			"_usage": {"image_tokens": result["image_tokens"]},
		}

	# ── Gate 3: never pay to enhance the same photo twice. Per photo URL, not per
	# target: a URL shared by the listing and a variant is queued once, and stage two
	# fills every row that references it.
	done = {} if force else already_enhanced(item_code)
	todo = []
	for target in targets:
		url = target["source_url"]
		if url not in done and url not in todo:
			todo.append(url)

	# `targets` is the plan stage two delivers against: every use of every photo —
	# the listing's own and each variant's — with the url already known for it. It is
	# queued EVEN WHEN `todo` is empty (everything was enhanced on an earlier run),
	# because writing those rows back onto the listing is the job's other half, and
	# reconciling them costs nothing when there is nothing left to render.
	plan = [dict(target, url=done.get(target["source_url"])) for target in targets]
	image_stage.queue_step(
		item_code,
		image_stage.GENERATE,
		{"urls": todo, "targets": plan},
		# A whole-product run wants one job per product; a caller working photo by
		# photo needs its photo to be the unit, or the second photo asked for gets
		# swallowed as a duplicate of the first. See image_stage.queue_step.
		job_key=_photo_job_key(source_urls) if source_urls is not None else None,
	)

	# The same plan, for the model to report. Every entry is returned — including the
	# ones reused from an earlier run — because save_listing rebuilds the image table
	# from what this run reports; an entry left out here would disappear from the
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

	notes = []
	if todo:
		notes.append(
			f"{len(todo)} photo(s) queued for enhancement. They are being processed in "
			"the background and will be attached to this listing when they are ready — "
			"this is normal and is NOT a failure. Copy these entries verbatim "
			"(including each entry's item_variant), leave url as null, and do NOT "
			"record them in needs_review."
		)
	reused = sum(1 for entry in plan if entry["url"])
	if reused:
		notes.append(
			f"{reused} entr(ies) were enhanced on an earlier run and are reused "
			"as-is, with their existing url. Copy them verbatim too."
		)
	variants = sum(1 for entry in plan if entry["item_variant"])
	if variants:
		notes.append(
			f"{variants} of these entries are variant photos, each carrying the "
			"item_variant it belongs to."
		)
	if notes:
		result["note"] = " ".join(notes)

	return result


def _photo_job_key(source_urls):
	"""A stable job key for one specific set of photos — see image_stage.queue_step.

	Derived from the urls themselves, and sorted, so the same photo asked for twice
	produces the same key and collapses into the one job, while two different photos
	get their own. Hashed rather than used raw because a job id is a Redis key and a
	full CDN url is a poor one.
	"""
	joined = "\n".join(sorted(url for url in source_urls if url))
	return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def already_enhanced(item_code):
	"""{source_url: url} for photos a previous run already enhanced.

	"Already done" means the listing holds an enhanced image for that exact source
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


def render_generated(item_code, work):
	"""Retouch the queued photos. Stage two's worker — see image_stage.py.

	`work` holds the photos to enhance (`urls`) and, for an item_code run, the plan
	they were queued for (`targets`: every use of every photo as
	{source_url, item_variant, url-already-known}).

	Returns {"images": [{source_url, item_variant, url, note}, ...], "image_tokens":
	int} — ONE ENTRY PER USE, not per photo. A photo the listing and two variants
	share is enhanced once and comes back as three entries, so image_stage._apply
	writes it onto all three rows and each variant's row carries its own
	item_variant. A photo that failed comes back with url=None and a note, so one
	bad photo costs one photo rather than the whole product.

	Without `targets` (a URL-only product) it falls back to one entry per photo,
	which is all that shape has.

	The photos are rendered concurrently. They are independent calls to a service
	that takes about a minute each, and running them in sequence was the single
	largest cost in the whole enrichment.
	"""
	urls = work.get("urls") or []
	targets = work.get("targets")

	# The house style, resolved here for the same reason the sources are: it reads
	# hooks and site config, which a worker thread has no site context for. None
	# when the site contributes no guidelines, and then nothing is composited.
	style = image_style.load()
	retouch = retouch_wanted(style)

	# Only a job that is actually going to generate needs the service — a job that
	# is only compositing does not, and neither does one queued purely to write an
	# earlier run's results back onto the listing.
	client = llm.image_client() if (urls and retouch) else None
	if client and not client.image_support().get("generate"):
		frappe.throw("Image enhancement is not available on this site.")

	# Resolved on THIS thread, before the pool starts: reading a stored Frappe File
	# or downloading an external photo needs the site context, which a worker thread
	# does not have. The client instance itself is thread-safe by contract once
	# built (see alaiy_os/engine/ai_client.py).
	sources = []
	failed_to_read = {}
	for url in urls:
		try:
			sources.append((url, images.reference_source(url)))
		except Exception as exc:
			# A photo we cannot even read is this photo's failure, not the product's.
			failed_to_read[url] = f"Could not read the source photo: {exc}"[:200]

	def work_on(pair):
		source = pair[1]
		if retouch:
			return _try_generate(client, images.data_uri(source), style)
		# No generative step at all: the photograph's own pixels are what get
		# composited, so nothing can alter the product.
		return _try_finish(base64.b64decode(source["data"]), source["media_type"], style)

	results = []
	if sources:
		with ThreadPoolExecutor(max_workers=min(_RENDER_CONCURRENCY, len(sources))) as pool:
			results = list(pool.map(work_on, sources))

	# What this run produced, per photo. The fan-out onto each use of the photo
	# happens below, so a shared photo is stored once here.
	fresh = {}
	total_tokens = 0
	for url, message in failed_to_read.items():
		frappe.log_error(
			title="Shopify listing: image enhancement failed",
			message=f"{item_code} / {url}\n{message}",
		)
		fresh[url] = {"url": None, "note": message}

	for (url, _), (payload, error) in zip(sources, results, strict=True):
		if error:
			# Logged here rather than in the worker thread: frappe.log_error needs
			# the request context that only this thread has.
			frappe.log_error(
				title="Shopify listing: image enhancement failed",
				message=f"{item_code} / {url}\n{error}",
			)
			fresh[url] = {"url": None, "note": f"Enhancement failed: {error}"[:200]}
			continue

		total_tokens += (payload.get("usage") or {}).get("total_tokens", 0)
		# Saving writes a File row, so it stays on this thread too. The cutout is a
		# second file rather than a replacement: the composited image is what the
		# listing publishes, and the transparent one is what lets the background be
		# changed later without paying to render the photo again.
		cutout = payload.get("cutout")
		fresh[url] = {
			"url": images.save_public_image(
				"listing-enhanced",
				payload["content"],
				payload.get("media_type") or "image/png",
			),
			"cutout_url": (
				images.save_public_image("listing-cutout", cutout, "image/png")
				if cutout
				else None
			),
			# Normally None. Set when the photo came back but the house finish could
			# not be applied to it — the photo is still good, so this is a note on a
			# successful row, not a failure.
			"note": payload.get("note"),
		}

	if targets is None:
		# Ordered by `urls`, not by `fresh`: the photos a reviewer sees should come
		# back in the order they were given, not with the unreadable ones first.
		return {
			"images": [{"source_url": url, **fresh[url]} for url in urls],
			"image_tokens": total_tokens,
		}

	out = []
	for target in targets:
		source_url = target["source_url"]
		if source_url in fresh:
			produced = fresh[source_url]
		else:
			# Nothing was owed for this photo: an earlier run already enhanced it,
			# and the url came along in the plan.
			produced = {
				"url": target.get("url"),
				"note": _REUSED_NOTE if target.get("url") else None,
			}
		out.append({
			"source_url": source_url,
			"item_variant": target.get("item_variant"),
			**produced,
		})

	return {"images": out, "image_tokens": total_tokens}


def retouch_wanted(style):
	"""Whether a photo goes through a generative retouch before it is composited.

	True with no house style at all, because that is this tool's original job and
	the only thing it did. A style may turn it off, and The Solist's does: with it
	off nothing regenerates the product, so the only thing that changes about a
	photograph is what is behind it.
	"""
	return True if not style else bool(style.get("retouch"))


def _try_finish(content, media_type, style):
	"""One photo, composited onto the house ground and nothing else.

	The counterpart to _try_generate for a site whose style has retouching off.
	Same (payload, error) contract, so the caller does not care which ran — but no
	image service is involved, nothing is charged, and the product's pixels reach
	the canvas exactly as the photographer took them.
	"""
	try:
		finished = image_style.apply_finish(content, style)
	except Exception as exc:
		return None, str(exc)

	return {
		"content": finished["image"],
		"media_type": finished["mime"] or media_type,
		# No generative call, so nothing to bill.
		"usage": None,
		"cutout": finished["cutout"],
		"note": finished["note"],
	}, None


def _try_generate(client, reference_data_uri, style=None):
	"""One photo, in a worker thread. Returns (payload, error) — never raises.

	    payload = {"content": bytes, "media_type": str, "usage": dict,
	               "cutout": bytes|None, "note": str|None}

	Nothing in here touches Frappe: the thread has no request context, so a
	frappe.throw or a db read from inside it would fail in a confusing way. The
	client was built on the calling thread and is thread-safe by contract, and
	`style` was resolved there too.

	The photo goes in as the reference and the assembled prompt is the whole of the
	instruction, so the service is always editing a real photograph rather than
	composing from a description.

	The house finish runs HERE rather than back on the main thread, because it is
	pure pixel work with no site context to need and this thread is otherwise idle
	the whole time — compositing a whole product's photos in series afterwards would
	add that time to every job for no reason. When the site has no house style, or
	the render did not come back in a state that can be finished, `content` is
	simply what the service returned.
	"""
	try:
		payload = client.generate_image(enhance_prompt(style), reference_data_uri=reference_data_uri)
	except Exception as exc:
		return None, str(exc)

	content = base64.b64decode(payload["b64"])
	media_type = payload.get("media_type") or "image/png"
	if not style:
		return {"content": content, "media_type": media_type, "usage": payload.get("usage")}, None

	finished = image_style.apply_finish(content, style)
	return {
		"content": finished["image"],
		# apply_finish reports no media type when it left the image alone.
		"media_type": finished["mime"] or media_type,
		"usage": payload.get("usage"),
		"cutout": finished["cutout"],
		"note": finished["note"],
	}, None
