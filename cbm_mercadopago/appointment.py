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
def gerar_link_pagamento(appointment: str, enviar_email: int = 0, gateway: str | None = None) -> dict:
	"""Devolve (e opcionalmente envia por e-mail) o link de pagamento.

	`gateway` é opcional. Quando há um só meio configurado, ele é usado
	automaticamente; quando há mais de um e nenhum foi escolhido, devolve
	`escolher_gateway` para a tela perguntar. Isso permite ligar outro meio
	(Stripe, por exemplo) apenas configurando, sem mexer no código.
	"""
	doc = frappe.get_doc("Patient Appointment", appointment)
	doc.check_permission("write")

	_validar_consulta(doc, gateway)

	conta = _resolver_conta_gateway(gateway)
	if isinstance(conta, dict):
		return conta  # a tela precisa perguntar qual meio usar

	sales_invoice = _garantir_fatura(doc)
	payment_request = _garantir_payment_request(sales_invoice, bool(int(enviar_email or 0)), conta)

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


def _validar_consulta(doc, gateway: str | None = None):
	if doc.status in STATUS_BLOQUEADOS:
		frappe.throw(_("Não é possível cobrar uma consulta com situação {0}.").format(_(doc.status)))

	if not frappe.db.get_value("Patient", doc.patient, "customer"):
		frappe.throw(
			_("O paciente {0} não tem cliente vinculado, e sem isso não é possível emitir a fatura.").format(
				doc.patient
			)
		)

	# A checagem de "ativado" só se aplica ao nosso gateway; os demais
	# (Stripe etc.) têm cada um a sua própria configuração.
	if gateway in (None, GATEWAY) and not frappe.get_cached_doc("Mercado Pago Settings").enabled:
		if gateway == GATEWAY or _gateways_configurados() == [GATEWAY]:
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


def _garantir_payment_request(sales_invoice: str, enviar_email: bool, conta_gateway: str):
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
		payment_gateway_account=conta_gateway,
		submit_doc=True,
		mute_email=not enviar_email,
		return_doc=True,
	)

	nome = resultado.get("name") if isinstance(resultado, dict) else getattr(resultado, "name", None)
	if not nome:
		frappe.throw(_("Não foi possível criar o pedido de pagamento."))
	return frappe.get_doc("Payment Request", nome)


def _gateways_configurados() -> list[str]:
	"""Meios de pagamento com conta de gateway criada nesta instalação."""
	return sorted(
		{
			c.payment_gateway
			for c in frappe.get_all("Payment Gateway Account", fields=["payment_gateway"])
			if c.payment_gateway
		}
	)


@frappe.whitelist()
def listar_gateways() -> list[str]:
	return _gateways_configurados()


def _resolver_conta_gateway(gateway: str | None):
	"""Decide qual meio de pagamento usar.

	Devolve o nome da Payment Gateway Account, ou um dict pedindo escolha
	quando há mais de um meio configurado e nenhum foi indicado.
	"""
	disponiveis = _gateways_configurados()

	if not disponiveis:
		frappe.throw(
			_(
				"Nenhum meio de pagamento está configurado. Abra o Mercado Pago Settings e salve "
				"para que a conta de gateway seja criada."
			)
		)

	if gateway:
		if gateway not in disponiveis:
			frappe.throw(_("O meio de pagamento {0} não está configurado.").format(gateway))
		escolhido = gateway
	elif len(disponiveis) == 1:
		escolhido = disponiveis[0]
	else:
		# Mais de um meio ativo e nenhum indicado: quem decide é o usuário.
		return {"escolher_gateway": disponiveis}

	conta = frappe.db.get_value("Payment Gateway Account", {"payment_gateway": escolhido}, "name")
	if not conta:
		frappe.throw(_("Não existe conta de gateway para {0}.").format(escolhido))
	return conta
