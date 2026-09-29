# Hermès Picotin Lock 18 Monitor — Brasil

Monitor open-source em Python + Playwright para verificar a disponibilidade real de **Picotin Lock 18** na Hermès Brasil e enviar o status para Telegram.

## O que ele faz

O workflow:

- roda aproximadamente a cada 10 minutos;
- abre a categoria oficial da Hermès Brasil;
- descobre as páginas atuais de Picotin Lock 18;
- mantém algumas URLs oficiais conhecidas como fallback;
- abre cada produto em **3 contextos de navegador novos**;
- classifica cada checagem como `DISPONÍVEL`, `INDISPONÍVEL` ou `NÃO CONFIRMADO`;
- só aceita o estado final `DISPONÍVEL` com **3/3 checagens positivas**;
- só aceita o estado final `INDISPONÍVEL` com **3/3 checagens negativas explícitas**;
- envia o status ao Telegram em **toda execução**.

Não usa LLM, banco de dados, servidor próprio ou API de IA paga.

## Como o estoque é confirmado

### DISPONÍVEL

Uma checagem só é `DISPONÍVEL` quando todos estes pontos estão satisfeitos:

1. o produto é identificado como **Picotin Lock 18**;
2. existe um CTA de compra como **Adicionar à sacola**;
3. o CTA está visível e não está desabilitado por atributo, `aria-disabled`, classes de indisponibilidade, `pointer-events:none` etc.;
4. a página não apresenta texto explícito de falta de estoque/indisponibilidade;
5. o JSON-LD não informa `OutOfStock`/`SoldOut`;
6. a página não aparenta ser CAPTCHA/bloqueio/interstitial.

O JSON-LD `InStock` é usado como evidência adicional quando disponível, mas a confirmação transacional principal é o CTA de compra habilitado.

### INDISPONÍVEL

Uma checagem só é `INDISPONÍVEL` quando:

1. o produto é identificado como **Picotin Lock 18**;
2. não existe CTA de compra habilitado;
3. existe evidência explícita de indisponibilidade, esgotamento, aviso de reposição/lista de espera ou `OutOfStock`/`SoldOut`;
4. a página não aparenta estar bloqueada.

### NÃO CONFIRMADO

Qualquer caso ambíguo fica em `NÃO CONFIRMADO`:

- página incompleta;
- CAPTCHA ou bloqueio;
- timeout;
- sinais contraditórios;
- botão e indicador de estoque divergentes;
- descoberta de produto falhou.

O monitor **nunca transforma falha de acesso em indisponível**.

## 3 verificações independentes

Cada produto é checado três vezes em novos contextos do navegador. O resultado final é:

- `AVAILABLE` somente se **3/3** forem `AVAILABLE`;
- `UNAVAILABLE` somente se **3/3** forem `UNAVAILABLE`;
- caso contrário, `UNKNOWN`.

Isso reduz falsos positivos causados por carregamento parcial ou inconsistência momentânea.

## Telegram

### Token

Crie o bot com `@BotFather` e guarde o token como GitHub Secret:

`TELEGRAM_BOT_TOKEN`

### Chat ID automático

`TELEGRAM_CHAT_ID` é opcional.

Para autodetecção:

1. abra o bot no Telegram;
2. clique em **Start**;
3. envie `teste`;
4. execute o workflow manualmente uma vez.

O monitor consulta `getUpdates` e, se existir **uma única conversa privada** associada ao bot, usa esse Chat ID automaticamente.

Se mais de uma pessoa usar o bot, o monitor não adivinha: crie o Secret `TELEGRAM_CHAT_ID` para evitar enviar mensagens para a pessoa errada.

## GitHub Actions

Para maximizar o uso gratuito, este projeto foi preparado para **repositório público + `ubuntu-latest`**. O uso dos runners padrão hospedados pelo GitHub é gratuito e ilimitado em repositórios públicos.

O cron usado é:

```text
7,17,27,37,47,57 * * * *
```

Isso representa aproximadamente uma tentativa a cada 10 minutos. O GitHub pode atrasar execuções agendadas.

## Configuração rápida

1. crie um repositório GitHub público;
2. envie todos os arquivos deste projeto;
3. crie o Secret `TELEGRAM_BOT_TOKEN`;
4. no Telegram, abra o bot, pressione Start e envie `teste`;
5. abra **Actions → Monitor Hermès Picotin Lock 18 → Run workflow**;
6. confira a primeira mensagem do Telegram.

## Segurança

- nunca coloque o token do Telegram no código;
- se um token antigo foi exposto, revogue-o no BotFather e gere outro;
- nunca publique senha da Hermès no repositório;
- o monitor não tenta contornar CAPTCHA, rate limits ou bloqueios;
- o monitor não adiciona o produto à sacola nem realiza compras.

## Fontes oficiais

- Hermès Brasil — categoria Picotin: https://www.hermes.com/br/pt/content/316316-bolsas-hermes-picotin/
- Hermès Brasil — Picotin Lock 18: https://www.hermes.com/br/pt/product/bolsa-picotin-lock-18-H056289CC37/
- Telegram Bot API: https://core.telegram.org/bots/api
- GitHub Actions runners: https://docs.github.com/en/actions/reference/runners/github-hosted-runners

## Licença

MIT — veja `LICENSE`.
