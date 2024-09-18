from __future__ import unicode_literals
import frappe
from frappe import _
import requests.exceptions
from .woocommerce_requests import get_woocommerce_customers, post_request, put_request
from .utils import make_woocommerce_log

def sync_customers():
    woocommerce_customer_list = []
    sync_woocommerce_customers(woocommerce_customer_list)
    frappe.local.form_dict.count_dict["customers"] = len(woocommerce_customer_list)

def sync_woocommerce_customers(woocommerce_customer_list):
    for woocommerce_customer in get_woocommerce_customers():
        # import new customer or update existing customer
        if not frappe.db.get_value("Customer", {"woocommerce_customer_id": woocommerce_customer.get('id')}, "name"):
            #only synch customers with address
            if woocommerce_customer.get("billing").get("address_1") != "" and woocommerce_customer.get("shipping").get("address_1") != "":
                create_customer(woocommerce_customer, woocommerce_customer_list)
            # else:
            #    make_woocommerce_log(title="customer without address", status="Error", method="create_customer",
            #        message= "customer without address found",request_data=woocommerce_customer, exception=False)
        else:
            update_customer(woocommerce_customer)

def update_customer(woocommerce_customer):
    return

def create_customer(order_billing, order_shipping, customer_id,woocommerce_customer_list=[]):
    import frappe.utils.nestedset

    woocommerce_settings = frappe.get_doc("WooCommerce Config", "WooCommerce Config")
    
    cust_name = order_billing.get("first_name") + " " + order_billing.get("last_name") 
        
    try:
        # try to match territory
        country_name = get_country_name(order_billing.get("country"))
        if frappe.db.exists("Territory", country_name):
            territory = country_name
        else:
            territory = frappe.utils.nestedset.get_root_of("Territory")
        
        customer = frappe.get_doc({
            "doctype": "Customer",
            "customer_name" : cust_name,
            "woocommerce_customer_id": customer_id,
            "sync_with_woocommerce": 0,
            "customer_group": woocommerce_settings.customer_group,
            "territory": territory,
            "mobile_no": order_billing.get("phone"),
            "email_id": order_billing.get("email"),
            "customer_type": _("Individual")
        })
        customer.flags.ignore_mandatory = True
        customer.insert()
        
        if customer:
            customer_shipping_address = create_customer_address("Shipping", order_shipping, customer.name)
            customer_billing_address = create_customer_address("Billing", order_shipping, customer.name)
            customer_contact = create_customer_contact(customer.name, order_billing)
    
        woocommerce_customer_list.append(customer_id)
        frappe.db.commit()
        make_woocommerce_log(title="create customer", status="Success", method="create_customer",
            message= "create customer",request_data=order_billing, exception=False)
        
        return customer.name, customer_shipping_address, customer_billing_address, customer_contact
    
    except Exception as e:
        if e.args[0] and e.args[0].startswith("402"):
            raise e
        else:
            make_woocommerce_log(title=e, status="Error", method="create_customer", message=frappe.get_traceback(),
                request_data=order_billing, exception=True)
        return None, None, None
        
def create_customer_address(type, address_details, customer):
    try :    
        address_name = frappe.db.get_value(
        "Address", 
        {
            "woocommerce_address_id": type,
            "address_line1": address_details.get("address_1"),
            "address_line2": address_details.get("address_2"),
        }, 
        "name")

        if not address_name:
            country = get_country_name(address_details.get("country"))
            address_doc = frappe.get_doc({
                "doctype": "Address",
                "woocommerce_address_id": type,
                "address_title": customer,
                "address_type": type,
                "address_line1": address_details.get("address_1") or "Address 1",
                "address_line2": address_details.get("address_2"),
                "city": address_details.get("city") or "City",
                "state": address_details.get("state"),
                "pincode": address_details.get("postcode"),
                "country": country,
                "phone": address_details.get("phone"),
                "email_id": address_details.get("email"),
            })
            address_doc.links = []
            address_doc.links.append({
                    "link_doctype": "Customer",
                    "link_name": customer
                })
            address_name = address_doc.name
        return address_name
    except Exception as e:
        make_woocommerce_log(title=e, status="Error", method="create_customer_address", message=frappe.get_traceback(),
                request_data=address_details, exception=True)
        return None

def create_customer_contact(customer, order_billing):
    try :
        customer_contact = frappe.db.get_value(
        "Contact", 
        {
            "first_name": order_billing.get("first_name"),
            "last_name": order_billing.get("last_name"),
            "mobile_no": order_billing.get("phone")
        }, 
        "name")
        if not customer_contact:
            customer_contact = frappe.new_doc("Contact")
            customer_contact.first_name = order_billing["first_name"]
            customer_contact.last_name = order_billing["last_name"]
            if order_billing["email"]:
                customer_contact.email_ids = []
                customer_contact.email_ids.append({
                    "email_id": order_billing["email"],
                    "is_primary": 1
                })
            customer_contact.phone_nos = [{
                "phone": order_billing["phone"],
                "is_primary_phone": 1
            }]
            customer_contact.links = []
            customer_contact.links.append({
                "link_doctype": "Customer",
                "link_name": customer
            })
            customer_contact.is_primary_contact = 1
            customer_contact.is_billing_contact = 1
            customer_contact.save()
        return customer_contact

    except Exception as e:
        make_woocommerce_log(title=e, status="Error", method="create_customer_contact", message=frappe.get_traceback(),
                request_data=order_billing, exception=True)
        return None

def get_country_name(code):
    country_name = frappe.db.get_value("Country", {"code": code.lower()}, "country_name")
    return country_name
