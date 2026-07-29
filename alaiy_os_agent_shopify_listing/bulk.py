# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Bulk enrichment: many products, still one OS Agent Run each.

The engine in alaiy_os runs exactly one product per Run, and this does not change
that — a batch is a fan-out over it. The rows of a Shopify Listing Bulk Enrich are
split into chunks of `batch_size`, one background job per chunk, and each job walks
its rows in order: create the Run, then execute it in-process through core's
``run_queued``. So every product keeps its own run history, output, transcript and
token counts, exactly as a single Enrich does.

Chunks rather than one job per product on purpose: 200 products would otherwise put
200 jobs on the shared `long` queue and starve everything else on it. Chunk count is
the parallelism knob, chunk size the per-job length.

Ordering matters in here. ``run_queued`` commits as it goes and calls
``frappe.db.rollback()`` on failure, so this module never holds a dirty batch
document across a product — row state is written with ``frappe.db.set_value`` and
committed *before* the run starts.
"""

import json

import frappe
from frappe.utils import now_datetime

BATCH_DOCTYPE = "Shopify Listing Bulk Enrich"
ITEM_DOCTYPE = "Shopify Listing Bulk Enrich Item"
ENRICHED_DOCTYPE = "Shopify Enriched Listing"

DEFAULT_BATCH_SIZE = 5
# One product: the agent's LLM turns plus, when the image toggles are on, several
# image round-trips. A chunk job gets this much time per row it carries.
PER_ITEM_TIMEOUT = 900

PENDING_STATES = ("Pending", "Running")


def enqueue_chunks(batch, rows=None):
	"""Split `rows` (default: every Pending row) into chunks, one worker job each.

	Returns the number of jobs enqueued.
	"""
	doc = frappe.get_doc(BATCH_DOCTYPE, batch)
	names = rows if rows is not None else [row.name for row in doc.items if row.status == "Pending"]
	size = max(1, int(doc.batch_size or DEFAULT_BATCH_SIZE))
	chunks = [names[i : i + size] for i in range(0, len(names), size)]

	for n, chunk in enumerate(chunks):
		frappe.enqueue(
			"alaiy_os_agent_shopify_listing.bulk.run_chunk",
			queue="long",
			timeout=len(chunk) * PER_ITEM_TIMEOUT,
			# Distinct per chunk, and stable across a retry of the same chunk.
			job_id=f"bulk-enrich::{batch}::{n}::{chunk[0]}",
			batch=batch,
			rows=chunk,
			enqueue_after_commit=True,
		)
	return len(chunks)


def run_chunk(batch, rows):
	"""Worker entry point: enrich this chunk's products, one at a time."""
	try:
		agent, options, skip_enriched = _request(batch)
	except Exception as e:
		# Nothing product-specific can run (the agent is gone, disabled, or its
		# override won't parse). Fail this chunk's rows rather than dying with them
		# left Pending, which would strand the batch in Running forever.
		frappe.log_error(title=f"Bulk enrich {batch}: could not start")
		for row in rows:
			_set_row(row, {"status": "Failed", "error": _error_summary(str(e))})
		_finalize(batch)
		return

	_mark_running(batch)

	for row in rows:
		if frappe.db.get_value(BATCH_DOCTYPE, batch, "status") == "Cancelled":
			_set_row(row, {"status": "Cancelled", "error": "Batch cancelled."})
			continue
		_run_row(batch, row, agent, options, skip_enriched)

	_finalize(batch)


def _run_row(batch, row, agent, options, skip_enriched):
	item_code = frappe.db.get_value(ITEM_DOCTYPE, row, "item_code")

	if skip_enriched and frappe.db.exists(ENRICHED_DOCTYPE, item_code):
		_set_row(row, {"status": "Skipped", "error": "Already enriched."})
		_publish(batch, row, item_code, "Skipped")
		return

	try:
		run = _create_run(agent, {"item_code": item_code, **options})
		# Committed before the run starts: run_queued rolls back on failure, which
		# would otherwise discard this row's own state along with the run's.
		_set_row(row, {"status": "Running", "run": run, "error": None})

		from alaiy_os.engine.executor import run_queued

		# Records its own failure on the Run and returns; it does not raise.
		run_queued(run)

		status, error = frappe.db.get_value("OS Agent Run", run, ["status", "error"])
		row_status = "Success" if status == "Success" else "Failed"
		_set_row(row, {"status": row_status, "error": _error_summary(error)})
	except Exception as e:
		# Anything before or around the run itself (product missing, agent disabled,
		# a broken payload) — one bad product must not take the chunk down with it.
		frappe.db.rollback()
		frappe.log_error(title=f"Bulk enrich {batch}: {item_code} failed")
		row_status = "Failed"
		_set_row(row, {"status": row_status, "error": _error_summary(str(e))})

	_publish(batch, row, item_code, row_status)


