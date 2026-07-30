"""Validação da assinatura (x-signature) dos webhooks do Mercado Pago.

Módulo deliberadamente puro (sem Frappe) para poder ser testado isoladamente.
Referência: https://www.mercadopago.com.br/developers/pt/docs/your-integrations/notifications/webhooks

O manifest é montado como:

    id:{data.id};request-id:{x-request-id};ts:{ts};

omitindo os pares cujo valor não veio na requisição. O `data.id` entra em
minúsculas. O HMAC é SHA-256 em hexadecimal, comparado em tempo constante.
"""

import hashlib
import hmac
import time

# Janela de tolerância do timestamp. Protege contra replay de notificações antigas.
MAX_AGE_SECONDS = 300

# Acima disto o timestamp é milissegundos, não segundos. O Mercado Pago emite
# os dois formatos dependendo do endpoint (visto `ts=1704908010` e
# `ts=1742505638683` na própria documentação), então normalizamos.
_MILLIS_THRESHOLD = 10**11


class SignatureError(Exception):
	"""Assinatura ausente, malformada, expirada ou inválida."""


def parse_x_signature(header: str) -> tuple[str, str]:
	"""Extrai (ts, v1) de um header no formato `ts=...,v1=...`."""
	ts = v1 = None
	for part in (header or "").split(","):
		key, sep, value = part.partition("=")
		if not sep:
			continue
		key = key.strip()
		value = value.strip()
		if key == "ts":
			ts = value
		elif key == "v1":
			v1 = value
	if not ts or not v1:
		raise SignatureError("header x-signature ausente ou malformado")
	return ts, v1


def build_manifest(data_id: str | None, request_id: str | None, ts: str) -> str:
	"""Monta o manifest exatamente como o Mercado Pago espera."""
	parts = []
	if data_id:
		parts.append(f"id:{data_id.lower()}")
	if request_id:
		parts.append(f"request-id:{request_id}")
	parts.append(f"ts:{ts}")
	return ";".join(parts) + ";"


def normalize_ts(ts: str) -> float:
	"""Converte o `ts` do header para segundos (epoch), aceitando ms ou s."""
	try:
		raw = int(ts)
	except (TypeError, ValueError):
		raise SignatureError("ts não numérico no x-signature")
	return raw / 1000.0 if raw > _MILLIS_THRESHOLD else float(raw)


def check_freshness(ts: str, now: float | None = None, max_age: int = MAX_AGE_SECONDS) -> None:
	"""Rejeita notificações fora da janela — em qualquer direção.

	O valor absoluto também barra timestamps no futuro, que indicariam
	relógio adulterado em vez de atraso de rede.
	"""
	emitted_at = normalize_ts(ts)
	current = time.time() if now is None else now
	age = abs(current - emitted_at)
	if age > max_age:
		raise SignatureError(f"timestamp fora da janela de {max_age}s (diferença de {age:.0f}s)")


def compute(secret: str, manifest: str) -> str:
	"""HMAC-SHA256 do manifest, em hexadecimal."""
	return hmac.new(secret.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256).hexdigest()


def verify(
	secret: str,
	x_signature: str,
	x_request_id: str | None,
	data_id: str | None,
	now: float | None = None,
	max_age: int = MAX_AGE_SECONDS,
) -> None:
	"""Valida a assinatura. Levanta SignatureError se qualquer etapa falhar."""
	verify_candidatos(secret, x_signature, x_request_id, [data_id], now=now, max_age=max_age)


def verify_candidatos(
	secret: str,
	x_signature: str,
	x_request_id: str | None,
	data_ids: list[str | None],
	now: float | None = None,
	max_age: int = MAX_AGE_SECONDS,
) -> str | None:
	"""Valida aceitando mais de uma origem possível para o `data.id`.

	O Mercado Pago nem sempre repete o `data.id` na query string: nas
	notificações reais do Checkout Pro ele vem só no corpo, mas a assinatura
	é calculada **incluindo** esse id. Testar as duas origens cobre os dois
	formatos sem afrouxar nada — cada tentativa continua sendo um HMAC
	completo com o nosso segredo.

	Devolve o `data.id` que fechou a conta (ou None, se o manifest válido
	era o sem id). Levanta SignatureError se nenhum candidato conferir.

	Ordem deliberada: formato, frescor e só então o HMAC — não faz sentido
	gastar comparação criptográfica numa notificação já expirada.
	"""
	if not secret:
		raise SignatureError("webhook secret não configurado no Mercado Pago Settings")

	ts, received = parse_x_signature(x_signature)
	check_freshness(ts, now=now, max_age=max_age)

	vistos: list[str | None] = []
	for data_id in data_ids:
		normalizado = data_id or None
		if normalizado in vistos:
			continue
		vistos.append(normalizado)
		expected = compute(secret, build_manifest(normalizado, x_request_id, ts))
		if hmac.compare_digest(expected, received):
			return normalizado

	raise SignatureError("assinatura não confere")
