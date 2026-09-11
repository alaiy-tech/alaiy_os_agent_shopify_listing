# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
The house studio finish: the part of a listing photo that must NOT be left to a model.

A retouched photograph and a catalog photograph are different things. The retouch —
even lighting, true colour, no dust — is judgement about a specific piece, and an
image model is good at it. The house look is the opposite: an exact background hex,
the same margin on every product, the same shadow at the same angle on all of them.
Ask a model for `#f4f4f4` and you get a grey near it, a different grey on the next
photo, and a margin that drifts with whatever it thought the composition wanted. So
the model is asked for the product on an empty white ground and nothing else, and
everything measurable is composited here, in code.

Two things live in this module:

  * `load()` — the style spec, contributed by the client app through the
    `listing_image_style` hook, exactly the way the attribute matrix arrives (see
    matrix.py). A look belongs to the store, not to this app: The Solist's catalog
    is light grey with a hard, light shadow; another client's is not, and a base app
    that hardcodes one customer's brand guidelines is a base app no one else can
    install. No provider means no finish, which is this app's behaviour to date.

  * `apply_finish()` — the compositor. Free of Frappe, so it can run on a worker
    thread; pure Pillow apart from the matte.

How the product is separated from its background is the one real choice in here,
and there are two ways:

  * `segment` — a segmentation model (rembg / ISNet) computes an alpha mask. It
    reads the photo and outputs an opacity per pixel; it does not draw anything.
    The product's own pixels are carried through untouched, which is the whole
    reason this is the default: the background is altered and the product is not,
    by construction rather than by asking a generative model nicely.
  * `flood` — fill inward from the frame edge over near-white pixels. No model, no
    dependency, and no cost, but it only works on a photo that is ALREADY on a
    clean, even, pale ground. Kept for exactly that case.

