from __future__ import unicode_literals
import frappe
from frappe import _
from .exceptions import woocommerceError
from .utils import make_woocommerce_log
from .sync_customers import create_customer, create_customer_address, create_customer_contact, get_country_name
from frappe.utils import flt, nowdate, cint
from .woocommerce_requests import get_request, get_woocommerce_orders, get_woocommerce_tax, get_woocommerce_customer, put_request
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note, make_sales_invoice
import requests.exceptions
import requests


def sync_orders():
    sync_woocommerce_orders()

def sync_woocommerce_orders():
    frappe.local.form_dict.count_dict["orders"] = 0
    woocommerce_settings = frappe.get_doc("WooCommerce Config", "WooCommerce Config")
    woocommerce_order_status_for_import = get_woocommerce_order_status_for_import()
    synced_orders = frappe.get_list("Sales Order", 
                                    filters={"woocommerce_order_id": ["not in", [None,""]]},
                                    pluck="woocommerce_order_id")

    for woocommerce_order_status in woocommerce_order_status_for_import:
        wc_orders_list = get_woocommerce_orders(woocommerce_order_status)
        for wc_order in wc_orders_list:

            if str(wc_order.get("id")) not in synced_orders:
                if valid_products(wc_order):
                    try:
                        create_order(wc_order, woocommerce_settings)
                        frappe.local.form_dict.count_dict["orders"] += 1

                    except woocommerceError as e:
                        make_woocommerce_log(status="Error", method="sync_woocommerce_orders", message=frappe.get_traceback(),
                            request_data=wc_order, exception=True)
                    except Exception as e:
                        if e.args and e.args[0] and e.args[0].decode("utf-8").startswith("402"):
                            raise e
                        else:
                            make_woocommerce_log(title=e.message, status="Error", method="sync_woocommerce_orders", message=frappe.get_traceback(),
                                request_data=wc_order, exception=True)

def get_woocommerce_order_status_for_import():
    status_list = frappe.get_all("WooCommerce SO Status", pluck="status")
    if not status_list:
        status_list = ["processing"]
    return status_list

def valid_products(wc_order):
    if wc_order.get("status").lower() == "cancelled":
        return False
    warehouse = frappe.get_doc("WooCommerce Config", "WooCommerce Config").warehouse
    
    for item in wc_order.get("line_items"):
        if item.get("product_id"):
            erp_item = frappe.db.get_value("Item",
                        {"woocommerce_product_id": item.get("product_id"),
                         "woocommerce_variant_id": item.get("variation_id")}, 
                        "item_code")
            if not erp_item:
                make_woocommerce_log(title="Item missing in ERPNext!", 
                    status="Error", method="valid_customer_and_product", 
                    message="Item with id {0} is missing in ERPNext! The Order {1} will not be imported! For details of order see below".format(item.get("product_id"), wc_order.get("id")),
                    request_data=wc_order, exception=True)
                return False
        else:
            make_woocommerce_log(title="Item id missing in WooCommerce!", 
                status="Error", method="valid_customer_and_product", 
                message="Item id is missing in WooCommerce! The Order {0} will not be imported! For details of order see below".format(wc_order.get("product_id")),
                request_data=wc_order, exception=True)
            return False

    return True

