# CBM Mercado Pago

Integração do **Mercado Pago (Checkout Pro)** com o Frappe Health, feita para a
Clínica Médica Bernardo Motta.

## O que faz

Na consulta (`Patient Appointment`), o botão **Mercado Pago → Gerar link de
pagamento** cria a fatura em aberto e devolve um link do Checkout Pro para
enviar ao paciente. Quando o pagamento é aprovado, o Mercado Pago notifica o
sistema, que confere tudo, dá baixa na fatura e marca a consulta como
**Confirmada**.

```
Patient Appointment
  └─ botão "Gerar link de pagamento"
       ├─ Sales Invoice (em aberto)
       └─ Payment Request ──► preferência no Mercado Pago ──► link do Checkout Pro

paciente paga ──► webhook ──► valida assinatura
                              ├─ consulta a API do MP (status e valor reais)
                              ├─ confere o valor contra o esperado
                              ├─ Payment Request.set_as_paid() ──► Payment Entry
                              └─ consulta ──► "Confirmed"
```

## Segurança

O webhook é público por natureza, então tudo nele é tratado como hostil:

- **Assinatura obrigatória.** HMAC-SHA256 sobre o manifest
  `id:{data.id};request-id:{x-request-id};ts:{ts};`, com `data.id` em
  minúsculas, comparado com `hmac.compare_digest` (tempo constante).
  Assinatura inválida responde **401** e não processa nada.
- **Sem chave, nada passa.** Se o webhook secret não estiver configurado, a
  validação falha — nunca "passa por omissão".
- **Anti-replay.** Notificações com `ts` fora de uma janela de 5 minutos (para
  mais ou para menos) são rejeitadas.
- **O webhook não é fonte de verdade.** O corpo da notificação só diz qual
  pagamento olhar; status e valor vêm de `GET /v1/payments/{id}` na API do
  Mercado Pago, autenticada com o nosso token.
- **Conferência de valor.** Divergência entre o pago e o esperado nunca vira
  baixa automática — fica registrada para conferência humana.
- **Idempotência.** Reprocessar a mesma notificação não gera segundo
  lançamento, com trava própria (`Integration Request`) e a trava nativa do
  ERPNext contra pagamento duplicado.
- **Credenciais criptografadas.** Ficam em campos `Password` do
  `Mercado Pago Settings`, nunca no código nem no repositório.
- **Sandbox por padrão.** `use_sandbox` nasce marcado.

## Configuração

1. `Mercado Pago Settings` → preencher Access Token e chave secreta do webhook
   (aba de teste enquanto não estiver em produção) e marcar **Ativado**.
2. No painel do Mercado Pago, apontar o webhook para:
   `https://<seu-site>/api/method/cbm_mercadopago.api.webhook`
3. Ajustar **Liberar horário não pago após (minutos)** — padrão 30, zero desliga.

## E-mail

O link de pagamento é enviado pelo app, não pelo ERPNext: o nativo só envia
dentro do `before_submit` do Payment Request, então um pedido já existente
nunca reenviaria nada. O envio é **síncrono**, para que uma falha de SMTP
apareça na tela em vez de sumir num job — a tela nunca anuncia envio que não
aconteceu.

Requisitos, conferidos antes de qualquer envio:

- uma `Email Account` de saída configurada (`default_outgoing`);
- e-mail no cadastro do paciente;
- `{{ payment_url }}` no campo **Mensagem** da `Payment Gateway Account`, senão
  o e-mail sairia sem o link.

O **aviso de consulta confirmada** é uma `Notification` comum (texto editável
na tela), com `Evento = Method` e `Método = cbm_pagamento_confirmado`. Não é
"Value Change" de propósito: o webhook grava o status com `frappe.db.set_value`,
que não dispara gatilho de documento, e o aviso jamais sairia. Os dois caminhos
— pagamento aprovado e confirmação manual na tela — chamam esse mesmo gatilho.
O destinatário é o Custom Field `patient_email` (`fetch_from = patient.email`),
necessário porque o destinatário de uma `Notification` não atravessa o vínculo
até o paciente.

## Testes

```bash
python -m pytest tests/ -q
```

Os testes de assinatura rodam sem Frappe e sem rede — cobrem o formato do
manifest, timestamps em segundos e milissegundos, e os casos de ataque
(assinatura adulterada, segredo errado, assinatura de outro pagamento,
replay, timestamp futuro e segredo vazio).

## Licença

MIT
