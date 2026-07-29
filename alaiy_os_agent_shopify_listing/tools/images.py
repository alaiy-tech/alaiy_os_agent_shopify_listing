# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
Image plumbing shared by the agent's two image tools.

Nothing here is a tool: these are the primitives the image tools are built out of. The base app deliberately ships NO image tool of its own — producing
imagery always means a third-party service with its own credentials and its own
notion of what "the image step" is (The Solist generates editorial shots; naya
translates the Chinese text printed on supplier photos). That belongs in the
customer app. What is genuinely common is everything around it: turning a photo
into something the model can see, and re-hosting a result so it survives.

Names here are public (no leading underscore) because the tool modules import them.
"""

import base64
import os

import frappe

# Anthropic vision accepts JPEG, PNG, GIF, WEBP.
MEDIA_TYPES = {
	".jpg": "image/jpeg",
	".jpeg": "image/jpeg",
	".png": "image/png",
	".gif": "image/gif",
	".webp": "image/webp",
}
MEDIA_TYPES_BY_MIME = {v: k for k, v in MEDIA_TYPES.items()}

# Some product-photo CDNs block requests with no browser-like User-Agent
# (confirmed: Anthropic's own url-source fetch got refused on one such CDN) — so
# we always fetch external images ourselves rather than passing a bare URL for
# the model to fetch.
FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AlaiyOS-ShopifyListing/1.0)"}


def media_type(path_or_name):
	"""Guess an image media type from a filename or URL extension, or None."""
	ext = os.path.splitext(path_or_name or "")[1].lower()
	return MEDIA_TYPES.get(ext)


def image_block_from_file(file_name):
	"""Build a base64 Anthropic image block from a File docname, or None."""
	mime = media_type(file_name)
	try:
		file_doc = frappe.get_doc("File", file_name)
		mime = mime or media_type(file_doc.file_name or file_doc.file_url)
		if not mime:
			return None
		content = file_doc.get_content()  # bytes for a binary/image file
		if isinstance(content, str):
			content = content.encode("utf-8", "ignore")
		return {
			"type": "image",
			"source": {
				"type": "base64",
				"media_type": mime,
				"data": base64.b64encode(content).decode("ascii"),
			},
		}
	except Exception:
		return None


def fetch_image_bytes(image_url):
	"""Download an external image URL ourselves. Returns (bytes, media_type)."""
	import requests

	resp = requests.get(image_url, timeout=30, headers=FETCH_HEADERS)
	resp.raise_for_status()
	mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
	if not mime or not mime.startswith("image/"):
		mime = media_type(image_url) or "image/jpeg"
	return resp.content, mime


def fetch_image_block(image_url):
	"""Build a base64 Anthropic image block from an external image URL."""
	content, mime = fetch_image_bytes(image_url)
	return {
		"type": "image",
		"source": {
			"type": "base64",
			"media_type": mime,
			"data": base64.b64encode(content).decode("ascii"),
		},
	}


def image_block_from_url(url):
	"""
	Build a vision block from an image URL stored on a listing row: resolve a
	local /files or /private/files URL to its File doc, otherwise fetch an
	external http(s) URL directly. Returns None if it cannot be read.
	"""
	if not url:
		return None
	file_name = frappe.db.get_value("File", {"file_url": url}, "name")
	if file_name:
		return image_block_from_file(file_name)
	if url.startswith("http"):
		try:
			return fetch_image_block(url)
		except Exception:
			return None
	return None


def reference_source(url):
	"""
	An Anthropic-style image `source` (base64) for a reference photo, resolving
	both a stored Frappe File url (e.g. '/files/x.jpg', which is not
	HTTP-fetchable on its own) by reading the File directly, and an external
	http(s) url by downloading it. Used to ground an image call in the real
	product photo.
	"""
	file_name = frappe.db.get_value("File", {"file_url": url}, "name")
	if file_name:
		block = image_block_from_file(file_name)
		if block:
			return block["source"]
	return fetch_image_block(url)["source"]


def reference_data_uri(url):
	"""`reference_source` as a data: URI, the form most image APIs want."""
	source = reference_source(url)
	return f"data:{source['media_type']};base64,{source['data']}"


def public_image_url(url):
	"""
	An absolute URL a third-party service can fetch for itself.

	Supplier CDN photos are already absolute and pass straight through. A photo
	stored as a local Frappe File is only a site-relative path ('/files/x.jpg'),
	so we expand it against the site URL; that only actually resolves when the
	site is reachable from the public internet, which is why a local/dev site
	will fail for any service that fetches the image itself.
	"""
	if url.startswith("http://") or url.startswith("https://"):
		return url
	return frappe.utils.get_url(url)


def save_public_image(prefix, content, mime, default_ext=".png"):
	"""
	Store image bytes as a standalone public File and return its file_url.

	Standalone (attached to no doctype) on purpose: an image a run produced shows
	up in that run's own output instead of mutating the product it came from, and
	the original photo is never overwritten — so a bad result is always
	recoverable.
	"""
	from frappe.utils.file_manager import save_file

	ext = MEDIA_TYPES_BY_MIME.get(mime, default_ext)
	file_name = f"{prefix}-{frappe.generate_hash(length=8)}{ext}"
	return save_file(file_name, content, None, None, is_private=0).file_url
