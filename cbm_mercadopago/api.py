"""Endpoints HTTP da integração com o Mercado Pago.

`webhook`  — recebe as notificações do Mercado Pago (servidor a servidor).
`retorno`  — para onde o paciente volta depois de pagar (back_urls).

Princípio que rege este arquivo: **o webhook nunca é fonte de verdade**.
Ele apenas diz "olhe o pagamento X". Quem responde se foi aprovado e por
quanto é a API do Mercado Pago, consultada com o nosso access token.
"""

import json

import frappe
from frappe import _
from frappe.utils import flt

from cbm_mercadopago import mp_client
from cbm_mercadopago.signature import SignatureError, verify

# Tolerância na conferência de valor (centavos, arredondamento de moeda).
TOLERANCIA_VALOR = 0.01


# ---------------------------------------------------------------------- #
# webhook                                                                 #
# ---------------------------------------------------------------------- #


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook(**kwargs):
	"""Notificação do Mercado Pago.

	Códigos de resposta são deliberados:
	  401 — assinatura inválida (o MP não deve reenviar; é ataque ou má config)
	  200 — processado, ou ignorável de forma permanente (o MP para de reenviar)
	  500 — falha transitória (o MP reenvia a cada 15 min, que é o desejado)
	"""
	settings = frappe.get_cached_doc("Mercado Pago Settings")

	assinatura_ok = _validar_assinatura(settings)
	if not assinatura_ok:
		frappe.local.response["http_status_code"] = 401
		return {"ok": False, "erro": "assinatura invalida"}

	corpo = _ler_corpo()
	tipo = corpo.get("type") or frappe.request.args.get("type")
	if tipo != "payment":
		# merchant_order, chargebacks etc. não interessam a este fluxo.
		return {"ok": True, "ignorado": tipo}

	payment_id = (corpo.get("data") or {}).get("id") or frappe.request.args.get("data.id")
	if not payment_id:
		return {"ok": True, "ignorado": "sem data.id"}

	try:
		resultado = processar_pagamento(settings, str(payment_id))
	except Exception as e:
		if _pagamento_inexistente(e):
			# 404 é definitivo: ou o id não existe, ou é de outra conta. Reenviar
			# não resolveria, então respondemos 200 para o Mercado Pago parar de
			# insistir a cada 15 minutos.
			frappe.log_error(
				title="Mercado Pago: notificacao de pagamento inexistente",
				message=f"payment_id={payment_id} — a API do Mercado Pago respondeu 404.",
			)
			return {"ok": True, "resultado": "pagamento_inexistente"}

		frappe.log_error(
			title="Mercado Pago: falha ao processar webhook",
			message=f"payment_id={payment_id}\n\n{frappe.get_traceback()}",
		)
		frappe.local.response["http_status_code"] = 500
		return {"ok": False, "erro": "falha transitoria"}

	return {"ok": True, "resultado": resultado}


def _pagamento_inexistente(erro: Exception) -> bool:
	"""Distingue 'este pagamento não existe' (definitivo) de falha de rede."""
	resposta = getattr(erro, "response", None)
	return getattr(resposta, "status_code", None) == 404


def _validar_assinatura(settings) -> bool:
	"""Valida x-signature. Nunca deixa passar por omissão."""
	try:
		verify(
			secret=settings.get_webhook_secret(),
			x_signature=frappe.request.headers.get("x-signature"),
			x_request_id=frappe.request.headers.get("x-request-id"),
			# O manifest usa o data.id da QUERY STRING. Se não veio, o par é
			# omitido — usar o valor do corpo aqui produziria manifest errado.
			data_id=frappe.request.args.get("data.id"),
		)
		return True
	except SignatureError as e:
		# Registra o motivo sem jamais logar a chave secreta.
		frappe.log_error(
			title="Mercado Pago: webhook com assinatura invalida",
			message=(
				f"motivo: {e}\n"
				f"x-request-id: {frappe.request.headers.get('x-request-id')}\n"
				f"data.id: {frappe.request.args.get('data.id')}\n"
				f"origem: {frappe.local.request_ip}"
			),
		)
		return False


def _ler_corpo() -> dict:
	try:
		bruto = frappe.request.get_data(as_text=True)
		return json.loads(bruto) if bruto else {}
	except (ValueError, TypeError):
		return {}


# ---------------------------------------------------------------------- #
# processamento                                                           #
# ---------------------------------------------------------------------- #


