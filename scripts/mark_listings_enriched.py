"""
One-off: mark the listings in a Shopify-ready export CSV as enriched.

`is_enriched` is this app's Custom Field on Shopify Product Listing (see
setup/install.py) -- the "Enriched" checkbox in Desk. It is normally set only
when a Shopify Enriched Listing is approved, so a batch of listings whose
content was enriched and shipped outside that flow (e.g. reviewed straight in
the exported CSV) shows up as un-enriched. This backfills the flag from such a
CSV.

Row -> listing resolution. The CSV is a Shopify product export: one row per
variant, `Variant SKU` shaped `SH-<product_id>-<variant_id>`. Everything from the
second '-' on is the Shopify variant id and is discarded, leaving the product key
`SH-<product_id>`.

A Shopify Product Listing is named after its Item, and one product can own several
of those: the plain key plus underscore-suffixed siblings (SH-1001, SH-1001_1,
SH-1001_2 ...). Every listing whose name is the key or starts with `<key>_` is
marked, so a product is never half-flagged.

A key that matches no listing at all is reported, never guessed at.

Every product in the CSV is marked. The `Needs Review` column -- the agent's own
list of attributes it could not fill confidently -- is reported but not acted on;
pass --skip-needs-review to leave those products alone instead.

Runnable from any directory (it finds the bench itself). Dry-run first, then apply:
    ~/alaiy_os_bench/env/bin/python scripts/mark_listings_enriched.py <site_name> --dry-run
    ~/alaiy_os_bench/env/bin/python scripts/mark_listings_enriched.py <site_name> --apply

--csv defaults to the export committed beside this script under scripts/data/.
"""
import argparse
import csv
import os
import sys

import frappe

DEFAULT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "product_shopify_ready.csv")


def product_key(sku):
	"""`SH-753680950207890-5581296860621` -> `SH-753680950207890`.

	Only the first two dash-separated segments identify the product; anything
	after the second '-' is the Shopify variant id. A SKU with no variant
	suffix is already a product key.
	"""
	return "-".join(sku.split("-")[:2])


def parse_csv(path, skip_needs_review=False):
	"""Collect one product key per handle in the export.

	The product-level columns (Title, Needs Review, ...) are only filled on a
	handle's FIRST row; the rest carry variants. So Needs Review is read from
	whichever row of the handle has it, and any row of the handle can supply the
	product key -- every variant of a product carries the same one.
	"""
	handles = {}
	# utf-8-sig: Shopify's export carries a BOM, which would otherwise become
	# part of the first column's name.
	with open(path, encoding="utf-8-sig", newline="") as fh:
		for row in csv.DictReader(fh):
			handle = (row.get("Handle") or "").strip()
			if not handle:
				continue
			entry = handles.setdefault(handle, {"key": None, "needs_review": ""})

			needs_review = (row.get("Needs Review") or "").strip()
			if needs_review:
				entry["needs_review"] = needs_review

			if entry["key"]:
				continue
			sku = (row.get("Variant SKU") or "").strip()
			if sku:
				entry["key"] = product_key(sku)

	selected, needs_review, no_sku = [], [], []
	for handle, entry in handles.items():
		if not entry["key"]:
			no_sku.append(handle)
			continue
		if entry["needs_review"]:
			needs_review.append(handle)
			if skip_needs_review:
				continue
		selected.append((handle, entry["key"]))

	return selected, needs_review, no_sku


def resolve_listings(key):
	"""Every Shopify Product Listing belonging to a product key.

	That is the listing named exactly `key`, plus its underscore-suffixed
	siblings (`key_1`, `key_2`, ...). `_` and `%` are LIKE wildcards, so the key
	is escaped before being used as a prefix -- an unescaped `_` in a key would
	otherwise match a single character of an unrelated name.
	"""
	prefix = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
	rows = frappe.db.sql(
		"""SELECT name, is_enriched FROM `tabShopify Product Listing`
		   WHERE name = %s OR name LIKE %s""",
		(key, prefix + "\\_%"),
		as_dict=True,
	)
	return rows


def bench_sites_dir():
	"""The bench's sites/ directory, derived from this file's location.

	frappe.init() resolves sites/ relative to the current directory, and its
	logger resolves the bench path as '..' from it, so frappe only works with the
	sites dir as cwd. Rather than make that the caller's problem, walk up from
	apps/<app>/scripts/ to the bench and chdir there -- the script then runs from
	anywhere with just a site name.
	"""
	bench = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
	return os.path.join(bench, "sites")