def get_erp_customer_details(order_billing, order_shipping, order_customer_id):
    try:
        customer_id = int(order_customer_id)
    except:
        customer_id = 0

    # if shipping phone is empty, set it to billing phone
    if not order_shipping.get("phone"):
        order_shipping["phone"] = order_billing.get("phone")

    customer_full_name = order_billing.get("first_name") + " " + order_billing.get("last_name")
    erp_customer = frappe.db.get_value(
        "Customer",
        {"mobile_no": order_billing.get("phone"), "customer_name": customer_full_name},
        ["name"]
    )

    if erp_customer and customer_id > 0:
        customer = frappe.get_doc("Customer", erp_customer)
        customer.woocommerce_customer_id = customer_id
        customer.save()

        # if billing or shipping are not in erp, create them and return their name
        billing_address = create_customer_address("Billing", order_billing, customer.name)
        shipping_address = create_customer_address("Shipping", order_shipping, customer.name)
        customer_contact = create_customer_contact(customer.name, order_billing)

        frappe.db.commit()
    elif erp_customer:
        # if billing or shipping are not in erp, create them and return their name
        billing_address = create_customer_address("Billing", order_billing, erp_customer)
        shipping_address = create_customer_address("Shipping", order_shipping, erp_customer)
        customer_contact = create_customer_contact(erp_customer, order_billing)
    else:
        customer, shipping_address, billing_address, customer_contact = create_customer(order_billing,
                                                                                        order_shipping,
                                                                                        customer_id)
        erp_customer = customer
    return erp_customer, shipping_address, billing_address, customer_contact

def create_order(wc_order, woocommerce_settings, company=None):
    so = create_sales_order(wc_order, woocommerce_settings, company)
    # check if sales invoice should be created
    if cint(woocommerce_settings.sync_sales_invoice) == 1:
        create_sales_invoice(wc_order, woocommerce_settings, so)

    #Fix this -- add shipping stuff
    #if wc_order.get("fulfillments") and cint(woocommerce_settings.sync_delivery_note):
        #create_delivery_note(wc_order, woocommerce_settings, so)

def create_sales_order(wc_order, woocommerce_settings, company=None):
    id = str(wc_order.get("customer_id"))
    customer, shipping, billing, customer_contact = get_erp_customer_details(wc_order.get("billing"), 
                                                                            wc_order.get("shipping"), id)
    if customer:
        customer_name = customer
    else:
        frappe.log_error("No customer found. This should never happen.")

    # get applicable tax rule from configuration
    tax_rules = frappe.get_all(
        "WooCommerce Tax Rule", 
        filters={'currency': wc_order.get("currency")}, 
        fields=['tax_rule'])
    if not tax_rules:
        # fallback: currency has no tax rule, try catch-all
        tax_rules = frappe.get_all("WooCommerce Tax Rule", filters={'currency': "%"}, fields=['tax_rule'])
    if tax_rules:
        tax_rules = tax_rules[0]['tax_rule']
    else:
        tax_rules = ""

    so = frappe.new_doc('Sales Order')
    so.naming_series = woocommerce_settings.sales_order_series or "SO-woocommerce-"
    so.order_type = "Sales"
    so.woocommerce_order_id = wc_order.get("id"),
    so.woocommerce_payment_method = wc_order.get("payment_method_title"),
    so.customer = customer_name,
    so.customer_group = woocommerce_settings.customer_group,  # hard code group, as this was missing since v12
    so.delivery_date = nowdate(),
    so.selling_price_list = woocommerce_settings.price_list,
    # so.ignore_pricing_rule = 1,
    so.company = woocommerce_settings.company,
    so.currency = wc_order.get("currency"),
    so.customer_address = billing,
    so.shipping_address_name = shipping,
    so.posting_date = wc_order.get("date_created")[:10]
    so.set_warehouse = woocommerce_settings.warehouse
    total = 0
    for woocommerce_item in wc_order.get("line_items"):
        # item_code = get_item_code(woocommerce_item)
        item_code = woocommerce_item.get("sku")
        so.append("items",{
                "item_code": item_code,
                "rate": flt(woocommerce_item.get("price")),
                "delivery_date": nowdate(),
                "qty": woocommerce_item.get("quantity"),
                "warehouse": woocommerce_settings.warehouse
            })
        total += flt(woocommerce_item.get("price"))
    
    so.append("payment_schedule", {"due_date": nowdate(), 
                                    "payment_amount": total,
                                    "invoice_portion": 100})
    # so.flags.ignore_mandatory = True

    # alle orders in ERP = submitted
    so.save(ignore_permissions=True)
    so.submit()
    #if wc_order.get("status") == "on-hold":
    #    so.save(ignore_permissions=True)
    #elif wc_order.get("status") in ("cancelled", "refunded", "failed"):
    #    so.save(ignore_permissions=True)
    #    so.submit()
    #    so.cancel()
    #else:
    #    so.save(ignore_permissions=True)
    #    so.submit()

    frappe.db.commit()
    make_woocommerce_log(title="create sales order", status="Success", method="create_sales_order",
            message= "create sales_order",request_data=wc_order, exception=False)
    return so