def processar_pagamento(settings, payment_id: str) -> str:
	"""Consulta o pagamento real na API do MP e dá baixa, se for o caso.

	Idempotente: reprocessar a mesma notificação não gera segundo lançamento.
	"""
	pagamento = mp_client.obter_pagamento(settings.get_access_token(), payment_id)

	if not mp_client.esta_pago(pagamento):
		return f"nao_aprovado:{pagamento.get('status')}"

	referencia = pagamento.get("external_reference")
	if not referencia:
		return "sem_external_reference"

	if not frappe.db.exists("Integration Request", referencia):
		frappe.log_error(
			title="Mercado Pago: external_reference desconhecido",
			message=f"payment_id={payment_id} external_reference={referencia}",
		)
		return "referencia_desconhecida"

	integration = frappe.get_doc("Integration Request", referencia)
	if integration.status == "Completed":
		return "ja_processado"

	dados = json.loads(integration.data or "{}")
	ref_dt = dados.get("reference_doctype")
	ref_dn = dados.get("reference_docname")
	if ref_dt != "Payment Request" or not ref_dn:
		return f"referencia_nao_suportada:{ref_dt}"

	return _baixar_payment_request(integration, ref_dn, pagamento, payment_id)


def _baixar_payment_request(integration, pr_name: str, pagamento: dict, payment_id: str) -> str:
	pr = frappe.get_doc("Payment Request", pr_name)

	if pr.status == "Paid":
		integration.handle_success(pagamento)
		return "ja_pago"

	pago = mp_client.valor_pago(pagamento)
	esperado = flt(pr.grand_total)
	if abs(pago - esperado) > TOLERANCIA_VALOR:
		# Divergência de valor nunca vira baixa automática. Fica para conferência humana.
		integration.db_set("status", "Failed", update_modified=False)
		integration.db_set(
			"error",
			json.dumps({"motivo": "valor divergente", "pago": pago, "esperado": esperado}, indent=2),
			update_modified=False,
		)
		frappe.log_error(
			title="Mercado Pago: valor divergente, baixa NAO realizada",
			message=(
				f"Payment Request: {pr_name}\n"
				f"payment_id: {payment_id}\n"
				f"esperado: {esperado}\npago: {pago}"
			),
		)
		return "valor_divergente"

	pr.run_method("set_as_paid")
	integration.db_set("request_id", str(payment_id), update_modified=False)
	integration.handle_success(pagamento)

	confirmada = confirmar_consulta(pr)
	frappe.db.commit()
	return f"pago:{confirmada}" if confirmada else "pago"


def confirmar_consulta(pr) -> str | None:
	"""Marca como Confirmada a consulta ligada à fatura deste pagamento.

	O caminho é Payment Request -> Sales Invoice -> item com
	`reference_dt = Patient Appointment`, que é o elo que o próprio Healthcare
	cria em `get_appointment_item()`.
	"""
	if pr.reference_doctype != "Sales Invoice":
		return None

	consulta = frappe.db.get_value(
		"Sales Invoice Item",
		{"parent": pr.reference_name, "reference_dt": "Patient Appointment"},
		"reference_dn",
	)
	if not consulta:
		return None

	status_atual = frappe.db.get_value("Patient Appointment", consulta, "status")
	if status_atual in ("Cancelled", "Closed", "Checked Out", "No Show"):
		# Não ressuscita consulta encerrada; o pagamento fica registrado na fatura.
		frappe.log_error(
			title="Mercado Pago: pagamento de consulta ja encerrada",
			message=f"Consulta {consulta} está em '{status_atual}' e recebeu pagamento.",
		)
		return None

	if status_atual != "Confirmed":
		frappe.db.set_value("Patient Appointment", consulta, "status", "Confirmed")
	return consulta


# ---------------------------------------------------------------------- #
# retorno do paciente (back_urls)                                         #
# ---------------------------------------------------------------------- #


@frappe.whitelist(allow_guest=True)
def retorno(**kwargs):
	"""Página para onde o paciente volta depois do checkout.

	Só informa. A baixa acontece pelo webhook, que é a via confiável —
	o paciente pode fechar o navegador antes de voltar.
	"""
	status = frappe.request.args.get("status") or frappe.request.args.get("collection_status")

	if status == "approved":
		titulo = _("Pagamento aprovado")
		mensagem = _("Recebemos seu pagamento. Sua consulta está confirmada e você receberá os detalhes por e-mail.")
		cor = "green"
	elif status in ("pending", "in_process"):
		titulo = _("Pagamento em processamento")
		mensagem = _("Seu pagamento está sendo processado. Assim que for aprovado, sua consulta será confirmada.")
		cor = "orange"
	else:
		titulo = _("Pagamento não concluído")
		mensagem = _("O pagamento não foi concluído. Você pode tentar novamente pelo link que enviamos.")
		cor = "red"

	frappe.respond_as_web_page(titulo, mensagem, indicator_color=cor)
