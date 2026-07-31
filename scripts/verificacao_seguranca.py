"""Bateria de verificação de segurança e de fluxo de dinheiro do cbm_mercadopago.

Existe porque "foi testado" tem prazo de validade. A Fase 4 rodou 66 verificações
em scripts avulsos que se perderam com a sessão; quando a Fase 5 mexeu no webhook
e criou um caminho novo de baixa automática, não havia como reprovar uma regressão.
Este arquivo é versionado justamente para poder ser re-executado antes de todo
go-live e depois de toda mudança que toque em dinheiro.

**Roda contra o ambiente de teste.** Recusa-se a executar com a trava de sandbox
desligada — não é um script para apontar para produção. Como o sistema entrou em
produção em 2026-07-31, rodar isto agora exige **ligar a trava de teste por alguns
minutos e desligar depois** (uma caixinha em `Mercado Pago Settings`, com o dono).

**Lição de 2026-08-01, gravada aqui porque se repete:** esta bateria tinha 28
verificações e mesmo assim deixou passar um defeito que quebrava *todo* pagamento
real. Os ataques batem no webhook por HTTP de verdade, mas sempre contra um
pagamento inexistente — param no 404, antes do `set_as_paid`. Os testes de dinheiro
chegam ao `set_as_paid`, mas chamam a função direto, como Administrator. O defeito
morava na **junta** entre as duas metades: o caminho do dinheiro percorrido com a
sessão real do webhook (Guest). Ao acrescentar um caso aqui, pergunte não só "que
comportamento eu testo", mas **"em que contexto de execução ele roda em produção"**.

Como rodar, de dentro do container backend:

    docker compose --project-name cbm -f /opt/cbm/gitops/compose.cbm.yaml \\
      exec -T backend bench --site <site> console <<'EOF'
    exec(compile(open("/tmp/verificacao_seguranca.py", encoding="utf-8").read(),
                 "verificacao_seguranca.py", "exec"), globals())
    EOF

Ao final imprime um placar. Qualquer REPROVADO bloqueia o go-live.
"""

import hashlib
import hmac
import json
import time
import uuid

import frappe
import requests
from frappe.utils import add_days, add_to_date, now_datetime, nowdate

from cbm_mercadopago import api, appointment, mp_client, tasks
from cbm_mercadopago.signature import build_manifest

PREFIXO = "ZZSEC"
EMPRESA = "Clinica Medica Bernardo Motta"
ITEM = "CONSULTA-ONLINE"
VALOR = 200.0

_placar = {"ok": 0, "falhou": 0}
_falhas: list[str] = []


def checar(rotulo: str, condicao: bool, detalhe: str = ""):
	if condicao:
		_placar["ok"] += 1
		print(f"  [ok]       {rotulo}")
	else:
		_placar["falhou"] += 1
		_falhas.append(rotulo)
		print(f"  [REPROVADO] {rotulo}  {detalhe}")


# --------------------------------------------------------------------------- #
# trava: esta bateria não roda apontada para produção                          #
# --------------------------------------------------------------------------- #

settings = frappe.get_doc("Mercado Pago Settings")
if not settings.use_sandbox:
	raise SystemExit(
		"ABORTADO: 'Usar ambiente de teste' está desmarcado. Esta bateria cria e "
		"cancela cobranças e nunca deve rodar contra credencial de produção."
	)

SEGREDO = settings.get_webhook_secret()
if not SEGREDO:
	raise SystemExit("ABORTADO: chave secreta do webhook não configurada.")

URL = (frappe.utils.get_url() or "").rstrip("/") + "/api/method/cbm_mercadopago.api.webhook"
print(f"alvo: {URL}")
print(f"sandbox: {settings.use_sandbox} | expiração: {settings.unpaid_expiry_minutes} min")
print()


# --------------------------------------------------------------------------- #
# A. ataques ao webhook, por HTTP de verdade                                    #
# --------------------------------------------------------------------------- #


def assinar(data_id, request_id, ts, segredo=None):
	manifest = build_manifest(data_id, request_id, str(ts))
	return hmac.new((segredo or SEGREDO).encode(), manifest.encode(), hashlib.sha256).hexdigest()


def bater(query: str, corpo: dict | str, cabecalhos: dict) -> requests.Response:
	dados = corpo if isinstance(corpo, str) else json.dumps(corpo)
	return requests.post(f"{URL}?{query}", data=dados, headers=cabecalhos, timeout=30)


