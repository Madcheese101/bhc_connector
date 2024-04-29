from __future__ import unicode_literals
import frappe
from frappe import _
from frappe.utils import flt, cint, get_url, get_datetime
from frappe.query_builder import DocType

import requests.exceptions, requests
from .exceptions import woocommerceError
from .utils import make_woocommerce_log, disable_woocommerce_sync_for_item
from .woocommerce_requests import (post_request, get_woocommerce_items,
                                   get_woocommerce_item_variants,  
                                   put_request,
                                   get_woocommerce_categories,
                                   get_woocommerce_media)

@frappe.whitelist()
def fix_issues():
    sync_attributes()
    sync_to_erpnext()
    woo_attrs = {i["name"]: int(i["woocommerce_id"]) 
                for i in frappe.get_list("Item Attribute",fields=['name', "woocommerce_id"], filters={"woocommerce_id":["!=",""]})}
    woo_prods = get_woocommerce_items(True)
    for prod in woo_prods:
        old_attr = prod["attributes"]
        prod_id = prod["id"]
        for attr in old_attr:
            if attr["name"] in woo_attrs.keys():
                attr["id"] = woo_attrs[attr["name"]]

        variants = get_woocommerce_item_variants(prod_id)
        for var in variants:
            var_attr = var["attributes"]
            for attr in var_attr:
                if attr["name"] in woo_attrs.keys():
                    attr["id"] = woo_attrs[attr["name"]] 
        edited_prod = put_request(f"products/{prod_id}", {"attributes":old_attr})
        var_result = put_request(f"products/{prod_id}/variations/batch", {"update": variants})
    frappe.msgprint("done")

def sync_products(price_list):
    woocommerce_settings = frappe.get_doc("WooCommerce Config", "WooCommerce Config")
    woocommerce_item_list = []
    # if sync_from_woocommerce:
    #     sync_woocommerce_items(warehouse, woocommerce_item_list)
    frappe.local.form_dict.count_dict["products"] = len(woocommerce_item_list)
    if woocommerce_settings.if_not_exists_create_item_to_woocommerce == 1:
        sync_erpnext_items(price_list)

def sync_erpnext_items(price_list):
    default_warehouse = frappe.db.get_single_value('WooCommerce Config', 'warehouse')
    warehouse_list = frappe.get_all("WooWarehouses", pluck="warehouse") or []
    warehouse_list.append(default_warehouse)

    variants_to_insert = {}
    image_error_list = []
    item_has_no_image = []

    woo_prods = {i["name"]: int(i["id"]) 
                for i in get_woocommerce_items(True)}
    woo_media = get_media()
    woo_cats = sync_categories()

    for item in get_erpnext_items(price_list):
        try:
            img = {}
            if item.image_name:
                item_image = item.image_name.split("/")[-1]
                if item_image in woo_media.keys():
                    img = {"id": woo_media[item_image]}
                else:
                    image_error_list.append({"item_code": item.name,
                                             "image_name":item_image})
                #     # img = {"src": get_url(item.get("image")).replace("http:","https:")}
                #     img = {"src": ( "https://erpnext-154757-0.cloudclusters.net"+ item.get("image"))}
            else:
                item_has_no_image.append({"item_code": item.name, 
                                             "name":item.item_name})

            if item.get("woo_sync_as_variant"):
                parent_id, variant_data = sync_to_woo_as_var(item, price_list,
                                   warehouse_list, woo_prods,
                                   woo_cats, img)
                if variant_data and parent_id in variants_to_insert.keys():
                    variants_to_insert[parent_id]["create"].append(variant_data)
                elif variant_data:
                    variants_to_insert[parent_id] = {"create":[variant_data]}
            else:
                sync_to_woo_as_simple(item, price_list,
                                      warehouse_list, woo_prods,
                                      woo_cats, img)
            # sync_item_with_woocommerce(item, price_list, warehouse, woocommerce_item_list.get(item.get('woocommerce_product_id')))
            frappe.local.form_dict.count_dict["products"] += 1

        except woocommerceError as e:
            make_woocommerce_log(title="{0}".format(e), 
                                 status="Error", 
                                 method="sync_woocommerce_items", 
                                 message=frappe.get_traceback(),
                request_data=item, exception=True)
        except Exception as e:
            make_woocommerce_log(title="{0}".format(e), 
                                 status="Error", 
                                 method="sync_woocommerce_items", 
                                 message=frappe.get_traceback(),
                request_data=item, exception=True)
    
    for key, value in variants_to_insert.items():
        woo_variants = post_request(f"products/{key}/variations/batch", value)
        for variant in woo_variants["create"]:
            if variant.get("sku"):
                erp_item = frappe.get_doc("Item", variant.get("sku"))
                erp_item.flags.ignore_mandatory = True
                erp_item.woocommerce_variant_id = variant.get("id")
                erp_item.save()
                frappe.db.commit()
            else:
                frappe.throw(str(variant))
    
    if image_error_list:
        make_woocommerce_log(title="Image(s) not found in WordPress", 
                                 status="Error", 
                                 method="sync_woocommerce_items", 
                                 item_missing_img=image_error_list)
    if item_has_no_image:
        make_woocommerce_log(title="Item(s) Has no Image", 
                                 status="Error", 
                                 method="sync_woocommerce_items", 
                                 message=f"{item_has_no_image}")
 
