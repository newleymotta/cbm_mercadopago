// Botão "Gerar link de pagamento" no formulário da consulta.

frappe.ui.form.on("Patient Appointment", {
	refresh(frm) {
		if (frm.is_new()) return;

		const bloqueados = ["Cancelled", "Closed", "Checked Out", "No Show"];
		if (bloqueados.includes(frm.doc.status)) return;

		frm.add_custom_button(
			__("Gerar link de pagamento"),
			() => gerar_link(frm),
			__("Pagamento")
		);
	},
});

function gerar_link(frm, gateway) {
	frappe.call({
		method: "cbm_mercadopago.appointment.gerar_link_pagamento",
		args: { appointment: frm.doc.name, enviar_email: 0, gateway: gateway || null },
		freeze: true,
		freeze_message: __("Gerando link de pagamento..."),
		callback: (r) => {
			if (!r.message) return;
			// Mais de um meio de pagamento configurado: perguntar qual usar.
			if (r.message.escolher_gateway) {
				escolher_gateway(frm, r.message.escolher_gateway);
				return;
			}
			if (!r.message.payment_url) return;
			mostrar_link(frm, r.message);
		},
	});
}

function escolher_gateway(frm, opcoes) {
	const d = new frappe.ui.Dialog({
		title: __("Qual meio de pagamento?"),
		fields: [
			{
				fieldname: "gateway",
				fieldtype: "Select",
				label: __("Meio de pagamento"),
				options: opcoes.join("\n"),
				default: opcoes.includes("Mercado Pago") ? "Mercado Pago" : opcoes[0],
				reqd: 1,
			},
		],
		primary_action_label: __("Gerar link"),
		primary_action(valores) {
			d.hide();
			gerar_link(frm, valores.gateway);
		},
	});
	d.show();
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
