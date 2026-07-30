"""Cliente HTTP da API do Mercado Pago.

Só o necessário para Checkout Pro: criar preferência e consultar pagamento.
Toda chamada usa os helpers do Frappe, que já fazem `raise_for_status()` e
parsing de JSON.
"""

import json

import frappe
from frappe.integrations.utils import make_get_request, make_post_request

API_BASE = "https://api.mercadopago.com"

# Status do Mercado Pago que significam "dinheiro efetivamente disponível".
# `authorized` fica de fora de propósito: é reserva de valor, não captura.
STATUS_PAGOS = {"approved"}


def _headers(access_token: str, idempotency_key: str | None = None) -> dict:
	headers = {
		"Authorization": f"Bearer {access_token}",
		"Content-Type": "application/json",
	}
	if idempotency_key:
		# Evita criar preferências duplicadas se a requisição for repetida.
		headers["X-Idempotency-Key"] = idempotency_key
	return headers


def criar_preferencia(access_token: str, payload: dict, idempotency_key: str | None = None) -> dict:
	"""POST /checkout/preferences — devolve o dict com `init_point`."""
	return make_post_request(
		f"{API_BASE}/checkout/preferences",
		headers=_headers(access_token, idempotency_key),
		data=json.dumps(payload),
	)


def obter_pagamento(access_token: str, payment_id: str) -> dict:
	"""GET /v1/payments/{id} — a fonte de verdade sobre status e valor.

	O corpo do webhook nunca é confiável: ele só diz "olhe o pagamento X".
	Quem responde quanto foi pago e se foi aprovado é esta chamada.
	"""
	return make_get_request(
		f"{API_BASE}/v1/payments/{payment_id}",
		headers=_headers(access_token),
	)


def buscar_pagamentos(access_token: str, external_reference: str) -> list[dict]:
	"""GET /v1/payments/search — pagamentos de uma cobrança nossa.

	Usado como rede de segurança antes de liberar um horário por falta de
	pagamento: se uma notificação se perdeu (servidor fora do ar, rede,
	erro transitório), o dinheiro já entrou e ninguém avisou.
	"""
	resposta = make_get_request(
		f"{API_BASE}/v1/payments/search",
		headers=_headers(access_token),
		params={"external_reference": external_reference},
	)
	return (resposta or {}).get("results") or []


def esta_pago(pagamento: dict) -> bool:
	return (pagamento or {}).get("status") in STATUS_PAGOS


def valor_pago(pagamento: dict) -> float:
	"""Valor efetivamente aprovado, descontando estornos parciais."""
	total = frappe.utils.flt((pagamento or {}).get("transaction_amount"))
	estornado = frappe.utils.flt((pagamento or {}).get("transaction_amount_refunded"))
	return total - estornado
