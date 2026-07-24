# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
Tool handlers for the Listing Enrichment agent.

Each callable here is referenced by dotted path in agent_meta.py and invoked by
the Alaiy OS executor's tool loop as ``handler(**tool_input)``. A handler
either:

  • returns JSON-serializable data (dict/list/str/…), which is sent back to the
    model as the tool_result, or
  • returns a dict with a "_content_blocks" key holding ready-made Anthropic
    content blocks — used here so the model can actually *see* the product
    photos (vision), not just read their URLs.

Raising is fine: the executor catches the exception and feeds it back to the
model as an errored tool_result. We still prefer to degrade gracefully (skip an
unreadable image, guard optional rows) so a single bad attachment does not sink
the whole enrichment.

The source of truth this agent reads is the **Shopify Product Listing** DocType
(its `name` is the template item_code, autoname: field:item), NOT the Item —
the listing's own fields (title, description, price, variants) and its `images`
child table are all we look at. This agent does not edit the listing (or the
Item behind it) or publish to Shopify — that is the admin approval / connector
step. generate_product_images stores its output as a standalone public File
(not attached to any doctype) so it shows up in the run's own output rather
than mutating anything. save_listing persists the finished enrichment into its
own Enriched Listing DocType (in "Needs Review" status) for the admin to edit
and approve.
"""

import base64
import os

import frappe

# Anthropic vision accepts JPEG, PNG, GIF, WEBP.
_MEDIA_TYPES = {
	".jpg": "image/jpeg",
	".jpeg": "image/jpeg",
	".png": "image/png",
	".gif": "image/gif",
	".webp": "image/webp",
}
_MEDIA_TYPES_BY_MIME = {v: k for k, v in _MEDIA_TYPES.items()}

# Cap how many photos we send to keep token/latency cost bounded.
_MAX_IMAGES = 5

# Some product-photo CDNs block requests with no browser-like User-Agent
# (confirmed: Anthropic's own url-source fetch got refused on one such CDN) —
# so we always fetch external images ourselves rather than passing the bare
# URL for Claude to fetch.
_FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AlaiyOS-ListingEnrichment/1.0)"}

# Image generation goes through OpenRouter's own Unified Image API
# (POST /api/v1/images) — see generate_image.
_IMAGE_GEN_MODEL = "openai/gpt-image-1"

# The DocType this agent reads from. Its `name` is the template item_code
# (autoname: field:item), so a caller's item_code doubles as the listing name.
_LISTING_DOCTYPE = "Shopify Product Listing"


def _media_type(path_or_name):
	ext = os.path.splitext(path_or_name or "")[1].lower()
	return _MEDIA_TYPES.get(ext)


def _image_block_from_file(file_name):
	"""Build a base64 Anthropic image block from a File docname, or None."""
	media_type = _media_type(file_name)
	try:
		file_doc = frappe.get_doc("File", file_name)
		media_type = media_type or _media_type(file_doc.file_name or file_doc.file_url)
		if not media_type:
			return None
		content = file_doc.get_content()  # bytes for a binary/image file
		if isinstance(content, str):
			content = content.encode("utf-8", "ignore")
		return {
			"type": "image",
			"source": {
				"type": "base64",
				"media_type": media_type,
				"data": base64.b64encode(content).decode("ascii"),
			},
		}
	except Exception:
		return None


def _fetch_image_block(image_url):
	"""Download an external image URL ourselves and build a base64 Anthropic
	image block from it — more reliable than a url-source block, since some
	CDNs refuse fetches with no browser-like User-Agent."""
	import requests

	resp = requests.get(image_url, timeout=30, headers=_FETCH_HEADERS)
	resp.raise_for_status()
	media_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
	if not media_type or not media_type.startswith("image/"):
		media_type = _media_type(image_url) or "image/jpeg"
	return {
		"type": "image",
		"source": {
			"type": "base64",
			"media_type": media_type,
			"data": base64.b64encode(resp.content).decode("ascii"),
		},
	}


def _image_block_from_url(url):
	"""Build a vision block from an image URL stored on a listing row: resolve
	a local /files or /private/files URL to its File doc, otherwise fetch an
	external http(s) URL directly. Returns None if it cannot be read."""
	if not url:
		return None
	file_name = frappe.db.get_value("File", {"file_url": url}, "name")
	if file_name:
		return _image_block_from_file(file_name)
	if url.startswith("http"):
		try:
			return _fetch_image_block(url)
		except Exception:
			return None
	return None


def _listing_image_rows(listing):
	"""The listing's images child rows, sorted by sort_order."""
	return sorted(
		listing.get("images") or [],
		key=lambda r: (r.get("sort_order") or 0),
	)


