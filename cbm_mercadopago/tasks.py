"""Rotinas agendadas.

Só existe uma: liberar o horário de consultas cobradas e não pagas.
Necessária porque o Healthcare cria a consulta antes do pagamento e nunca
libera o horário sozinho.
"""

import frappe
from frappe.utils import add_to_date, cint, now_datetime

GATEWAY = "Mercado Pago"

# Só expira consulta que ainda está esperando o paciente aparecer.
STATUS_EXPIRAVEIS = {"Scheduled", "Open"}


def expirar_agendamentos_nao_pagos():
	"""Cancela consultas cujo link de pagamento venceu sem ser pago.

	Deliberadamente conservador: só mexe em consultas que têm um pedido de
	pagamento pendente pelo Mercado Pago. Consulta criada sem cobrança (paciente
	que vai pagar por outro meio, cortesia, retorno) nunca é tocada.
	"""
	settings = frappe.get_cached_doc("Mercado Pago Settings")
	minutos = cint(settings.unpaid_expiry_minutes)
	if not settings.enabled or minutos <= 0:
		return

	limite = add_to_date(now_datetime(), minutes=-minutos)

	pendentes = frappe.get_all(
		"Payment Request",
		filters={
			"docstatus": 1,
			"status": ["in", ["Requested", "Initiated"]],
			"payment_gateway": GATEWAY,
			"reference_doctype": "Sales Invoice",
			"creation": ["<", limite],
		},
		fields=["name", "reference_name"],
	)

	for pendente in pendentes:
		try:
			_expirar(pendente.name, pendente.reference_name)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title="Mercado Pago: falha ao expirar consulta nao paga",
				message=f"Payment Request: {pendente.name}\n\n{frappe.get_traceback()}",
			)


def _expirar(payment_request: str, sales_invoice: str):
	consulta = frappe.db.get_value(
		"Sales Invoice Item",
		{"parent": sales_invoice, "reference_dt": "Patient Appointment"},
		"reference_dn",
	)

	# Corrida com o webhook: se o pagamento entrou entre a consulta e agora,
	# não cancela nada.
	if frappe.db.get_value("Payment Request", payment_request, "status") == "Paid":
		return

	pr = frappe.get_doc("Payment Request", payment_request)
	pr.flags.ignore_permissions = True
	pr.cancel()

	si = frappe.get_doc("Sales Invoice", sales_invoice)
	if si.docstatus == 1:
		# Cancelamos a fatura explicitamente: o `cancel_sales_invoice` do
		# Healthcare só age quando `show_payment_popup` está ligado, o que não
		# é o nosso caso — sem isso a fatura ficaria como recebível fantasma.
		si.flags.ignore_permissions = True
		si.cancel()

	if consulta:
		status_atual = frappe.db.get_value("Patient Appointment", consulta, "status")
		if status_atual in STATUS_EXPIRAVEIS:
			frappe.db.set_value("Patient Appointment", consulta, "status", "Cancelled")
