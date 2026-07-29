app_name = "cbm_mercadopago"
app_title = "CBM Mercado Pago"
app_publisher = "Clinica Medica Bernardo Motta"
app_description = "Integração Mercado Pago (Checkout Pro) para Frappe Health"
app_email = "clinicabernardomotta@gmail.com"
app_license = "MIT"

required_apps = ["frappe/erpnext", "frappe/payments"]

# Botão "Gerar link de pagamento" no formulário da consulta.
doctype_js = {"Patient Appointment": "public/js/patient_appointment.js"}

scheduler_events = {
	"cron": {
		# A cada 5 minutos: libera horários de consultas cobradas e não pagas.
		# A granularidade real de expiração é o campo `unpaid_expiry_minutes`.
		"*/5 * * * *": ["cbm_mercadopago.tasks.expirar_agendamentos_nao_pagos"],
	},
}
