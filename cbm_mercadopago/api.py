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

from cbm_mercadopago import appointment, mp_client
from cbm_mercadopago.signature import SignatureError, verify_candidatos

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
	if _e_ipn_antigo():
		return {"ok": True, "ignorado": "ipn"}

	settings = frappe.get_cached_doc("Mercado Pago Settings")
	corpo = _ler_corpo()

	assinatura_confere, payment_id = _validar_assinatura(settings, corpo)
	if not assinatura_confere:
		frappe.local.response["http_status_code"] = 401
		return {"ok": False, "erro": "assinatura invalida"}

	tipo = corpo.get("type") or frappe.request.args.get("type")
	if tipo != "payment":
		# merchant_order, chargebacks etc. não interessam a este fluxo.
		return {"ok": True, "ignorado": tipo}

	# Agimos **só** sobre o id que a assinatura cobre. Ler o id do corpo seria
	# agir sobre algo que ninguém assinou: quem tivesse uma assinatura válida
	# de uma notificação nossa poderia trocar o alvo. As conferências seguintes
	# (external_reference, valor, idempotência) já barrariam prejuízo, mas isso
	# é segurança por consequência; aqui ela passa a ser por construção.
	if not payment_id:
		return {"ok": True, "ignorado": "sem data.id assinado"}

	# Daqui em diante o pedido já está autenticado pela assinatura — o mesmo
	# nível de confiança que a rotina de conciliação tem ao rodar como
	# Administrator. Sem isso, `pr.run_method("set_as_paid")` esbarra num
	# `frappe.has_permission(..., throw=True)` de dentro do hrms
	# (set_missing_ref_details) checando o usuário Guest da sessão do webhook,
	# e todo pagamento cai para a conciliação em vez de confirmar na hora.
	usuario_original = frappe.session.user
	frappe.set_user("Administrator")
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
	finally:
		frappe.set_user(usuario_original)

	return {"ok": True, "resultado": resultado}


def _e_ipn_antigo() -> bool:
	"""O Mercado Pago avisa o mesmo evento duas vezes, em dois formatos.

	O webhook moderno chega como `?data.id=...&type=payment`, com corpo JSON e
	assinatura que confere com a chave do painel. O IPN antigo chega como
	`?id=...&topic=payment`, com uma assinatura que **não** confere com essa
	chave — e virava um 401 a cada pagamento, com cara de ataque. Foi o que fez
	a Fase 4 parecer ter um bug de assinatura.

	Ignorar o IPN não perde evento nenhum: os dois formatos chegam juntos, e
	nunca agimos com base no IPN. Se ainda assim um aviso se perder, a rotina
	de expiração confere na API do Mercado Pago antes de liberar o horário.
	"""
	args = frappe.request.args
	return bool(args.get("topic")) and not args.get("data.id")


def _pagamento_inexistente(erro: Exception) -> bool:
	"""Distingue 'este pagamento não existe' (definitivo) de falha de rede."""
	resposta = getattr(erro, "response", None)
	return getattr(resposta, "status_code", None) == 404


def _validar_assinatura(settings, corpo: dict) -> tuple[bool, str | None]:
	"""Valida x-signature. Nunca deixa passar por omissão.

	Devolve `(confere, data_id_assinado)`. O segundo item é o id que fechou a
	conta do HMAC — é o único que o chamador pode usar como alvo.
	"""
	da_query = frappe.request.args.get("data.id") or frappe.request.args.get("id")
	do_corpo = str((corpo.get("data") or {}).get("id") or "") or None

	try:
		assinado = verify_candidatos(
			secret=settings.get_webhook_secret(),
			x_signature=frappe.request.headers.get("x-signature"),
			x_request_id=frappe.request.headers.get("x-request-id"),
			# O Mercado Pago assina com o `data.id`, mas nem sempre o repete na
			# query string: nas notificações reais do Checkout Pro ele vem só no
			# corpo. Tentamos as duas origens — cada uma é um HMAC completo, então
			# aceitar as duas não afrouxa a validação.
			data_ids=[da_query, do_corpo],
		)
		return True, assinado
	except SignatureError as e:
		_registrar_falha_de_assinatura(settings, e, da_query, do_corpo, corpo)
		return False, None


def _registrar_falha_de_assinatura(settings, erro, da_query, do_corpo, corpo):
	"""Registra o suficiente para diagnosticar — nunca a chave secreta.

	Uma assinatura recusada pode ser ataque ou configuração errada, e
	distinguir os dois sem esses detalhes é praticamente impossível.
	"""
	from cbm_mercadopago.signature import build_manifest, parse_x_signature

	cabecalho = frappe.request.headers.get("x-signature") or ""
	request_id = frappe.request.headers.get("x-request-id")
	linhas = [
		f"motivo: {erro}",
		f"origem: {frappe.local.request_ip}",
		f"x-signature: {cabecalho}",
		f"x-request-id: {request_id}",
		f"query string: {dict(frappe.request.args)}",
		f"data.id na query: {da_query!r}",
		f"data.id no corpo: {do_corpo!r}",
		f"corpo: {frappe.as_json(corpo)[:400]}",
	]

	# Registra QUAIS manifests foram tentados, mas nunca o HMAC que cada um
	# produziria. O atacante escolhe `data.id` e `x-request-id`, ou seja, escolhe
	# o texto assinado: publicar o resultado do cálculo transformaria este
	# registro num oráculo de assinaturas para quem tivesse acesso ao Error Log.
	# O despejo dos HMACs existiu para caçar a recusa de assinatura da Fase 4 —
	# que acabou sendo o formato IPN antigo, não o algoritmo. Propósito cumprido.
	try:
		ts, recebido = parse_x_signature(cabecalho)
		linhas.append(f"v1 recebido: {recebido}")
		for rotulo, valor in (("query", da_query), ("corpo", do_corpo), ("sem id", None)):
			linhas.append(f"  [{rotulo}] manifest tentado: {build_manifest(valor, request_id, ts)!r}")
	except Exception:
		pass

	frappe.log_error(title="Mercado Pago: webhook com assinatura invalida", message="\n".join(linhas))


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
		# `db.set_value` não dispara gatilho de documento — de propósito, para
		# não rodar as validações da consulta no meio de uma baixa de
		# pagamento. Por isso o aviso ao paciente é chamado aqui, na mão.
		appointment.disparar_aviso_de_confirmacao(consulta)

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