# fix conditions
def get_erpnext_items(price_list):
    erpnext_items = []
    woocommerce_settings = frappe.get_doc("WooCommerce Config", "WooCommerce Config")

    last_sync_condition, item_price_condition = "", ""
    if woocommerce_settings.last_sync_datetime:
        last_sync_condition = "and modified >= '{0}' ".format(woocommerce_settings.last_sync_datetime)
        # item_price_condition = "AND `tabItem Price`.`modified` >= '{0}' ".format(woocommerce_settings.last_sync_datetime)

    item_from_master = """select name, item_code, item_name, item_group,
        description, woocommerce_description, has_variants, variant_of, stock_uom, image_name, woocommerce_product_id,
        woocommerce_variant_id, sync_qty_with_woocommerce, weight_per_unit, weight_uom from tabItem
        where sync_with_woocommerce=1 and (variant_of is null or variant_of = '')
        and (disabled is null or disabled = 0)  %s """ % last_sync_condition

    erpnext_items.extend(frappe.db.sql(item_from_master, as_dict=1))

    template_items = [item.name for item in erpnext_items if item.has_variants]

    if len(template_items) > 0:
    #    item_price_condition += ' and i.variant_of not in (%s)'%(' ,'.join(["'%s'"]*len(template_items)))%tuple(template_items)
        # escape raw item name
        for i in range(len(template_items)):
            template_items[i] = template_items[i].replace("'", r"\'")
        # combine condition
        # item_price_condition += ' AND `tabItem`.`variant_of` NOT IN (\'{0}\')'.format(
        #     ("' ,'".join(template_items)))
    
    item_from_item_price = """SELECT `tabItem`.`name`, 
                                     `tabItem`.`item_code`, 
                                     `tabItem`.`item_name`, 
                                     `tabItem`.`item_group`, 
                                     `tabItem`.`description`,
                                     `tabItem`.`woocommerce_description`, 
                                     `tabItem`.`has_variants`, 
                                     `tabItem`.`variant_of`, 
                                     `tabItem`.`stock_uom`, 
                                     `tabItem`.`image_name`, 
                                     `tabItem`.`woocommerce_product_id`,
                                     `tabItem`.`woocommerce_variant_id`, 
                                     `tabItem`.`sync_qty_with_woocommerce`, 
                                     `tabItem`.`weight_per_unit`, 
                                     `tabItem`.`weight_uom`,
                                     `tabItem`.`woo_sync_as_variant`
        FROM `tabItem`, `tabItem Price`
        WHERE `tabItem Price`.`price_list` = '%s' 
          AND `tabItem`.`name` = `tabItem Price`.`item_code`
          AND `tabItem`.`sync_with_woocommerce` = 1 
          AND (`tabItem`.`disabled` IS NULL OR `tabItem`.`disabled` = 0) %s""" %(price_list, item_price_condition)
    
    # frappe.log_error("{0}".format(item_from_item_price))
    # woo_category = get_filter_request("products/categories/", {"name":"سجاد"})
    # prods = get_woocommerce_items(True)
    # frappe.throw(str(prods))

    updated_price_item_list = frappe.db.sql(item_from_item_price, as_dict=1)
    # frappe.log_error("{0}".format(updated_price_item_list))

    # to avoid item duplication
    return [frappe._dict(tupleized) for tupleized in set(tuple(item.items())
        for item in erpnext_items + updated_price_item_list)]

