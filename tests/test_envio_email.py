"""Testes da regra que decide se dá para enviar o link por e-mail.

A regra existe para a tela nunca anunciar um envio que não aconteceu, que era
o defeito original: o botão dizia "Link enviado ao paciente" mesmo sem conta
de e-mail configurada e sem e-mail do paciente.

Rodam sem Frappe e sem rede:  python -m pytest tests/ -q
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Importado do módulo, não copiado: se a regra mudar lá, o teste acusa.
from cbm_mercadopago.envio import motivo_para_nao_enviar  # noqa: E402


def test_com_conta_e_email_pode_enviar():
	assert motivo_para_nao_enviar("paciente@exemplo.com", True) is None


def test_sem_conta_de_envio_nao_envia():
	assert motivo_para_nao_enviar("paciente@exemplo.com", False) == "sem_conta"


def test_sem_email_do_paciente_nao_envia():
	assert motivo_para_nao_enviar(None, True) == "sem_email"


@pytest.mark.parametrize("vazio", ["", "   ", "\t", "\n"])
def test_email_em_branco_nao_conta_como_email(vazio):
	assert motivo_para_nao_enviar(vazio, True) == "sem_email"


def test_faltando_os_dois_reclama_da_conta_primeiro():
	# A conta de envio bloqueia todo paciente; o e-mail bloqueia um só. Quem
	# recebe a mensagem precisa saber primeiro do problema maior.
	assert motivo_para_nao_enviar(None, False) == "sem_conta"
