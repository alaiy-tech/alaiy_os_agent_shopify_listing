# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
Single source of truth for this agent's registration metadata — the agent
equivalent of a connector's connector_meta.py. Consumed by setup/install.py →
upserted into alaiy_os's OS Agent Registry (and its OS Agent Tool child rows).

This agent — "Listing Enrichment" — turns raw supplier product data (an
ERPNext Item created from a supplier CSV) into a structured, Shopify-ready
listing for The Solist: editorial title + description, bullet points, SEO
fields, Shopify tags, and category-specific attributes. It READS the Item and
its images and RETURNS a JSON object for admin review; it does not write back
to the Item or publish to Shopify (that is the approval / connector step).

Credentials are NOT stored here. Model access is provided by Alaiy OS core
(the engine's anthropic_api_key) and any third-party keys/usage/billing are
handled by a separate Alaiy service, not by this app.
"""

import json
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent


def _read(relpath):
	return (_APP_DIR / relpath).read_text(encoding="utf-8")


agent_meta = {
	# ── Identity (OS Agent Registry) ──────────────────────────────────────────
	# agent_id is the primary key. Keep it stable across releases — changing it
	# orphans run history and creates a second agent.
	"agent_id": "listing_enrichment",
	"agent_name": "Listing Enrichment",
	"description": (
		"Generates a structured, Shopify-ready product listing from raw supplier "
		"data — editorial title and description, bullet points, SEO fields, "
		"Shopify tags, and category-specific attributes — for admin review."
	),
	"icon": "sparkles",  # Lucide/Feather icon name, shown in the Agents hub
	# Reached through the core Agents hub; no custom desk Page shipped here.
	"page": None,
	# No settings DocType: this app stores no credentials (see module docstring).
	"settings_doctype": None,

	# ── Engine config ─────────────────────────────────────────────────────────
	# Opus for luxury-standard editorial copy and reliable attribute extraction
	# from product photos. Admin-triggered and low-volume, so cost is not a
	# concern; drop to claude-sonnet-5 if you want it cheaper/faster.
	"model": "claude-opus-4-8",
	"max_turns": 8,
	"system_prompt": _read("prompts/system.md"),
	# Schema-validated object: the enriched listing.
	"output_format": "JSON",
	"output_schema": json.loads(_read("schemas/output.json")),

	# ── Tools (OS Agent Tool child rows) ──────────────────────────────────────
	# handler: importable dotted path to a callable in this app.
	# parameters_schema: JSON Schema (type: object) for the tool's arguments.
	# connector: optional OS Connector Registry id this tool depends on; the
	#            engine refuses to run the agent if that connector is missing.
	#            None here — this agent only reads local data and returns JSON.
	"tools": [
		{
			"tool_id": "get_product",
			"description": (
				"Fetch a product (ERPNext Item) by its item_code, returning its "
				"raw supplier data — name, description, brand, item group, prices "
				"(selling and cost), barcode, supplier reference — together with "
				"its product photos as images you can look at. ALWAYS call this "
				"first when the input contains an item_code, and study the photos: "
				"they are the primary evidence for material, gemstones, dial, "
				"strap, clasp and other visual attributes."
			),
			"handler": "alaiy_os_agent_listing.tools.handlers.get_product",
			"parameters_schema": {
				"type": "object",
				"properties": {
					"item_code": {
						"type": "string",
						"description": "The ERPNext Item code (name) to enrich.",
					},
				},
				"required": ["item_code"],
			},
			"connector": None,
		},
		{
			"tool_id": "get_reference_values",
			"description": (
				"Return existing catalog vocabulary already in use on The Solist "
				"store — known brands, product categories (item groups), Shopify "
				"tags already applied to other products, and Shopify locations. "
				"Call this before finalising the brand, category, and shopify_tags "
				"so your output stays consistent with existing listings instead of "
				"inventing new variants of the same value."
			),
			"handler": "alaiy_os_agent_listing.tools.handlers.get_reference_values",
			"parameters_schema": {
				"type": "object",
				"properties": {},
			},
			"connector": None,
		},
		{
			"tool_id": "generate_image",
			"description": (
				"Generate one editorial product image. Call this once per shot in "
				"the `images` set (hero, detail, angle, lifestyle, scale) using "
				"whatever product evidence is available (supplier photos, "
				"description, or a reference image URL). `brief` must be a "
				"detailed, self-contained prompt describing the exact product "
				"(material, style, gemstone, setting) and the shot itself "
				"(framing, background, lighting) — the image model has no memory "
				"of the conversation, only this brief. Returns the stored file's "
				"url; use it verbatim in the final `images` array."
			),
			"handler": "alaiy_os_agent_listing.tools.handlers.generate_image",
			"parameters_schema": {
				"type": "object",
				"properties": {
					"kind": {
						"type": "string",
						"enum": ["hero", "detail", "angle", "lifestyle", "scale"],
						"description": "Which shot in the image set this is.",
					},
					"brief": {
						"type": "string",
						"description": "Detailed, self-contained prompt for the image to generate.",
					},
				},
				"required": ["kind", "brief"],
			},
			"connector": None,
		},
	],
}