# Delete once finished editing the project
def sync_item_with_woocommerce(item, price_list, warehouse, woocommerce_item=None):
    variant_list = []
    item_data = {
            "name": item.get("item_name"),
            "description": item.get("woocommerce_description") or item.get("web_long_description") or item.get("description"),
            "short_description": item.get("description") or item.get("web_long_description") or item.get("woocommerce_description"),
    }
    item_data.update( get_price_and_stock_details(item, warehouse, price_list) )

    # if item.get("has_variants"):  # we are dealing a variable product
    if item.get("variant_of"):  # we are dealing a variable product
        item_data["type"] = "variable"

        parent_item = frappe.get_doc("Item", item.get("variant_of"))

        variant_list, options, variant_item_name = get_variant_attributes(item, price_list, warehouse)
        item_data["attributes"] = options

    else:   # we are dealing with a simple product
        item_data["type"] = "simple"


    erp_item = frappe.get_doc("Item", item.get("name"))
    erp_item.flags.ignore_mandatory = True

    if not item.get("woocommerce_product_id"):
        item_data["status"] = "draft"

        # create_new_item_to_woocommerce(item, item_data, erp_item, variant_item_name_list)

    else:
        item_data["id"] = item.get("woocommerce_product_id")
        try:
            # update item
            put_request("products/{0}".format(item.get("woocommerce_product_id")), item_data)

        except requests.exceptions.HTTPError as e:
            if e.args[0] and (e.args[0].startswith("404") or e.args[0].startswith("400")):
                if frappe.db.get_value("WooCommerce Config", "WooCommerce Config", "if_not_exists_create_item_to_woocommerce"):
                    item_data["id"] = ''
                    # create_new_item_to_woocommerce(item, item_data, erp_item, variant_item_name_list)
                else:
                    disable_woocommerce_sync_for_item(erp_item)
            else:
                raise e

    if variant_list:
        for variant in variant_list:
            erp_varient_item = frappe.get_doc("Item", variant["item_name"])
            if erp_varient_item.woocommerce_product_id: #varient exist in woocommerce let's update only
                r = put_request("products/{0}/variations/{1}".format(erp_item.woocommerce_product_id, erp_varient_item.woocommerce_product_id),variant)
            else:
                woocommerce_variant = post_request("products/{0}/variations".format(erp_item.woocommerce_product_id), variant)

                erp_varient_item.woocommerce_product_id = woocommerce_variant.get("id")
                erp_varient_item.woocommerce_variant_id = woocommerce_variant.get("id")
                erp_varient_item.save()

    frappe.db.commit()

def sync_to_woo_as_var(item, price_list, warehouse_list
                       ,woo_prods, woo_cats, img):
    parent_woo_item = None
    erp_item = frappe.get_doc("Item", item.get("name"))
    erp_item.flags.ignore_mandatory = True
    variant_result = {}
    # if item is a variant
    if item.get("variant_of"):
        # get item attributes
        item_attributes = frappe.get_all(
                            "Item Variant Attribute",
                            fields=["attribute","attribute_value"],
                            filters={"parent": item.item_code, "parentfield": "attributes"}
                        )
        # get category id by item.item_group
        category = woo_cats[item.get("item_group")]
        # create parent item name for woo
        item_name, item_attrs = get_item_name_and_attrs(item_attributes)
        
        # if created parent name exists in woo_prods
        # in erpnext item
        if (item_name in woo_prods.keys()):
            # get parent item
            parent_woo_item = woo_prods[item_name]
        # else create new item and set item fields
        else:
            options = []
            # set parent attribute options
            if item_attrs:
                for i, (attr, values) in enumerate(item_attrs.items()):
                    options.append({
                        "name": attr,
                        "visible": "True",
                        "variation": "True",
                        "position": i+1,
                        "options": values
                    })
            
            # create parent item data
            product_data = {
                "name": item_name,
                "attributes": options or [],
                "type": "variable",
                "categories": [{"id": category}]
                }
            # insert product to woo
            post_result = post_request("products", product_data)
            parent_woo_item = post_result.get("id")
            woo_prods[post_result.get("name")] = parent_woo_item
        
        # update erp woocommerce_product_id field
        # to parent woo product id
        erp_item.woocommerce_product_id = parent_woo_item
        erp_item.save()
        frappe.db.commit()
        
        # if woocommerce_variant_id in 
        # erpnext item is not set
        if not item.get("woocommerce_variant_id"):
            meta = [{"key": "ideapark_variation_images", "value": [img.get("id")]}]
            variant_data = {
                "sku": item.get("name"),
                "image": img,
                "meta_data": meta
            }
            variant_data.update( get_price_and_stock_details(item, warehouse_list, price_list) )
            variant_options = []
            for attr in erp_item.get("attributes"):
                if attr.attribute in ["المقاس","اللون"]:
                    variant_options.append({
                        "name": attr.attribute, "option": attr.attribute_value})
            variant_data["attributes"] = variant_options

            variant_result = variant_data
            
    return parent_woo_item, variant_result

