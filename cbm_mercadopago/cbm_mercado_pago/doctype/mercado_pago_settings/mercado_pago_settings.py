"""Controller do gateway Mercado Pago.

O nome do doctype (`Mercado Pago Settings`) não é livre: o app `payments`
resolve o controller por convenção de nome em
`payments.utils.get_payment_gateway_controller`, que faz
`frappe.get_doc(f"{gateway} Settings")`. Renomear quebra a integração.
"""

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, flt, get_url

from cbm_mercadopago import mp_client

GATEWAY = "Mercado Pago"


class MercadoPagoSettings(Document):
	# Consumido por `validate_transaction_currency`, parte do contrato do gateway.
	supported_currencies = ["BRL"]

	# ------------------------------------------------------------------ #
	# ciclo de vida                                                       #
	# ------------------------------------------------------------------ #

	def validate(self):
		if not self.enabled:
			return
		if not self.get_access_token():
			ambiente = "teste" if self.use_sandbox else "produção"
			frappe.throw(_("Informe o Access Token de {0} antes de ativar.").format(ambiente))
		if not self.get_webhook_secret():
			frappe.throw(
				_("Informe a chave secreta do webhook. Sem ela não há como validar as notificações do Mercado Pago.")
			)
		if self.statement_descriptor and len(self.statement_descriptor) > 22:
			frappe.throw(_("A descrição na fatura do cartão deve ter no máximo 22 caracteres."))
		if flt(self.unpaid_expiry_minutes) < 0:
			frappe.throw(_("O prazo de liberação do horário não pode ser negativo."))

	def on_update(self):
		from payments.utils import create_payment_gateway

		create_payment_gateway(GATEWAY, settings=self.doctype, controller=self.name)
		call_hook_method("payment_gateway_enabled", gateway=GATEWAY, payment_channel="Email")

	# ------------------------------------------------------------------ #
	# credenciais                                                         #
	# ------------------------------------------------------------------ #

	def get_access_token(self) -> str | None:
		campo = "sandbox_access_token" if self.use_sandbox else "production_access_token"
		return self.get_password(campo, raise_exception=False)

	def get_webhook_secret(self) -> str | None:
		campo = "sandbox_webhook_secret" if self.use_sandbox else "production_webhook_secret"
		return self.get_password(campo, raise_exception=False)

	# ------------------------------------------------------------------ #
	# contrato exigido pelo app `payments`                                #
	# ------------------------------------------------------------------ #

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_("O Mercado Pago desta integração só opera em BRL. Moeda recebida: {0}.").format(currency)
			)

	def get_payment_url(self, **kwargs) -> str:
		"""Cria a preferência no Mercado Pago e devolve a URL do Checkout Pro.

		Envolvido em try/except de propósito: o ERPNext chama este método por
		dentro de `payment_gateway_validation()`, que faz `except Exception:
		return False` e engole o erro — o Payment Request submeteria "com
		sucesso", com `payment_url` vazio e sem nenhum log. Registramos o
		traceback antes de deixar a exceção subir.
		"""
		try:
			return self._get_payment_url(**kwargs)
		except Exception:
			frappe.log_error(
				title="Mercado Pago: falha ao gerar link de pagamento",
				message=frappe.get_traceback(),
			)
			raise

	def _get_payment_url(self, **kwargs) -> str:
		if not self.enabled:
			frappe.throw(_("A integração com o Mercado Pago está desativada."))

		token = self.get_access_token()
		if not token:
			frappe.throw(_("Access Token do Mercado Pago não configurado."))

		self.validate_transaction_currency(kwargs.get("currency") or "BRL")

		amount = flt(kwargs.get("amount"))
		if amount <= 0:
			frappe.throw(_("Valor inválido para cobrança: {0}").format(amount))

		# O Integration Request é o nosso fio condutor: guarda os kwargs
		# originais e o `name` dele vai como `external_reference` na
		# preferência, permitindo reencontrar tudo quando o webhook chegar.
		integration = create_request_log(kwargs, service_name=GATEWAY)

		payload = self._montar_preferencia(integration.name, amount, kwargs)
		preferencia = mp_client.criar_preferencia(token, payload, idempotency_key=integration.name)

		url = self._extrair_init_point(preferencia)
		if not url:
			frappe.throw(_("O Mercado Pago não devolveu uma URL de pagamento."))

		integration.db_set("request_id", preferencia.get("id"), update_modified=False)
		integration.db_set("url", url, update_modified=False)
		return url

	def _montar_preferencia(self, external_reference: str, amount: float, kwargs: dict) -> dict:
		titulo = kwargs.get("description") or kwargs.get("title") or _("Consulta")
		payload = {
			"items": [
				{
					"id": external_reference,
					"title": str(titulo)[:250],
					"quantity": 1,
					"currency_id": "BRL",
					"unit_price": amount,
				}
			],
			"external_reference": external_reference,
			"notification_url": get_url("/api/method/cbm_mercadopago.api.webhook"),
			"back_urls": {
				"success": get_url("/api/method/cbm_mercadopago.api.retorno"),
				"pending": get_url("/api/method/cbm_mercadopago.api.retorno"),
				"failure": get_url("/api/method/cbm_mercadopago.api.retorno"),
			},
			"auto_return": "approved",
		}

		if kwargs.get("payer_email"):
			payload["payer"] = {"email": kwargs["payer_email"]}
		if self.statement_descriptor:
			payload["statement_descriptor"] = self.statement_descriptor

		return payload

	def _extrair_init_point(self, preferencia: dict) -> str | None:
		if self.use_sandbox:
			# Credenciais de teste já devolvem um init_point de teste; o
			# sandbox_init_point é o formato legado e nem sempre vem.
			return preferencia.get("sandbox_init_point") or preferencia.get("init_point")
		return preferencia.get("init_point")
