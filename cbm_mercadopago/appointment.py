"""Geração do link de pagamento a partir de uma consulta.

Fluxo: Patient Appointment -> Sales Invoice (em aberto) -> Payment Request.

Passamos pela fatura porque `ALLOWED_DOCTYPES_FOR_PAYMENT_REQUEST` do ERPNext
não inclui `Patient Appointment`. Fazendo pelo caminho nativo ganhamos de graça
a geração da URL, o `set_as_paid()` e a proteção nativa contra pagamento em
duplicidade — sem alterar constante nenhuma do core.

O envio do e-mail, esse sim, é nosso: o ERPNext só envia lá dentro do
`before_submit` do Payment Request, então um pedido já existente nunca
reenviaria nada (ver `_enviar_link_por_email`).
"""

import frappe
from frappe import _

from cbm_mercadopago import envio

GATEWAY = "Mercado Pago"

# Situações em que não faz sentido cobrar.
STATUS_BLOQUEADOS = {"Cancelled", "Closed", "Checked Out", "No Show"}

# Gatilho que a `Notification` "Consulta confirmada" escuta
# (na tela do aviso: Evento = "Method", Método = este valor).
EVENTO_CONFIRMACAO = "cbm_pagamento_confirmado"


@frappe.whitelist()
def gerar_link_pagamento(appointment: str, enviar_email: int = 0, gateway: str | None = None) -> dict:
	"""Devolve (e opcionalmente envia por e-mail) o link de pagamento.

	`gateway` é opcional. Quando há um só meio configurado, ele é usado
	automaticamente; quando há mais de um e nenhum foi escolhido, devolve
	`escolher_gateway` para a tela perguntar. Isso permite ligar outro meio
	(Stripe, por exemplo) apenas configurando, sem mexer no código.

	Quando `enviar_email` é pedido, a resposta traz `enviado_para` com o
	endereço que realmente recebeu — é o que a tela mostra. Se não houver como
	enviar, a chamada falha antes de criar coisa alguma, e nada é anunciado.
	"""
	doc = frappe.get_doc("Patient Appointment", appointment)
	doc.check_permission("write")

	enviar = bool(int(enviar_email or 0))

	_validar_consulta(doc, gateway)

	conta = _resolver_conta_gateway(gateway)
	if isinstance(conta, dict):
		return conta  # a tela precisa perguntar qual meio usar

	email_paciente = email_do_paciente(doc.patient)

	# Conferido antes de criar fatura ou pedido de pagamento: assim um envio
	# impossível não deixa documento pela metade para trás.
	if enviar:
		_exigir_condicoes_de_envio(email_paciente)

	sales_invoice = _garantir_fatura(doc)
	payment_request = _garantir_payment_request(sales_invoice, conta, email_paciente)

	if not payment_request.payment_url:
		# Sintoma clássico de falha engolida pelo `payment_gateway_validation()`.
		frappe.throw(
			_(
				"O pedido de pagamento foi criado mas o Mercado Pago não devolveu a URL. "
				"Verifique o Mercado Pago Settings e o Registro de Erros."
			)
		)

	resultado = {
		"payment_url": payment_request.payment_url,
		"payment_request": payment_request.name,
		"sales_invoice": sales_invoice,
	}

	if enviar:
		_enviar_link_por_email(payment_request, email_paciente)
		resultado["enviado_para"] = email_paciente

	return resultado


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


def email_do_paciente(patient: str) -> str | None:
	return (frappe.db.get_value("Patient", patient, "email") or "").strip() or None


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


def _garantir_payment_request(sales_invoice: str, conta_gateway: str, email_paciente: str | None):
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
		# Sem `recipient_id` o ERPNext usa o dono da fatura, ou seja, quem a
		# criou na clínica. Isso erraria o destinatário do e-mail e, pior,
		# abriria o Checkout Pro com o e-mail da própria clínica como pagador
		# (`payer_email`), que o Mercado Pago recusa como "pagar para si mesmo".
		recipient_id=email_paciente,
		# O envio é sempre nosso: o ERPNext só envia dentro do `before_submit`,
		# então um pedido já existente nunca reenviaria.
		mute_email=True,
		return_doc=True,
	)

	nome = resultado.get("name") if isinstance(resultado, dict) else getattr(resultado, "name", None)
	if not nome:
		frappe.throw(_("Não foi possível criar o pedido de pagamento."))
	return frappe.get_doc("Payment Request", nome)


# ---------------------------------------------------------------------- #
# envio do link por e-mail                                                #
# ---------------------------------------------------------------------- #


def _tem_conta_de_envio() -> bool:
	from frappe.email.doctype.email_account.email_account import EmailAccount

	try:
		return bool(EmailAccount.find_default_outgoing())
	except Exception:
		return False


