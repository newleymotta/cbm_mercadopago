"""Testes da validação de assinatura do webhook do Mercado Pago.

Rodam sem Frappe e sem rede:  python -m pytest tests/ -q
"""

import hashlib
import hmac
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cbm_mercadopago.signature import (  # noqa: E402
	SignatureError,
	build_manifest,
	compute,
	normalize_ts,
	parse_x_signature,
	verify,
	verify_candidatos,
)

SECRET = "chave-secreta-de-teste"


def assinar(secret, data_id, request_id, ts):
	"""Reproduz o que o Mercado Pago faz do lado dele."""
	manifest = build_manifest(data_id, request_id, ts)
	return hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()


# --- manifest -------------------------------------------------------------

def test_manifest_no_formato_documentado():
	assert build_manifest("123", "req-abc", "1704908010") == "id:123;request-id:req-abc;ts:1704908010;"


def test_manifest_forca_minusculas_no_data_id():
	# A documentação do MP é explícita: data.id vai em minúsculas.
	assert build_manifest("ABC-DEF", "r1", "1") == "id:abc-def;request-id:r1;ts:1;"


def test_manifest_omite_pares_ausentes():
	assert build_manifest(None, "r1", "9") == "request-id:r1;ts:9;"
	assert build_manifest("42", None, "9") == "id:42;ts:9;"
	assert build_manifest(None, None, "9") == "ts:9;"


# --- parsing do header ----------------------------------------------------

def test_parse_header_padrao():
	assert parse_x_signature("ts=1704908010,v1=abc123") == ("1704908010", "abc123")


def test_parse_header_com_espacos_e_ordem_invertida():
	assert parse_x_signature(" v1=deadbeef , ts=99 ") == ("99", "deadbeef")


@pytest.mark.parametrize("header", ["", None, "ts=123", "v1=abc", "lixo", "ts=,v1="])
def test_parse_header_malformado_rejeita(header):
	with pytest.raises(SignatureError):
		parse_x_signature(header)


# --- timestamp ------------------------------------------------------------

def test_ts_em_segundos_e_milissegundos():
	assert normalize_ts("1704908010") == 1704908010.0
	assert normalize_ts("1742505638683") == 1742505638.683


def test_ts_nao_numerico_rejeita():
	with pytest.raises(SignatureError):
		normalize_ts("ontem")


# --- verify: caminho feliz ------------------------------------------------

def test_assinatura_valida_passa():
	ts = str(int(time.time()))
	v1 = assinar(SECRET, "payment-1", "req-1", ts)
	verify(SECRET, f"ts={ts},v1={v1}", "req-1", "payment-1")  # não levanta


def test_assinatura_valida_com_data_id_maiusculo():
	# O MP assina o valor em minúsculas; se chegar maiúsculo tem que bater igual.
	ts = str(int(time.time()))
	v1 = assinar(SECRET, "abc-xyz", "req-1", ts)
	verify(SECRET, f"ts={ts},v1={v1}", "req-1", "ABC-XYZ")


def test_assinatura_valida_sem_request_id():
	ts = str(int(time.time()))
	v1 = assinar(SECRET, "p1", None, ts)
	verify(SECRET, f"ts={ts},v1={v1}", None, "p1")


# --- verify: ataques ------------------------------------------------------

def test_assinatura_adulterada_rejeita():
	ts = str(int(time.time()))
	with pytest.raises(SignatureError, match="não confere"):
		verify(SECRET, f"ts={ts},v1={'0' * 64}", "req-1", "payment-1")


def test_segredo_errado_rejeita():
	ts = str(int(time.time()))
	v1 = assinar("outro-segredo", "p1", "r1", ts)
	with pytest.raises(SignatureError, match="não confere"):
		verify(SECRET, f"ts={ts},v1={v1}", "r1", "p1")


def test_data_id_trocado_rejeita():
	"""Assinatura legítima de OUTRO pagamento não pode valer para este."""
	ts = str(int(time.time()))
	v1 = assinar(SECRET, "pagamento-do-fulano", "r1", ts)
	with pytest.raises(SignatureError, match="não confere"):
		verify(SECRET, f"ts={ts},v1={v1}", "r1", "pagamento-do-sicrano")


