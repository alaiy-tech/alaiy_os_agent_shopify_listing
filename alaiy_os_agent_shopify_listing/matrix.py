"""
The client's mandatory-attribute matrix, as this app sees it.

A listing agent's attribute rules belong to the seller, not to this app: what
"mandatory" means for a luxury watch marketplace is nothing like what it means
for a homeware store. So the rules arrive at runtime from whichever client app is
installed, through the `listing_attribute_matrix` hook, and this module is the
only thing here that knows the hook exists.

    # in the client app's hooks.py
    listing_attribute_matrix = ["<client_app>.agents.attribute_matrix.matrix"]

Why a hook, when the prompt override next door (`agent_meta.find_override`) is
just "does the file exist": that override is prose, and the worst a bad one does
is read oddly. This is executable policy -- it gates a write path and reshapes
the output schema -- so it wants a declared owner and a validated contract, the
way `alaiy_os_agents/agents/listing/channels.py` handles the same kind of
contribution.

Everything here degrades to "no matrix" when no client provides one, so this app
still installs and runs standalone. `match_keys` and `is_placeholder` are the
exception: they are generic text handling, not client policy, and work either
way.
"""

import re

import frappe

HOOK = "listing_attribute_matrix"

# What a matrix has to carry. `resolve` and `mandatory` are dotted paths, so the
# policy -- including which conditionals are decidable -- stays in the client app.
REQUIRED_KEYS = ("category_field", "field_labels", "profiles", "resolve", "mandatory")

_UNSET = object()


def load():
	"""
	The installed matrix, or None. Validated once per request.

	Fails loud on a broken contract rather than silently enforcing nothing: a
	matrix that cannot be read is a mandatory-attribute check that is not
	running, which is exactly the failure this whole module exists to end.
	"""
	cached = getattr(frappe.local, "_listing_attribute_matrix", _UNSET)
	if cached is not _UNSET:
		return cached

	spec = _build()
	frappe.local._listing_attribute_matrix = spec
	return spec


def _build():
	providers = frappe.get_hooks(HOOK) or []
	if not providers:
		return None

	if len(providers) > 1:
		# Two clients' guidelines would silently merge into one site-wide policy.
		frappe.throw(
			f"More than one app provides {HOOK}: {providers}. A site has one "
			"listing agent and one field guideline, so leave only the customer "
			"app whose store this site is."
		)

	spec = frappe.get_attr(providers[0])()

	missing = [key for key in REQUIRED_KEYS if not spec.get(key)]
	if missing:
		frappe.throw(f"{HOOK} ({providers[0]}) is missing: {', '.join(missing)}.")

	labels = spec["field_labels"]
	unlabelled = sorted({
		key
		for profile in spec["profiles"].values()
		for key in list(profile.get("mandatory") or [])
		+ [rule["key"] for rule in (profile.get("conditional") or [])]
		if key not in labels
	})
	if unlabelled:
		# Without a label there is nothing to write into needs_review, so the
		# key would be enforced and then reported as its raw snake_case name.
		frappe.throw(
			f"{HOOK} ({providers[0]}) names attributes with no entry in "
			f"field_labels: {', '.join(unlabelled)}."
		)

	return spec


# ── what the rest of the app asks ─────────────────────────────────────────────


def category_field():
	"""The fieldname the client stores its category in, or None."""
	spec = load()
	return spec and spec.get("category_field")


def category_source_field():
	spec = load()
	return spec and spec.get("category_source_field")


def category_label():
	spec = load()
	return (spec and spec.get("category_field_label")) or "Category"


def profiles():
	"""The allowed category values, in the client's own order."""
	spec = load()
	return list(spec["profiles"]) if spec else []


def label(key):
	spec = load()
	return ((spec or {}).get("field_labels") or {}).get(key, key)


def resolve(category=None, product_type=None, title=None):
	"""The client's profile for this listing, or None when nothing says."""
	spec = load()
	if not spec:
		return None
	return frappe.get_attr(spec["resolve"])(
		category=category, product_type=product_type, title=title
	)


def mandatory(profile, product_type=None):
	"""`[(key, label)]` this profile must fill or flag. Empty when no matrix."""
	spec = load()
	if not spec or not profile:
		return []
	return frappe.get_attr(spec["mandatory"])(profile, product_type=product_type)


# ── generic text handling (no matrix required) ────────────────────────────────

# Values that name the absence of a value. Kept deliberately short: every
# addition is a chance to blank a real answer. A value that trips one of these is
# treated as unfilled and blanked in the child row, which is also what stops it
# reaching Shopify as a live metafield on approval.
_PLACEHOLDERS = (
	"not provided", "not specified", "not available", "not known",
	"not determinable", "not determined", "unable to determine",
	"cannot determine", "could not determine", "unknown", "n a", "tbd",
	"to be determined", "to be confirmed", "manual review", "needs review",
	"see description", "none provided", "no data", "pending",
)


def _words(text):
	return re.findall(r"[a-z0-9]+", (text or "").lower())


def is_placeholder(value):
	"""
	True when a value is a note about the absence of the value.

	Deliberately no length heuristic: `features` and `complications` are
	legitimately long, so "it reads like a sentence" would blank real answers.
	"""
	if not value:
		return False
	joined = " ".join(_words(value))
	return any(marker in joined for marker in _PLACEHOLDERS)


def _contiguous(haystack, needle):
	span = len(needle)
	return any(haystack[i:i + span] == needle for i in range(len(haystack) - span + 1))


def match_keys(lines):
	"""
	The attribute keys a `needs_review` list is talking about.

	The agent writes human-readable labels ("Case Size", sometimes with a reason
	in brackets), so matching them back to keys takes care: a plain substring
	test makes "Dial Color (not visible)" look like the `color` attribute too,
	and then `Color` is reported as covered when it is not. Labels are matched as
	a contiguous run of words and only the LONGEST match on a line is kept, so
	that line yields `dial_color` alone -- and `color`, `size` and `back_type`
	stay correctly unmatched against `dial_color`, `case_size` and `case_back`.
	"""
	labels = ((load() or {}).get("field_labels") or {}).items()
	found = set()

	for line in lines or []:
		# Everything after the first bracket is the agent's reasoning, and it
		# mentions other fields ("Case Size (see Dimensions)").
		words = _words(str(line).split("(")[0])
		if not words:
			continue

		hits = [(key, _words(text)) for key, text in labels]
		hits = [(key, lw) for key, lw in hits if lw and _contiguous(words, lw)]
		found.update(
			key
			for key, lw in hits
			if not any(len(other) > len(lw) and _contiguous(other, lw) for _, other in hits)
		)

	return sorted(found)
