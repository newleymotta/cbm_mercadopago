"""Agendamento pelo próprio paciente, a partir do site público.

Antes disto, marcar consulta era trabalho humano: o paciente falava no WhatsApp,
alguém da clínica abria o painel, criava a consulta e gerava o link de pagamento.
Este módulo abre a porta da frente — da confirmação em diante, tudo o que já
existia continua valendo (webhook assinado, baixa, Meet, e-mail).

**O motor de horários é do Healthcare, não nosso.** Reaproveitamos
`get_available_slots`, que já trata dia da semana, consultas existentes,
capacidade e sobreposição. O que fazemos aqui é o que ele não faz: fatiar as
faixas na duração do serviço, tirar o que já passou e **devolver só o que é
seguro mostrar a um anônimo**.

## As três regras que este arquivo existe para cumprir

1. **Nada é agendável até ser marcado.** `cbm_agendavel_online` nasce desligado.
   Sem opt-in explícito, nenhum profissional aparece — é o que impede cadastro
   de teste ou ficha pela metade de virar oferta pública.

2. **`appointments` nunca sai daqui.** `get_available_slots` devolve, junto com
   os horários livres, a lista de consultas já marcadas. Para um visitante
   anônimo isso é "quem tem consulta com quem e quando" — dado de saúde. Usamos
   internamente para calcular o que está ocupado e **montamos a resposta do
   zero**, campo a campo, em vez de repassar o retorno.

3. **Não se coleta dado de saúde aqui.** Nome, e-mail e telefone, e só. O
   `/patient-registration` que veio de fábrica pede alergias, medicamentos e
   histórico médico sem login — risco registrado na Fase 6 e que este fluxo
   **não repete**. Informação clínica se colhe na consulta, com profissional.
"""

import datetime

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import get_datetime, getdate, now_datetime

CAMPO_AGENDAVEL = "cbm_agendavel_online"


def _praticantes_agendaveis() -> list[str]:
	return frappe.get_all(
		"Healthcare Practitioner",
		filters={"status": "Active", CAMPO_AGENDAVEL: 1},
		pluck="name",
	)


def _exigir_agendavel(profissional: str):
	"""Porta única: nada neste módulo age sobre quem não foi liberado."""
	if profissional not in _praticantes_agendaveis():
		frappe.throw(_("Profissional não disponível para agendamento online."))


def _servicos_do_profissional(nome: str) -> set[str]:
	texto = frappe.db.get_value("Healthcare Practitioner", nome, "cbm_servicos") or ""
	return {linha.strip() for linha in texto.split("\n") if linha.strip()}


def _preco_e_duracao(servico: str) -> tuple[float, int]:
	doc = frappe.get_cached_doc("Appointment Type", servico)
	preco = 0.0
	for linha in doc.get("items") or []:
		if (linha.get("op_consulting_charge") or 0) > 0:
			preco = float(linha.op_consulting_charge)
			break
	return preco, int(doc.default_duration or 30)


# --------------------------------------------------------------------------- #
# leitura pública                                                              #
# --------------------------------------------------------------------------- #


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=120, seconds=3600)
def servicos():
	"""Serviços que ao menos um profissional liberado oferece."""
	oferecidos = set()
	for p in _praticantes_agendaveis():
		oferecidos |= _servicos_do_profissional(p)
	if not oferecidos:
		return []

	saida = []
	for nome in sorted(oferecidos):
		if not frappe.db.exists("Appointment Type", nome):
			continue
		preco, duracao = _preco_e_duracao(nome)
		if preco <= 0:
			continue  # sem preço não se cobra, e sem cobrança não se agenda por aqui
		saida.append({"servico": nome, "preco": preco, "duracao": duracao})
	return saida


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=120, seconds=3600)
def profissionais(servico: str):
	"""Quem atende esse serviço. Só nome e especialidade — nada além."""
	saida = []
	for p in _praticantes_agendaveis():
		if servico not in _servicos_do_profissional(p):
			continue
		d = frappe.db.get_value(
			"Healthcare Practitioner", p, ["name", "practitioner_name", "department"], as_dict=True
		)
		saida.append(
			{
				"id": d.name,
				"nome": d.practitioner_name,
				"especialidade": d.department,
			}
		)
	return sorted(saida, key=lambda x: x["nome"] or "")


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=300, seconds=3600)
def horarios(profissional: str, servico: str, data: str):
	"""Horários livres de um profissional num dia.

	Devolve **apenas** uma lista de horas ("09:00"). A lista de consultas já
	marcadas, que o motor entrega junto, é usada para calcular o que está
	ocupado e **descartada** — ver a regra 2 no topo do arquivo.
	"""
	_exigir_agendavel(profissional)
	if servico not in _servicos_do_profissional(profissional):
		frappe.throw(_("Esse profissional não atende esse serviço."))

	dia = getdate(data)
	if dia < getdate(now_datetime()):
		return []

	_, duracao = _preco_e_duracao(servico)
	doc = frappe.get_doc("Healthcare Practitioner", profissional)

	from healthcare.healthcare.doctype.patient_appointment.patient_appointment import (
		get_available_slots,
	)

	try:
		blocos = get_available_slots(doc, dia) or []
	except Exception:
		# Feriado, licença ou agenda mal configurada: para o visitante o efeito
		# é o mesmo — não há horário. O detalhe fica no log, não na tela dele.
		frappe.log_error(
			title="Agendamento: falha ao consultar horários",
			message=f"profissional={profissional} data={data}\n\n{frappe.get_traceback()}",
		)
		return []

	agora = now_datetime()
	livres: set[str] = set()

	for bloco in blocos:
		ocupados = _minutos_ocupados(bloco, dia)
		for faixa in bloco.get("avail_slot") or []:
			inicio = get_datetime(f"{dia} {faixa.from_time}")
			fim = get_datetime(f"{dia} {faixa.to_time}")
			atual = inicio
			while atual + datetime.timedelta(minutes=duracao) <= fim:
				termina = atual + datetime.timedelta(minutes=duracao)
				if atual > agora and not _colide(atual, termina, ocupados):
					livres.add(atual.strftime("%H:%M"))
				atual = termina

	return sorted(livres)