def corpo_pagamento(payment_id: str) -> dict:
	return {"action": "payment.created", "type": "payment", "data": {"id": str(payment_id)}}


# Id que não existe na conta do Mercado Pago: uma assinatura válida sobre ele
# passa pela autenticação e morre no 404 da API, sem tocar em documento nenhum.
ID_INEXISTENTE = "1"

print("=== A. ATAQUES AO WEBHOOK (assinatura) ===")

agora = int(time.time())
rid = str(uuid.uuid4())

# Controle positivo: sem ele, um 401 universal (por exemplo, chave errada)
# passaria por "tudo seguro".
r = bater(
	f"data.id={ID_INEXISTENTE}&type=payment",
	corpo_pagamento(ID_INEXISTENTE),
	{
		"x-signature": f"ts={agora},v1={assinar(ID_INEXISTENTE, rid, agora)}",
		"x-request-id": rid,
		"Content-Type": "application/json",
	},
)
checar("controle positivo: assinatura válida é aceita", r.status_code == 200, f"HTTP {r.status_code}")

ataques = [
	(
		"sem cabeçalho x-signature",
		{"x-request-id": rid, "Content-Type": "application/json"},
		f"data.id={ID_INEXISTENTE}&type=payment",
		corpo_pagamento(ID_INEXISTENTE),
	),
	(
		"assinatura forjada",
		{"x-signature": f"ts={agora},v1={'0' * 64}", "x-request-id": rid, "Content-Type": "application/json"},
		f"data.id={ID_INEXISTENTE}&type=payment",
		corpo_pagamento(ID_INEXISTENTE),
	),
	(
		"assinada com o segredo errado",
		{
			"x-signature": f"ts={agora},v1={assinar(ID_INEXISTENTE, rid, agora, segredo='chave-do-atacante')}",
			"x-request-id": rid,
			"Content-Type": "application/json",
		},
		f"data.id={ID_INEXISTENTE}&type=payment",
		corpo_pagamento(ID_INEXISTENTE),
	),
	(
		"assinatura de outro pagamento (reaproveitada)",
		{
			"x-signature": f"ts={agora},v1={assinar('999999', rid, agora)}",
			"x-request-id": rid,
			"Content-Type": "application/json",
		},
		f"data.id={ID_INEXISTENTE}&type=payment",
		corpo_pagamento(ID_INEXISTENTE),
	),
	(
		"assinatura com outro x-request-id",
		{
			"x-signature": f"ts={agora},v1={assinar(ID_INEXISTENTE, 'outro-request-id', agora)}",
			"x-request-id": rid,
			"Content-Type": "application/json",
		},
		f"data.id={ID_INEXISTENTE}&type=payment",
		corpo_pagamento(ID_INEXISTENTE),
	),
	(
		"replay: timestamp velho (fora da janela de 300s)",
		{
			"x-signature": f"ts={agora - 3600},v1={assinar(ID_INEXISTENTE, rid, agora - 3600)}",
			"x-request-id": rid,
			"Content-Type": "application/json",
		},
		f"data.id={ID_INEXISTENTE}&type=payment",
		corpo_pagamento(ID_INEXISTENTE),
	),
	(
		"timestamp no futuro (relógio adulterado)",
		{
			"x-signature": f"ts={agora + 3600},v1={assinar(ID_INEXISTENTE, rid, agora + 3600)}",
			"x-request-id": rid,
			"Content-Type": "application/json",
		},
		f"data.id={ID_INEXISTENTE}&type=payment",
		corpo_pagamento(ID_INEXISTENTE),
	),
	(
		"x-signature malformado",
		{"x-signature": "nao-e-uma-assinatura", "x-request-id": rid, "Content-Type": "application/json"},
		f"data.id={ID_INEXISTENTE}&type=payment",
		corpo_pagamento(ID_INEXISTENTE),
	),
	(
		"ts não numérico",
		{"x-signature": f"ts=abc,v1={'a' * 64}", "x-request-id": rid, "Content-Type": "application/json"},
		f"data.id={ID_INEXISTENTE}&type=payment",
		corpo_pagamento(ID_INEXISTENTE),
	),
	(
		"corpo malformado, sem assinatura válida",
		{"x-signature": f"ts={agora},v1={'b' * 64}", "x-request-id": rid, "Content-Type": "application/json"},
		f"data.id={ID_INEXISTENTE}&type=payment",
		"{isto nao e json",
	),
]

