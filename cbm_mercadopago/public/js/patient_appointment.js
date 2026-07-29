// Botão "Gerar link de pagamento" no formulário da consulta.

frappe.ui.form.on("Patient Appointment", {
	refresh(frm) {
		if (frm.is_new()) return;

		const bloqueados = ["Cancelled", "Closed", "Checked Out", "No Show"];
		if (bloqueados.includes(frm.doc.status)) return;

		frm.add_custom_button(
			__("Gerar link de pagamento"),
			() => gerar_link(frm),
			__("Mercado Pago")
		);
	},
});

function gerar_link(frm) {
	frappe.call({
		method: "cbm_mercadopago.appointment.gerar_link_pagamento",
		args: { appointment: frm.doc.name, enviar_email: 0 },
		freeze: true,
		freeze_message: __("Gerando link no Mercado Pago..."),
		callback: (r) => {
			if (!r.message || !r.message.payment_url) return;
			mostrar_link(frm, r.message);
		},
	});
}

function mostrar_link(frm, dados) {
	const url = frappe.utils.escape_html(dados.payment_url);

	const d = new frappe.ui.Dialog({
		title: __("Link de pagamento"),
		size: "large",
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "conteudo",
				options: `
					<p>${__("Envie este link ao paciente. A consulta é confirmada automaticamente assim que o pagamento for aprovado.")}</p>
					<div class="form-control" style="word-break:break-all;user-select:all;padding:10px;background:var(--fg-color)">
						<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>
					</div>
					<p class="text-muted small" style="margin-top:10px">
						${__("Fatura")}: ${frappe.utils.escape_html(dados.sales_invoice)} &middot;
						${__("Pedido de pagamento")}: ${frappe.utils.escape_html(dados.payment_request)}
					</p>
				`,
			},
		],
		primary_action_label: __("Copiar link"),
		primary_action() {
			frappe.utils.copy_to_clipboard(dados.payment_url);
			d.hide();
		},
		secondary_action_label: __("Enviar por e-mail"),
		secondary_action() {
			d.hide();
			enviar_por_email(frm);
		},
	});

	d.show();
	frm.reload_doc();
}

function enviar_por_email(frm) {
	frappe.call({
		method: "cbm_mercadopago.appointment.gerar_link_pagamento",
		args: { appointment: frm.doc.name, enviar_email: 1 },
		freeze: true,
		freeze_message: __("Enviando..."),
		callback: () => {
			frappe.show_alert({ message: __("Link enviado ao paciente."), indicator: "green" });
		},
	});
}
