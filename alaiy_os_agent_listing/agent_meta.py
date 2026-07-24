# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
Single source of truth for this agent's registration metadata — the agent
equivalent of a connector's connector_meta.py. Consumed by setup/install.py →
upserted into alaiy_os's OS Agent Registry (and its OS Agent Tool child rows).

This agent — "Listing Enrichment" — turns raw supplier product data (an
ERPNext Item created from a supplier CSV) into a structured, Shopify-ready
listing for The Solist: editorial title + description, bullet points, SEO
fields, Shopify tags, and category-specific attributes. It READS the product's
Shopify Product Listing and its images, RETURNS a JSON object, and (via the save_listing tool) persists
that listing into its own Enriched Listing DocType in "Needs Review" status for
admin review. It does not modify the listing (or the Item behind it) or
publish to Shopify (that is
the approval / connector step).

Credentials are NOT stored here. Model access is provided by Alaiy OS core
(the engine's anthropic_api_key) and any third-party keys/usage/billing are
handled by a separate Alaiy service, not by this app.
"""

import json
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent


def _read(relpath):
	return (_APP_DIR / relpath).read_text(encoding="utf-8")


# The enriched-listing schema. Used both as the agent's output_format schema and
# as the `listing` argument schema for the save_listing tool, so a persisted row
# is validated against exactly the shape the agent returns.
_OUTPUT_SCHEMA = json.loads(_read("schemas/output.json"))


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
	"output_schema": _OUTPUT_SCHEMA,

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
				"Fetch a product's Shopify Product Listing by its item_code, "
				"returning the listing's current data — title, description, "
				"price, Shopify status, and variants (each variant's item code, "
				"price, enabled flag) — together with its product photos as "
				"images you can look at. ALWAYS call this first when the input "
				"contains an item_code, and study the photos: they are the "
				"primary evidence for material, gemstones, dial, strap, clasp, "
				"earring back, style and other visual attributes. Each photo is "
				"labelled with its source (Original vs. AI Enhanced)."
			),
			"handler": "alaiy_os_agent_listing.tools.handlers.get_product",
			"parameters_schema": {
				"type": "object",
				"properties": {
					"item_code": {
						"type": "string",
						"description": "The item_code to enrich; also the name of its Shopify Product Listing.",
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
			"tool_id": "view_image",
			"description": (
				"Fetch an external image URL (e.g. an `image_url` given in the "
				"input) and show it to you as an actual image, not just a string. "
				"ALWAYS call this before writing anything if your only product "
				"evidence is a URL rather than an item_code — you cannot "
				"accurately describe, enrich, or generate imagery for a product "
				"you have never looked at."
			),
			"handler": "alaiy_os_agent_listing.tools.handlers.view_image",
			"parameters_schema": {
				"type": "object",
				"properties": {
					"image_url": {
						"type": "string",
						"description": "The image URL to fetch and look at.",
					},
				},
				"required": ["image_url"],
			},
			"connector": None,
		},
		{
			"tool_id": "generate_product_images",
			"description": (
				"Generate the full editorial image set (hero, detail, angle, "
				"lifestyle, scale) in ONE call — pass all 5 briefs together, in "
				"that order. Whether images are actually produced is decided by "
				"the tool itself, NOT by you: it generates ONLY when the product "
				"has an original photo to edit AND generate_images is true, and "
				"every shot is generated by editing that real photo — it never "
				"invents a product from scratch. Pass `item_code` (for an "
				"item_code run — the tool reads the listing's own photo) and "
				"`generate_images` copied verbatim from the input (default "
				"false). For a URL-only product, pass the real photo as "
				"`reference_image_url` instead. Each brief must be a detailed, "
				"self-contained prompt describing the exact product (material, "
				"style, gemstone, setting) and the shot itself (framing, "
				"background, lighting) — the image model has no memory of the "
				"conversation, only that brief and the reference photo. Returns "
				"{images: [{kind, brief, url}, ...]}; copy that list verbatim "
				"into the final `images` array. If it returns an empty list with "
				"a note (no original photo, or the toggle is off), that is "
				"expected — set images to [] and record the note. If image "
				"generation isn't configured, each entry comes back with url=null "
				"— do not retry, just include them as-is and note it."
			),
			"handler": "alaiy_os_agent_listing.tools.handlers.generate_product_images",
			"parameters_schema": {
				"type": "object",
				"properties": {
					"briefs": {
						"type": "array",
						"minItems": 1,
						"maxItems": 5,
						"description": "One entry per shot, in order: hero, detail, angle, lifestyle, scale.",
						"items": {
							"type": "object",
							"properties": {
								"kind": {
									"type": "string",
									"enum": ["hero", "detail", "angle", "lifestyle", "scale"],
									"description": "Which shot this is.",
								},
								"brief": {
									"type": "string",
									"description": "Detailed, self-contained prompt for this shot.",
								},
							},
							"required": ["kind", "brief"],
						},
					},
					"item_code": {
						"type": "string",
						"description": (
							"The item_code being enriched (= its Shopify Product "
							"Listing name). The tool reads that listing's own "
							"original photo and edits it; if the listing has no "
							"photo, nothing is generated."
						),
					},
					"generate_images": {
						"type": "boolean",
						"description": (
							"The per-request opt-in toggle, copied verbatim from "
							"the input (default false). Images are produced only "
							"when this is true AND an original photo exists."
						),
					},
					"reference_image_url": {
						"type": "string",
						"description": (
							"Only for a URL-only product with no item_code: the URL "
							"of a real photo of this exact product, which every "
							"shot is generated by editing. Ignored when item_code "
							"resolves to a listing photo."
						),
					},
				},
				"required": ["briefs"],
			},
			"connector": None,
		},
		{
			"tool_id": "save_listing",
			"description": (
				"Persist the finished listing into the Enriched Listing DocType "
				"for admin review. Call this ONCE as your FINAL action, after you "
				"have assembled the complete listing (including the generated "
				"images), passing the `item_code` and the exact same object you "
				"are about to return as `listing`. It upserts by item_code — "
				"re-running on the same product updates its row rather than "
				"creating a duplicate — and lands the row in 'Needs Review' "
				"status. After it returns, reply with the listing JSON as usual. "
				"Skip this tool ONLY when there is no item_code (a URL-only "
				"input), since the record is keyed to the product's item_code."
			),
			"handler": "alaiy_os_agent_listing.tools.handlers.save_listing",
			"parameters_schema": {
				"type": "object",
				"properties": {
					"item_code": {
						"type": "string",
						"description": "The item_code this listing is for (upsert key).",
					},
					"listing": _OUTPUT_SCHEMA,
				},
				"required": ["item_code", "listing"],
			},
			"connector": None,
		},
	],
}