def sync_to_woo_as_simple(item, price_list, warehouse_list,
                          woo_prods, woo_cats, img):
    if item.get("variant_of") and not item.get("woocommerce_product_id"):
        erp_item = frappe.get_doc("Item", item.get("name"))
        erp_item.flags.ignore_mandatory = True
        # get category id by item.item_group
        category = woo_cats[item.get("item_group")]
        # create parent item data
        product_data = {
            "name": item.get("item_name"),
            "type": "simple",
            "categories": [{"id": category}],
            "sku": item.get("name"),
            "image": img
            }
        product_data.update( get_price_and_stock_details(item, warehouse_list, price_list) )

        # insert product to woo
        woo_product = post_request("products", product_data)
        woo_prods[woo_product.get("name")] = woo_product.get("id")
        # update erp woocommerce_product_id field
        # to parent woo product id
        erp_item.woocommerce_product_id = woo_product.get("id")
        erp_item.save()
        frappe.db.commit()

def get_item_name_and_attrs(attributes):
    group = ""
    model = ""
    attrs = {}
    for attribute in attributes:
        if attribute["attribute"] == "النوع":
            group = attribute["attribute_value"]
        if (attribute["attribute"] == "النقشة" and 
            attribute["attribute_value"] != "N/A"):
                model = f" {attribute['attribute_value']}"
        if (attribute["attribute"] == "اللون" and 
            attribute["attribute_value"] != "N/A"):
            attrs["اللون"] = []
        if (attribute["attribute"] == "المقاس" and 
            attribute["attribute_value"] != "N/A"):
            attrs["المقاس"] = []
                
    if model:
        for key in attrs.keys():
            result = get_attr_values(model, key)
            attrs[key] = result

    item_name = f"{group}{model}"
    return item_name or "", attrs

def get_media():
    media = {}
    for image in get_woocommerce_media():
        detail = image.get("media_details")
        if "file" in detail.keys():
            name = detail["file"].split("/")[-1]
            media[name]= int(image.get("id"))
    return media

def sync_parent_categories():
    # get unsynced parent item groups
    unsynced_parents = frappe.get_list("Item Group",
                fields=['name', 'woocommerce_id'],
                filters={"is_group": 1,
                        "sync_with_woocommerce": 1,
                        "woocommerce_id": "",
                        "parent_item_group": "Products - منتجات"}, #make this line dynamic
                )
    if len(unsynced_parents) > 100:
        parent_chunks = [unsynced_parents[i:i + 100] for i in range(0, len(unsynced_parents), 100)]
        values = {}
        for chunck in parent_chunks:
            result = post_request("products/categories/batch", {"create":chunck})
            values.update({i["name"]: int(i["id"]) for i in result["create"]})
    elif unsynced_parents:
        result = post_request("products/categories/batch", {"create":unsynced_parents})
        values = {i["name"]: int(i["id"]) for i in result["create"]}
    
    for parent in unsynced_parents:
        frappe.db.set_value('Item Group', parent.name, 'woocommerce_id', values[parent.name])