def create_sales_invoice(wc_order, woocommerce_settings, so):
    if not frappe.db.get_value("Sales Invoice", {"woocommerce_order_id": wc_order.get("id")}, "name")\
        and so.docstatus==1 and not so.per_billed:
        si = make_sales_invoice(so.name)
        si.woocommerce_order_id = wc_order.get("id")
        si.naming_series = woocommerce_settings.sales_invoice_series or "SI-woocommerce-"
        si.flags.ignore_mandatory = True
        set_cost_center(si.items, woocommerce_settings.cost_center)
        si.submit()
        if cint(woocommerce_settings.import_payment) == 1:
            make_payment_entry_against_sales_invoice(si, woocommerce_settings)
        frappe.db.commit()

def set_cost_center(items, cost_center):
    for item in items:
        item.cost_center = cost_center

def make_payment_entry_against_sales_invoice(doc, woocommerce_settings):
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
    payment_entry = get_payment_entry(doc.doctype, doc.name, bank_account=woocommerce_settings.cash_bank_account)
    payment_entry.flags.ignore_mandatory = True
    payment_entry.reference_no = doc.name
    payment_entry.reference_date = nowdate()
    payment_entry.submit()

def create_delivery_note(wc_order, woocommerce_settings, so):
    for fulfillment in wc_order.get("fulfillments"):
        if not frappe.db.get_value("Delivery Note", {"woocommerce_fulfillment_id": fulfillment.get("id")}, "name")\
            and so.docstatus==1:
            dn = make_delivery_note(so.name)
            dn.woocommerce_order_id = fulfillment.get("order_id")
            dn.woocommerce_fulfillment_id = fulfillment.get("id")
            dn.naming_series = woocommerce_settings.delivery_note_series or "DN-woocommerce-"
            dn.items = get_fulfillment_items(dn.items, fulfillment.get("line_items"), woocommerce_settings)
            dn.flags.ignore_mandatory = True
            dn.save()
            frappe.db.commit()

def get_fulfillment_items(dn_items, fulfillment_items, woocommerce_settings):

    return [dn_item.update({"qty": item.get("quantity")}) for item in fulfillment_items for dn_item in dn_items\
            if get_item_code(item) == dn_item.item_code]
    
#def get_discounted_amount(order):
    #discounted_amount = flt(order.get("discount_total") or 0)
    #return discounted_amount

def get_order_items(order_items, woocommerce_settings):
    items = []
    # frappe.trhow(str(order_items))
    for woocommerce_item in order_items:
        # item_code = get_item_code(woocommerce_item)
        item_code = woocommerce_item.get("sku")
        items.append({
            "item_code": item_code,
            "rate": woocommerce_item.get("price"),
            "delivery_date": nowdate(),
            "qty": woocommerce_item.get("quantity"),
            "warehouse": woocommerce_settings.warehouse
        })
    return items

def get_item_code(woocommerce_item):
    if cint(woocommerce_item.get("variation_id")) > 0:
        # variation
        item_code = frappe.db.get_value("Item", {"woocommerce_product_id": woocommerce_item.get("variation_id")}, "item_code")
    else:
        # single
        item_code = frappe.db.get_value("Item", {"woocommerce_product_id": woocommerce_item.get("product_id")}, "item_code")

    return item_code

