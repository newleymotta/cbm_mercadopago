"""Rotina agendada, a cada 5 minutos.

Faz duas coisas com as cobranças pendentes, nesta ordem:

1. **Concilia.** Pergunta à API do Mercado Pago se já existe pagamento
   aprovado. É a rede de segurança contra o pior cenário do projeto — o
   paciente paga, o dinheiro entra e o sistema não registra — que acontece
   sempre que uma notificação se perde: servidor reiniciando, rede, ou
   notificação recusada. Aqui a fonte de verdade é a API, não o aviso.
2. **Expira.** Só então libera o horário das que continuam sem pagamento há
   mais tempo que o configurado. Necessário porque o Healthcare cria a
   consulta antes do pagamento e nunca libera o horário sozinho.
"""

import frappe
from frappe.utils import add_to_date, cint, now_datetime

GATEWAY = "Mercado Pago"

# Só expira consulta que ainda está esperando o paciente aparecer.
STATUS_EXPIRAVEIS = {"Scheduled", "Open"}

try:
	from cbm_whatsapp.automation import queue_payment_confirmation_whatsapp
except Exception:  # pragma: no cover - app may not be installed yet in local test env
	queue_payment_confirmation_whatsapp = None


def expirar_agendamentos_nao_pagos():
	"""Concilia as cobranças pendentes e expira as que continuam sem pagamento.

	Deliberadamente conservadora: só olha consultas que têm um pedido de
	pagamento pendente pelo Mercado Pago. Consulta criada sem cobrança (paciente
	que vai pagar por outro meio, cortesia, retorno) nunca é tocada.
	"""
	settings = frappe.get_cached_doc("Mercado Pago Settings")
	if not settings.enabled:
		return

	minutos = cint(settings.unpaid_expiry_minutes)
	limite = add_to_date(now_datetime(), minutes=-minutos) if minutos > 0 else None

	# Sem filtro de data: a conciliação vale para toda cobrança pendente, não só
	# para as vencidas. Um pagamento cuja notificação se perdeu precisa ser
	# encontrado em minutos, não depois do prazo de expiração inteiro.
	pendentes = frappe.get_all(
		"Payment Request",
		filters={
			"docstatus": 1,
			"status": ["in", ["Requested", "Initiated"]],
			"payment_gateway": GATEWAY,
			"reference_doctype": "Sales Invoice",
		},
		fields=["name", "reference_name", "creation"],
	)

	for pendente in pendentes:
		try:
			if _pagou_sem_avisar(settings, pendente.name):
				# Dinheiro entrou e o aviso se perdeu. Nunca cancelar.
				frappe.db.commit()
				continue

			if limite and pendente.creation < limite:
				_expirar(pendente.name, pendente.reference_name)
				frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title="Mercado Pago: falha ao conciliar ou expirar consulta",
				message=f"Payment Request: {pendente.name}\n\n{frappe.get_traceback()}",
			)


def _pagou_sem_avisar(settings, payment_request: str) -> bool:
	"""Rede de segurança: confere na API do MP antes de liberar o horário.

	A notificação do Mercado Pago pode se perder — servidor reiniciando, rede,
	erro transitório. Sem esta conferência, o pior cenário possível acontece em
	silêncio: o paciente paga, o dinheiro entra e o sistema cancela a consulta.
	Aqui a fonte de verdade é a API, não o aviso.

	Falha de rede nunca vira cancelamento: qualquer erro devolve True, e a
	consulta fica de pé até a próxima rodada.
	"""
	from cbm_mercadopago import api, mp_client

	# Um mesmo pedido pode ter mais de uma cobrança (link gerado duas vezes),
	# e cada uma tem o seu `external_reference`. Conferimos todas.
	referencias = frappe.get_all(
		"Integration Request",
		filters={"reference_docname": payment_request, "integration_request_service": GATEWAY},
		pluck="name",
	)
	if not referencias:
		return False

	aprovado = None
	try:
		token = settings.get_access_token()
		for referencia in referencias:
			pagamentos = mp_client.buscar_pagamentos(token, referencia)
			aprovado = next((p for p in pagamentos if mp_client.esta_pago(p)), None)
			if aprovado:
				break
	except Exception:
		frappe.log_error(
			title="Mercado Pago: falha ao conferir pagamento antes de expirar",
			message=f"Payment Request: {payment_request}\n\n{frappe.get_traceback()}",
		)
		return True

	if not aprovado:
		return False

	frappe.log_error(
		title="Mercado Pago: pagamento aprovado sem notificacao, baixa pela rotina",
		message=(
			f"Payment Request: {payment_request}\n"
			f"payment_id: {aprovado.get('id')}\n"
			"A notificacao do Mercado Pago nao chegou; a baixa foi feita pela rotina de expiracao."
		),
	)
	api.processar_pagamento(settings, str(aprovado.get("id")))
	return True


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


def _disparar_whatsapp_pagamento(payment_request_name: str):
	if not queue_payment_confirmation_whatsapp:
		return
	try:
		queue_payment_confirmation_whatsapp(payment_request_name)
	except Exception:
		frappe.log_error(
			title="Mercado Pago: falha ao avisar pagamento por WhatsApp na rotina",
			message=f"Payment Request: {payment_request_name}\n\n{frappe.get_traceback()}",
		)
