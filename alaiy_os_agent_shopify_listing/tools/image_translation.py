# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
The `translate_product_images` tool: supplier photos with their printed Chinese
text rendered into English.

One of the two image steps this app ships. It does not generate new imagery — that
is `generate_product_images`, a deliberately different capability. Both are always
registered; the agent's prompt is what decides which it calls — see agent_meta.py.
Requires `alphashop_ak` / `alphashop_sk` in site_config.json.

Each translated photo is stored as its own public File, so the original supplier
photo is never overwritten and a bad translation is always recoverable.
"""

import time
from datetime import datetime, timedelta, timezone

import frappe

from alaiy_os_agent_shopify_listing.tools import handlers as base
from alaiy_os_agent_shopify_listing.tools import images

# Cap how many photos we push through the paid translation API per product.
_MAX_TRANSLATED_IMAGES = 10

# ── alphashop image translation ───────────────────────────────────────────────
# Chinese text printed on supplier photos → English, via alphashop's
# ai.image.translateImage API. Auth is a short-lived HS256 JWT signed with an
# access key / secret key pair, both read from site_config.json:
#
#   "alphashop_ak": "..."
#   "alphashop_sk": "..."
#   "alphashop_base_url": "https://api.alphashop.cn"   (optional override)
#
# IMPORTANT: this API fetches the image itself from the `imageUrl` we send, so that
# URL must be reachable from alphashop's servers. Supplier CDN URLs (the normal
# case for Nayaglobal, whose images come from Alibaba's CDN) work as-is; a photo
# stored as a local Frappe File only works when the site is publicly reachable —
# see images.public_image_url.
#
# alphashop also offers ai.image.imageObjectExtraction (white-background removal).
# Deliberately not wired up: out of scope for now.
_ALPHASHOP_BASE_URL = "https://api.alphashop.cn"
_ALPHASHOP_TRANSLATE_PATH = "/ai.image.translateImage/1.0"
_ALPHASHOP_AK_KEY = "alphashop_ak"
_ALPHASHOP_SK_KEY = "alphashop_sk"
_ALPHASHOP_BASE_URL_KEY = "alphashop_base_url"

# JWT lifetime, and a little backdating so minor clock skew doesn't reject us.
_JWT_TTL_SECONDS = 1800
_JWT_LEEWAY_SECONDS = 5

# Per-image retry policy for the translate call.
_TRANSLATE_MAX_RETRIES = 3
_TRANSLATE_RETRY_DELAYS = (2, 4, 8)


def _alphashop_headers(ak, sk):
	"""
	Auth headers for alphashop: a short-lived HS256 JWT signed with the secret key,
	issued by the access key. Minted per attempt rather than reused, so a retry after
	a long backoff never sends a token that has since expired.

	PyJWT ships with Frappe, so this adds no dependency.
	"""
	import jwt

	now = datetime.now(timezone.utc)
	token = jwt.encode(
		{
			"iss": ak,
			"exp": now + timedelta(seconds=_JWT_TTL_SECONDS),
			"nbf": now - timedelta(seconds=_JWT_LEEWAY_SECONDS),
		},
		sk,
		algorithm="HS256",
		headers={"alg": "HS256"},
	)
	if isinstance(token, bytes):
		token = token.decode("utf-8")
	return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}


def _alphashop_translate_image(image_url, ak, sk, base_url):
	"""
	One image through alphashop's ai.image.translateImage API. Returns the URL of
	the translated image.

	This is the ONLY place the alphashop request/response contract is expressed. The
	response nests the payload two levels deep — result.result — with the outer
	block carrying retCode/retMsg when the call fails logically despite a 200.
	Retries with exponential backoff; raises once retries are exhausted so the
	caller can record the failure against that one photo.
	"""
	import requests

	endpoint = base_url.rstrip("/") + _ALPHASHOP_TRANSLATE_PATH
	body = {
		"imageUrl": image_url,
		"sourceLanguage": "zh",
		"targetLanguage": "en",
		"includingProductArea": True,
		"useImageEditor": True,
		"translatingBrandInTheProduct": True,
	}

	last_err = None
	for attempt in range(_TRANSLATE_MAX_RETRIES):
		try:
			resp = requests.post(
				endpoint, headers=_alphashop_headers(ak, sk), json=body, timeout=30
			)
			if not (resp.headers.get("Content-Type") or "").startswith("application/json"):
				raise RuntimeError(f"non-JSON response: {resp.text[:200]}")

			data = resp.json()
			result_block = data.get("result")
			if not isinstance(result_block, dict):
				# A null result with a top-level resultCode is how alphashop reports
				# most failures. FAIL_SERVER_INTERNAL_ERROR in particular is what you
				# get when it could not fetch `imageUrl` at all — so say so, since the
				# code alone points at their server rather than at our unreachable URL.
				code = data.get("resultCode") or "unknown"
				hint = ""
				if code == "FAIL_SERVER_INTERNAL_ERROR":
					hint = (
						f" — most often this means alphashop could not fetch the image;"
						f" check that {image_url} is publicly reachable and returns an image"
					)
				raise RuntimeError(f"{code} (requestId={data.get('requestId')}){hint}")

			translated = (result_block.get("result") or {}).get("translatedImageUrl")
			if not translated:
				raise RuntimeError(
					f"no translatedImageUrl (retCode={result_block.get('retCode')}, "
					f"retMsg={result_block.get('retMsg', 'Unknown error')})"
				)
			return translated

		except Exception as exc:
			last_err = exc
			if attempt < _TRANSLATE_MAX_RETRIES - 1:
				time.sleep(_TRANSLATE_RETRY_DELAYS[min(attempt, len(_TRANSLATE_RETRY_DELAYS) - 1)])

	raise RuntimeError(
		f"alphashop translate failed after {_TRANSLATE_MAX_RETRIES} attempts: {last_err}"
	)


def translate_product_images(item_code=None, image_urls=None, translate_images=False):
	"""
	Translate the Chinese text printed on a product's supplier photos into English
	via alphashop's ai.image.translateImage API. Returns
	{"images": [{source_url, url, note}, ...]} — copy that list verbatim into the
	final `images` array.

	Whether we translate at all is decided HERE, deterministically — not left to the
	model's judgement — by two gates:

	  1. There must be at least one photo to translate. For an item_code run those
	     are the listing's own photos (read from the Shopify Product Listing's images
	     table); for a URL-only product they are image_urls. No photos → empty list,
	     nothing done. This gate is airtight: it holds regardless of what the model
	     passes.
	  2. Translation is opt-in per request: translate_images must be true. When
	     photos exist but the toggle is off, we return empty too.

	Per-image failures degrade rather than raise: that entry comes back with
	url=None and a note, and the remaining photos still process. If the service
	isn't configured at all (and both gates pass), the model is told via the thrown
	message not to retry and to fall back to null placeholders instead of stalling
	the rest of the listing.
	"""
	# ── Gate 1 (airtight): resolve the photos; no photos → nothing to do.
	urls = []
	if item_code and frappe.db.exists(base.LISTING_DOCTYPE, item_code):
		urls = base.listing_image_urls(frappe.get_doc(base.LISTING_DOCTYPE, item_code))
	if not urls and image_urls:
		urls = [u for u in image_urls if u]
	if not urls:
		return {
			"images": [],
			"note": "This product has no photos, so nothing was translated.",
		}

	# ── Gate 2: image translation is opt-in per request.
	if not translate_images:
		return {
			"images": [],
			"note": (
				"The product has photos, but the translate_images toggle is off, so "
				"no images were translated."
			),
		}

	ak = frappe.conf.get(_ALPHASHOP_AK_KEY)
	sk = frappe.conf.get(_ALPHASHOP_SK_KEY)
	if not ak or not sk:
		frappe.throw(
			f"Image translation is not configured (set {_ALPHASHOP_AK_KEY} and "
			f"{_ALPHASHOP_SK_KEY} in site_config.json). Do NOT retry; return each "
			"image with url=null so the team can translate it manually."
		)
	base_url = frappe.conf.get(_ALPHASHOP_BASE_URL_KEY) or _ALPHASHOP_BASE_URL

	out = []
	for url in urls[:_MAX_TRANSLATED_IMAGES]:
		try:
			# alphashop fetches the image itself, so hand it an absolute URL.
			translated_url = _alphashop_translate_image(
				images.public_image_url(url), ak, sk, base_url
			)
			# Re-host the result: alphashop's URL is theirs and may expire, and we want
			# the reviewed listing to keep working regardless.
			out_bytes, out_media_type = images.fetch_image_bytes(translated_url)
			hosted = images.save_public_image(
				"listing-translated", out_bytes, out_media_type, default_ext=".jpg"
			)
			out.append({"source_url": url, "url": hosted, "note": None})
		except Exception as exc:
			# One bad photo must not sink the rest of the enrichment.
			frappe.log_error(
				title="Shopify listing: image translation failed",
				message=f"{url}\n{frappe.get_traceback()}",
			)
			out.append({
				"source_url": url,
				"url": None,
				"note": f"Translation failed: {exc}"[:200],
			})

	result = {"images": out}
	skipped = len(urls) - len(out)
	if skipped > 0:
		result["note"] = (
			f"{skipped} further photo(s) were not translated: this product has more "
			f"than the {_MAX_TRANSLATED_IMAGES}-photo per-run cap. Record this in notes."
		)
	return result
