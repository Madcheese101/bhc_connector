// Copyright (c) 2016, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('woocommerce Log', {
	refresh: function(frm) {
		frm.add_custom_button(__('Resync Images'), function() {
			frm.call("sync_missing_images")});
	}
});
