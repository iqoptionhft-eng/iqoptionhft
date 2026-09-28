# HFT Gemma · IQ Option

Bot local para IQ Option (API **não oficial** da comunidade `iqoptionapi`) com:

- troca **Prática / Real** na interface
- sinais rápidos por análise técnica (EMA, RSI, MACD, estocástico, engolfo)
- confirmação pelo **Gemma 4 31B** via **Ollama** (`gemma4:31b-it-qat`)
- limites de perda diária, sequência de losses e payout mínimo

Isto **não é HFT de exchange** (latência de microssegundos). A IQ Option só expõe WebSocket HTTP; o ciclo é da ordem de centenas de milissegundos. O motor entra nos **últimos segundos do candle de 1 minuto**, que é o ponto mais estável nesse tipo de opção.

## Segurança

- Credenciais ficam só no arquivo `.env` (não versionado).
- A senha foi colada no chat: **troque a senha da IQ Option** depois, se essa conversa não for privada.
- Conta **REAL** só liga se você digitar `REAL` no campo de confirmação.
- Opções binárias/digitais são de alto risco. Use a prática primeiro.

## Como subir

1. Instale [Python 3.12+](https://www.python.org/downloads/) com **Add python.exe to PATH**.
2. Confirme o Ollama: `ollama list` deve mostrar `gemma4:31b-it-qat`.
3. Na pasta do projeto:

```bat
start.bat
```

Ou:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

4. Abra `http://127.0.0.1:8787`
5. **Conectar** → deixe em **Prática** → **Iniciar**.

Se o login da IQ pedir 2FA por SMS, desative o 2FA na conta ou o robô não consegue autenticar sozinho (limitação da API não oficial).

## Lógica de entrada

1. Ativo aberto e payout ≥ mínimo.
2. TA aponta CALL ou PUT com score mínimo.
3. Se Gemma estiver ligado: precisa **concordar** com a TA (senão a ordem é abortada).
4. Se o modelo estourar o timeout: só opera se a TA estiver forte.

O Gemma 31B é grande; por isso o timeout padrão é curto e a TA não espera o modelo para tudo. Isso é o compromisso entre velocidade e acerto nesta máquina.
