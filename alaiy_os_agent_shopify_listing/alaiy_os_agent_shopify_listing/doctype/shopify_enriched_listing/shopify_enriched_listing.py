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
		listing_doc.listing_seo_title = self.seo_title
		listing_doc.listing_seo_description = self.seo_description

		self._sync_images(listing_doc)
		self._sync_variant_images(listing_doc)
		self._sync_attributes_as_metafields(listing_doc)

		listing_doc.save(ignore_permissions=True)

		self._sync_tags()

		frappe.db.commit()

	def _sync_tags(self):
		"""Push the enriched tag list onto the Item's Shopify tag list.

		Tags are Item-level (`Item.sh_shopify_tags`, a Table MultiSelect of Item
		Shopify Tag rows -> Shopify Tag), not a Shopify Product Listing field, so this
		writes to a different doctype than `_push_to_listing`'s other syncs. Guarded
		because Shopify Tag/Item Shopify Tag belong to the Shopify connector app and
		may not be installed. Self-heals any Shopify Tag master that doesn't exist
		locally yet, mirroring the connector's own import-side behaviour — a tag an
		admin just approved should not silently fail to publish because nothing has
		cached it before. A tag containing '<' or '>' is skipped (Frappe's own name
		validation rejects those characters on a Shopify Tag insert).
		"""
		if not self.shopify_tags:
			return
		if not frappe.db.exists("DocType", "Shopify Tag") or not frappe.db.exists("Item", self.item_code):
			return

		tag_names = [t.strip() for t in self.shopify_tags.splitlines() if t.strip()]
		usable = []
		for tag_name in tag_names:
			if "<" in tag_name or ">" in tag_name:
				continue
			if not frappe.db.exists("Shopify Tag", tag_name):
				frappe.get_doc({"doctype": "Shopify Tag", "tag_name": tag_name}).insert(
					ignore_permissions=True
				)
			usable.append(tag_name)

		item = frappe.get_doc("Item", self.item_code)
		item.set("sh_shopify_tags", [{"shopify_tag": t} for t in usable])
		item.save(ignore_permissions=True)

	def _sync_images(self, listing_doc):
		"""Map enriched listing images to Shopify listing images.

		Variant images (rows with item_variant) are not listing images — they are
		delivered by _sync_variant_images instead.

		A row with no result falls back to the photo it was made from, published as
		the original it is. This table REPLACES the listing's images rather than
		adding to them, so skipping such a row does not leave the old photo alone —
		it deletes it. Without the fallback, approving a listing whose imagery failed,
		or is still rendering, would strip the product of the very photos the
		enrichment was supposed to improve.
		"""
		listing_doc.set("images", [])

		for idx, enriched_img in enumerate(self.images or []):
			if enriched_img.item_variant:
				continue
			if not enriched_img.url and not enriched_img.source_url:
				continue

			source_map = {
				"hero": "Original",
				"generated": "AI Enhanced",
				"translated": "AI Enhanced",
			}
			source = source_map.get((enriched_img.kind or "").lower(), "AI Enhanced")
			if not enriched_img.url:
				# Falling back to the source photo: whatever the row was going to
				# become, what is being published is the original.
				source = "Original"

			row = listing_doc.append("images", {
				"image": enriched_img.url or enriched_img.source_url,
				"source": source,
				"sort_order": idx,
				"generated_by_agent": self.name if source == "AI Enhanced" else None,
			})

	def _sync_variant_images(self, listing_doc):
		"""Write each variant image onto its variant row's `variant_image`.

		The variant rows are updated IN PLACE, never rebuilt: they carry
		`sh_shopify_variant_id` and `variant_price`, which enrichment must not
		disturb. A variant that has since been removed from the listing is skipped
		with a message rather than failing the approval.
		"""
		variant_rows = {row.item_variant: row for row in (listing_doc.variants or [])}

		for enriched_img in self.images or []:
			if not enriched_img.url or not enriched_img.item_variant:
				continue

			row = variant_rows.get(enriched_img.item_variant)
			if row is None:
				frappe.msgprint(
					f"Variant {enriched_img.item_variant} is no longer on the "
					"listing; its enriched image was not applied."
				)
				continue
			row.variant_image = enriched_img.url

	def _sync_attributes_as_metafields(self, listing_doc):
		"""Convert the enriched attributes into Shopify metafield rows.

		The `attributes` child table is the source, not `attributes_json`: the table
		is what the Desk form shows and what a reviewer corrects before approving, so
		it is what has to reach Shopify. The JSON stays the agent's untouched original.

		A row enriched before the table existed has only the JSON, so an empty table
		falls back to it rather than silently publishing no metafields.
		"""
		listing_doc.set("metafields", [])

		for key, value in self._attributes():
			if not value:
				continue

			listing_doc.append("metafields", {
				"namespace": "custom",
				"key": key,
				"type": "single_line_text_field",
				"value": str(value),
			})

	def _attributes(self):
		"""(key, value) pairs to publish — the table, or the JSON for an older row."""
		if self.attributes:
			return [(row.key, row.value) for row in self.attributes if row.key]

		try:
			return list((json.loads(self.attributes_json or "{}") or {}).items())
		except (json.JSONDecodeError, ValueError):
			frappe.msgprint("Warning: attributes_json is not valid JSON, skipping metafields sync.")
			return []