def _primary_listing_image_url(listing):
	"""
	A stable URL for the listing's main photo, handed to the model so it can pass
	it to generate_product_images as reference_image_url — every generated shot
	then edits the real product photo instead of imagining one from text. Prefers
	an ``Original`` (real) photo over an ``AI Enhanced`` render as the edit base;
	falls back to the first image row of any source. Returns None if the listing
	has no usable photo. Note this is the File's own url (e.g. a '/files/...'
	path); generate_product_images resolves such local files directly (see
	_reference_source).
	"""
	rows = _listing_image_rows(listing)
	originals = [r for r in rows if (r.get("source") or "").lower().startswith("original")]
	for row in (originals or rows):
		if row.get("image"):
			return row.get("image")
	return None


def _collect_image_blocks(listing):
	"""
	Gather up to _MAX_IMAGES photo blocks from a Shopify Product Listing's
	`images` child table, in sort order. Each row's `image` is an Attach Image
	URL (a stored File or an external URL). Labels carry the row's `source`
	(Original / AI Enhanced) so the model knows which photos are the real
	product vs. already-enhanced renders.
	"""
	blocks = []
	for row in _listing_image_rows(listing):
		if len(blocks) >= _MAX_IMAGES:
			break
		url = row.get("image")
		block = _image_block_from_url(url)
		if block:
			label = f"{url} ({row.get('source')})" if row.get("source") else url
			blocks.append((label, block))
	return blocks


def get_product(item_code):
	"""
	Return a Shopify Product Listing's data plus its product photos as vision
	content blocks. The listing's `name` is the template item_code, so the
	caller's item_code is used directly as the listing name. The model receives
	a text block of the structured data followed by one labelled image block per
	photo. Reads strictly from the listing — never the underlying Item.
	"""
	if not frappe.db.exists(_LISTING_DOCTYPE, item_code):
		frappe.throw(
			f"No {_LISTING_DOCTYPE} found for item_code '{item_code}'. "
			"Check the input or ask the admin to confirm the product has a listing."
		)

	listing = frappe.get_doc(_LISTING_DOCTYPE, item_code)

	data = {
		"item_code": listing.item,
		"title": listing.get("listing_title"),
		"description": listing.get("listing_description"),
		"price": listing.get("listing_price"),
		"shopify_status": listing.get("sh_shopify_status"),
		"is_enabled": bool(listing.get("is_enabled")),
		"shopify_product_id": listing.get("sh_shopify_product_id"),
		# The real product photo to reuse as the image-gen reference (or None).
		"primary_image_url": _primary_listing_image_url(listing),
		"variants": [
			{
				"item_variant": v.get("item_variant"),
				"price": v.get("variant_price"),
				"is_enabled": bool(v.get("is_enabled")),
			}
			for v in (listing.get("variants") or [])
		],
	}

	labelled = _collect_image_blocks(listing)
	data["image_count"] = len(labelled)

	blocks = [
		{
			"type": "text",
			"text": "Shopify Product Listing data (JSON):\n" + frappe.as_json(data),
		}
	]
	if labelled:
		blocks.append({"type": "text", "text": f"\n{len(labelled)} product photo(s) follow:"})
		for idx, (label, image_block) in enumerate(labelled, start=1):
			blocks.append({"type": "text", "text": f"Photo {idx}: {label}"})
			blocks.append(image_block)
	else:
		blocks.append({
			"type": "text",
			"text": "No usable product photos are on the listing. Enrich from the text "
			"only and flag every visually-determined attribute in needs_review.",
		})

	return {"_content_blocks": blocks}


def view_image(image_url):
	"""
	Fetch an external image URL and hand it back as a vision block, so the
	model can actually look at a product it only knows as a bare URL (no
	item_code / listing to read a photo from otherwise).
	"""
	return {
		"_content_blocks": [
			{"type": "text", "text": f"Reference image ({image_url}):"},
			_fetch_image_block(image_url),
		]
	}


def _reference_source(url):
	"""
	Build an Anthropic-style image `source` (base64) for a reference photo,
	resolving both a stored Frappe File url (e.g. '/files/x.jpg', which is not
	HTTP-fetchable on its own) by reading the File directly, and an external
	http(s) url by downloading it. Used to ground image generation in the real
	product photo.
	"""
	file_name = frappe.db.get_value("File", {"file_url": url}, "name")
	if file_name:
		block = _image_block_from_file(file_name)
		if block:
			return block["source"]
	return _fetch_image_block(url)["source"]


