"""One-time reconcile of Shopify Product Listing.is_enriched.

The flag is maintained only by a status transition on Shopify Enriched Listing
(see its on_update). Nothing sets it retroactively, so listings approved before
the custom field existed — and every row the field was created on, since it lands
with default 0 — read as unenriched until this runs.

Safe to run more than once: it reconciles in BOTH directions against the current
Approved set, so it is a no-op once the site is consistent.

Usage:
    # preview (default) — reports what would change, writes nothing
    bench --site <site> execute alaiy_os_agent_shopify_listing.scripts.backfill_is_enriched.run

    # apply
    bench --site <site> execute alaiy_os_agent_shopify_listing.scripts.backfill_is_enriched.run \
        --kwargs "{'dry_run': False}"

Note: this is a reconcile, not an append. A listing whose flag is 1 while its
enrichment is no longer Approved (or no longer exists) is cleared — that is the
same rule the controller applies, just applied to history.
"""

import frappe

LISTING_DOCTYPE = "Shopify Product Listing"
ENRICHED_DOCTYPE = "Shopify Enriched Listing"

# Rows per UPDATE. Keeps the IN clause and the transaction a sane size on a site
# with tens of thousands of listings.
CHUNK = 500


def run(dry_run=True):
	if not frappe.db.exists("DocType", LISTING_DOCTYPE):
		print(f"{LISTING_DOCTYPE} does not exist on this site — is the Shopify connector installed?")
		return

	# The column is created by this app's sync_custom_fields (after_migrate). If it
	# is missing, migrate has not run with that hook yet and every write below would
	# fail on an unknown column.
	if not frappe.db.has_column(LISTING_DOCTYPE, "is_enriched"):
		print(
			f"{LISTING_DOCTYPE} has no is_enriched column yet. Run `bench --site "
			"<site> migrate` first so the custom field is created, then re-run this."
		)
		return

	# item_code is the Shopify Product Listing name — see ShopifyEnrichedListing._push_to_listing.
	approved = set(
		frappe.get_all(
			ENRICHED_DOCTYPE, filters={"status": "Approved"}, pluck="item_code"
		)
	)
	approved.discard(None)

	# Only listings that actually exist can carry the flag.
	existing = set(frappe.get_all(LISTING_DOCTYPE, pluck="name"))
	should_be_set = approved & existing
	missing_listings = approved - existing

	currently_set = set(
		frappe.get_all(LISTING_DOCTYPE, filters={"is_enriched": 1}, pluck="name")
	)

	to_set = sorted(should_be_set - currently_set)
	to_clear = sorted(currently_set - should_be_set)

	print(f"{ENRICHED_DOCTYPE} rows at Approved : {len(approved)}")
	print(f"{LISTING_DOCTYPE} already flagged    : {len(currently_set)}")
	print(f"  to set   (approved, flag 0) : {len(to_set)}")
	print(f"  to clear (flag 1, not approved) : {len(to_clear)}")

	if missing_listings:
		print(
			f"\nWARNING: {len(missing_listings)} approved enrichment(s) point at a "
			f"{LISTING_DOCTYPE} that no longer exists — skipped: "
			f"{sorted(missing_listings)[:10]}{' ...' if len(missing_listings) > 10 else ''}"
		)

	if not to_set and not to_clear:
		print("\nNothing to do — the flag already matches the approved set.")
		return {"set": 0, "cleared": 0, "dry_run": bool(dry_run)}

	if dry_run:
		_preview("would set", to_set)
		_preview("would clear", to_clear)
		print("\nDry run — nothing written. Re-run with --kwargs \"{'dry_run': False}\" to apply.")
		return {"set": len(to_set), "cleared": len(to_clear), "dry_run": True}

	_update(to_set, 1)
	_update(to_clear, 0)
	frappe.db.commit()

	print(f"\nDone: {len(to_set)} set, {len(to_clear)} cleared.")
	return {"set": len(to_set), "cleared": len(to_clear), "dry_run": False}


def _preview(label, names, limit=10):
	if not names:
		return
	shown = ", ".join(names[:limit])
	print(f"  {label}: {shown}{' ...' if len(names) > limit else ''}")


def _update(names, value):
	"""Set is_enriched in chunks.

	Deliberately a direct column write rather than doc.save(): the enrichment was
	already pushed onto the listing when it was approved, so re-saving each doc
	would re-run the connector's validation and touch `modified` on rows whose
	content is not changing. This only repairs the flag.
	"""
	table = f"`tab{LISTING_DOCTYPE}`"
	for i in range(0, len(names), CHUNK):
		chunk = names[i : i + CHUNK]
		placeholders = ", ".join(["%s"] * len(chunk))
		frappe.db.sql(
			f"UPDATE {table} SET is_enriched = %s WHERE name IN ({placeholders})",
			[value, *chunk],
		)
