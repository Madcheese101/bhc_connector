from __future__ import unicode_literals
import frappe
from frappe import _
from frappe.utils import flt, cint, get_url, get_datetime
from frappe.query_builder import DocType
from frappe.query_builder.custom import ConstantColumn
from frappe.query_builder.functions import Sum
from frappe.utils.background_jobs import enqueue
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
    enqueue("woocommerceconnector.sync_products.job_que", queue='long',
             timeout=2000)
    frappe.msgprint(_("Queued for syncing. It may take a few minutes to an hour if this is your first sync."))

@frappe.whitelist()
def job_que():
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

def sync_products(woocommerce_settings):
    woocommerce_item_list = []
    frappe.local.form_dict.count_dict["products"] = len(woocommerce_item_list)
    sync_items_to_woo(woocommerce_settings)

def sync_items_to_woo(woocommerce_settings):
    woo_has_variant_prods = woocommerce_settings.get('woo_has_variant_prods')
    naming_attributes_list = frappe.get_all("Naming Attributes", 
                                       pluck="attribute", order_by="attribute_order")
    variants_attributes_table = woocommerce_settings.get('variants_attributes_table')
    default_warehouse = woocommerce_settings.get("warehouse")
    warehouse_list = frappe.get_all("WooWarehouses", 
                                    pluck="warehouse") or []
    warehouse_list.append(default_warehouse)

    missing_attributes = []
    woo_missing_imgs = []
    item_has_no_image = []
    missing_item_grps = []
    set_variant_settings_error = ""

    variants_to_insert = {}
    woo_media = get_media()
    woo_cats = sync_categories() #will return categories after sync
    woo_attr = sync_attributes() #will return attributes after sync
    woo_prods_list = frappe.get_all("WooCommerce Product Names", 
                                    fields=["name", "woo_id"])
    woo_prods = {i["name"]: int(i["woo_id"]) 
                for i in woo_prods_list}
    
    for item in get_erpnext_items():
        # check if item group not in woo cats
        if item.item_group not in woo_cats.keys():
            if item.item_group not in missing_item_grps:
                # insert it to missing grps list
                missing_item_grps.append(item.item_group)
            continue # will go to next item
        try:
            # get Image ID from woo
            img = get_woo_img(item.image_name, item.name, item.item_name, 
                              woo_media, woo_missing_imgs, item_has_no_image)
            # if sync as Variant then insert to woo as Variable Products
            if item.get("woo_sync_as_variant") and item.get("variant_of"):
                if woo_has_variant_prods and naming_attributes_list and variants_attributes_table:
                    parent_id, variant_data, error = sync_to_woo_as_var(
                                item, woo_prods,
                                woo_cats, woo_attr, img,
                                naming_attributes_list)
                    
                    # Now prepare the the variants list to insert to Woo
                    if variant_data and parent_id in variants_to_insert.keys():
                        variants_to_insert[parent_id]["create"].append(variant_data)
                        frappe.local.form_dict.count_dict["products"] += 1
                    elif variant_data:
                        variants_to_insert[parent_id] = {"create":[variant_data]}
                        frappe.local.form_dict.count_dict["products"] += 1
                    elif error:
                        if item.item_name not in missing_attributes: missing_attributes.append(item.item_name) 
                else:
                    set_variant_settings_error = "Could not sync Products as variants due to WooCommerce Variant Settings not enabled or not set properly. Please fix that from the WooCommerce Config Page"
            # else sync as simple type product
            # TO-DO: use the correct Image Insertion
            else:
                sync_to_woo_as_simple(item, woo_prods, woo_cats, img)
            
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
    # now insert woo product variants
    insert_product_variants(variants_to_insert)

    # Insert Errors Logs if exists
    if woo_missing_imgs:
        make_woocommerce_log(title="Image(s) not found in WordPress", 
                                 status="Error", 
                                 method="sync_woocommerce_items", 
                                 item_missing_img=woo_missing_imgs)
    if item_has_no_image:
        make_woocommerce_log(title="Item(s) Has no Image", 
                                 status="Error", 
                                 method="sync_woocommerce_items", 
                                 message=f"{item_has_no_image}")
    if set_variant_settings_error != "":
        make_woocommerce_log(title="WooCommerce Variant Settings not enabled or not set", 
                                 status="Error", 
                                 method="sync_items_to_woo", 
                                 message=set_variant_settings_error)
    if missing_attributes:
        make_woocommerce_log(title="Missing Variant Attributes for Items", 
                                 status="Error", 
                                 method="sync_woocommerce_items", 
                                 message=f"{missing_attributes}")
    if missing_item_grps:
        make_woocommerce_log(title="Item Groups not synced to WooCommerce", 
                                 status="Error", 
                                 method="sync_woocommerce_items", 
                                 message=f"{missing_item_grps}")
        
