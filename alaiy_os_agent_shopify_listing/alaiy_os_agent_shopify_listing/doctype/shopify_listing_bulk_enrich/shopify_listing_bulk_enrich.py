# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""One bulk enrichment request: a list of products and the state of each one's run.

The doc is the request *and* the progress record. It owns no execution logic — the
fan-out over Frappe workers lives in ``bulk.py``, which the actions below hand off to.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import cint, now_datetime

from alaiy_os_agent_shopify_listing.bulk import DEFAULT_BATCH_SIZE, enqueue_chunks

# Statuses a batch can be (re)started from — anything but in-flight. "Generating
# Images" is deliberately restartable: the runs are done, and a re-run is also the
# escape hatch for imagery stuck on a lost worker job.
RESTARTABLE = ("Draft", "Generating Images", "Completed", "Completed with Errors", "Failed", "Cancelled")
IN_FLIGHT = ("Queued", "Running")


class ShopifyListingBulkEnrich(Document):
	def validate(self):
		self._dedupe_items()
		self.total_items = len(self.items)
		self.batch_size = max(1, cint(self.batch_size) or DEFAULT_BATCH_SIZE)

	def _dedupe_items(self):
		"""The same product twice would mean two runs racing on one enriched listing."""
		seen, kept = set(), []
		for row in self.items:
			if row.item_code in seen:
				continue
			seen.add(row.item_code)
			row.idx = len(kept) + 1
			kept.append(row)
		if len(kept) != len(self.items):
			self.set("items", kept)

	@frappe.whitelist()
	def start(self):
		"""Queue every product in this batch. Returns {batch, items, jobs}."""
		self.check_permission("write")
		if self.status in IN_FLIGHT:
			frappe.throw(f"{self.name} is already {self.status.lower()}.")
		if not self.items:
			frappe.throw("Add at least one product before starting.")

		for row in self.items:
			row.status = "Pending"
			row.run = None
			row.error = None
		self.succeeded = self.failed = self.skipped = 0
		self.status = "Queued"
		self.started_at = now_datetime()
		self.ended_at = None
		self.save()

		return {"batch": self.name, "items": len(self.items), "jobs": enqueue_chunks(self.name)}

	@frappe.whitelist()
	def cancel_batch(self):
		"""Stop after the products already in flight.

		Named `cancel_batch` rather than `cancel` so it does not shadow
		Document.cancel. Each worker re-reads this status before its next product,
		so the effect is immediate for everything not yet started.
		"""
		self.check_permission("write")
		if self.status not in IN_FLIGHT:
			frappe.throw(f"{self.name} is not running.")
		self.db_set("status", "Cancelled", commit=True)
		return {"batch": self.name, "status": self.status}

	@frappe.whitelist()
	def retry_failed(self):
		"""Re-queue only the rows that failed or were cancelled, in this same batch."""
		self.check_permission("write")
		if self.status in IN_FLIGHT:
			frappe.throw(f"{self.name} is already {self.status.lower()}.")

		rows = [row for row in self.items if row.status in ("Failed", "Cancelled")]
		if not rows:
			frappe.throw("Nothing to retry — no failed or cancelled products.")

		for row in rows:
			row.status = "Pending"
			row.run = None
			row.error = None
		self.status = "Queued"
		self.ended_at = None
		self.save()

		jobs = enqueue_chunks(self.name, rows=[row.name for row in rows])
		return {"batch": self.name, "items": len(rows), "jobs": jobs}
