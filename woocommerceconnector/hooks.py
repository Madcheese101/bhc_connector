# -*- coding: utf-8 -*-
from __future__ import unicode_literals
from . import __version__ as app_version

app_name = "woocommerceconnector"
app_title = "WooCommerce Connector"
app_publisher = "libracore"
app_description = "WooCommerce Connector for ERPNext"
app_icon = "fa fa-wordpress"
app_color = "#bc3bff"
app_email = "info@libracore.com"
app_license = "AGPL"
app_url = "https://github.com/libracore/woocommerceconnector"

# fixtures = ["Custom Field"]
fixtures =[   {
        "doctype": "Custom Field",
        "filters": [
            [
                "name",
                "in",
                (
                    "Print Settings-compact_item_print",
                    "Customer-woocommerce_customer_id",
                    "Customer-sync_with_woocommerce", 
                    "Supplier-woocommerce_supplier_id", 
                    "Address-woocommerce_address_id",
                    "Address-woocommerce_company_name", 
                    "Sales Order-woocommerce_order_id",
                    "Sales Order-woocommerce_payment_method", 
                    "Sales Invoice-woocommerce_order_id",
                    "Delivery Note-woocommerce_order_id",
                    "Delivery Note-woocommerce_fulfillment_id",  
                    "Item-sync_with_woocommerce", 
                    "Item-sync_qty_with_woocommerce", 
                    "Item-stock_keeping_unit", 
                    "Item-woocommerce_description", 
                    "Item-woocommerce_product_id", 
                    "Item-woocommerce_variant_id", 
                    "Item-custom_woo_sync_as_variant",
                    "Item-custom_image_name",
                    "Item Attribute-custom_sync_with_woocommerce",
                    "Item Attribute-woocommerce_attribute_id",
                    "Item Attribute Value-custom_woocommerce_id",
                    "Item Group-custom_woocommerce_id",
                    "Item Group-custom_sync_with_woocommerce"
                ),
            ]
        ],
    },] 
# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/woocommerceconnector/css/woocommerceconnector.css"
# app_include_js = "/assets/woocommerceconnector/js/woocommerceconnector.js"

# include js, css files in header of web template
# web_include_css = "/assets/woocommerceconnector/css/woocommerceconnector.css"
# web_include_js = "/assets/woocommerceconnector/js/woocommerceconnector.js"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
#	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Installation
# ------------

# before_install = "woocommerceconnector.install.before_install"
after_install = "woocommerceconnector.after_install.create_weight_uom"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "woocommerceconnector.notifications.get_notification_config"

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

doc_events = {
	"Sales Invoice": {
		"on_submit": "woocommerceconnector.sync_products.trigger_update_item_stock",
		"on_cancel": "woocommerceconnector.sync_products.trigger_update_item_stock",
	},
    "Purchase Receipt": {
		"on_submit": "woocommerceconnector.sync_products.trigger_update_item_stock",
		"on_cancel": "woocommerceconnector.sync_products.trigger_update_item_stock",
	}
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"hourly": [
		"woocommerceconnector.api.check_hourly_sync"
	]
}

# Testing
# -------

# before_tests = "woocommerceconnector.install.before_tests"

# Overriding Whitelisted Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "woocommerceconnector.event.get_events"
# }