def _generate_one_image(api_key, prompt, reference_data_uri):
	"""One call to OpenRouter's Unified Image API. Returns (b64_json, media_type, usage)."""
	import requests

	payload = {"model": _IMAGE_GEN_MODEL, "prompt": prompt, "n": 1, "output_format": "png"}
	if reference_data_uri:
		payload["input_references"] = [{"type": "image_url", "image_url": {"url": reference_data_uri}}]

	resp = requests.post(
		"https://openrouter.ai/api/v1/images",
		headers={"Authorization": f"Bearer {api_key}"},
		json=payload,
		timeout=180,
	)
	if resp.status_code != 200:
		frappe.throw(f"Image generation failed ({resp.status_code}): {resp.text[:500]}")
	data = resp.json()
	image = data["data"][0]
	return image["b64_json"], image.get("media_type", "image/png"), data.get("usage", {})


def generate_product_images(briefs, item_code=None, reference_image_url=None, generate_images=False):
	"""
	Generate up to 5 editorial shots in one call via OpenRouter's Unified
	Image API (POST /api/v1/images — a dedicated OpenRouter endpoint, NOT the
	OpenAI Images REST API; confirmed live, the OpenAI SDK's images.generate/
	images.edit both 404 against OpenRouter). Each of `briefs` is
	{"kind": ..., "brief": ...}; each is saved as its own public File.
	Returns {"images": [{kind, brief, url}, ...]} — copy that list verbatim
	into the final `images` array.

	Whether we generate at all is decided HERE, deterministically — not left to
	the model's judgement — by two gates:

	  1. There must be an ORIGINAL product photo to edit. For an item_code run
	     that is the listing's own primary photo (read from the Shopify Product
	     Listing's images table); for a URL-only product it is
	     reference_image_url. If neither exists we return an empty list and
	     generate nothing — this agent never invents a product from scratch.
	     This gate is airtight: it holds regardless of what the model passes.
	  2. Generation is opt-in per request: generate_images must be true. When an
	     original photo exists but the toggle is off, we return empty too.

	Every shot is generated by EDITING that original photo (passed as a base64
	input_reference), so the set stays grounded in the real piece. The photo is
	resolved once — a stored Frappe File is read directly, an external url is
	downloaded — because a '/files/...' path isn't publicly fetchable and some
	CDNs refuse fetches with no browser-like User-Agent (see _reference_source /
	_fetch_image_block).

	If openrouter_api_key isn't configured (and both gates pass), the model is
	told via the thrown message not to retry and to fall back to url=null
	placeholders instead of stalling the rest of the listing.
	"""
	# ── Gate 1 (airtight): resolve the original photo; no photo → no generation.
	reference = None
	if item_code and frappe.db.exists(_LISTING_DOCTYPE, item_code):
		reference = _primary_listing_image_url(frappe.get_doc(_LISTING_DOCTYPE, item_code))
	if not reference and reference_image_url:
		reference = reference_image_url
	if not reference:
		return {"images": [], "note": (
			"No original product photo exists for this product, so no images "
			"were generated — this agent never creates product imagery from "
			"scratch."
		)}

	# ── Gate 2: editorial images are opt-in per request.
	if not generate_images:
		return {"images": [], "note": (
			"An original product photo exists, but the generate_images toggle is "
			"off, so no images were generated."
		)}

	api_key = frappe.conf.get("openrouter_api_key")
	if not api_key:
		frappe.throw(
			"Image generation is not configured (set openrouter_api_key in "
			"site_config.json). Do NOT retry; return each image with url=null "
			"and its brief so the team can shoot or generate it manually."
		)

	from frappe.utils.file_manager import save_file

	ref_source = _reference_source(reference)
	reference_data_uri = f"data:{ref_source['media_type']};base64,{ref_source['data']}"

	images = []
	total_tokens = 0
	for entry in briefs[:5]:
		b64_data, media_type, usage = _generate_one_image(api_key, entry["brief"], reference_data_uri)
		total_tokens += usage.get("total_tokens", 0)

		ext = _MEDIA_TYPES_BY_MIME.get(media_type, ".png")
		file_name = f"listing-{entry['kind']}-{frappe.generate_hash(length=8)}{ext}"
		file_doc = save_file(file_name, base64.b64decode(b64_data), None, None, is_private=0)

		images.append({"kind": entry["kind"], "brief": entry["brief"], "url": file_doc.file_url})

	# Surface cost via the executor's "_usage" convention so it lands in
	# OS Agent Run.image_tokens instead of vanishing from token accounting.
	return {"images": images, "_usage": {"image_tokens": total_tokens}}