Either way the compositor refuses rather than guesses: if what comes back is not a
believable separation, the original image is returned with a note a reviewer can
read. A mangled photo of a $40k watch is worse than an unfinished one.
"""

import io
import threading

import frappe
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

HOOK = "listing_image_style"

# A site can tune the look without a code change — same idea as `listing_image_queue`
# in image_stage.py. Merged OVER whatever the client app's hook returns, so a bench
# can nudge padding or shadow for one store without editing that store's app.
CONF_KEY = "listing_image_style"

# Everything but the background has a working default: a client contributing a look
# has an opinion about the colour, and usually not about blur radii.
DEFAULTS = {
	# The ground every product is composited onto.
	"background": None,
	# How the product is separated from its background: "segment" (a model
	# computes an alpha mask and the product's pixels are untouched) or "flood"
	# (fill in from the frame edge; needs an already-clean pale background).
	"matte": "segment",
	# Which segmentation model, when matte is "segment". ISNet over rembg's u2net
	# default on the strength of the catalog it will actually see: u2net erases a
	# bag's chain strap and a watch bracelet almost entirely, which for a jewelry
	# and accessories catalog is the common case, not the corner case. ISNet keeps
	# them link for link at roughly twice the (still sub-two-second) cost.
	"segment_model": "isnet-general-use",
	# Whether the photo is sent through a generative retouch BEFORE it is
	# composited. Off means nothing regenerates the product: the original
	# photograph's pixels are what get composited, and only the ground changes.
	"retouch": False,
	# Minimum clear margin on each side, as a fraction of the canvas. A MINIMUM, not
	# a target — see `apply_finish` on why the product is never scaled up to meet it.
	"padding": 0.06,
	# Canvas width / height. 1.0 is square.
	"aspect": 1.0,
	# Longest edge of the finished image. Only ever scales a product DOWN.
	"max_size": 2048,
	# "Hard, but light": a small blur so the edge stays defined, a low opacity so it
	# reads as contact with a surface rather than as a second object, and a short
	# drop. Not a reflection — there is no mirrored copy anywhere in this module.
	"shadow": {
		"offset": 0.025,
		"blur": 0.012,
		"opacity": 0.15,
		"color": "#000000",
	},
	# Keep the transparent cutout as its own file, so the background can be changed
	# later without paying for the photo again.
	"keep_cutout": True,
}

_UNSET = object()

# Built lazily by _session and then reused for the life of the worker.
_SESSIONS = {}
_SESSION_LOCK = threading.Lock()

# ── what "the model did as it was told" means, in numbers ────────────────────

# How wide a ring around the edge counts as "the background", as a fraction of the
# shorter side. Wide enough to catch a gradient, narrow enough that a product
# running to the edge of a tight crop does not dominate it.
_BORDER_FRACTION = 0.04

# The ring has to be pale and it has to be flat. A ground with a gradient, a
# vignette or a visible surface has a standard deviation well above this; a clean
# white sweep sits near zero.
_BORDER_MIN_MEAN = 225
_BORDER_MAX_STDEV = 12

# How far from a corner's own colour the fill is allowed to travel. Generous enough
# to cross the slight unevenness of a real render, tight enough to stop at the
# edge of anything that is actually the product.
_FILL_TOLERANCE = 36

# A pixel counts as "filled" if the flood moved it at all. Compared against the
# untouched original rather than against the fill colour, so a product that happens
# to contain the sentinel colour is not punched full of holes.
_FILL_DELTA = 8
_FILL_COLOR = (255, 0, 255)

# What fraction of the frame the product may occupy for the result to be believable.
# Below the floor the fill ate the piece; above the ceiling it found no background
# at all and we would be "cutting out" the whole frame. The floor is very low on
# purpose: it is there to catch a flood that swallowed the product (which leaves
# essentially zero), NOT to insist a product be large in its frame — a stud shot at
# the same distance as a cuff is supposed to occupy very little of it.
_MIN_SUBJECT = 0.001
_MAX_SUBJECT = 0.98

# How much variation the selected region must contain to be a product at all —
# see _featureless. Very low: this separates "a patch of flat background" from any
# real photograph of an object, and is not a judgement about the object.
# Calibrated against the catalog rather than guessed: over a sample of real
# product photos the flattest subject measured 10.5 and most sit between 20 and
# 60, while a hallucinated blob on a blank frame is 0. This sits ~7x under the
# worst real photo and well clear of the failure it is for.
_MIN_SUBJECT_DETAIL = 1.5

# Softens the one-pixel staircase the flood leaves behind. Deliberately under a
# pixel: any more and a bright metal edge starts to glow against the grey.
_EDGE_FEATHER = 0.7


# ── the spec ─────────────────────────────────────────────────────────────────


def load():
	"""
	The house style for this site, or None when no client app contributes one.

	Validated once per request and cached on `frappe.local`, like matrix.load().
	None is a first-class answer: it means "finish nothing", which is what every
	site did before this module existed and what a site with no brand guidelines
	should keep doing.
	"""
	cached = getattr(frappe.local, "_listing_image_style", _UNSET)
	if cached is not _UNSET:
		return cached

	spec = _build()
	frappe.local._listing_image_style = spec
	return spec


def _build():
	providers = frappe.get_hooks(HOOK) or []
	override = frappe.conf.get(CONF_KEY) or {}

	if not providers and not override:
		return None

	if len(providers) > 1:
		# Two looks would silently merge into one, and the losing store's catalog
		# would quietly start publishing in another store's brand colours.
		frappe.throw(
			f"More than one app provides {HOOK}: {providers}. A site has one "
			"store and one house style, so leave only the customer app whose "
			"store this site is."
		)

	spec = dict(DEFAULTS)
	if providers:
		spec.update(frappe.get_attr(providers[0])() or {})
	if override:
		# Shadow is merged a level deeper: a site tuning the opacity should not
		# have to restate the offset, blur and colour to keep them.
		shadow = dict(spec["shadow"], **(override.get("shadow") or {}))
		spec.update(override)
		spec["shadow"] = shadow

	if not spec.get("background"):
		source = providers[0] if providers else f"site_config {CONF_KEY}"
		frappe.throw(f"{HOOK} ({source}) does not name a background colour.")

	return spec


# ── the compositor ───────────────────────────────────────────────────────────


def apply_finish(content, spec):
	"""
	Put one rendered photo onto the house ground. Returns

	    {"image": bytes, "mime": str, "cutout": bytes|None, "note": str|None}

	`content` is what the image service returned; `spec` is `load()`'s. Touches no
	Frappe at all — it runs on a worker thread beside the render (see
	image_generation._try_generate), where there is no site context to read.

	Never raises and never approximates. If the render did not come back on a clean
	empty ground, `image` is `content` unchanged and `note` says the finish was
	skipped, because a bad cutout on a luxury piece is worse than an unfinished
	photograph — a halo or a bitten-off clasp is a misrepresentation, while a plain
	white background is merely off-brand.
	"""
	try:
		return _finish(content, spec)
	except Exception as exc:
		return _skipped(content, f"could not be processed ({exc})")


def _finish(content, spec):
	image = Image.open(io.BytesIO(content))
	image.load()
	image = image.convert("RGB")

	if (spec.get("matte") or DEFAULTS["matte"]) == "segment":
		alpha = _segment_alpha(image, spec.get("segment_model") or DEFAULTS["segment_model"])
	else:
		# The flood needs the ground to already be clean; the model does not.
		uneven = _ground_complaint(image)
		if uneven:
			return _skipped(content, uneven)
		alpha = _subject_alpha(image)

	subject = _coverage(alpha)
	if subject < _MIN_SUBJECT:
		return _skipped(content, "almost nothing was left after separating the product")
	if subject > _MAX_SUBJECT:
		return _skipped(content, "no background could be separated from the product")
	if _featureless(image, alpha):
		return _skipped(content, "no product could be made out in the photo")

	box = alpha.getbbox()
	if not box:
		return _skipped(content, "no product could be separated from the background")

	cutout = image.convert("RGBA")
	cutout.putalpha(alpha)
	cutout = cutout.crop(box)

	return {
		"image": _encode(_compose(cutout, image.size, spec)),
		"mime": "image/png",
		"cutout": _encode(cutout) if spec.get("keep_cutout") else None,
		"note": None,
	}


def _segment_alpha(image, model):
	"""An opacity mask for the product, from a segmentation model.

	The model is only ever asked for the MASK — `only_mask=True`. It never gets to
	compose or repaint anything, so whatever it decides about the edges, the pixels
	that survive are the photograph's own. That is the property that makes this the
	default: the background changes and the product cannot.

	Alpha matting is deliberately off. rembg can refine the edge with it, at rather
	more than double the time, and on this catalog's photography it made no visible
	difference to the thing it would be for — a chain strap against white.
	"""
	from rembg import remove

	return remove(image, session=_session(model), only_mask=True, post_process_mask=True)


def _session(model):
	"""One rembg session per model, built once and shared.

	Building a session loads a ~180MB ONNX graph, which is far too expensive to do
	per photo, and the pool renders several photos at once. onnxruntime releases
	the GIL and its `Run` is safe to call from several threads, so the session is
	shared rather than made thread-local; only the building is serialised, so two
	threads arriving together cannot both pay for it.

	The model file itself is downloaded to ~/.rembg on first use. On a fresh bench
	that makes the very first photo slow rather than broken — see the README.
	"""
	with _SESSION_LOCK:
		if model not in _SESSIONS:
			from rembg import new_session

			_SESSIONS[model] = new_session(model)
		return _SESSIONS[model]


def _ground_complaint(image):
	"""Why this photo's edge is not the empty ground we asked for, or None.

	Only the border ring is judged. What is inside the frame is the product's
	business — a dark dial or a black strap says nothing about whether the ground
	behind it is clean.
	"""
	width, height = image.size
	ring = max(1, int(min(width, height) * _BORDER_FRACTION))
	if width <= ring * 2 or height <= ring * 2:
		return "the rendered photo is too small to finish"

	# The ring, laid out as one strip: the two full-width bands plus what is left
	# of the sides between them.
	mask = Image.new("L", image.size, 0)
	draw = ImageDraw.Draw(mask)
	draw.rectangle([0, 0, width - 1, height - 1], fill=255)
	draw.rectangle([ring, ring, width - 1 - ring, height - 1 - ring], fill=0)

	stat = ImageStat.Stat(image, mask)
	if min(stat.mean) < _BORDER_MIN_MEAN:
		return "the rendered photo did not come back on a plain light background"
	if max(stat.stddev) > _BORDER_MAX_STDEV:
		return "the background of the rendered photo was not even enough to separate"
	return None


def _subject_alpha(image):
	"""An opacity mask for the product: the frame minus the ground touching its edge.

	Flooded inward from the four corners rather than keyed on a colour. A colour key
	is the obvious implementation and the wrong one for this catalog: white is not
	only the background here, it is also a pearl, the table of a diamond and the
	specular highlight down a polished band, and keying would punch every one of
	them out of the middle of the product. A flood only ever reaches background that
	is connected to the edge of the frame, so an enclosed white stays.
	"""
	flooded = image.copy()
	width, height = flooded.size
	for corner in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
		ImageDraw.floodfill(flooded, corner, _FILL_COLOR, thresh=_FILL_TOLERANCE)

	# "Filled" is "this pixel moved", not "this pixel is now magenta" — so a product
	# that genuinely contains the sentinel colour survives.
	moved = ImageChops.difference(flooded, image).convert("L")
	alpha = moved.point(lambda v: 0 if v > _FILL_DELTA else 255)
	return alpha.filter(ImageFilter.GaussianBlur(_EDGE_FEATHER))


def _featureless(image, alpha):
	"""True when what was selected has no detail in it, and so is not a product.

	Handed a blank or near-blank frame, a segmentation model does not answer "there
	is nothing here" — it invents a subject, and reliably the same one: on a plain
	white frame ISNet returns a confident blob over about 6% of the image, in the
	same place every time. Coverage bounds cannot catch that, because 6% is a
	perfectly ordinary size for a stud earring.

	What separates the two is texture. A real piece has structure inside its
	outline — an edge, a highlight, a change of tone somewhere. A hallucinated blob
	is a patch of the flat background it was cut from, and its standard deviation
	is essentially zero. So the selection is measured, not its size.
	"""
	mask = alpha.point(lambda v: 255 if v > 128 else 0)
	if not mask.getbbox():
		return True
	return max(ImageStat.Stat(image, mask).stddev) < _MIN_SUBJECT_DETAIL


def _coverage(alpha):
	"""What fraction of the frame the product occupies, 0..1."""
	width, height = alpha.size
	return ImageStat.Stat(alpha).sum[0] / (255.0 * width * height)


def _compose(cutout, frame, spec):
	"""The cutout on the house ground, with its margin and its shadow.

	`frame` is the size of the photo the cutout came out of, and it matters as much
	as the cutout does — see below.
	"""
	shadow = dict(DEFAULTS["shadow"], **(spec.get("shadow") or {}))
	padding = float(spec.get("padding", DEFAULTS["padding"]))
	aspect = float(spec.get("aspect") or DEFAULTS["aspect"])
	max_size = int(spec.get("max_size") or DEFAULTS["max_size"])

	product = cutout
	width, height = product.size

	# The product is never resized to fit the canvas; the canvas is grown around the
	# product. That is the whole difference between a catalog where a stud reads as
	# small and one where every piece is blown up to the same width — fitting each
	# cutout to a fixed frame would put a 4mm earring and a 60mm hoop on the page at
	# the same size, which the guidelines explicitly rule out.
	#
	# So the canvas starts at the size of the photo the product was shot in, which is
	# what carries that relative scale, and only grows when the product sits closer to
	# an edge than the margin allows. A photo that already has room keeps its own
	# framing and its own resolution. Padding is a FLOOR on the empty space, never a
	# target, and the only resize in this whole module is downward, for max_size.
	usable = max(1.0 - 2.0 * padding, 0.05)
	frame_w, frame_h = frame
	canvas_h = max(frame_h, frame_w / aspect, height / usable, width / (usable * aspect))
	canvas_w = canvas_h * aspect

	longest = max(canvas_w, canvas_h)
	if longest > max_size:
		scale = max_size / longest
		canvas_w *= scale
		canvas_h *= scale
		product = product.resize(
			(max(1, round(width * scale)), max(1, round(height * scale))),
			Image.LANCZOS,
		)

	canvas_w, canvas_h = max(1, round(canvas_w)), max(1, round(canvas_h))
	width, height = product.size
	left = (canvas_w - width) // 2
	top = (canvas_h - height) // 2

	base = Image.new("RGB", (canvas_w, canvas_h), _rgb(spec["background"]))
	base = _cast_shadow(base, product, (left, top), shadow)
	base.paste(product, (left, top), product)
	return base


def _cast_shadow(base, product, position, shadow):
	"""Drop the product's own silhouette onto the ground, below it.

	One offset copy of the alpha, blurred a little and held down to a low opacity —
	"hard, but light". There is deliberately no mirrored, fading second copy: the
	guidelines ask for a shadow and rule out a reflection, and the two are easy to
	conflate when reaching for a stock "product on a surface" effect.
	"""
	opacity = float(shadow.get("opacity") or 0)
	if opacity <= 0:
		return base

	canvas_w, canvas_h = base.size
	left, top = position
	drop = round(canvas_h * float(shadow.get("offset") or 0))
	blur = canvas_h * float(shadow.get("blur") or 0)

	mask = Image.new("L", base.size, 0)
	mask.paste(product.getchannel("A"), (left, top + drop))
	if blur > 0:
		mask = mask.filter(ImageFilter.GaussianBlur(blur))
	mask = mask.point(lambda v: round(v * opacity))

	base.paste(Image.new("RGB", base.size, _rgb(shadow.get("color") or "#000000")), (0, 0), mask)
	return base


def _rgb(color):
	"""`#rrggbb` (or any Pillow colour name) as an (r, g, b) tuple."""
	if isinstance(color, list | tuple):
		return tuple(color[:3])
	from PIL import ImageColor

	return ImageColor.getrgb(color)


def _encode(image):
	buffer = io.BytesIO()
	# PNG, not JPEG: the ground is a single flat tone and the product edge sits
	# right against it, which is exactly where JPEG puts its worst ringing — on a
	# catalog whose whole point is zooming into a stone.
	image.save(buffer, format="PNG", optimize=True)
	return buffer.getvalue()


def _skipped(content, why):
	"""The original render, with an account of why it was not finished."""
	return {
		"image": content,
		"mime": None,
		"cutout": None,
		"note": f"Studio finish skipped: {why}.",
	}
