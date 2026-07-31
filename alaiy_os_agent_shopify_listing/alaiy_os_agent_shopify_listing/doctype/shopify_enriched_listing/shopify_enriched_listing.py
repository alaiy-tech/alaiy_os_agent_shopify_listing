# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.model.document import Document


class ShopifyEnrichedListing(Document):
	def on_update(self):
		# on_update fires after the row is written, so the database already says
		# "Approved" — the previous status must come from the pre-save snapshot.
		# A brand-new doc has no snapshot; an agent never inserts as Approved, so
		# only a real transition (anything -> Approved) pushes.
		before = self.get_doc_before_save()
		if self.status == "Approved":
			if before and before.status != "Approved":
				self._push_to_listing()
		elif before is None or before.status == "Approved":
			# The listing no longer carries approved content: either the agent
			# re-ran (save_listing resets status to "Needs Review", and a fresh
			# insert after a delete has no snapshot) or an admin un-approved it.
			self._clear_enriched_flag()

	def on_trash(self):
		# Deleting the enrichment record means nothing vouches for the listing's
		# content any more.
		self._clear_enriched_flag()

	def _clear_enriched_flag(self):
		if frappe.db.exists("Shopify Product Listing", self.item_code):
			frappe.db.set_value(
				"Shopify Product Listing", self.item_code, "is_enriched", 0, update_modified=False
			)

	def _push_to_listing(self):
		"""Push approved enrichment back to the Shopify Product Listing."""
		listing_name = self.item_code
		if not frappe.db.exists("Shopify Product Listing", listing_name):
			frappe.throw(f"Shopify Product Listing '{listing_name}' not found.")

		listing_doc = frappe.get_doc("Shopify Product Listing", listing_name)

		listing_doc.is_enriched = 1
		listing_doc.listing_title = self.title
		listing_doc.listing_description = self.description
		listing_doc.listing_category = self.category
		listing_doc.listing_product_type = self.product_type

		self._sync_images(listing_doc)
		self._sync_attributes_as_metafields(listing_doc)

		listing_doc.save(ignore_permissions=True)
		frappe.db.commit()

	def _sync_images(self, listing_doc):
		"""Map enriched listing images to Shopify listing images."""
		listing_doc.set("images", [])

		for idx, enriched_img in enumerate(self.images or []):
			if not enriched_img.url:
				continue

			source_map = {
				"hero": "Original",
				"generated": "AI Enhanced",
				"translated": "AI Enhanced",
			}
			source = source_map.get((enriched_img.kind or "").lower(), "AI Enhanced")

			row = listing_doc.append("images", {
				"image": enriched_img.url,
				"source": source,
				"sort_order": idx,
				"generated_by_agent": self.name if source == "AI Enhanced" else None,
			})

	def _sync_attributes_as_metafields(self, listing_doc):
		"""Convert enriched attributes JSON to Shopify metafield rows."""
		listing_doc.set("metafields", [])

		try:
			attributes = json.loads(self.attributes_json or "{}")
		except (json.JSONDecodeError, ValueError):
			frappe.msgprint("Warning: attributes_json is not valid JSON, skipping metafields sync.")
			return

		for key, value in (attributes or {}).items():
			if not value:
				continue

			listing_doc.append("metafields", {
				"namespace": "custom",
				"key": key,
				"type": "single_line_text_field",
				"value": str(value),
			})