def sync_child_categories():
        # get unsynced item groups
    synced_parents = frappe.get_list("Item Group",
                fields=['name', 'woocommerce_id'],
                filters={"is_group": 1,
                        "sync_with_woocommerce": 1,
                        "woocommerce_id": ["!=",""],
                        "parent_item_group": "Products - منتجات"}, #make this line dynamic
                )
    
    for parent in synced_parents:
        parent_woo_id = parent.woocommerce_id
        children = frappe.get_list("Item Group",
                                   fields=['name', f'({parent_woo_id}) as parent'],
                                   filters={
                                       "parent_item_group": parent.name, 
                                       "sync_with_woocommerce": 1, 
                                       "woocommerce_id": ""})
        if len(children) > 100:
            parent_chunks = [children[i:i + 100] for i in range(0, len(children), 100)]
            values = {}
            for chunck in parent_chunks:
                result = post_request("products/categories/batch", {"create":chunck})
                values.update({i["name"]: int(i["id"]) for i in result["create"]})
        elif children:
            result = post_request("products/categories/batch", {"create":children})
            values = {i["name"]: int(i["id"]) for i in result["create"]}
        
        for child in children:
            frappe.db.set_value('Item Group', child.name, 'woocommerce_id', values[child.name])

def sync_categories():
    sync_parent_categories()
    sync_child_categories()
    # fetch all item groups synced to wordpress
    woo_categories = frappe.get_list("Item Group",
                fields=['name', 'woocommerce_id'],
                filters={
                        "sync_with_woocommerce": 1,
                        "woocommerce_id": ["!=",""],
                    },
                )
    # make each item group name as key and woo_id as its value
    return_result = {i["name"]: int(i["woocommerce_id"]) for i in woo_categories}
    return return_result #to use it with product creation

def sync_new_attributes():
        # check for new attributes
    new_erp_attributes = frappe.get_list("Item Attribute", 
                            filters={
                                        "sync_with_woocommerce": 1,
                                        "woocommerce_id": ""
                                    },
                            pluck="name")
    # insert new attributes with their values
    for attribute in new_erp_attributes:
        woo_att = post_request("products/attributes", {"name": attribute})
        children = frappe.get_all("Item Attribute Value",
                        fields=["(attribute_value) as name", "(name) as child_id"],
                        filters={"parent":attribute})
        if children and woo_att:
            woo_att_id = woo_att["id"]
            # if children length is longer than 100 then insert in chuncks
            if len(children) > 100:
                child_chunks = [children[i:i + 100] for i in range(0, len(children), 100)]
                values = {}
                for chunck in child_chunks:
                    result = post_request(f"products/attributes/{woo_att_id}/terms/batch", {"create":chunck})
                    values.update({i["name"]: int(i["id"]) for i in result["create"]})

            # if less than 100 then insert in one go
            elif children:
                result = post_request(f"products/attributes/{woo_att_id}/terms/batch", {"create": children})
                values = {i["name"]: int(i["id"]) for i in result["create"]}

            frappe.db.set_value('Item Attribute', attribute, 'woocommerce_id', int(woo_att_id))

            for child in children:
                frappe.db.set_value('Item Attribute Value', child.child_id, 'woocommerce_id', values[child.name])

def sync_new_attribute_values():
    # TO-DO: filter it by last last synced date
    upd_erp_attributes = frappe.get_list("Item Attribute",
                            fields=['name','woocommerce_id'],
                            filters={
                                    "sync_with_woocommerce": 1,
                                    "woocommerce_id": ["!=", ""]})
    # add new values to existing attribute
    for attribute in upd_erp_attributes:
        new_children = frappe.get_all("Item Attribute Value",
                                  fields=["(attribute_value) as name", "(name) as child_id"],
                                  filters={"woocommerce_id":"", "parent":attribute.name})
        if new_children:
            woo_att_id = attribute.get("woocommerce_id")
            # if children length is longer than 100 then insert in chuncks
            if len(new_children) > 100:
                child_chunks = [new_children[i:i + 100] for i in range(0, len(new_children), 100)]
                values = {}
                for chunck in child_chunks:
                    result = post_request(f"products/attributes/{woo_att_id}/terms/batch", {"create":chunck})
                    values.update({i["name"]: int(i["id"]) for i in result["create"]})

            # if less than 100 then insert in one go
            elif new_children:
                result = post_request(f"products/attributes/{woo_att_id}/terms/batch", {"create":new_children})
                values = {i["name"]: int(i["id"]) for i in result["create"]}

            for child in new_children:
                frappe.db.set_value('Item Attribute Value', child.child_id, 'woocommerce_id', values[child.name])