def get_erpnext_items():
    woocommerce_settings = frappe.get_doc("WooCommerce Config", "WooCommerce Config")
    default_warehouse = woocommerce_settings.get("warehouse")
    warehouse_list = frappe.get_all("WooWarehouses", pluck="warehouse") or []
    warehouse_list.append(default_warehouse)
    price_list = woocommerce_settings.get("price_list")

    item_doc = frappe.qb.DocType("Item")
    item_price_doc = frappe.qb.DocType("Item Price")
    bin_doc = frappe.qb.DocType("Bin")

    qty = (frappe.qb.from_(bin_doc)
           .select((Sum(bin_doc.actual_qty) - Sum(bin_doc.reserved_qty)).as_("actual_qty"))
           .where(bin_doc.item_code==item_doc.name)
           .where((bin_doc.warehouse).isin(warehouse_list))
           )
    data_qb = (frappe.qb
                        .from_(item_doc)
                        .from_(item_price_doc)
                        .select(
                            item_doc.name,
                            item_doc.item_code,
                            item_doc.item_group,
                            item_doc.description,
                            item_doc.has_variants,
                            item_doc.variant_of,
                            item_doc.image,
                            item_doc.image_name,
                            item_doc.woo_sync_as_variant,
                            item_doc.woocommerce_product_id,
                            item_doc.woocommerce_variant_id,
                            item_doc.sync_qty_with_woocommerce,
                            item_doc.weight_per_unit,
                            item_doc.weight_uom,
                            item_price_doc.price_list_rate,
                            qty.as_("actual_qty")
                        )
                        .where(item_doc.disabled == 0)
                        .where(item_doc.sync_with_woocommerce == 1)
                        .where(item_price_doc.item_code == item_doc.name)
                        .where(item_price_doc.price_list == price_list)
                        .groupby(item_doc.item_code)
                        )
    if woocommerce_settings.last_sync_datetime:
        data_qb = data_qb.where(item_doc.modified >= woocommerce_settings.last_sync_datetime)

    result = data_qb.run(as_dict=True)
    return result

def get_woo_img(item_image, item_code, item_name, 
                woo_media, woo_missing_imgs, item_has_no_image):
    img = {}
    if item_image:
        item_image = item_image.split("/")[-1]
        if item_image in woo_media.keys():
            img = {"id": woo_media[item_image]}
        else:
            woo_missing_imgs.append({"item_code": item_code,
                                        "image_name":item_image})
    else:
        item_has_no_image.append({"item_code": item_code, 
                                        "name": item_name})
    return img

def sync_to_woo_as_var(item, woo_prods, woo_cats, 
                        woo_attr, img, naming_attributes_list):
    
    variant_result = None
    erp_item = frappe.get_doc("Item", item.get("name"))
    parent_woo_item = erp_item.woocommerce_product_id
    # get item attributes
    item_attributes = frappe.get_all(
                        "Item Variant Attribute",
                        fields=["attribute","attribute_value"],
                        filters={"parent": item.item_code, "parentfield": "attributes"}
                    )

    if not parent_woo_item:
        # get Item Doc to modify later
        # get category id by item.item_group
        category = woo_cats[item.get("item_group")]
        # create parent item name for woo
        item_name, item_attrs, missing_attribute = get_item_name_and_attrs(item_attributes, 
                                                        naming_attributes_list,
                                                        woo_attr)
        if missing_attribute:
            return None, None, True
        # if created parent name exists in woo_prods
        # in erpnext item
        if (item_name in woo_prods.keys()):
            # get parent item
            parent_woo_item = woo_prods[item_name]
        # else create new item and set item fields
        else:        
            # create parent item data
            product_data = {
                "name": item_name,
                "attributes": item_attrs or [],
                "type": "variable",
                "categories": [{"id": category}]
                }
            # insert product to woo
            post_result = post_request("products", product_data)
            parent_woo_item = post_result.get("id")
            woo_prods[post_result.get("name")] = parent_woo_item
            # insert product name in WooCommerce Product Name doctype
            doc = frappe.new_doc('WooCommerce Product Names')
            doc.product_name = post_result.get("name")
            doc.woo_id = parent_woo_item
            doc.save(ignore_permissions=True)

        
        # update erp woocommerce_product_id field
        # to parent woo product id
        erp_item.flags.ignore_mandatory = True
        erp_item.woocommerce_product_id = parent_woo_item
        erp_item.save()
        frappe.db.commit()
    
    # if woocommerce_variant_id in 
    # erpnext item is not set
    if not item.get("woocommerce_variant_id"):
        meta = [{"key": "ideapark_variation_images", "value": [img.get("id")]}]
        variant_options = []
        for attr in erp_item.get("attributes"):
            if attr.attribute in woo_attr.keys():
                variant_options.append({
                    "id": woo_attr[attr.attribute], 
                    # "name": attr.attribute,
                    "option": attr.attribute_value})
        variant_data = {
            "sku": item.get("name"),
            "image": img,
            "meta_data": meta,
            "attributes": variant_options
        }
        variant_data.update(set_price_stock(item))
        variant_result = variant_data
            
    return parent_woo_item, variant_result, False

