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

from alaiy_os_agent_shopify_listing.tools.images import FETCH_HEADERS

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
	return {
		"query": query,
		"answer": result.get("answer") or "",
		"citations": result.get("citations") or [],
	}


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
	return {
		"_content_blocks": [
			{"type": "text", "text": f"Page content ({url}):\n{text}"},
		]
	}
