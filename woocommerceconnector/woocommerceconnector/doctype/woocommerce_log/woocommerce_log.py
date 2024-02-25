# -*- coding: utf-8 -*-
# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe.model.document import Document
from woocommerceconnector.woocommerce_requests import (put_request, post_request,
                                   get_woocommerce_media)

class woocommerceLog(Document):
	@frappe.whitelist()
	def sync_missing_images(self):
		woo_media = {(i["media_details"]["file"]).split("/")[-1]: int(i["id"]) 
					for i in get_woocommerce_media()}
		items = self.get("items_missing_images")
		fixed_items = []
		update_dict = {}

		for item in items:
			woo_prod_id, woo_var_id = frappe.get_value("Item", item.item_code, 
					 ["woocommerce_product_id","woocommerce_variant_id"])
			if (item.image_name in woo_media.keys() and
	   			woo_prod_id and woo_var_id):
				if woo_prod_id in update_dict.keys():
					update_dict[woo_prod_id].append(
							{
								"id": woo_var_id,
								"image": {"id": woo_media[item.image_name]}
							}
					)
				else:
					update_dict[woo_prod_id] = [
							{
								"id": woo_var_id,
								"image": {"id": woo_media[item.image_name]}
							}
						]
				fixed_items.append(item)

		for id, value in update_dict.items():
			data = {"update": value}
			put_request("products/{0}/variations/batch".format(id), data)
		
		frappe.msgprint(f"Fixed {len(fixed_items)} item(s)")
		new_list = [x for x in items if x not in fixed_items]
		if new_list:
			self.set("items_missing_images", new_list)

		if len(self.items_missing_images) == 0:
			self.delete()
		else:
			self.save(ignore_permissions=True)
			frappe.db.commit()
			self.reload()



		