def insert_product_variants(variants_to_insert):
    no_sku = []
    for prod_id, value in variants_to_insert.items():
        try:
            woo_variants = post_request(f"products/{prod_id}/variations/batch", value, "wc/v2")
            for variant in woo_variants["create"]:
                variant_id = variant.get("id")
                if variant.get("sku"):
                    erp_item = frappe.get_doc("Item", variant.get("sku"))
                    erp_item.flags.ignore_mandatory = True
                    erp_item.woocommerce_variant_id = variant_id
                    erp_item.save()
                    frappe.db.commit()
                else:
                    no_sku.append([f"Product ID: {prod_id}", f"Variant ID: {variant_id}"])
        except woocommerceError as e:
            make_woocommerce_log(title="{0}".format(e), 
                                 status="Error", 
                                 method="insert_product_variants", 
                                 message=frappe.get_traceback(),
                request_data=[prod_id, value], exception=True)
        except Exception as e:
            make_woocommerce_log(title="{0}".format(e), 
                                 status="Error", 
                                 method="insert_product_variants", 
                                 message=frappe.get_traceback(),
                request_data=[prod_id, value], exception=True)
    if no_sku:
        make_woocommerce_log(title="WooCommerce Product Variants no SKU", 
                                 status="Error", 
                                 method="insert_product_variants", 
                                 message=f"{no_sku}")
   
def sync_to_woo_as_simple(item, woo_prods, woo_cats, img):
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
        product_data.update(set_price_stock(item))

        # insert product to woo
        woo_product = post_request("products", product_data)
        woo_prods[woo_product.get("name")] = woo_product.get("id")
        # update erp woocommerce_product_id field
        # to parent woo product id
        erp_item.woocommerce_product_id = woo_product.get("id")
        erp_item.save()
        frappe.db.commit()
        frappe.local.form_dict.count_dict["products"] += 1

def get_item_name_and_attrs(item_attributes,
                            naming_attributes_list,
                            woo_attr):
    item_attributes_dict = {i.attribute:i.attribute_value for i in item_attributes}
    parent_product_attributes = []
    product_name = ""
    error = False

    for attribute in naming_attributes_list:
        add_value = item_attributes_dict[attribute]
        product_name += f" {add_value}"
    product_name = product_name.strip()
    for attribute, value in woo_attr.items():
        if attribute not in naming_attributes_list:
            if attribute not in item_attributes_dict.keys():
                error = True
                break

            options = get_attr_values(product_name, attribute)
            parent_product_attributes.append({"id":value, 
                                                "options":options,
                                                "visible": "True",
                                                "variation": "True"})
    return product_name, parent_product_attributes, error

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
        frappe.db.commit()

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
            frappe.db.commit()

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

def sync_new_attributes(allowed_attributes):
        # check for new attributes
    new_erp_attributes = frappe.get_list("Item Attribute", 
                            filters={
                                        "woocommerce_id": "",
                                        "name": ["in", allowed_attributes]
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
            frappe.db.commit()
            for child in children:
                frappe.db.set_value('Item Attribute Value', child.child_id, 'woocommerce_id', values[child.name])
                frappe.db.commit()

def sync_new_attribute_values(allowed_attributes):
    # TO-DO: filter it by last last synced date
    upd_erp_attributes = frappe.get_list("Item Attribute",
                            fields=['name','woocommerce_id'],
                            filters={
                                    "name": ["in", allowed_attributes],
                                    # "sync_with_woocommerce": 1,
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
                frappe.db.commit()

def sync_attributes():
    allowed_attributes = frappe.get_all("Variant Attributes", pluck="attribute")
    # adds new Item Attributes with values to Wordpress
    sync_new_attributes(allowed_attributes)
    # add new attribute values to existing attributes
    sync_new_attribute_values(allowed_attributes)

    synced_attributes = frappe.get_list("Item Attribute", 
                                        fields=['name','woocommerce_id'], 
                                        filters={"woocommerce_id":["!=", ""]})
    result = {i["name"]: int(i["woocommerce_id"]) 
                for i in synced_attributes}
    # return the list of synced attributes as dict
    return result

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

def set_price_stock(item):
    item_price_and_quantity = {
        "regular_price": "{0}".format(flt(item.price_list_rate)) #only update regular price
    }

    if item.weight_per_unit:
        if item.weight_uom and item.weight_uom.lower() in ["kg", "g", "oz", "lb", "lbs"]:
            item_price_and_quantity.update({
                "weight": "{0}".format(get_weight_in_woocommerce_unit(item.weight_per_unit, item.weight_uom))
            })

    if item.get("sync_qty_with_woocommerce"):
        item_price_and_quantity.update({
            "stock_quantity": "{0}".format(cint(item.actual_qty) if item.actual_qty else 0),
            "manage_stock": "True"
        })
    return item_price_and_quantity
    
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