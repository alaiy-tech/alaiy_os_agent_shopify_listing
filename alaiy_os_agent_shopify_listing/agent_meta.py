# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
The Shopify listing agent: its prompt, its schema, its tools — and how a customer
app overrides the prompt.

There is ONE listing agent per site, `shopify_listing`. This app defines all of it.
A customer app changes it by dropping a single markdown file at:

    <customer_app>/agents/shopify_listing.md

Whatever is in that file is appended to the vanilla prompt below, so it says what is
true of that store: who they are, their house style, their categories and
attributes, and which image tool the agent should use. No registration, no hook, no
config — the file being there is the whole mechanism.

It may start with optional frontmatter, for the two things a prompt cannot express:

    ---
    model: claude-opus-4-8
    description: Shown in the Agents hub.
    ---
    Everything from here down is appended to the vanilla prompt.

All tools are registered on every site. The prompt decides which get called, which
is why picking image translation over image generation is a sentence, not a setting.
"""

import json
from pathlib import Path

import frappe

_APP = "alaiy_os_agent_shopify_listing"
_APP_DIR = Path(__file__).resolve().parent

# Where a customer app puts its override, relative to its own package directory.
OVERRIDE_PATH = ("agents", "shopify_listing.md")

# Frontmatter keys we honour. Anything else in there is a typo, so say so.
OVERRIDE_KEYS = ("model", "description")


def read_text(relpath):
	"""Read a file relative to THIS app's package directory."""
	return (_APP_DIR / relpath).read_text(encoding="utf-8")


# The one listing agent per site: the OS Agent Registry primary key, and what you
# pass as `agent` to alaiy_os.api.agents.run_agent.
AGENT_ID = "shopify_listing"
AGENT_NAME = "Shopify Listing"
AGENT_ICON = "sparkles"

DEFAULT_MODEL = "gemini-3.1-flash-lite"
DEFAULT_MAX_TURNS = 8
DEFAULT_DESCRIPTION = (
	"Generates a structured, Shopify-ready product listing from raw product data — "
	"title, description, bullet points, SEO fields, Shopify tags and product "
	"attributes — for admin review."
)

BASE_PROMPT = read_text("prompts/system.md")
BASE_SCHEMA = json.loads(read_text("schemas/output.json"))

_HANDLERS = f"{_APP}.tools.handlers"
_IMAGE_GEN = f"{_APP}.tools.image_generation"
_IMAGE_TRANS = f"{_APP}.tools.image_translation"


# ── the tools ─────────────────────────────────────────────────────────────────
# Every tool the listing agent has, keyed by tool_id. All of them are registered on
# every site; the agent's prompt is what decides which ones it actually calls. That
# is why a customer override is only a prompt — telling the agent to translate
# supplier photos rather than generate editorial shots needs no configuration.
#
# An entry may carry `input_option`, the per-request toggle the desk surfaces render
# for it (see api.py).
#
# It is a function, not a constant, only because save_listing's `listing` argument is
# the output schema itself.


