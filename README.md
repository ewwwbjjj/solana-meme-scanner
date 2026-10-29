# Conservative Solana Early-Token Alert Scanner

This is an alert-only starter project intended to surface a *small number* of newly active Solana tokens for manual research. It does not place trades.

## Important: no bot can guarantee a high win rate

A 75/100 score is a heuristic momentum score, **not a 75% chance of winning**. Memecoins can rug, liquidity can be fake, volume can be wash-traded, insiders can dump, and data services can be delayed or wrong. This scanner intentionally fails closed when required authority/RugCheck checks are unavailable, which can mean missing opportunities or producing no alerts.

It is not financial advice. Never use money you cannot afford to lose.

## What it checks

- Newly listed token profiles from DexScreener.
- Pair age, reported liquidity, 5-minute and 1-hour volume, recent transaction count.
- Buy/sell activity and short-term price movement.
- Solana mint and freeze authorities using a public Solana RPC.
- RugCheck summary and reported high/critical risks (third-party data; response format may change).
- Alerts only after hard filters and minimum score are met.

## What it does NOT prove

- It does not guarantee the token can be sold.
- It does not fully analyze every holder cluster, insider wallet, bundled launch, liquidity lock, or token extension.
- A missing mint/freeze authority does not mean a token is safe.
- Passing these filters does not mean a token will increase in price.

## Run it

Requires Python 3.10+.

```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and optionally configure Telegram.

### Telegram setup

1. In Telegram, message `@BotFather`.
2. Send `/newbot` and follow its prompts.
3. Put the resulting bot token in `TELEGRAM_BOT_TOKEN` in `.env`.
4. Open your new bot and send `/start`.
5. Set `TELEGRAM_CHAT_ID` to your own chat ID, obtained through a trusted method.
6. Keep `.env` private. Never send anyone your bot token, wallet seed phrase, or private key.

Start:

```bash
python scanner.py
```

If Telegram is not configured, alerts print in the terminal. The program must be running somewhere with internet access to keep scanning; a local terminal closing stops it. For 24/7 alerts, deploy it to a trusted server and keep API limits in mind.

## Tuning

The defaults are deliberately conservative. If no alerts appear, do not immediately lower all thresholds; first verify API access and review why tokens are being filtered. Lower thresholds mean more noise and risk.

- `MIN_LIQUIDITY_USD`: minimum reported pool liquidity.
- `MIN_VOLUME_5M_USD` / `MIN_VOLUME_1H_USD`: activity thresholds.
- `MIN_SCORE`: minimum heuristic momentum score.
- `REQUIRE_RUGCHECK=true`: skips alerts if RugCheck cannot be queried or its expected fields are missing.
- `REQUIRE_NO_MINT_AUTHORITY=true` and `REQUIRE_NO_FREEZE_AUTHORITY=true`: skip tokens with these authorities still present.
- `POLL_SECONDS`: minimum 30 seconds to reduce API pressure.

## Suggested validation before using alerts

Run in observation mode for at least several days. Save every alert and then review outcomes at fixed horizons (for example 15 minutes, 1 hour, and 24 hours), including tokens that crashed. Calculate win rate only after defining "win" (such as net return after fees/slippage above a threshold), and track missed opportunities and false positives. Do not judge it only by selected winners.