for rotulo, cabecalhos, query, corpo in ataques:
	r = bater(query, corpo, cabecalhos)
	# 401 é a nossa recusa por assinatura. 417 é o Frappe barrando um corpo que
	# nem chega a virar JSON — recusa igualmente válida, feita antes do handler.
	checar(f"recusa: {rotulo}", r.status_code in (401, 417), f"HTTP {r.status_code}")

print()
print("=== B. CAMINHOS NOVOS DA FASE 5 (desvio de IPN e id assinado) ===")

# O desvio de IPN responde 200 antes da validação de assinatura. Tem que ser
# inerte: aceita e não processa. Se um dia virar bypass, estes casos reprovam.
r = bater("id=123&topic=payment", {"resource": "x", "topic": "payment"}, {"Content-Type": "application/json"})
corpo_r = r.json().get("message", r.json())
checar(
	"IPN antigo (?id=&topic=) é aceito e ignorado, sem processar",
	r.status_code == 200 and corpo_r.get("ignorado") == "ipn",
	f"HTTP {r.status_code} {corpo_r}",
)

# Com data.id presente NÃO é IPN: tem que seguir para a validação de assinatura.
r = bater(
	f"data.id={ID_INEXISTENTE}&topic=payment&type=payment",
	corpo_pagamento(ID_INEXISTENTE),
	{"x-signature": f"ts={agora},v1={'c' * 64}", "x-request-id": rid, "Content-Type": "application/json"},
)
checar(
	"desvio de IPN não vira bypass quando há data.id",
	r.status_code == 401,
	f"HTTP {r.status_code} — assinatura inválida deveria ter sido recusada",
)

# Assinatura válida para o id da query, corpo apontando outro id: o sistema tem
# que agir sobre o id ASSINADO, nunca sobre o do corpo.
rid2 = str(uuid.uuid4())
r = bater(
	f"data.id={ID_INEXISTENTE}&type=payment",
	corpo_pagamento("77777777777"),
	{
		"x-signature": f"ts={agora},v1={assinar(ID_INEXISTENTE, rid2, agora)}",
		"x-request-id": rid2,
		"Content-Type": "application/json",
	},
)
corpo_r = r.json().get("message", r.json())
checar(
	"age sobre o id assinado, não sobre o id do corpo",
	r.status_code == 200 and corpo_r.get("resultado") == "pagamento_inexistente",
	f"HTTP {r.status_code} {corpo_r}",
)

print()
print("=== C. SUPERFÍCIE EXPOSTA ===")

checar(
	"listar_gateways não existe mais como endpoint",
	not hasattr(appointment, "listar_gateways"),
	"endpoint sem checagem de permissão ainda presente",
)

expostos = []
for modulo in (api, appointment):
	for nome in dir(modulo):
		fn = getattr(modulo, nome)
		if callable(fn) and getattr(fn, "__module__", "") == modulo.__name__:
			if nome in frappe.whitelisted or getattr(fn, "__wrapped__", None) in frappe.whitelisted:
				expostos.append(f"{modulo.__name__}.{nome}")
print(f"   métodos expostos: {expostos or 'nenhum detectado por introspecção'}")

usuario_original = frappe.session.user
try:
	frappe.set_user("Guest")
	negou = False
	try:
		appointment.gerar_link_pagamento("qualquer-consulta")
	except Exception as e:
		negou = "permission" in str(e).lower() or "permit" in str(e).lower() or isinstance(
			e, (frappe.PermissionError, frappe.DoesNotExistError)
		)
	checar("gerar_link_pagamento recusa Guest", negou)
finally:
	frappe.set_user(usuario_original)

print()
print("=== D. FLUXO DE DINHEIRO ===")


def _apagar(doctype, filtros=None, cancelar=False):
	for nome in frappe.get_all(doctype, filters=filtros or {}, pluck="name"):
		try:
			if cancelar:
				d = frappe.get_doc(doctype, nome)
				if d.docstatus == 1:
					d.flags.ignore_permissions = True
					d.cancel()
			frappe.delete_doc(doctype, nome, force=True, ignore_permissions=True, delete_permanently=True)
			frappe.db.commit()
		except Exception as e:
			frappe.db.rollback()
			print(f"   aviso: não apagou {doctype} {nome}: {str(e)[:90]}")


