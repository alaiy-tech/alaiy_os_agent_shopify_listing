# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.model.document import Document

from alaiy_os_agent_shopify_listing.tools.handlers import ATTRIBUTE_NAMESPACE


class ShopifyEnrichedListing(Document):
	def on_update(self):
		# on_update fires after the row is written, so the database already says
		# "Approved" — the previous status must come from the pre-save snapshot.
		# A brand-new doc has no snapshot; an agent never inserts as Approved.
		before = self.get_doc_before_save()
		if self.status == "Approved":
			if before and before.status != "Approved":
				# The transition itself: content AND imagery, the whole approval.
				self._push_to_listing()
			elif before and self._content_changed(before):
				# Already approved, and someone edited it anyway - the reviewer
				# screen lets an admin correct an approved listing's title,
				# description or attributes, and until this branch existed those
				# corrections stopped at this record and never reached the
				# product. Content only: imagery is REPLACED wholesale by
				# _sync_images, so re-pushing it here would undo any photo work
				# done on the listing since approval. Photos have their own
				# commit (api.publish_listing_images).
				#
				# Only when the content actually moved. An approved record is
				# saved for reasons that have nothing to say to the listing --
				# image_stage writes rendered photos onto it row by row -- and
				# pushing there would save the listing, re-sync the Item's tags
				# and, on a synced listing, send Shopify a product it already
				# has, once per render.
				self._push_content_to_listing()
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

		self._apply_content(listing_doc)
		self.apply_images(listing_doc)

		listing_doc.save(ignore_permissions=True)

		self._sync_tags()

		frappe.db.commit()

	def _content_changed(self, before):
		"""Whether this save touched anything the listing publishes.

		The attribute rows are compared as a dict rather than row by row: the
		edit endpoint rebuilds the whole child table on every save, so every row
		is "new" by name even when the values are identical.
		"""
		if any(self.get(field) != before.get(field) for field in self.CONTENT_FIELDS):
			return True
		if (self.shopify_tags or "") != (before.shopify_tags or ""):
			return True
		return {r.key: r.value for r in (self.attributes or [])} != {
			r.key: r.value for r in (before.attributes or [])
		}

	def _push_content_to_listing(self):
		"""Push this record's text and attributes to the listing, leaving its
		imagery alone - an edit to an already-approved listing.

		Same write as _push_to_listing minus apply_images, so a hand correction
		reaches the product (and, if sync is enabled, Shopify) the moment it is
		saved rather than waiting for an approval that has already happened.
		"""
		listing_name = self.item_code
		if not frappe.db.exists("Shopify Product Listing", listing_name):
			frappe.throw(f"Shopify Product Listing '{listing_name}' not found.")

		listing_doc = frappe.get_doc("Shopify Product Listing", listing_name)

		self._apply_content(listing_doc)

		listing_doc.save(ignore_permissions=True)

		self._sync_tags()

		frappe.db.commit()

	# Enriched field -> the Shopify Product Listing field it publishes into.
	CONTENT_FIELDS = {
		"title": "listing_title",
		"description": "listing_description",
		"category": "listing_category",
		"product_type": "listing_product_type",
		"seo_title": "listing_seo_title",
		"seo_description": "listing_seo_description",
	}

	def _apply_content(self, listing_doc):
		"""The non-image half of an approval: the listing's own text fields and
		the attribute metafields. Does not save - the caller owns the write.

		A field this record has nothing to say about (the run never produced
		SEO, say) leaves the listing's own value alone rather than clearing it.
		Publishing an empty title over a real one is never what either an
		approval or a hand edit meant, and the same rule already governs the
		attribute metafields below.
		"""
		listing_doc.is_enriched = 1
		for field, listing_field in self.CONTENT_FIELDS.items():
			value = self.get(field)
			if (value or "").strip():
				listing_doc.set(listing_field, value)

		self._sync_attributes_as_metafields(listing_doc)

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

	def apply_images(self, listing_doc):
		"""Put this record's imagery onto the listing — both halves, together.

		Public, and separate from _push_to_listing, because imagery can be
		committed on its own: retouching photos does not make a listing's TEXT
		reviewed, and a product whose photos were cleaned up should not have to
		approve a content enrichment it never asked for to publish them (see
		api.publish_listing_images). Approval calls exactly this, so the two
		routes cannot drift into applying imagery differently.

		Does not save — the caller owns the write, because approval has more to
		put on the document first.
		"""
		self._sync_images(listing_doc)
		self._sync_variant_images(listing_doc)

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

		A run with no image rows at all - image generation wasn't requested, so
		the agent never touched images - is different from one whose rows failed:
		there is nothing to fall back to per-row, so leave the listing's existing
		images alone rather than replacing them with nothing.
		"""
		if not self.images:
			return

		listing_doc.set("images", [])

		for idx, enriched_img in enumerate(self.images):
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

		Merged into the listing's metafields, never rebuilt from them. That table is
		the store's mirror of every metafield on the product — this store carries
		some eighty, most of them written by other apps — and an enrichment speaks
		for the dozen-odd attributes it was asked about. Replacing the table made
		approving one listing drop every metafield the run happened not to mention.

		A key already published keeps its existing `type`: the value is ours to
		update, but how Shopify stores it was settled when the metafield was
		defined, and a list metafield rewritten as text is a broken definition.
		"""
		published = {
			row.key: row
			for row in (listing_doc.get("metafields") or [])
			if row.namespace == ATTRIBUTE_NAMESPACE and row.key
		}

		for key, value in self._attributes():
			if not key or not value:
				continue

			row = published.get(key)
			if row:
				row.value = str(value)
				continue

			listing_doc.append("metafields", {
				"namespace": ATTRIBUTE_NAMESPACE,
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
