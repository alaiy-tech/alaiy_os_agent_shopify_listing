# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Fill the Attributes and Variants child tables from the JSON they were read from.

Both were JSON fields on Shopify Enriched Listing before; they are now child tables,
so a reviewer reads and edits rows instead of a blob — and, for attributes, so what
the reviewer edits is what approval publishes. Listings enriched before that change
have the JSON and nothing else, and would show two empty tables until the agent
re-ran over them.

The JSON fields are left exactly as they are: they are the agent's own words, kept
for audit, and the tables are a second copy rather than a replacement.

Rows are inserted directly rather than through the parent document. Saving the parent
would fire ShopifyEnrichedListing.on_update on every historical listing, and a
migration has no business touching what is or is not published.
"""

import json

import frappe

from alaiy_os_agent_shopify_listing.tools.handlers import ENRICHED_DOCTYPE, _flatten


def execute():
	if not frappe.db.table_exists(ENRICHED_DOCTYPE):
		return

	for listing in frappe.get_all(
		ENRICHED_DOCTYPE, fields=["name", "attributes_json", "variants_json"]
	):
		_backfill_attributes(listing)
		_backfill_variants(listing)

	frappe.db.commit()


def _backfill_attributes(listing):
	if _has_rows(listing.name, "attributes"):
		return

	idx = 0
	for key, value in (_loads(listing.attributes_json, {}) or {}).items():
		if not key:
			continue
		idx += 1
		_insert(listing.name, "Shopify Enriched Listing Attribute", "attributes", idx, {
			"key": key,
			"value": _flatten(value),
		})


def _backfill_variants(listing):
	if _has_rows(listing.name, "variants"):
		return

	idx = 0
	for variant in _loads(listing.variants_json, []) or []:
		item_variant = (variant or {}).get("item_variant")
		# The row links to the variant Item. A variant since deleted from the catalog
		# would fail the insert and take the whole migration with it; the JSON still
		# has it, which is what an audit copy is for.
		if not item_variant or not frappe.db.exists("Item", item_variant):
			continue
		idx += 1
		_insert(listing.name, "Shopify Enriched Listing Variant", "variants", idx, {
			"item_variant": item_variant,
			"observed": "\n".join(
				f"{name}: {_flatten(value)}"
				for name, value in (variant.get("observed") or {}).items()
			),
			"suggestions": "\n".join(variant.get("suggestions") or []),
			"notes": variant.get("notes"),
		})


def _has_rows(parent, parentfield):
	"""A listing already migrated — or already re-enriched — is left alone."""
	return bool(
		frappe.db.exists(
			"Shopify Enriched Listing Attribute" if parentfield == "attributes"
			else "Shopify Enriched Listing Variant",
			{"parent": parent, "parenttype": ENRICHED_DOCTYPE, "parentfield": parentfield},
		)
	)


def _insert(parent, doctype, parentfield, idx, values):
	frappe.get_doc({
		"doctype": doctype,
		"parent": parent,
		"parenttype": ENRICHED_DOCTYPE,
		"parentfield": parentfield,
		"idx": idx,
		**values,
	}).insert(ignore_permissions=True)


def _loads(raw, default):
	try:
		return json.loads(raw or "null") or default
	except (json.JSONDecodeError, ValueError, TypeError):
		return default