def tool_catalog(output_schema):
	return {
		"get_product": {
			"description": (
				"Fetch a product's Shopify Product Listing by its item_code, "
				"returning the listing's current data — title, description, price, "
				"Shopify status, and variants (each variant's item code, price, "
				"enabled flag, photo) — together with its product photos as images "
				"you can look at, followed by each variant's own photo labelled with "
				"that variant's item code. ALWAYS call this first when the input "
				"contains an item_code, and study the photos: they are the primary "
				"evidence for material, colour, pattern, construction, what is in the "
				"box, and any spec text printed onto the image. Each photo is "
				"labelled with its source; the variant photos are your evidence for "
				"the per-variant option values in the `variants` output array."
			),
			"handler": f"{_HANDLERS}.get_product",
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
		},
		"get_reference_values": {
			"description": (
				"Return existing catalog vocabulary already in use on this store — "
				"known brands, product categories (item groups), Shopify tags already "
				"applied to other products, and Shopify locations. Call this before "
				"finalising the brand, category, and shopify_tags so your output stays "
				"consistent with existing listings instead of inventing new variants "
				"of the same value."
			),
			"handler": f"{_HANDLERS}.get_reference_values",
			"parameters_schema": {"type": "object", "properties": {}},
		},
		"view_image": {
			"description": (
				"Fetch an external image URL (e.g. an `image_url` given in the input) "
				"and show it to you as an actual image, not just a string. ALWAYS call "
				"this before writing anything if your only product evidence is a URL "
				"rather than an item_code — you cannot accurately describe or enrich a "
				"product you have never looked at."
			),
			"handler": f"{_HANDLERS}.view_image",
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
		},
		"generate_product_images": {
			"description": (
				"Generate the full editorial image set (hero, detail, angle, "
				"lifestyle, scale) in ONE call — pass all 5 briefs together, in that "
				"order. Whether images are actually produced is decided by the tool "
				"itself, NOT by you: it generates ONLY when the product has an "
				"original photo to edit AND generate_images is true, and every shot is "
				"generated by editing that real photo — it never invents a product "
				"from scratch. Pass `item_code` (for an item_code run — the tool reads "
				"the listing's own photo) and `generate_images` copied verbatim from "
				"the input (default false). For a URL-only product, pass the real "
				"photo as `reference_image_url` instead. Each brief must be a "
				"detailed, self-contained prompt describing the exact product "
				"(material, style, gemstone, setting) and the shot itself (framing, "
				"background, lighting) — the image model has no memory of the "
				"conversation, only that brief and the reference photo. Returns "
				"{images: [{kind, brief, url}, ...]}; copy that list verbatim into the "
				"final `images` array. EXPECT url TO BE null: the shots are rendered "
				"in the background after this run finishes and are attached to the "
				"listing then. That is success, not failure — do NOT retry the tool, "
				"do NOT call it a second time, do NOT list the images in needs_review, "
				"and do NOT describe them as missing or failed anywhere in your "
				"output. If it returns an empty list with a note (no original photo, "
				"or the toggle is off), that is also expected — set images to [] and "
				"record the note."
			),
			"handler": f"{_IMAGE_GEN}.generate_product_images",
			"input_option": {
				"fieldname": "generate_images",
				"label": "Enrich images",
				"description": (
					"Generate the editorial image set for this listing by editing its "
					"real photo. Costs money per image; turn off for a faster, "
					"text-only enrichment."
				),
				"default": 0,
			},
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
							"Listing name). The tool reads that listing's own original "
							"photo and edits it; if the listing has no photo, nothing "
							"is generated."
						),
					},
					"generate_images": {
						"type": "boolean",
						"description": (
							"The per-request opt-in toggle, copied verbatim from the "
							"input (default false). Images are produced only when this "
							"is true AND an original photo exists."
						),
					},
					"reference_image_url": {
						"type": "string",
						"description": (
							"Only for a URL-only product with no item_code: the URL of "
							"a real photo of this exact product, which every shot is "
							"generated by editing. Ignored when item_code resolves to a "
							"listing photo."
						),
					},
				},
				"required": ["briefs"],
			},
		},
		"translate_product_images": {
			"description": (
				"Translate the text printed on the product's supplier photos into "
				"English, via alphashop's image translation API. Call it ONCE for the "
				"whole product — it covers the listing's photos AND each variant's "
				"own photo in that one call. Whether images are actually translated "
				"is decided by the tool itself, NOT by you: it runs ONLY when the "
				"product has photos AND translate_images is true. Pass `item_code` "
				"(for an item_code run — the tool reads the listing's own photos and "
				"its variants' photos) and `translate_images` copied verbatim from "
				"the input (default false). For a URL-only product, pass the photo "
				"URLs as `image_urls` instead. Returns "
				"{images: [{source_url, item_variant, url, note}, ...]} — an entry "
				"with item_variant set is that variant's photo; copy the list "
				"verbatim, item_variant included, into the final `images` array. EXPECT url TO BE null: the photos are "
				"translated in the background after this run finishes and are attached "
				"to the listing then. That is success, not failure — do NOT retry the "
				"tool, do NOT call it a second time, do NOT list the images in "
				"needs_review, and do NOT describe them as missing or failed anywhere "
				"in your output. Some entries MAY come back with a real url: those are "
				"photos an earlier run already translated, reused instead of being "
				"paid for twice. A mix of real urls and nulls in one result is normal "
				"— copy them all verbatim exactly as returned. If it returns an empty "
				"list with a note (no photos, or the toggle is off), that is also "
				"expected — set images to [] and record the note."
			),
			"handler": f"{_IMAGE_TRANS}.translate_product_images",
			"input_option": {
				"fieldname": "translate_images",
				"label": "Translate product images",
				"description": (
					"Sends each supplier photo to the image translation service. Costs "
					"money per image, and only works for photos reachable from the "
					"public internet."
				),
				"default": 0,
			},
			"parameters_schema": {
				"type": "object",
				"properties": {
					"item_code": {
						"type": "string",
						"description": (
							"The item_code being enriched (= its Shopify Product "
							"Listing name). The tool reads that listing's own photos; "
							"if the listing has no photos, nothing is done."
						),
					},
					"translate_images": {
						"type": "boolean",
						"description": (
							"The per-request opt-in toggle, copied verbatim from the "
							"input (default false). Images are translated only when "
							"this is true AND the product has photos."
						),
					},
					"image_urls": {
						"type": "array",
						"items": {"type": "string"},
						"description": (
							"Only for a URL-only product with no item_code: the photo "
							"URLs to translate. Ignored when item_code resolves to a "
							"listing with photos."
						),
					},
				},
			},
		},
		"save_listing": {
			"description": (
				"Persist the finished listing into the Shopify Enriched Listing "
				"DocType for admin review. Call this ONCE as your FINAL action, after "
				"you have assembled the complete listing (including any images), "
				"passing the `item_code` and the exact same object you are about to "
				"return as `listing`. It upserts by item_code — re-running on the same "
				"product updates its row rather than creating a duplicate — and lands "
				"the row in 'Needs Review' status. After it returns, reply with the "
				"listing JSON as usual. Skip this tool ONLY when there is no item_code "
				"(a URL-only input), since the record is keyed to the product's "
				"item_code."
			),
			"handler": f"{_HANDLERS}.save_listing",
			"parameters_schema": {
				"type": "object",
				"properties": {
					"item_code": {
						"type": "string",
						"description": "The item_code this listing is for (upsert key).",
					},
					"listing": output_schema,
				},
				"required": ["item_code", "listing"],
			},
		},
	}