def _create_run(agent, payload):
	"""An OS Agent Run for one product, queued but not enqueued — this worker runs it.

	Mirrors alaiy_os.engine.executor.execute_agent, minus the frappe.enqueue: the
	whole point of a chunk is that its products run in *this* job.
	"""
	enabled = frappe.db.get_value("OS Agent Registry", agent, "is_enabled")
	if enabled is None:
		frappe.throw(f"Agent {agent} does not exist.")
	if not enabled:
		frappe.throw(f"Agent {agent} is disabled.")

	run = frappe.get_doc(
		{
			"doctype": "OS Agent Run",
			"agent": agent,
			"trigger_type": "API",
			"status": "Queued",
			"input": json.dumps(payload, indent=1),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return run.name


def _request(batch):
	"""What every product in `batch` shares: the agent, its options, the skip flag.

	The toggles are read off the batch by the fieldnames the agent's tools declare
	(`input_options`), so nothing here names a tool — same contract as the single-run
	desk surfaces.
	"""
	from alaiy_os_agent_shopify_listing.agent_meta import build_agent_meta

	meta = build_agent_meta()
	doc = frappe.get_doc(BATCH_DOCTYPE, batch)

	options = {opt["fieldname"]: bool(doc.get(opt["fieldname"])) for opt in meta["input_options"]}
	if doc.notes:
		options["notes"] = doc.notes
	return meta["agent_id"], options, bool(doc.skip_enriched)


def _set_row(row, values):
	"""Write one row's state and commit it — see this module's docstring on ordering."""
	frappe.db.set_value(ITEM_DOCTYPE, row, values, update_modified=False)
	frappe.db.commit()


def _mark_running(batch):
	"""Whichever chunk starts first flips the batch out of Queued."""
	if frappe.db.get_value(BATCH_DOCTYPE, batch, "status") == "Queued":
		frappe.db.set_value(BATCH_DOCTYPE, batch, "status", "Running", update_modified=False)
		frappe.db.commit()


def _finalize(batch):
	"""Close the batch once no row is left to run.

	Every chunk calls this; the "anything still pending?" check is what makes it
	idempotent, so whichever chunk happens to finish last is the one that closes.
	"""
	statuses = [
		row.status
		for row in frappe.get_all(
			ITEM_DOCTYPE,
			filters={"parent": batch, "parenttype": BATCH_DOCTYPE},
			fields=["status"],
		)
	]
	if any(status in PENDING_STATES for status in statuses):
		return

	failed = statuses.count("Failed")
	succeeded = statuses.count("Success")
	skipped = statuses.count("Skipped")

	if frappe.db.get_value(BATCH_DOCTYPE, batch, "status") == "Cancelled":
		status = "Cancelled"
	elif not failed:
		status = "Completed"
	elif not succeeded:
		status = "Failed"
	else:
		status = "Completed with Errors"

	frappe.db.set_value(
		BATCH_DOCTYPE,
		batch,
		{
			"status": status,
			"succeeded": succeeded,
			"failed": failed,
			"skipped": skipped,
			"ended_at": now_datetime(),
		},
		update_modified=False,
	)
	frappe.db.commit()
	frappe.publish_realtime(
		"bulk_enrich_done",
		{"batch": batch, "status": status, "succeeded": succeeded, "failed": failed, "skipped": skipped},
		doctype=BATCH_DOCTYPE,
		docname=batch,
	)


def _publish(batch, row, item_code, status):
	"""Per-product progress, so a UI can follow a batch without polling."""
	frappe.publish_realtime(
		"bulk_enrich_progress",
		{"batch": batch, "row": row, "item_code": item_code, "status": status},
		doctype=BATCH_DOCTYPE,
		docname=batch,
	)


def _error_summary(text):
	"""The last line of a traceback is the actual error; keep that, drop the frames."""
	if not text:
		return None
	lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
	return lines[-1][:500] if lines else None