def save_listing(listing, item_code=None):
	"""
	Persist an enriched listing into the Enriched Listing DocType for admin
	review. Upserts by item_code (one Enriched Listing per product): re-running
	the agent on the same product updates the existing row instead of creating a
	duplicate.

	`listing` is the full enrichment object — the same shape the agent returns
	(schemas/output.json). `item_code` identifies the source product and is the
	upsert key; it falls back to listing["item_code"] if not passed separately.
	The row lands in "Needs Review" status so an admin edits/approves it before
	anything is published.

	List-valued fields (bullet_points, seo_keywords, shopify_tags, needs_review,
	notes) are flattened to one-per-line text for a readable Desk form, and the
	structured attributes plus the verbatim listing are stored as JSON — so
	nothing is lost even if the flattened fields drift from the schema.

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
	if not frappe.db.exists(_LISTING_DOCTYPE, item_code):
		frappe.throw(
			f"No {_LISTING_DOCTYPE} found for item_code '{item_code}'; cannot save the listing."
		)

	if frappe.db.exists("Enriched Listing", item_code):
		doc = frappe.get_doc("Enriched Listing", item_code)
	else:
		doc = frappe.new_doc("Enriched Listing")
		doc.item_code = item_code

	doc.status = "Needs Review"
	doc.category = listing.get("category")
	doc.brand = listing.get("brand")
	doc.title = listing.get("title")
	doc.description = listing.get("description")
	doc.seo_title = listing.get("seo_title")
	doc.seo_description = listing.get("seo_description")
	doc.shopify_category = listing.get("shopify_category")
	doc.confidence = listing.get("confidence")

	# list-valued fields -> one item per line for a readable Desk form
	doc.bullet_points = "\n".join(listing.get("bullet_points") or [])
	doc.seo_keywords = "\n".join(listing.get("seo_keywords") or [])
	doc.shopify_tags = "\n".join(listing.get("shopify_tags") or [])
	doc.needs_review = "\n".join(listing.get("needs_review") or [])
	doc.notes = "\n".join(listing.get("notes") or [])

	# structured attributes -> pretty JSON; whole payload kept verbatim for audit
	doc.attributes_json = frappe.as_json(listing.get("attributes") or {})
	doc.output_json = frappe.as_json(listing)

	# rebuild the image child table from the generated shots
	doc.set("images", [])
	for img in (listing.get("images") or []):
		doc.append("images", {
			"kind": img.get("kind"),
			"brief": img.get("brief"),
			"url": img.get("url"),
		})

	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"name": doc.name,
		"status": doc.status,
		"url": f"/app/enriched-listing/{doc.name}",
	}


def _distinct_csv_values(doctype, column, limit=2000):
	"""Distinct, split, de-duplicated values of a comma-separated column."""
	if not frappe.db.has_column(doctype, column):
		return []
	rows = frappe.get_all(
		doctype,
		filters={column: ["is", "set"]},
		pluck=column,
		limit=limit,
	)
	values = set()
	for raw in rows:
		for part in (raw or "").split(","):
			part = part.strip()
			if part:
				values.add(part)
	return sorted(values)


def _distinct_values(doctype, column, limit=2000):
	"""Distinct non-empty scalar values of a column."""
	if not frappe.db.has_column(doctype, column):
		return []
	rows = frappe.get_all(
		doctype,
		filters={column: ["is", "set"]},
		pluck=column,
		limit=limit,
	)
	return sorted({(r or "").strip() for r in rows if (r or "").strip()})


def get_reference_values():
	"""
	Existing catalog vocabulary, so the agent reuses established values instead
	of inventing near-duplicates. Every lookup is guarded: fields added by
	sibling apps (thesolist, shopify connector) may be absent.
	"""
	return {
		"brands": frappe.get_all("Brand", pluck="name", limit=500),
		"categories": frappe.get_all(
			"Item Group", filters={"is_group": 0}, pluck="name", limit=500
		),
		"shopify_tags": _distinct_csv_values("Item", "sh_shopify_tags"),
		"shopify_locations": _distinct_values("Item", "shopify_location"),
	}
