# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
The listing agent's one route to the public web: finding the same product on a
competitor's site and reading what it says there, for catalog-level specs a
supplier's own data and photos don't carry.

Both tools go through Alaiy OS core's `ai_client` seam like everything else this
agent does — `search_competitor_listings` via `llm.web_search`, the same
provider-agnostic call `chat/websearch.py` uses for Ask Alaiy. `view_page` has no
such seam to go through: fetching and reading a page is not a model capability,
so it is plain `requests` plus a stdlib HTML strip, the same way `images.py`
fetches an external image itself rather than trusting the model to.
"""

import re
from html.parser import HTMLParser

import frappe

from alaiy_os.engine import llm
from alaiy_os.engine.context import get_agent_context

from alaiy_os_agent_shopify_listing.tools.images import FETCH_HEADERS

#: Where this run's web work accumulates, for the rest of the run to read.
#:
#: `save_listing` is the run's LAST action, so by the time provenance is worked
#: out the searches and page fetches have already happened — but the run is
#: still executing, which means its OS Agent Run transcript has not been
#: written yet and the engine's own tool ledger is still in memory. Neither can
#: be read back from the database mid-run.
#:
#: So the tools that reach the web record what they found here, on the request,
#: and `provenance.py` reads it at the end of the same run. `frappe.flags` is
#: cleared between jobs — but NOT between runs, which is the case that matters
#: here: `bulk.py` deliberately executes a whole chunk of products inside one
#: job, so without the run id below, product B could have a value "sourced" to
#: a page fetched for product A, and every product's stored research would
#: carry its predecessors' searches. A wrong URL is worse than no URL in a
#: feature whose entire purpose is that a source can be trusted.
_RESEARCH_FLAG = "_listing_research"

#: A product spec page has no business being longer than this once boilerplate
#: is stripped; truncating protects the turn budget from a page that is mostly
#: navigation, scripts, or an unrelated wall of related-products markup.
MAX_PAGE_CHARS = 12_000

#: Tags whose content is never page copy, so it is dropped instead of stripped-and-kept.
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}


class _TextExtractor(HTMLParser):
	"""Bare-bones HTML-to-text: keeps tag text, drops markup and skip-tag bodies."""

	def __init__(self):
		super().__init__()
		self._skip_depth = 0
		self.chunks = []

	def handle_starttag(self, tag, attrs):
		if tag in _SKIP_TAGS:
			self._skip_depth += 1

	def handle_endtag(self, tag):
		if tag in _SKIP_TAGS and self._skip_depth:
			self._skip_depth -= 1

	def handle_data(self, data):
		if not self._skip_depth and data.strip():
			self.chunks.append(data.strip())


def _extract_text(html):
	parser = _TextExtractor()
	parser.feed(html)
	text = "\n".join(parser.chunks)
	return re.sub(r"\n{3,}", "\n\n", text).strip()


def research_record():
	"""This run's web work so far: what was searched, and what was actually read.

	`pages` holds the extracted text of every page the run really fetched, which
	is what makes a sourced attribute checkable rather than merely claimed — a
	value that appears in one of these was read off that page, and a URL that is
	not here was not opened, whatever the model's notes say about it.

	Scoped to the RUN, not the job. The record is stamped with the run that
	opened it and thrown away the moment a different run asks for it, so a bulk
	chunk cannot carry one product's pages into the next. Outside a run (a tool
	called from a script or a test) the id is None, which behaves as a single
	implicit run — fine, because nothing is being attributed.
	"""
	run = get_agent_context().get("run")
	record = frappe.flags.get(_RESEARCH_FLAG)
	if record is None or record.get("run") != run:
		record = {"run": run, "searches": [], "pages": []}
		frappe.flags[_RESEARCH_FLAG] = record
	return record


def search_competitor_listings(query):
	"""
	Web-search for `query` and return a grounded answer plus its sources.

	Mirrors `chat/websearch.py`'s confirmation-free variant: the listing agent
	doesn't need to ask permission the way Ask Alaiy does, since it isn't a
	conversation with a person mid-turn — the prompt itself decides when to
	reach for this. Gated on `llm.web_search_support()` so a site whose AI
	client can't search (BYOK with no `ai_base_url`) declines cleanly instead
	of failing every call.
	"""
	query = str(query or "").strip()
	if not query:
		frappe.throw("search_competitor_listings needs a `query` — what should I look up?")

	if not llm.web_search_support():
		frappe.throw(
			"Web search is not available on this site (the active AI client cannot "
			"search). Do NOT retry; treat this attribute as unresolved and add it to "
			"needs_review instead."
		)

	result = llm.web_search(query)
	answer = result.get("answer") or ""
	citations = result.get("citations") or []
	research_record()["searches"].append({"query": query, "citations": citations})
	return {"query": query, "answer": answer, "citations": citations}


def view_page(url):
	"""
	Fetch an external page URL and hand back its readable text, so the model can
	read a competitor listing directly rather than trusting a search summary
	alone. Any fetch failure (bad status, timeout, unreachable host) raises and
	is surfaced to the model as an errored tool result by the executor — same
	as any other tool failure, no special handling needed here.
	"""
	import requests

	resp = requests.get(url, timeout=30, headers=FETCH_HEADERS)
	resp.raise_for_status()

	text = _extract_text(resp.text)[:MAX_PAGE_CHARS]
	# Recorded only on success, and only after raise_for_status: a page that
	# 404ed is a page the run did not read, and listing it here would let a
	# value be "sourced" from a page that never loaded.
	research_record()["pages"].append({"url": url, "text": text})
	return {
		"_content_blocks": [
			{"type": "text", "text": f"Page content ({url}):\n{text}"},
		]
	}
