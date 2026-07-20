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
unreadable image, guard optional custom fields) so a single bad attachment does
not sink the whole enrichment.

This agent is read-only: nothing here writes to the Item or publishes to
Shopify. That is the admin approval / connector step.
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

# Cap how many photos we send to keep token/latency cost bounded.
_MAX_IMAGES = 5

# ERPNext default price lists. Adjust if The Solist renames them.
_SELLING_PRICE_LIST = "Standard Selling"
_BUYING_PRICE_LIST = "Standard Buying"

# Item custom fields that may or may not exist depending on which sibling apps
# (thesolist, shopify connector) are installed. Always read via .get().
_OPTIONAL_ITEM_FIELDS = (
	"internal_id",          # supplier's raw product id (thesolist)
	"owned_by_supplier",    # Link -> Supplier (thesolist)
	"original_price",       # retail/MSRP (thesolist)
	"available_quantity",   # supplier feed qty (thesolist)
	"shopify_location",     # (thesolist)
)


def _media_type(path_or_name):
	ext = os.path.splitext(path_or_name or "")[1].lower()
	return _MEDIA_TYPES.get(ext)


def _price(item_code, price_list):
	"""First Item Price rate for this item on the given price list, or None."""
	return frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "price_list": price_list},
		"price_list_rate",
	)


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


def _collect_image_blocks(item):
	"""
	Gather up to _MAX_IMAGES photo blocks for an Item: its File attachments
	first, then item.image if it was not already covered. Falls back to a URL
	image source for an external item.image that is not a stored File.
	"""
	blocks = []
	seen_urls = set()

	attachments = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Item", "attached_to_name": item.name},
		fields=["name", "file_url", "file_name"],
		order_by="creation asc",
	)
	for att in attachments:
		if len(blocks) >= _MAX_IMAGES:
			break
		if not _media_type(att.file_name or att.file_url):
			continue
		block = _image_block_from_file(att.name)
		if block:
			blocks.append((att.file_name or att.file_url, block))
			if att.file_url:
				seen_urls.add(att.file_url)

	# Ensure the primary image is present even if it is not an attachment row.
	main = item.get("image")
	if main and main not in seen_urls and len(blocks) < _MAX_IMAGES:
		file_name = frappe.db.get_value("File", {"file_url": main}, "name")
		if file_name:
			block = _image_block_from_file(file_name)
			if block:
				blocks.append((main, block))
		elif main.startswith("http") and _media_type(main):
			# External URL — let Anthropic fetch it directly.
			blocks.append((main, {"type": "image", "source": {"type": "url", "url": main}}))

	return blocks


def get_product(item_code):
	"""
	Return an Item's raw supplier data plus its product photos as vision content
	blocks. The model receives a text block of the structured data followed by
	one labelled image block per photo.
	"""
	if not frappe.db.exists("Item", item_code):
		frappe.throw(
			f"No Item found with item_code '{item_code}'. "
			"Check the input or ask the admin to confirm the product."
		)

	item = frappe.get_doc("Item", item_code)

	data = {
		"item_code": item.name,
		"title": item.item_name,
		"description": item.description,
		"brand": item.get("brand"),
		"item_group": item.get("item_group"),
		"stock_uom": item.get("stock_uom"),
		"selling_price": _price(item_code, _SELLING_PRICE_LIST) or item.get("standard_rate"),
		"cost_price": _price(item_code, _BUYING_PRICE_LIST),
		"barcodes": [row.barcode for row in (item.get("barcodes") or []) if row.barcode],
	}
	for field in _OPTIONAL_ITEM_FIELDS:
		val = item.get(field)
		if val not in (None, ""):
			data[field] = val

	labelled = _collect_image_blocks(item)
	data["image_count"] = len(labelled)

	blocks = [
		{
			"type": "text",
			"text": "Raw product data (JSON):\n" + frappe.as_json(data),
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
			"text": "No usable product photos are attached. Enrich from the text only "
			"and flag visual attributes in needs_review.",
		})

	return {"_content_blocks": blocks}


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
