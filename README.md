# Hermès Picotin Lock 18 Monitor — Brasil

Monitor open-source em Python + Playwright para verificar a disponibilidade real de **Picotin Lock 18** na Hermès Brasil e enviar o status para Telegram.

## Objetivo

A cada aproximadamente 10 minutos, o workflow:

1. abre a categoria oficial da Hermès Brasil;
2. descobre as páginas atuais de **Picotin Lock 18**;
3. abre cada página em contextos de navegador novos;
4. faz **3 checagens independentes e sequenciais**;
5. exige **3 de 3** resultados iguais para concluir `DISPONÍVEL` ou `INDISPONÍVEL`;
6. envia o status ao Telegram em toda execução.

O monitor **não usa LLM, API paga, banco de dados pago ou servidor próprio**.

## Regra de disponibilidade

Uma checagem só marca `AVAILABLE` quando encontra, ao mesmo tempo:

- produto identificado como `Picotin Lock 18`;
- botão/link de compra equivalente a **Adicionar à sacola** visível e habilitado;
- sinal positivo de estoque no JSON-LD da página **ou** outra evidência positiva equivalente;
- nenhum texto explícito de indisponibilidade/lista de espera;
- nenhum `OutOfStock`/`SoldOut` no JSON-LD;
- nenhuma indicação de página bloqueada/CAPTCHA.

Uma checagem só marca `UNAVAILABLE` quando encontra:

- produto identificado como `Picotin Lock 18`;
- **nenhum** CTA de compra habilitado;
- evidência explícita de indisponibilidade, esgotamento/lista de espera ou `OutOfStock`/`SoldOut`;
- nenhuma indicação de bloqueio da página.

Qualquer outra situação vira `UNKNOWN` / **NÃO CONFIRMADO**. O monitor deliberadamente não transforma erro, CAPTCHA, página incompleta ou divergência em um falso “INDISPONÍVEL”. Para o estado final, exige unanimidade das 3 checagens.

## Telegram

Você recebe uma mensagem em cada execução, por exemplo:

```text
👜 HERMÈS — PICOTIN LOCK 18

• Bolsa Picotin Lock 18
  💰 R$ 24.900,00
  ✅ DISPONÍVEL — confirmado (3/3)
  🔗 https://...
```

ou:

```text
• Bolsa Picotin Lock 18
  ❌ INDISPONÍVEL — confirmado (3/3)
```

Se houver divergência:

```text
⚠️ NÃO CONFIRMADO — checagens divergentes/insuficientes
```

## Configuração

No GitHub, crie um repositório. Para maximizar a compatibilidade com execução gratuita, use um **repositório público** e os runners padrão (`ubuntu-latest`). O GitHub informa que runners padrão em repositórios públicos são gratuitos. Em repositórios privados, o plano GitHub Free inclui 2.000 minutos/mês antes da cobrança/impedimento por cota. Veja a documentação oficial do GitHub.

Crie os Secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Depois rode manualmente:

**Actions → Monitor Hermès Picotin Lock 18 → Run workflow**

## Por que o cron está em 7, 17, 27, 37, 47, 57?

Ele continua tendo intervalo de 10 minutos, mas evita o minuto `00`. O GitHub informa que o evento `schedule` pode sofrer atrasos em períodos de alta carga, especialmente no começo da hora.

## Limitações reais

- “A cada 10 minutos” significa **tentativa de disparo** a cada 10 minutos. O scheduler do GitHub pode atrasar uma execução.
- Em repositório público, workflows agendados podem ser desativados automaticamente depois de 60 dias sem atividade do repositório; reabilite o workflow ou faça alguma atividade no repositório.
- A Hermès pode alterar HTML, seletores ou mecanismos de estoque. Por isso o código usa múltiplos sinais e um estado `UNKNOWN` para evitar falsos positivos.
- O monitor não tenta contornar CAPTCHA, bloqueios, autenticação, rate limits ou outras proteções do site.
- O monitor não adiciona a bolsa à sacola nem realiza compra.

## Licença

MIT — veja `LICENSE`.
