"""Regra de "dá para enviar o e-mail?".

Módulo deliberadamente sem Frappe, como o `signature.py`: é a regra que impede
a tela de anunciar um envio que não aconteceu, e por isso precisa de teste que
rode sem servidor.
"""

SEM_CONTA = "sem_conta"
SEM_EMAIL = "sem_email"


def motivo_para_nao_enviar(email_paciente: str | None, tem_conta_de_envio: bool) -> str | None:
	"""Diz por que o e-mail não pode ser enviado, ou `None` se pode.

	A ordem importa: a falta de conta de envio bloqueia todo paciente, a falta
	de e-mail bloqueia um só. Quem lê a mensagem precisa saber primeiro do
	problema maior.
	"""
	if not tem_conta_de_envio:
		return SEM_CONTA
	if not (email_paciente or "").strip():
		return SEM_EMAIL
	return None