def test_replay_antigo_rejeita():
	antigo = str(int(time.time()) - 3600)
	v1 = assinar(SECRET, "p1", "r1", antigo)
	with pytest.raises(SignatureError, match="janela"):
		verify(SECRET, f"ts={antigo},v1={v1}", "r1", "p1")


def test_timestamp_no_futuro_rejeita():
	"""Relógio adulterado para o futuro também é rejeitado."""
	futuro = str(int(time.time()) + 3600)
	v1 = assinar(SECRET, "p1", "r1", futuro)
	with pytest.raises(SignatureError, match="janela"):
		verify(SECRET, f"ts={futuro},v1={v1}", "r1", "p1")


def test_secret_vazio_rejeita():
	"""Sem segredo configurado, nada passa — nunca 'passa por omissão'."""
	ts = str(int(time.time()))
	with pytest.raises(SignatureError, match="não configurado"):
		verify("", f"ts={ts},v1=qualquer", "r1", "p1")


def test_expirado_rejeita_antes_de_comparar_hmac():
	"""Notificação expirada é barrada mesmo com assinatura correta."""
	antigo = str(int(time.time()) - 99999)
	v1 = assinar(SECRET, "p1", "r1", antigo)
	with pytest.raises(SignatureError, match="janela"):
		verify(SECRET, f"ts={antigo},v1={v1}", "r1", "p1")


# --- data.id vindo do corpo em vez da query string ------------------------
# Regressão de um bug real: nas notificações do Checkout Pro o Mercado Pago
# assina incluindo o data.id, mas só o envia no CORPO — não na query string.
# Validar apenas pela query string rejeitava TODO pagamento real.

def test_aceita_data_id_vindo_do_corpo_quando_ausente_na_query():
	ts = str(int(time.time()))
	v1 = assinar(SECRET, "PAY-REAL-123", "req-1", ts)
	# query string sem data.id (None), corpo com o id
	encontrado = verify_candidatos(SECRET, f"ts={ts},v1={v1}", "req-1", [None, "PAY-REAL-123"])
	assert encontrado == "PAY-REAL-123"


def test_aceita_data_id_vindo_da_query_quando_presente():
	ts = str(int(time.time()))
	v1 = assinar(SECRET, "PAY-SIM-9", "req-1", ts)
	encontrado = verify_candidatos(SECRET, f"ts={ts},v1={v1}", "req-1", ["PAY-SIM-9", "PAY-SIM-9"])
	assert encontrado == "PAY-SIM-9"


def test_aceita_manifest_sem_id_quando_foi_assim_que_assinaram():
	ts = str(int(time.time()))
	v1 = assinar(SECRET, None, "req-1", ts)
	encontrado = verify_candidatos(SECRET, f"ts={ts},v1={v1}", "req-1", [None, "PAY-X"])
	assert encontrado is None


def test_nenhum_candidato_confere_ainda_rejeita():
	"""Aceitar duas origens não pode virar porta aberta."""
	ts = str(int(time.time()))
	v1 = assinar(SECRET, "PAY-LEGITIMO", "req-1", ts)
	with pytest.raises(SignatureError, match="não confere"):
		verify_candidatos(SECRET, f"ts={ts},v1={v1}", "req-1", ["PAY-FALSO", "OUTRO-FALSO"])


def test_candidatos_com_segredo_errado_rejeita():
	ts = str(int(time.time()))
	v1 = assinar("segredo-do-atacante", "PAY-1", "req-1", ts)
	with pytest.raises(SignatureError, match="não confere"):
		verify_candidatos(SECRET, f"ts={ts},v1={v1}", "req-1", [None, "PAY-1"])


# --- vetor fixo (regressão) ----------------------------------------------

def test_vetor_fixo_conhecido():
	"""Trava o algoritmo: se alguém mudar a montagem do manifest, quebra aqui."""
	manifest = build_manifest("123456", "abc-req", "1704908010")
	assert manifest == "id:123456;request-id:abc-req;ts:1704908010;"
	# Valor congelado. Se a montagem do manifest ou o algoritmo mudarem, quebra aqui.
	assert compute("segredo", manifest) == (
		"f10ce8829167dd68a478e788b42346734d8d82b5519c875748ad0568936f9c0c"
	)
	assert compute("segredo", manifest) == hmac.new(
		b"segredo", manifest.encode(), hashlib.sha256
	).hexdigest()