def _limpar(verificar=True):
	"""Remove o cenário de teste. Roda sempre, inclusive se um caso reprovar.

	**Tudo aqui é filtrado pelo paciente de teste.** Um apagar sem filtro
	funcionaria hoje, com o sistema vazio, e apagaria os pacientes reais da
	clínica no dia em que existirem. Bateria de teste que destrói produção é
	pior do que bateria nenhuma.
	"""
	print()
	print("=== E. LIMPEZA ===")
	paciente = f"{PREFIXO} Paciente"

	faturas = frappe.get_all("Sales Invoice", filters={"customer": paciente}, pluck="name")
	pedidos = frappe.get_all(
		"Payment Request", filters={"reference_name": ["in", faturas or [""]]}, pluck="name"
	)
	referencias = frappe.get_all(
		"Integration Request", filters={"reference_docname": ["in", pedidos or [""]]}, pluck="name"
	)

	_apagar("Payment Entry", {"party": paciente}, cancelar=True)
	_apagar("Payment Request", {"name": ["in", pedidos or [""]]}, cancelar=True)
	_apagar("Sales Invoice", {"customer": paciente}, cancelar=True)
	_apagar("Patient Appointment", {"patient": paciente})
	_apagar("Integration Request", {"name": ["in", referencias or [""]]})
	_apagar("Contact", {"name": ["like", f"{PREFIXO}%"]})
	_apagar("Patient", {"first_name": f"{PREFIXO} Paciente"})
	_apagar("Customer", {"name": ["like", f"{PREFIXO}%"]})
	_apagar("Healthcare Practitioner", {"first_name": f"{PREFIXO} Profissional"})
	_apagar("Appointment Type", {"name": f"{PREFIXO} Consulta"})
	_apagar("Medical Department", {"name": f"{PREFIXO} Especialidade"})

	# Lançamentos órfãos: o documento de origem já não existe, então não há o
	# que preservar. Seguro mesmo com dados reais no sistema.
	orfaos = 0
	for tabela in ("GL Entry", "Payment Ledger Entry"):
		for linha in frappe.get_all(tabela, fields=["name", "voucher_type", "voucher_no"]):
			if not frappe.db.exists(linha.voucher_type, linha.voucher_no):
				frappe.db.delete(tabela, {"name": linha.name})
				orfaos += 1
	frappe.db.commit()
	print(f"   lançamentos órfãos removidos: {orfaos}")

	restante = {
		"Patient ZZSEC": frappe.db.count("Patient", {"first_name": f"{PREFIXO} Paciente"}),
		"Appointment ZZSEC": frappe.db.count("Patient Appointment", {"patient": paciente}),
		"Sales Invoice ZZSEC": frappe.db.count("Sales Invoice", {"customer": paciente}),
		"Payment Entry ZZSEC": frappe.db.count("Payment Entry", {"party": paciente}),
	}
	print(f"   sobrou do teste: {restante}")
	if verificar:
		checar("limpeza não deixou resíduo do teste", all(v == 0 for v in restante.values()), f"{restante}")


def montar_cenario(hora="09:00:00"):
	"""Cria consulta + fatura + pedido de pagamento descartáveis.

	`hora` existe porque a bateria precisa de mais de um cenário vivo ao mesmo
	tempo (o do fluxo de dinheiro e o da regressão de sessão), e o Healthcare
	recusa duas consultas do mesmo profissional no mesmo horário.
	"""
	dept = f"{PREFIXO} Especialidade"
	if not frappe.db.exists("Medical Department", dept):
		d = frappe.new_doc("Medical Department")
		d.department = dept
		d.insert(ignore_permissions=True)

	prof = frappe.db.get_value("Healthcare Practitioner", {"first_name": f"{PREFIXO} Profissional"})
	if not prof:
		p = frappe.new_doc("Healthcare Practitioner")
		p.update(
			{
				"first_name": f"{PREFIXO} Profissional",
				"status": "Active",
				"department": dept,
				"op_consulting_charge_item": ITEM,
				"op_consulting_charge": VALOR,
			}
		)
		p.insert(ignore_permissions=True)
		prof = p.name

	tipo = f"{PREFIXO} Consulta"
	if not frappe.db.exists("Appointment Type", tipo):
		t = frappe.new_doc("Appointment Type")
		t.update({"appointment_type": tipo, "default_duration": 30, "allow_booking_for": "Practitioner"})
		t.insert(ignore_permissions=True)

	pac = frappe.db.get_value("Patient", {"first_name": f"{PREFIXO} Paciente"})
	if not pac:
		x = frappe.new_doc("Patient")
		x.update({"first_name": f"{PREFIXO} Paciente", "sex": "Male", "email": "zzsec@exemplo.invalido"})
		x.insert(ignore_permissions=True)
		pac = x.name

	c = frappe.new_doc("Patient Appointment")
	c.update(
		{
			"patient": pac,
			"appointment_for": "Practitioner",
			"practitioner": prof,
			"appointment_type": tipo,
			"appointment_date": add_days(nowdate(), 7),
			"appointment_time": hora,
			"company": EMPRESA,
			"duration": 30,
			# Sem vídeo de propósito: não é o que esta bateria testa, e evita criar
			# um evento no Google Calendar da clínica a cada execução.
			"add_video_conferencing": 0,
		}
	)
	c.insert(ignore_permissions=True)
	c.reload()

	r = appointment.gerar_link_pagamento(c.name, enviar_email=0)
	frappe.db.commit()
	return c.name, r["payment_request"], r["sales_invoice"]


