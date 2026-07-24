app_name = "alaiy_os_agent_listing"
app_title = "Alaiy Os Agent Listing"
app_publisher = "Alaiy"
app_description = "Listing enrichment agent for AlaiyOS"
app_email = "mail@alaiy.com"
app_license = "agpl-3.0"

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
# The agent engine, OS Agent Registry and OS Agent Run all live in alaiy_os.
# Add "erpnext" too if this agent's tools read/write ERPNext data (Item, ...).
required_apps = ["alaiy_os", "erpnext"]

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "alaiy_os_agent_listing",
# 		"logo": "/assets/alaiy_os_agent_listing/logo.png",
# 		"title": "Alaiy Os Agent Listing",
# 		"route": "/alaiy_os_agent_listing",
# 		"has_permission": "alaiy_os_agent_listing.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/alaiy_os_agent_listing/css/alaiy_os_agent_listing.css"
# app_include_js = "/assets/alaiy_os_agent_listing/js/alaiy_os_agent_listing.js"

# include js, css files in header of web template
# web_include_css = "/assets/alaiy_os_agent_listing/css/alaiy_os_agent_listing.css"
# web_include_js = "/assets/alaiy_os_agent_listing/js/alaiy_os_agent_listing.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "alaiy_os_agent_listing/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# "Enrich" button on the Item form -> deep-links to the Listing Enrichment
# agent (run-agent page) with the item pre-selected. See public/js/item_enrich.js.
doctype_js = {"Item": "public/js/item_enrich.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "alaiy_os_agent_listing/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "alaiy_os_agent_listing.utils.jinja_methods",
# 	"filters": "alaiy_os_agent_listing.utils.jinja_filters"
# }

# ---------------------------------------------------------------------------
# Installation / migration
# ---------------------------------------------------------------------------
# sync_agent_registry() (re)registers this agent in alaiy_os's OS Agent
# Registry from agent_meta.py. It is idempotent, so it runs on both install
# (agent works immediately) and every migrate (prompt/tool/model edits in
# source are reconciled onto the site).
# sync_agent_sidebar() adds the "Agents" section + "Listing Enrichment" link to
# alaiy_os's OS workspace sidebar. It runs after alaiy_os's own sidebar rebuild
# (this app migrates last, being downstream of alaiy_os), so the addition sticks.
after_install = [
    "alaiy_os_agent_listing.setup.install.sync_agent_registry",
    "alaiy_os_agent_listing.setup.install.sync_agent_sidebar",
]

after_migrate = [
    "alaiy_os_agent_listing.setup.install.sync_agent_registry",
    "alaiy_os_agent_listing.setup.install.sync_agent_sidebar",
]

# ---------------------------------------------------------------------------
# Uninstallation
# ---------------------------------------------------------------------------
# Remove this agent's OS Agent Registry row (OS Agent Run history is kept) and
# its sidebar entry.
before_uninstall = [
    "alaiy_os_agent_listing.setup.install.unregister",
    "alaiy_os_agent_listing.setup.install.unregister_sidebar",
]

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "alaiy_os_agent_listing.utils.before_app_install"
# after_app_install = "alaiy_os_agent_listing.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "alaiy_os_agent_listing.utils.before_app_uninstall"
# after_app_uninstall = "alaiy_os_agent_listing.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "alaiy_os_agent_listing.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "alaiy_os_agent_listing.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"alaiy_os_agent_listing.tasks.all"
# 	],
# 	"daily": [
# 		"alaiy_os_agent_listing.tasks.daily"
# 	],
# 	"hourly": [
# 		"alaiy_os_agent_listing.tasks.hourly"
# 	],
# 	"weekly": [
# 		"alaiy_os_agent_listing.tasks.weekly"
# 	],
# 	"monthly": [
# 		"alaiy_os_agent_listing.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "alaiy_os_agent_listing.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "alaiy_os_agent_listing.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "alaiy_os_agent_listing.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "alaiy_os_agent_listing.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["alaiy_os_agent_listing.utils.before_request"]
# after_request = ["alaiy_os_agent_listing.utils.after_request"]

# Job Events
# ----------
# before_job = ["alaiy_os_agent_listing.utils.before_job"]
# after_job = ["alaiy_os_agent_listing.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"alaiy_os_agent_listing.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