def sync_attributes():
    # adds new Item Attributes with values to Wordpress
    sync_new_attributes()
    # add new attribute values to existing attributes
    sync_new_attribute_values()

def get_attr_values(model, attribute):
    item = DocType("Item")
    item_variant = DocType("Item Variant Attribute")
    result = (
    frappe.qb.from_(item).from_(item_variant)
        .select(item_variant.attribute_value)
        .where(item.name==item_variant.parent)
        .where(item_variant.attribute==attribute)
        .where(item.item_name.like(f"%{model}%"))
        .groupby(item_variant.attribute_value)
        .orderby(item_variant.attribute_value)
    ).run(pluck=item_variant.attribute_value)
    return result

def get_variant_attributes(item, price_list, warehouse):
    options, variant_list, variant_item_name, attr_sequence = [], [], [], []
    attr_dict = {}

    for i, variant in enumerate(frappe.get_all("Item", filters={"variant_of": item.get("name")},
        fields=['name'])):

        item_variant = frappe.get_doc("Item", variant.get("name"))

        data = (get_price_and_stock_details(item_variant, warehouse, price_list))
        data["item_name"] = item_variant.name
        data["attributes"] = []
        for attr in item_variant.get('attributes'):
            attribute_option = {}
            attribute_option["name"] = attr.attribute
            attribute_option["option"] = attr.attribute_value
            data["attributes"].append(attribute_option)

            if attr.attribute not in attr_sequence:
                attr_sequence.append(attr.attribute)
            if not attr_dict.get(attr.attribute):
                attr_dict.setdefault(attr.attribute, [])

            attr_dict[attr.attribute].append(attr.attribute_value)

        variant_list.append(data)


    for i, attr in enumerate(attr_sequence):
        options.append({
            "name": attr,
            "visible": "True",
            "variation": "True",
            "position": i+1,
            "options": list(set(attr_dict[attr]))
        })
    return variant_list, options, variant_item_name

def get_price_and_stock_details(item, warehouse_list, price_list):
    qty = frappe.db.get_value("Bin", 
                              {"item_code":item.item_code,
                                "warehouse": ["in", warehouse_list]}, 
                                "sum(actual_qty) - sum(reserved_qty) as actual_qty") or 0
    
    price = frappe.db.get_value("Item Price", 
            {"price_list": price_list, "item_code": item.get("item_code")}, "price_list_rate")

    item_price_and_quantity = {
        "regular_price": "{0}".format(flt(price)) #only update regular price
    }

    if item.weight_per_unit:
        if item.weight_uom and item.weight_uom.lower() in ["kg", "g", "oz", "lb", "lbs"]:
            item_price_and_quantity.update({
                "weight": "{0}".format(get_weight_in_woocommerce_unit(item.weight_per_unit, item.weight_uom))
            })

    if item.stock_keeping_unit:
        item_price_and_quantity = {
        "sku": "{0}".format(item.stock_keeping_unit)
    }

    if item.get("sync_qty_with_woocommerce"):
        item_price_and_quantity.update({
            "stock_quantity": "{0}".format(cint(qty) if qty else 0),
            "manage_stock": "True"
        })

    #rlavaud Do I need this???
    if item.woocommerce_variant_id:
        item_price_and_quantity["id"] = item.woocommerce_variant_id


    return item_price_and_quantity