def pagamento_falso(external_reference, valor=VALOR, status="approved", estornado=0.0):
	return {
		"id": 999000111,
		"status": status,
		"external_reference": external_reference,
		"transaction_amount": valor,
		"transaction_amount_refunded": estornado,
	}


# Limpeza prévia: a bateria tem que ser re-executável mesmo depois de uma
# execução que morreu no meio e deixou o cenário anterior de pé (senão o
# horário fica ocupado e o Healthcare recusa por sobreposição).
_limpar(verificar=False)

consulta, pedido, fatura = montar_cenario()
referencia = frappe.db.get_value(
	"Integration Request",
	{"reference_docname": pedido, "integration_request_service": "Mercado Pago"},
	"name",
)
print(f"   cenário: consulta={consulta} pedido={pedido} referência={referencia}")

obter_original = mp_client.obter_pagamento
buscar_original = mp_client.buscar_pagamentos

try:
	# --- REGRESSÃO: a baixa funciona na SESSÃO DO WEBHOOK (Guest)? ---
	#
	# Este caso existe por causa de um defeito real, achado só no primeiro
	# pagamento de produção (2026-07-31). O webhook é `allow_guest=True`, então
	# roda como Guest; o `set_as_paid` do ERPNext dispara um gancho do HRMS
	# (`hrms/overrides/employee_payment_entry.py:239`) que faz
	# `frappe.has_permission(..., throw=True)` contra o usuário da sessão.
	# Guest nunca passa: todo pagamento morria com PermissionError e só era
	# salvo pela conciliação de 5 minutos, minutos depois.
	#
	# **Por que a bateria de 28 não pegou** — e é a lição que este caso carrega:
	# os testes de ataque batem no webhook por HTTP de verdade, mas sempre com
	# um pagamento inexistente, então param no 404 sem nunca chegar ao
	# `set_as_paid`. Os testes de dinheiro chegam ao `set_as_paid`, mas chamam
	# `processar_pagamento` direto, como Administrator. O buraco era exatamente
	# a interseção: caminho do dinheiro **com a sessão do webhook**. Testar as
	# duas metades separadamente não testa a junta entre elas.
	consulta_g, pedido_g, fatura_g = montar_cenario(hora="10:00:00")
	referencia_g = frappe.db.get_value(
		"Integration Request",
		{"reference_docname": pedido_g, "integration_request_service": "Mercado Pago"},
		"name",
	)
	mp_client.obter_pagamento = lambda t, i: pagamento_falso(referencia_g)

	usuario_antes = frappe.session.user
	erro_guest = None
	try:
		frappe.set_user("Guest")
		resultado_g = api.processar_pagamento(settings, "999000222")
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		erro_guest = f"{type(e).__name__}: {str(e)[:120]}"
		resultado_g = None
	finally:
		frappe.set_user(usuario_antes)

	checar(
		"baixa funciona na sessão do webhook (Guest), sem PermissionError",
		erro_guest is None and str(resultado_g or "").startswith("pago"),
		f"erro={erro_guest} resultado={resultado_g}",
	)
	checar(
		"a baixa como Guest confirma a consulta",
		frappe.db.get_value("Patient Appointment", consulta_g, "status") == "Confirmed",
		f"status={frappe.db.get_value('Patient Appointment', consulta_g, 'status')}",
	)

	# --- valor menor que o cobrado: nunca vira baixa automática ---
	mp_client.obter_pagamento = lambda t, i: pagamento_falso(referencia, valor=VALOR - 50)
	resultado = api.processar_pagamento(settings, "999000111")
	frappe.db.commit()
	checar(
		"valor divergente não dá baixa",
		resultado == "valor_divergente" and frappe.db.get_value("Payment Request", pedido, "status") != "Paid",
		f"resultado={resultado}",
	)

	# --- external_reference desconhecido ---
	mp_client.obter_pagamento = lambda t, i: pagamento_falso("referencia-que-nao-existe")
	resultado = api.processar_pagamento(settings, "999000111")
	checar(
		"external_reference desconhecido é ignorado",
		resultado == "referencia_desconhecida",
		f"resultado={resultado}",
	)

	# --- pagamento não aprovado ---
	mp_client.obter_pagamento = lambda t, i: pagamento_falso(referencia, status="pending")
	resultado = api.processar_pagamento(settings, "999000111")
	checar("pagamento pendente não dá baixa", resultado.startswith("nao_aprovado"), f"resultado={resultado}")

	# --- estorno total zera o valor considerado ---
	frappe.db.set_value("Integration Request", referencia, "status", "Queued")
	mp_client.obter_pagamento = lambda t, i: pagamento_falso(referencia, estornado=VALOR)
	resultado = api.processar_pagamento(settings, "999000111")
	frappe.db.commit()
	checar("estorno total não dá baixa", resultado == "valor_divergente", f"resultado={resultado}")

	# --- a rotina não cancela quando a API está fora do ar ---
	def api_fora(token, ref):
		raise RuntimeError("simulando API do Mercado Pago fora do ar")

	mp_client.buscar_pagamentos = api_fora
	checar(
		"API fora do ar não libera o horário",
		tasks._pagou_sem_avisar(settings, pedido) is True,
		"deveria devolver True para impedir o cancelamento",
	)

	# --- cobrança sem pagamento nenhum: pode expirar ---
	mp_client.buscar_pagamentos = lambda t, ref: []
	checar("cobrança sem pagamento não é conciliada", tasks._pagou_sem_avisar(settings, pedido) is False)

	# --- pagamento aprovado sem notificação: a rotina encontra e dá baixa ---
	frappe.db.set_value("Integration Request", referencia, "status", "Queued")
	frappe.db.commit()
	mp_client.buscar_pagamentos = lambda t, ref: [pagamento_falso(ref)] if ref == referencia else []
	mp_client.obter_pagamento = lambda t, i: pagamento_falso(referencia)

	conciliou = tasks._pagou_sem_avisar(settings, pedido)
	frappe.db.commit()
	checar(
		"pagamento sem notificação é conciliado pela rotina",
		conciliou is True and frappe.db.get_value("Payment Request", pedido, "status") == "Paid",
		f"status do pedido={frappe.db.get_value('Payment Request', pedido, 'status')}",
	)
	checar(
		"a conciliação confirma a consulta",
		frappe.db.get_value("Patient Appointment", consulta, "status") == "Confirmed",
	)
	checar(
		"a conciliação quita a fatura",
		frappe.db.get_value("Sales Invoice", fatura, "outstanding_amount") == 0,
	)

	# --- idempotência: reprocessar não duplica lançamento ---
	lancamentos_antes = frappe.db.count("Payment Entry")
	api.processar_pagamento(settings, "999000111")
	frappe.db.commit()
	checar(
		"reprocessar o mesmo pagamento não duplica lançamento",
		frappe.db.count("Payment Entry") == lancamentos_antes,
		f"antes={lancamentos_antes} depois={frappe.db.count('Payment Entry')}",
	)

	# --- cobrança já paga não é candidata a expirar ---
	pendentes = frappe.get_all(
		"Payment Request",
		filters={"docstatus": 1, "status": ["in", ["Requested", "Initiated"]], "payment_gateway": "Mercado Pago"},
		pluck="name",
	)
	checar("cobrança paga sai da fila de expiração", pedido not in pendentes, f"pendentes={pendentes}")

finally:
	mp_client.obter_pagamento = obter_original
	mp_client.buscar_pagamentos = buscar_original
	# A limpeza mora aqui de propósito: se um caso reprovar no meio, o cenário
	# não pode ficar largado no sistema esperando a próxima sessão.
	_limpar()

print()
print("=" * 62)
print(f"  APROVADOS: {_placar['ok']}    REPROVADOS: {_placar['falhou']}")
if _falhas:
	print("  reprovados:")
	for f in _falhas:
		print(f"    - {f}")
	print("\n  >>> GO-LIVE BLOQUEADO <<<")
else:
	print("  >>> bateria completa aprovada <<<")
print("=" * 62)
