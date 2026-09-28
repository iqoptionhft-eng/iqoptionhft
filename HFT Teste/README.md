# HFT Gemma · IQ Option

Bot local para IQ Option usando a biblioteca **não oficial** [`iqbroker`](https://github.com/zagmi/iqbroker)
(fork comunitário da antiga `iqoptionapi`), com:

- operação **somente em opções digitais** (1 ou 5 minutos)
- sinais rápidos por análise técnica (EMA, RSI, MACD, estocástico, engolfo)
- confirmação opcional por LLM local via **Ollama** (ex.: `gemma4:26b-a4b-it-qat`)
- limites de perda diária (por conta e por dia), sequência de losses e payout mínimo

Isto **não é HFT de exchange**. A IQ Option só expõe WebSocket; o ciclo é de centenas de milissegundos.
O motor entra nos **últimos segundos do candle de 1 minuto**.

## Segurança

- Credenciais ficam só no arquivo `.env` (não versionado; veja `.env.example`). O `.gitignore` bloqueia `.env` e `data/`.
- A senha foi colada no chat: **troque a senha da IQ Option** depois, se essa conversa não for privada.
- O robô **sempre inicia em PRÁTICA** (`DEFAULT_ACCOUNT=REAL` é ignorado).
- Conta **REAL** exige as três coisas: `ALLOW_REAL=1` no `.env`, digitar `REAL` no painel, motor parado e sem ordens abertas.
- O painel só escuta em `127.0.0.1`, recusa `Host` diferente de `127.0.0.1`/`localhost` (anti DNS rebinding) e
  exige o header `X-Token` em toda a API (anti CSRF). O token é impresso no console ao subir e salvo em
  `data/panel_token.txt`; abra a URL completa `http://127.0.0.1:8787/#token=...`.
- Opções binárias/digitais são de alto risco. Use a prática primeiro.

## Proteções de banca

- PnL, wins/losses e perdas seguidas são guardados **por conta (PRACTICE/REAL) e por data** em `data/ledger.json`
  (reiniciar o processo não zera o dia; virar o dia zera).
- Antes de cada ordem: `PnL do dia - exposição aberta - valor da ordem >= -perda máxima`. O valor por ordem nunca
  pode ser maior que a perda máxima do dia.
- Ordem sem resultado apurado até 90s após a expiração conta como **perda total** (`ERROR`).
- Ordem com resposta incerta da corretora (timeout) conta como perda e **pausa o motor** para conferência manual.
- Não há martingale.

## Como subir

1. Instale [Python 3.12+](https://www.python.org/downloads/) com **Add python.exe to PATH**.
2. (Opcional) Ollama: `ollama pull gemma4:26b-a4b-it-qat` e `ollama list`.
3. Copie `.env.example` para `.env` e preencha.
4. Na pasta do projeto rode `start.bat` (cria o venv e instala as dependências fixadas só na primeira vez).
5. Abra a URL com `#token=...` mostrada no console.
6. **Conectar** → confira **PRACTICE** → **Iniciar**.

Se o login pedir 2FA por SMS, o robô não consegue autenticar sozinho (limitação da API não oficial).

## Testes

```bat
pip install -r requirements-dev.txt
python -m pytest
```

Os testes usam um cliente falso da corretora; nenhum teste conecta na IQ Option.

## Lógica de entrada

1. Ativo aberto em digital e payout digital conhecido e ≥ mínimo.
2. TA aponta CALL ou PUT e a confiança da TA passa do mínimo.
3. Se a IA estiver ligada: precisa **concordar**; a confiança final é o **menor** valor entre TA e IA.
4. Se o modelo estourar o timeout: só opera se a TA estiver forte (fallback `safe`).
5. A janela de entrada é rechecada imediatamente antes de enviar a ordem (relógio do servidor).