def get_weight_in_woocommerce_unit(weight, weight_uom):
    woocommerce_settings = frappe.get_doc("WooCommerce Config", "WooCommerce Config")
    weight_unit = woocommerce_settings.weight_unit
    convert_to_gram = {
        "kg": 1000,
        "lb": 453.592,
        "lbs": 453.592,
        "oz": 28.3495,
        "g": 1
    }
    convert_to_oz = {
        "kg": 0.028,
        "lb": 0.062,
        "lbs": 0.062,
        "oz": 1,
        "g": 28.349
    }
    convert_to_lb = {
        "kg": 1000,
        "lb": 1,
        "lbs": 1,
        "oz": 16,
        "g": 0.453
    }
    convert_to_kg = {
        "kg": 1,
        "lb": 2.205,
        "lbs": 2.205,
        "oz": 35.274,
        "g": 1000
    }
    if weight_unit.lower() == "g":
        return weight * convert_to_gram[weight_uom.lower()]

    if weight_unit.lower() == "oz":
        return weight * convert_to_oz[weight_uom.lower()]

    if weight_unit.lower() == "lb"  or weight_unit.lower() == "lbs":
        return weight * convert_to_lb[weight_uom.lower()]

    if weight_unit.lower() == "kg":
        return weight * convert_to_kg[weight_uom.lower()]

def sync_to_erpnext():
    erp_cats = frappe.get_list("Item Group",
                fields=['name', 'woocommerce_id'],
                filters={
                        "sync_with_woocommerce": 1,
                        "woocommerce_id": "",
                    },
                )
    if erp_cats:
        woo_cats = {i["name"]: int(i["id"]) for i in get_woocommerce_categories()}
        for erp_cat in erp_cats:
            if erp_cat.name in woo_cats.keys():
                frappe.db.set_value('Item Group', erp_cat.name, 'woocommerce_id', woo_cats[erp_cat.name])

    woo_prods = get_woocommerce_items(True)
    erp_products = frappe.get_list("Item",
                fields=['woocommerce_product_id'],
                filters={
                        "sync_with_woocommerce": 1,
                        "woo_sync_as_variant":1,
                        "woocommerce_product_id": ["!=",""],
                        "woocommerce_variant_id": ["!=",""],
                    },
                    group_by="woocommerce_product_id",
                    pluck="woocommerce_product_id"
                )
    for woo_prod in woo_prods:
        if str(woo_prod["id"]) in erp_products:
            doc = frappe.new_doc('WooCommerce Product Names')
            doc.product_name = woo_prod["name"]
            doc.woo_id = woo_prod["id"]
            doc.save(ignore_permissions=True)
        
def trigger_update_item_stock(doc, method):
    if doc:
        woocommerce_settings = frappe.get_doc("WooCommerce Config", "WooCommerce Config")
        if (woocommerce_settings.woocommerce_url and 
            woocommerce_settings.enable_woocommerce and 
            woocommerce_settings.trigger_update_item_stock):
            for item in doc.get("items"):
                update_item_stock(item.item_code, woocommerce_settings, doc, force=True)

def update_item_stock_qty(force=False):
    woocommerce_settings = frappe.get_doc("WooCommerce Config", "WooCommerce Config")
    items_list = frappe.get_list("Item", 
                                fields=["item_code",
                                        "sync_qty_with_woocommerce",
                                        "woocommerce_product_id",
                                        "woocommerce_variant_id"
                                        ], 
                                filters={"sync_with_woocommerce": 1,
                                         "sync_qty_with_woocommerce": 1,
                                          "disabled": ("!=", 1)})
    for item in items_list:
        try:
            update_item_stock(item, woocommerce_settings, force=force)
        except woocommerceError as e:
            make_woocommerce_log(title="{0}".format(e), status="Error", method="sync_woocommerce_items", message=frappe.get_traceback(),
                request_data=item, exception=True)

        except Exception as e:
            if e.args[0] and e.args[0].startswith("402"):
                raise e
            else:
                make_woocommerce_log(title="{0}".format(e), status="Error", method="sync_woocommerce_items", message=frappe.get_traceback(),
                    request_data=item, exception=True)