def _exigir_condicoes_de_envio(email_paciente: str | None):
	motivo = envio.motivo_para_nao_enviar(email_paciente, _tem_conta_de_envio())

	if motivo == envio.SEM_CONTA:
		frappe.throw(
			_(
				"Nenhuma conta de e-mail está configurada para envio, então nada foi enviado. "
				"Abra a lista <b>Conta de E-mail</b> e configure a conta de saída da clínica."
			),
			title=_("Não foi possível enviar"),
		)

	if motivo == envio.SEM_EMAIL:
		frappe.throw(
			_(
				"O paciente não tem e-mail cadastrado, então nada foi enviado. "
				"Preencha o e-mail no cadastro do paciente e tente de novo."
			),
			title=_("Não foi possível enviar"),
		)


def _enviar_link_por_email(pr, email_paciente: str):
	"""Envia o link de pagamento ao paciente, na hora.

	Síncrono de propósito. O `send_email()` do ERPNext usa `enqueue`, então uma
	falha de SMTP sumiria num job em segundo plano e a tela anunciaria sucesso
	sem ter enviado nada. Aqui, se o envio falhar, o erro aparece na tela.
	"""
	if pr.email_to != email_paciente:
		pr.db_set("email_to", email_paciente, update_modified=False)
		pr.email_to = email_paciente

	_garantir_texto_com_link(pr)

	frappe.sendmail(
		recipients=[email_paciente],
		# O assunto nativo é "Payment Request for ACC-SINV-…", que não diz nada
		# a um paciente.
		subject=_("Link de pagamento da sua consulta"),
		message=pr.get_message(),
		reference_doctype=pr.reference_doctype,
		reference_name=pr.reference_name,
		# É um e-mail transacional de cobrança, não mala direta.
		add_unsubscribe_link=0,
		now=True,
	)


def _garantir_texto_com_link(pr):
	"""Impede que o e-mail saia sem o link.

	O texto é copiado da conta de gateway para o pedido no momento da criação.
	O texto de fábrica do ERPNext ("Please click on the link below…") não tem
	`{{ payment_url }}`, e um pedido criado antes da correção carregaria esse
	texto para sempre — o paciente receberia um e-mail sem link nenhum.
	"""
	if "payment_url" not in (pr.message or ""):
		da_conta = frappe.db.get_value("Payment Gateway Account", pr.payment_gateway_account, "message")
		if da_conta and "payment_url" in da_conta:
			pr.message = da_conta

	if "payment_url" not in (pr.message or ""):
		frappe.throw(
			_(
				"O texto de e-mail da conta de gateway {0} não contém {{{{ payment_url }}}}, "
				"então o paciente receberia um e-mail sem o link. Nada foi enviado: "
				"corrija o campo <b>Mensagem</b> dessa conta."
			).format(pr.payment_gateway_account),
			title=_("Não foi possível enviar"),
		)


# ---------------------------------------------------------------------- #
# aviso de consulta confirmada                                            #
# ---------------------------------------------------------------------- #


def disparar_aviso_de_confirmacao(consulta: str):
	"""Dispara o aviso configurável de consulta confirmada.

	Existe porque uma `Notification` do tipo "Value Change" não serviria para o
	caminho do pagamento: o webhook grava o status com `frappe.db.set_value`,
	que por definição não passa pelos gatilhos do documento. Chamamos o gatilho
	explicitamente, e o texto do e-mail continua editável na tela pelo dono.

	Nunca deixa o erro subir: `evaluate_alert` relança qualquer falha de
	template, e um texto de e-mail quebrado não pode desfazer um pagamento que
	já entrou.
	"""
	try:
		doc = frappe.get_doc("Patient Appointment", consulta)
		_sincronizar_email_do_paciente(doc)
		doc.run_method(EVENTO_CONFIRMACAO)
	except Exception:
		frappe.log_error(
			title="CBM: falha ao enviar aviso de consulta confirmada",
			message=f"Consulta: {consulta}\n\n{frappe.get_traceback()}",
		)


def _sincronizar_email_do_paciente(doc):
	"""Mantém `patient_email` em dia antes de disparar o aviso.

	O campo é preenchido por `fetch_from` quando a consulta é salva. Se o
	e-mail do paciente for cadastrado ou corrigido depois disso, o valor
	guardado fica velho e o aviso iria para o endereço errado — ou para lugar
	nenhum.
	"""
	if not doc.meta.has_field("patient_email"):
		return

	atual = email_do_paciente(doc.patient)
	if atual and doc.get("patient_email") != atual:
		doc.db_set("patient_email", atual, update_modified=False)
		doc.patient_email = atual


def notificar_confirmacao_manual(doc, method=None):
	"""Aviso quando alguém marca a consulta como Confirmada na tela.

	O caminho do pagamento não passa por aqui (o webhook usa `db.set_value`,
	que não dispara gatilho nenhum), então não existe risco de aviso duplicado.
	"""
	if doc.status != "Confirmed":
		return

	anterior = doc.get_doc_before_save()
	if not anterior or anterior.status == doc.status:
		# Sem documento anterior é criação de consulta: aí o link do Meet ainda
		# não existe (é gerado no `after_insert`), e o aviso sairia sem ele.
		return

	disparar_aviso_de_confirmacao(doc.name)


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
