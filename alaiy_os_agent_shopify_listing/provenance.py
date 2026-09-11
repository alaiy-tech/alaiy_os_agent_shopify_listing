# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
Where each attribute on an enriched listing actually came from.

The prompt describes an evidence ladder — what the product already publishes,
then the store's other records of it, then the listing text, then the photos,
then a competitor page, and only then `needs_review`. A reviewer reading the
finished grid cannot see any of that: `water_resistance: 30 m` looks identical
whether it was copied off a metafield the store has held for a year or read off
a watch forum thirty seconds ago, and those deserve very different amounts of
trust.

This module works the rung out **from the evidence, not from the model's
account of it**. That distinction is the whole point. The override prompt spends
three paragraphs telling the agent not to write "confirmed via manufacturer
sources" over a value it never sourced, which is a real failure and exactly the
kind a prompt cannot prevent on its own — a model that would invent the value
will invent the citation beside it. So nothing here asks the model where a value
came from:

  * `published` and `metafield` are decided by comparing the value against what
    the store already holds, which this app can read for itself.
  * `web` is granted only when the value literally appears in the text of a page
    `view_competitor_page` really fetched during this run (see
    `tools.websearch.research_record`). A URL the model cites but never opened
    earns nothing.
  * `product` is the residual: no store record and no fetched page contains it,
    so the agent derived it from the listing's own text or its photos. Those two
    are not distinguishable from outside the model, and pretending otherwise
    would be its own small lie — they are reported together as the product's own
    evidence.

`unsourced_claims` closes the loop the other way: URLs the agent named in its
`notes` that appear in no fetched page. That is the invented citation, caught
mechanically, and it is worth surfacing precisely because it reads as verified.
"""

import re

import frappe

from alaiy_os_agent_shopify_listing import matrix
from alaiy_os_agent_shopify_listing.tools import websearch

#: The rungs, most authoritative first. Stored on each attribute row.
PUBLISHED = "published"
METAFIELD = "metafield"
WEB = "web"
PRODUCT = "product"
REVISED = "revised"

#: How each rung reads to a reviewer. The Desk select and the portal both use
#: these, so the vocabulary is defined once.
LABELS = {
	PUBLISHED: "Already published",
	METAFIELD: "Store record",
	WEB: "Web source",
	PRODUCT: "Product text or photos",
	REVISED: "Changed from published",
}

#: A metafield value shorter than this is not evidence of anything — a bare "1"
#: or "S" matches half the attributes on the product by coincidence, and a
#: coincidence shown as provenance is worse than no provenance.
MIN_METAFIELD_CHARS = 2

_URL = re.compile(r"https?://[^\s,;\"'()<>\]]+")


def _clean_url(url):
	"""A URL as a person would compare it — no trailing sentence punctuation."""
	return (url or "").rstrip(".,;:)]}»\"'").strip()


def of_attribute(key, value, published, metafields, pages):
	"""
	The rung `value` came from, as `(source, detail)`.

	`detail` names the specific evidence where there is one — the metafield key,
	or the URL of the page the value was read off — and is None otherwise.
	"""
	live = (published or {}).get(key)
	if live:
		# The store already answers this. Same value: the run confirmed what was
		# published. Different value: the run is proposing to overwrite a live
		# one, which is the single most consequential thing it can do to an
		# attribute and should never be indistinguishable from a confirmation.
		return (PUBLISHED, None) if matrix.same_value(value, live) else (REVISED, live)

	match = _metafield_match(value, metafields)
	if match:
		return METAFIELD, match

	url = _page_match(value, pages)
	if url:
		return WEB, url

	return PRODUCT, None


def _metafield_match(value, metafields):
	"""The store's own record of this value under another name, if there is one.

	Exact matches are preferred over containment so `watch_face_size: "36"`
	wins over some longer field that merely mentions 36 — and containment is
	checked one way only, attribute-contains-metafield, because that is the
	direction the units go: the store keeps a bare `30` and the attribute is
	written `30 m`.
	"""
	contained = None
	for namespace, values in (metafields or {}).items():
		for mkey, mvalue in (values or {}).items():
			text = str(mvalue or "").strip()
			if len(text) < MIN_METAFIELD_CHARS:
				continue
			name = f"{namespace}.{mkey}" if namespace else mkey
			if matrix.same_value(value, text):
				return name
			if contained is None and matrix.contains_value(value, text):
				contained = name
	return contained


def _page_match(value, pages):
	"""The URL of a fetched page whose text contains this value, if any.

	`matrix.contains_value` matches whole runs of words, so a value is found
	because the page says it, not because its letters happen to occur inside a
	longer word.
	"""
	for page in pages or []:
		if page.get("text") and matrix.contains_value(page["text"], value):
			return page.get("url")
	return None


def unsourced_claims(notes, pages):
	"""URLs the agent cited that no `view_competitor_page` call ever fetched.

	The override prompt requires a sourced attribute to carry the URL it was
	read off. A URL in `notes` that is not among the pages actually opened is
	therefore either a citation the agent saw in a search result and never
	read, or one it made up — and both are the same problem for a reviewer, who
	has no way to tell a real source from a plausible-looking one by eye.
	"""
	fetched = {_clean_url(p.get("url")) for p in (pages or [])}
	cited = {_clean_url(u) for u in _URL.findall(notes or "")}
	return sorted(u for u in cited if u and u not in fetched)


def research(notes=None):
	"""
	This run's web work, as a record to store beside the listing.

	Page text is deliberately dropped: it is bulky, it is already in the run's
	transcript, and what a reviewer needs from here is which sources were
	consulted, not a second copy of them.
	"""
	record = websearch.research_record()
	pages = record.get("pages") or []
	return {
		"searches": [
			{"query": s.get("query"), "citations": s.get("citations") or []}
			for s in (record.get("searches") or [])
		],
		"pages": [{"url": p.get("url")} for p in pages],
		"unsourced_claims": unsourced_claims(notes, pages),
	}


def pages_read():
	"""The pages this run actually fetched, with their text, for matching."""
	return websearch.research_record().get("pages") or []


def other_metafields(listing_metafields, attribute_namespace):
	"""The store's records of a product other than the attributes themselves."""
	return {
		namespace: values
		for namespace, values in (listing_metafields or {}).items()
		if namespace != attribute_namespace
	}


def summarise(rows):
	"""One line per rung — "4 already published, 2 from the web" — for a caller
	that wants the shape of a run's evidence without reading every row."""
	counts = {}
	for row in rows or []:
		source = row.get("source")
		if source:
			counts[source] = counts.get(source, 0) + 1
	return counts


def label(source):
	return LABELS.get(source, source or "")


def source_options():
	"""The Select field's options, newline-joined as Frappe wants them."""
	return "\n".join(("",) + tuple(LABELS))


def log_failure(item_code):
	frappe.log_error(title=f"Listing provenance: could not work it out for {item_code}")