def update_item_stock(item, woocommerce_settings, bin=None, force=False):
    if isinstance(item, str):
        _item = frappe.get_doc("Item", item)
    else:
        _item = item
    
    item_code = _item.item_code
    bin_since_last_sync = 0
    if _item.sync_qty_with_woocommerce:
        if not _item.woocommerce_product_id:
            make_woocommerce_log(title="WooCommerce ID missing", status="Error", method="sync_woocommerce_items",
                message="Please sync WooCommerce IDs to ERP (missing for item {0})".format(item_code),
                  request_data=item_code, exception=True)
        else:
            # removed bin date check
            # check bin creation date
            if force == False:
                last_sync_datetime = get_datetime(woocommerce_settings.last_sync_datetime)
                bin_since_last_sync = frappe.db.sql("""SELECT COUNT(`name`) FROM `tabBin` WHERE `item_code` = '{item_code}'
                                                    AND `modified` > '{last_sync_datetime}'""".format(item_code=item_code, 
                                                                                                    last_sync_datetime=last_sync_datetime),
                                                                                                        as_list=True)[0][0]
            
            if bin_since_last_sync > 0 or force == True:
                warehouse_list = [woocommerce_settings.warehouse]
                warehouse_list.extend([w.warehouse for w in woocommerce_settings.warehouses])

                qty = frappe.db.get_value("Bin", 
                                    {"item_code":item_code, 
                                    "warehouse": ["in", warehouse_list]}, 
                                    "sum(actual_qty) - sum(reserved_qty) as actual_qty") or 0
                
                if _item.woocommerce_product_id and _item.woocommerce_variant_id:
					# item = variant
                    item_data, path = get_product_update_dict_and_resource(_item.woocommerce_product_id, _item.woocommerce_variant_id,
                                                                                is_variant=True, actual_qty=qty)
                else:
					# item = single
                    item_data, path = get_product_update_dict_and_resource(_item.woocommerce_product_id, actual_qty=qty)
                # frappe.throw(f"item data:{item_data}  <br> path: {path}")
                
                try:
					#make_woocommerce_log(title="Update stock of {0}".format(item.barcode), status="Started", method="update_item_stock", message="Resource: {0}, data: {1}".format(resource, item_data))
                    put_request(path, item_data)
                except requests.exceptions.HTTPError as e:
                    if e.args[0] and e.args[0].startswith("404"):
                        make_woocommerce_log(title=e.message, status="Error", method="update_item_stock", message=frappe.get_traceback(),
                            request_data=item_data, exception=True)
                        disable_woocommerce_sync_for_item(_item)
                    else:
                        raise e

def get_product_update_dict_and_resource(woocommerce_product_id, woocommerce_variant_id=None, is_variant=False, actual_qty=0):
    item_data = {}
    item_data["stock_quantity"] = "{0}".format(cint(actual_qty))
    item_data["manage_stock"] = "1"

    if is_variant:
        resource = "products/{0}/variations/{1}".format(woocommerce_product_id,woocommerce_variant_id)
    else: #simple item
        resource = "products/{0}".format(woocommerce_product_id)

    return item_data, resource

def add_w_id_to_erp():
    # purge WooCommerce IDs so that there cannot be any conflict
    purge_ids = """UPDATE `tabItem`
            SET `woocommerce_product_id` = NULL, `woocommerce_variant_id` = NULL;"""
    frappe.db.sql(purge_ids)
    frappe.db.commit()

    # loop through all items on WooCommerce and get their IDs (matched by barcode)
    woo_items = get_woocommerce_items()
    make_woocommerce_log(title="Syncing IDs", status="Started", method="add_w_id_to_erp", message='Item: {0}'.format(woo_items),
        request_data={}, exception=True)
    for woocommerce_item in woo_items:
        update_item = """UPDATE `tabItem`
            SET `woocommerce_product_id` = '{0}'
            WHERE `barcode` = '{1}';""".format(woocommerce_item.get("id"), woocommerce_item.get("sku"))
        frappe.db.sql(update_item)
        frappe.db.commit()
        for woocommerce_variant in get_woocommerce_item_variants(woocommerce_item.get("id")):
            update_variant = """UPDATE `tabItem`
                SET `woocommerce_variant_id` = '{0}', `woocommerce_product_id` = '{1}'
                WHERE `barcode` = '{2}';""".format(woocommerce_variant.get("id"), woocommerce_item.get("id"), woocommerce_variant.get("sku"))
            frappe.db.sql(update_variant)
            frappe.db.commit()
    make_woocommerce_log(title="IDs synced", status="Success", method="add_w_id_to_erp", message={},
        request_data={}, exception=True)