def _minutos_ocupados(bloco: dict, dia) -> list[tuple]:
	"""Converte as consultas já marcadas em intervalos, sem expor nada delas."""
	ocupados = []
	for ap in bloco.get("appointments") or []:
		hora = ap.get("appointment_time")
		duracao = int(ap.get("duration") or 0)
		if not hora:
			continue
		comeco = get_datetime(f"{dia} {hora}")
		ocupados.append((comeco, comeco + datetime.timedelta(minutes=duracao or 30)))
	return ocupados


def _colide(inicio, fim, ocupados) -> bool:
	return any(inicio < o_fim and o_inicio < fim for o_inicio, o_fim in ocupados)


# --------------------------------------------------------------------------- #
# escrita pública                                                              #
# --------------------------------------------------------------------------- #


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=10, seconds=3600)
def solicitar(nome: str, email: str, telefone: str, servico: str, profissional: str, data: str, hora: str):
	"""Cria a consulta e devolve o link de pagamento.

	Roda como Administrator **depois** de validar tudo, pelo mesmo motivo de
	`api.processar_pagamento`: criar paciente e consulta exige permissões que um
	visitante não tem, e a autorização aqui vem das checagens abaixo, não da
	sessão. Só se chega aqui com profissional liberado, serviço que ele atende e
	horário que o motor confirmou livre.
	"""
	_exigir_agendavel(profissional)

	nome = (nome or "").strip()
	email = (email or "").strip().lower()
	telefone = (telefone or "").strip()
	if not nome or not email:
		frappe.throw(_("Informe seu nome e e-mail."))
	if "@" not in email or "." not in email.split("@")[-1]:
		frappe.throw(_("Esse e-mail não parece válido."))

	if hora not in horarios(profissional=profissional, servico=servico, data=data):
		# Também cobre a corrida entre duas pessoas no mesmo horário: quem chega
		# depois não encontra mais o horário na lista.
		frappe.throw(_("Esse horário acabou de ser ocupado. Escolha outro, por favor."))

	usuario_original = frappe.session.user
	frappe.set_user("Administrator")
	try:
		paciente = _obter_ou_criar_paciente(nome, email, telefone)
		consulta = _criar_consulta(paciente, profissional, servico, data, hora)

		from cbm_mercadopago import appointment as agendamento_pago

		resultado = agendamento_pago.gerar_link_pagamento(consulta, enviar_email=0)
		frappe.db.commit()
		return {
			"consulta": consulta,
			"payment_url": resultado["payment_url"],
		}
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			title="Agendamento: falha ao criar consulta",
			message=f"servico={servico} profissional={profissional} {data} {hora}\n\n{frappe.get_traceback()}",
		)
		frappe.throw(_("Não conseguimos concluir o agendamento. Tente de novo em instantes."))
	finally:
		frappe.set_user(usuario_original)


def _obter_ou_criar_paciente(nome: str, email: str, telefone: str) -> str:
	"""Reaproveita o cadastro pelo e-mail; nunca revela se já existia.

	Dizer "esse e-mail já tem cadastro" a um visitante anônimo permitiria
	descobrir quem é paciente da clínica só testando endereços.
	"""
	existente = frappe.db.get_value("Patient", {"email": email}, "name")
	if existente:
		return existente

	p = frappe.new_doc("Patient")
	p.update(
		{
			"first_name": nome,
			"email": email,
			"mobile": telefone,
			"sex": "Other",  # não se pergunta no agendamento; ajusta-se na consulta
			"invite_user": 0,
		}
	)
	p.insert(ignore_permissions=True)
	return p.name


def _criar_consulta(paciente: str, profissional: str, servico: str, data: str, hora: str) -> str:
	_, duracao = _preco_e_duracao(servico)
	empresa = frappe.defaults.get_user_default("Company") or frappe.get_all(
		"Company", pluck="name", limit=1
	)[0]

	c = frappe.new_doc("Patient Appointment")
	c.update(
		{
			"patient": paciente,
			"appointment_for": "Practitioner",
			"practitioner": profissional,
			"appointment_type": servico,
			"appointment_date": data,
			"appointment_time": hora,
			"duration": duracao,
			"company": empresa,
			"add_video_conferencing": 1,
		}
	)
	c.insert(ignore_permissions=True)
	return c.name