# ── the customer override ─────────────────────────────────────────────────────


def find_override():
	"""
	The installed app that overrides the listing agent, and its markdown file.

	Discovery is just "does the file exist", so a customer app needs no hook and no
	Python. Returns (app, Path) or (None, None).

	Two apps overriding one agent is a mistake worth shouting about: their prompts
	would silently concatenate in installed-app order.
	"""
	found = []
	for app in frappe.get_installed_apps():
		if app == _APP:
			continue
		path = Path(frappe.get_app_path(app, *OVERRIDE_PATH))
		if path.exists():
			found.append((app, path))

	if len(found) > 1:
		frappe.throw(
			"More than one app overrides the Shopify listing agent: "
			f"{[app for app, _ in found]}. A site has one listing agent, so leave "
			"only the customer app whose store this site is."
		)
	return found[0] if found else (None, None)


def parse_override(text):
	"""
	Split an override file into (frontmatter dict, prompt body).

	Frontmatter is optional, `key: value` per line between two `---` lines. Kept
	deliberately dumb — it exists only for `model` and `description`, everything else
	belongs in the prompt itself.
	"""
	meta, body = {}, text

	if text.lstrip().startswith("---"):
		stripped = text.lstrip()
		end = stripped.find("\n---", 3)
		if end != -1:
			block = stripped[3:end]
			body = stripped[end + 4:].lstrip("-").lstrip("\n")
			for line in block.strip().splitlines():
				line = line.strip()
				if not line or line.startswith("#"):
					continue
				key, _, value = line.partition(":")
				key = key.strip()
				if key not in OVERRIDE_KEYS:
					frappe.throw(
						f"Unknown key '{key}' in an agents/shopify_listing.md "
						f"frontmatter. Supported: {', '.join(OVERRIDE_KEYS)}."
					)
				meta[key] = value.strip()

	return meta, body.strip()


def build_agent_meta():
	"""
	The registration manifest setup/install.py upserts into alaiy_os's OS Agent
	Registry (and its OS Agent Tool child rows): the vanilla agent, with this site's
	override appended to its prompt.

	Credentials are NOT part of this. Model access comes from Alaiy OS core (the
	engine's anthropic_api_key); the image tools read their own keys from
	site_config.json.
	"""
	app, path = find_override()
	meta, body = parse_override(path.read_text(encoding="utf-8")) if path else ({}, "")

	prompt = f"{BASE_PROMPT.rstrip()}\n\n{body}\n" if body else BASE_PROMPT
	tools = [dict(spec, tool_id=tool_id, connector=None)
	         for tool_id, spec in tool_catalog(BASE_SCHEMA).items()]

	return {
		"agent_id": AGENT_ID,
		"agent_name": AGENT_NAME,
		"description": meta.get("description") or DEFAULT_DESCRIPTION,
		"icon": AGENT_ICON,
		# Reached through this app's run-agent desk page and the Agents hub.
		"page": None,
		# No settings DocType: the agent stores no credentials.
		"settings_doctype": None,
		"model": meta.get("model") or DEFAULT_MODEL,
		"max_turns": DEFAULT_MAX_TURNS,
		"system_prompt": prompt,
		"output_format": "JSON",
		"output_schema": BASE_SCHEMA,
		"tools": tools,
		# A consequence of the tools, not a separate declaration.
		"input_options": [t["input_option"] for t in tools if t.get("input_option")],
		# Not registry fields; useful to whoever is debugging why a prompt looks the
		# way it does.
		"override_app": app,
	}