def main(site, csv_path, apply_changes=False, skip_needs_review=False):
	# Before chdir, or a relative --csv would resolve against the sites dir.
	csv_path = os.path.abspath(csv_path)

	sites = bench_sites_dir()
	if not os.path.isdir(sites):
		print(f"Could not find the bench sites directory at {sites} -- run this from the bench's sites dir.")
		sys.exit(1)
	os.chdir(sites)

	frappe.init(site=site)
	frappe.connect()

	if not frappe.db.has_column("Shopify Product Listing", "is_enriched"):
		print(
			"Shopify Product Listing has no is_enriched column -- run "
			"`bench --site {} migrate` so this app's custom fields exist.".format(site)
		)
		sys.exit(1)

	selected, needs_review, no_sku = parse_csv(csv_path, skip_needs_review)

	print(f"CSV: {csv_path}")
	print(f"Products in CSV: {len(selected) + len(no_sku) + (len(needs_review) if skip_needs_review else 0)}")
	print(f"  candidates:              {len(selected)}")
	print(f"  marked Needs Review:     {len(needs_review)}"
	      f"{' (skipped)' if skip_needs_review else ' (included)'}")
	print(f"  skipped, no Variant SKU: {len(no_sku)}")

	updated, already, unresolved, unapproved = [], [], [], []

	for handle, key in selected:
		listings = resolve_listings(key)
		if not listings:
			unresolved.append((handle, key))
			continue

		for row in listings:
			# An enrichment record that is not Approved will clear is_enriched the
			# next time it is saved (see ShopifyEnrichedListing.on_update), so the
			# flag set here would silently come undone -- worth naming, not fixing:
			# flipping that record to Approved would republish its content.
			status = frappe.db.get_value(
				"Shopify Enriched Listing", {"item_code": row.name}, "status"
			)
			if status and status != "Approved":
				unapproved.append((row.name, status))

			if row.is_enriched:
				already.append(row.name)
				continue

			if apply_changes:
				frappe.db.set_value(
					"Shopify Product Listing", row.name, "is_enriched", 1, update_modified=False
				)
			updated.append(row.name)

			if apply_changes and len(updated) % 200 == 0:
				frappe.db.commit()
				print(f"  ... {len(updated)} marked", flush=True)

	if apply_changes:
		frappe.db.commit()

	verb = "Marked" if apply_changes else "Would mark"
	print(f"\nListings matched: {len(updated) + len(already)} "
	      f"(from {len(selected) - len(unresolved)} products)")
	print(f"{verb} enriched: {len(updated)}")
	print(f"Already enriched: {len(already)}")

	if unapproved:
		print(
			f"\nWARNING: {len(unapproved)} of these have a Shopify Enriched Listing that is not "
			"Approved; saving that record will clear the flag again:"
		)
		for listing, status in unapproved[:20]:
			print(f"  {listing}: {status}")
		if len(unapproved) > 20:
			print(f"  ... and {len(unapproved) - 20} more")

	if unresolved:
		print(f"\nUnresolved ({len(unresolved)}) -- no listing found for these products:")
		for handle, parent_key in unresolved[:20]:
			print(f"  {parent_key}  ({handle})")
		if len(unresolved) > 20:
			print(f"  ... and {len(unresolved) - 20} more")

	if needs_review:
		if skip_needs_review:
			print(f"\nSkipped as needing review ({len(needs_review)}).")
		else:
			print(
				f"\nNote: {len(needs_review)} of these carry a non-empty Needs Review column and were "
				"included anyway; pass --skip-needs-review to leave them out."
			)

	if no_sku:
		print(f"\nSkipped with no Variant SKU ({len(no_sku)}): {', '.join(no_sku[:10])}")

	if not apply_changes:
		print("\nDry run -- nothing written. Re-run with --apply.")

	frappe.destroy()


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Mark CSV listings as enriched.")
	parser.add_argument("site", help="Frappe site name")
	parser.add_argument("--csv", default=DEFAULT_CSV, help=f"Export CSV (default: {DEFAULT_CSV})")
	parser.add_argument("--skip-needs-review", action="store_true",
	                    help="Leave out products whose Needs Review column is non-empty")
	group = parser.add_mutually_exclusive_group(required=True)
	group.add_argument("--dry-run", action="store_true", help="Report only")
	group.add_argument("--apply", action="store_true", help="Write the flag")
	args = parser.parse_args()

	main(args.site, args.csv, apply_changes=args.apply, skip_needs_review=args.skip_needs_review)
