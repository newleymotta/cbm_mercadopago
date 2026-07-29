"""Geração do link de pagamento a partir de uma consulta.

Fluxo: Patient Appointment -> Sales Invoice (em aberto) -> Payment Request.

Passamos pela fatura porque `ALLOWED_DOCTYPES_FOR_PAYMENT_REQUEST` do ERPNext
não inclui `Patient Appointment`. Fazendo pelo caminho nativo ganhamos de graça
a geração da URL, o envio de e-mail, o `set_as_paid()` e a proteção nativa
contra pagamento em duplicidade — sem alterar constante nenhuma do core.
"""

import frappe
from frappe import _
from frappe.utils import flt

GATEWAY = "Mercado Pago"

# Situações em que não faz sentido cobrar.
STATUS_BLOQUEADOS = {"Cancelled", "Closed", "Checked Out", "No Show"}


@frappe.whitelist()
def gerar_link_pagamento(appointment: str, enviar_email: int = 0) -> dict:
	"""Devolve (e opcionalmente envia por e-mail) o link do Checkout Pro."""
	doc = frappe.get_doc("Patient Appointment", appointment)
	doc.check_permission("write")

	_validar_consulta(doc)

	sales_invoice = _garantir_fatura(doc)
	payment_request = _garantir_payment_request(sales_invoice, bool(int(enviar_email or 0)))

	if not payment_request.payment_url:
		# Sintoma clássico de falha engolida pelo `payment_gateway_validation()`.
		frappe.throw(
			_(
				"O pedido de pagamento foi criado mas o Mercado Pago não devolveu a URL. "
				"Verifique o Mercado Pago Settings e o Registro de Erros."
			)
		)

	return {
		"payment_url": payment_request.payment_url,
		"payment_request": payment_request.name,
		"sales_invoice": sales_invoice,
	}


def _validar_consulta(doc):
	if doc.status in STATUS_BLOQUEADOS:
		frappe.throw(_("Não é possível cobrar uma consulta com situação {0}.").format(_(doc.status)))

	if not frappe.db.get_value("Patient", doc.patient, "customer"):
		frappe.throw(
			_("O paciente {0} não tem cliente vinculado, e sem isso não é possível emitir a fatura.").format(
				doc.patient
			)
		)

	settings = frappe.get_cached_doc("Mercado Pago Settings")
	if not settings.enabled:
		frappe.throw(_("A integração com o Mercado Pago está desativada."))


def _garantir_fatura(doc) -> str:
	"""Reaproveita a fatura da consulta, se houver; senão cria uma em aberto."""
	if doc.ref_sales_invoice:
		docstatus = frappe.db.get_value("Sales Invoice", doc.ref_sales_invoice, "docstatus")
		if docstatus == 1:
			return doc.ref_sales_invoice

	# A fatura precisa nascer EM ABERTO: quem a quita é o Payment Entry gerado
	# na confirmação do pagamento. Por isso `mode_of_payment` fica vazio — se
	# estivesse preenchido, `create_sales_invoice` marcaria a fatura como POS paga.
	if doc.mode_of_payment:
		doc.db_set("mode_of_payment", None, update_modified=False)

	from healthcare.healthcare.doctype.patient_appointment.patient_appointment import (
		create_sales_invoice,
	)

	create_sales_invoice(doc)
	doc.reload()

	if not doc.ref_sales_invoice:
		frappe.throw(_("Não foi possível gerar a fatura desta consulta."))
	return doc.ref_sales_invoice


def _garantir_payment_request(sales_invoice: str, enviar_email: bool):
	"""Reaproveita um pedido de pagamento pendente; senão cria um novo."""
	existente = frappe.db.get_value(
		"Payment Request",
		{
			"reference_doctype": "Sales Invoice",
			"reference_name": sales_invoice,
			"docstatus": 1,
			"status": ["in", ["Requested", "Initiated"]],
		},
		"name",
	)
	if existente:
		return frappe.get_doc("Payment Request", existente)

	from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request

	resultado = make_payment_request(
		dt="Sales Invoice",
		dn=sales_invoice,
		payment_gateway_account=_conta_gateway(),
		submit_doc=True,
		mute_email=not enviar_email,
		return_doc=True,
	)

	nome = resultado.get("name") if isinstance(resultado, dict) else getattr(resultado, "name", None)
	if not nome:
		frappe.throw(_("Não foi possível criar o pedido de pagamento."))
	return frappe.get_doc("Payment Request", nome)


def _conta_gateway() -> str:
	"""A Payment Gateway Account do Mercado Pago, criada quando o gateway é ativado."""
	conta = frappe.db.get_value("Payment Gateway Account", {"payment_gateway": GATEWAY}, "name")
	if not conta:
		frappe.throw(
			_(
				"Não existe conta de gateway para o {0}. Abra o Mercado Pago Settings e salve "
				"para que ela seja criada."
			).format(GATEWAY)
		)
	return conta