def get_order_taxes(wc_order, woocommerce_settings):
    taxes = []
    for tax in wc_order.get("tax_lines"):
        
        woocommerce_tax = get_woocommerce_tax(tax.get("rate_id"))
        rate = woocommerce_tax.get("rate")
        name = woocommerce_tax.get("name")
        
        taxes.append({
            "charge_type": "Actual",
            "account_head": get_tax_account_head(woocommerce_tax),
            "description": "{0} - {1}%".format(name, rate),
            "rate": rate,
            "tax_amount": flt(tax.get("tax_total") or 0) + flt(tax.get("shipping_tax_total") or 0), 
            "included_in_print_rate": 0,
            "cost_center": woocommerce_settings.cost_center
        })
    # old code with conditional brutto/netto prices
    # taxes.append({
        #     "charge_type": "On Net Total" if wc_order.get("prices_include_tax") else "Actual",
        #     "account_head": get_tax_account_head(woocommerce_tax),
        #     "description": "{0} - {1}%".format(name, rate),
        #     "rate": rate,
        #     "tax_amount": flt(tax.get("tax_total") or 0) + flt(tax.get("shipping_tax_total") or 0), 
        #     "included_in_print_rate": 1 if wc_order.get("prices_include_tax") else 0,
        #     "cost_center": woocommerce_settings.cost_center
        # })
    taxes = update_taxes_with_fee_lines(taxes, wc_order.get("fee_lines"), woocommerce_settings)
    taxes = update_taxes_with_shipping_lines(taxes, wc_order.get("shipping_lines"), woocommerce_settings)

    return taxes

def update_taxes_with_fee_lines(taxes, fee_lines, woocommerce_settings):
    for fee_charge in fee_lines:
        taxes.append({
            "charge_type": "Actual",
            "account_head": woocommerce_settings.fee_account,
            "description": fee_charge["name"],
            "tax_amount": fee_charge["amount"],
            "cost_center": woocommerce_settings.cost_center
        })

    return taxes

def update_taxes_with_shipping_lines(taxes, shipping_lines, woocommerce_settings):
    for shipping_charge in shipping_lines:
        #
        taxes.append({
            "charge_type": "Actual",
            "account_head": get_shipping_account_head(shipping_charge),
            "description": shipping_charge["method_title"],
            "tax_amount": shipping_charge["total"],
            "cost_center": woocommerce_settings.cost_center
        })

    return taxes



def get_shipping_account_head(shipping):
        shipping_title = shipping.get("method_title")
        shipping_account =  frappe.db.get_value("woocommerce Tax Account", 
                                                {"parent": "WooCommerce Config", 
                                                 "woocommerce_tax": shipping_title}, "tax_account")
        
        if not shipping_account:
                frappe.throw("Tax Account not specified for woocommerce shipping method  {0}".format(shipping.get("method_title")))

        return shipping_account


def get_tax_account_head(tax):
    tax_title = tax.get("name").encode("utf-8") or tax.get("method_title").encode("utf-8")

    tax_account =  frappe.db.get_value("woocommerce Tax Account", 
        {"parent": "WooCommerce Config", "woocommerce_tax": tax_title}, "tax_account")

    if not tax_account:
        frappe.throw("Tax Account not specified for woocommerce Tax {0}".format(tax.get("name")))

    return tax_account

def close_synced_woocommerce_orders():
    for wc_order in get_woocommerce_orders():
        if wc_order.get("status").lower() != "cancelled":
            order_data = {
                "status": "completed"
            }
            try:
                put_request("orders/{0}".format(wc_order.get("id")), order_data)
                    
            except requests.exceptions.HTTPError as e:
                make_woocommerce_log(title=e, status="Error", method="close_synced_woocommerce_orders", message=frappe.get_traceback(),
                    request_data=wc_order, exception=True)

def mark_wc_order_completed(doc, method):
    wc_order_id = doc.woocommerce_order_id
    if wc_order_id not in ["", None]:
        order_data = {
            "status": "completed"
        }
        try:
            put_request("orders/{0}".format(wc_order_id), order_data)
                
        except requests.exceptions.HTTPError as e:
            make_woocommerce_log(title=e.message, status="Error", method="close_synced_woocommerce_order", message=frappe.get_traceback(),
                request_data=wc_order_id, exception=True)
