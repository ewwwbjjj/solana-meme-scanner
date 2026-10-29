#!/usr/bin/env python3
"""
Conservative Solana memecoin alert scanner.
Alert-only. It does not buy/sell tokens or request wallet secrets.
No scoring system can guarantee profitable trades.
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

DEX = "https://api.dexscreener.com"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens"
TELEGRAM = "https://api.telegram.org"
RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

POLL_SECONDS = max(30, int(os.getenv("POLL_SECONDS", "45")))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "30000"))
MIN_VOLUME_5M_USD = float(os.getenv("MIN_VOLUME_5M_USD", "5000"))
MIN_VOLUME_1H_USD = float(os.getenv("MIN_VOLUME_1H_USD", "25000"))
MIN_TXNS_5M = int(os.getenv("MIN_TXNS_5M", "20"))
MIN_BUY_SELL_RATIO = float(os.getenv("MIN_BUY_SELL_RATIO", "1.25"))
MAX_TOKEN_AGE_MINUTES = int(os.getenv("MAX_TOKEN_AGE_MINUTES", "90"))
MAX_FDV_USD = float(os.getenv("MAX_FDV_USD", "5_000_000"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "75"))
MAX_RUGCHECK_SCORE = int(os.getenv("MAX_RUGCHECK_SCORE", "500"))
REQUIRE_RUGCHECK = os.getenv("REQUIRE_RUGCHECK", "true").lower() == "true"
REQUIRE_NO_MINT_AUTHORITY = os.getenv("REQUIRE_NO_MINT_AUTHORITY", "true").lower() == "true"
REQUIRE_NO_FREEZE_AUTHORITY = os.getenv("REQUIRE_NO_FREEZE_AUTHORITY", "true").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
STATE_FILE = Path(os.getenv("STATE_FILE", "seen_tokens.json"))

s = requests.Session()
s.headers.update({"User-Agent": "ConservativeSolanaAlertScanner/1.0"})


def get_json(url: str, **kwargs) -> Any:
    r = s.get(url, timeout=15, **kwargs)
    r.raise_for_status()
    return r.json()


def post_json(url: str, payload: dict) -> Any:
    r = s.post(url, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def n(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def load_seen() -> set[str]:
    try:
        value = json.loads(STATE_FILE.read_text())
        return set(value if isinstance(value, list) else [])
    except (OSError, json.JSONDecodeError):
        return set()


def save_seen(seen: set[str]) -> None:
    # Deterministic output and bounded history.
    STATE_FILE.write_text(json.dumps(sorted(seen)[-30000:], indent=2))


def age_minutes(pair: dict) -> float | None:
    created = pair.get("pairCreatedAt")
    if not created:
        return None
    try:
        return max(0.0, (time.time() * 1000 - float(created)) / 60000)
    except (TypeError, ValueError):
        return None


def extract_stats(pair: dict) -> dict:
    tx = pair.get("txns") or {}
    m5 = tx.get("m5") or {}
    h1 = tx.get("h1") or {}
    vol = pair.get("volume") or {}
    liq = pair.get("liquidity") or {}
    pc = pair.get("priceChange") or {}
    return {
        "age": age_minutes(pair),
        "liquidity": n(liq.get("usd")),
        "volume5m": n(vol.get("m5")),
        "volume1h": n(vol.get("h1")),
        "buys5m": int(n(m5.get("buys"))),
        "sells5m": int(n(m5.get("sells"))),
        "buys1h": int(n(h1.get("buys"))),
        "sells1h": int(n(h1.get("sells"))),
        "change5m": n(pc.get("m5")),
        "change1h": n(pc.get("h1")),
        "fdv": n(pair.get("fdv")),
    }


def mint_authorities(address: str) -> dict:
    """Query Solana RPC for parsed mint authority fields; fail closed on errors."""
    data = post_json(RPC_URL, {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [address, {"encoding": "jsonParsed", "commitment": "confirmed"}],
    })
    result = data.get("result", {}).get("value")
    if not result:
        raise ValueError("Mint account unavailable or not parsed")
    parsed = result.get("data", {}).get("parsed", {})
    info = parsed.get("info", {})
    if parsed.get("type") != "mint":
        raise ValueError("Address did not resolve to a parsed mint account")
    return {
        "mint_authority": info.get("mintAuthority"),
        "freeze_authority": info.get("freezeAuthority"),
    }


def rugcheck_report(address: str) -> dict:
    # RugCheck is a third-party service; response availability and fields can change.
    return get_json(f"{RUGCHECK}/{address}/report/summary")


def risk_summary(report: dict) -> tuple[bool, list[str], float | None]:
    """Interpret common RugCheck summary fields conservatively; unknown schema fails closed."""
    reasons = []
    score = report.get("score")
    if score is None:
        # Some responses may nest the report summary.
        score = (report.get("summary") or {}).get("score")
    score_num = n(score, -1)
    risks = report.get("risks")
    if risks is None:
        risks = (report.get("summary") or {}).get("risks")
    if not isinstance(risks, list):
        return False, ["RugCheck response missing risk list; cannot verify"], score_num if score_num >= 0 else None

    for risk in risks:
        if isinstance(risk, dict):
            level = str(risk.get("level", "")).lower()
            name = str(risk.get("name") or risk.get("description") or "risk flag")
            if level in {"danger", "critical", "high"}:
                reasons.append(f"RugCheck {level}: {name}")
    if score_num < 0:
        return False, reasons + ["RugCheck score unavailable; cannot verify"], None
    if score_num > MAX_RUGCHECK_SCORE:
        reasons.append(f"RugCheck score exceeds configured maximum ({score_num:g})")
    return score_num <= MAX_RUGCHECK_SCORE and not reasons, reasons, score_num


def score_pair(st: dict) -> tuple[int, list[str], list[str]]:
    """Conservative momentum score; score does not estimate probability of profit."""
    score = 0
    good, warnings = [], []
    age = st["age"]
    liq = st["liquidity"]
    v5 = st["volume5m"]
    v1 = st["volume1h"]
    buys = st["buys5m"]
    sells = st["sells5m"]
    ratio = buys / max(sells, 1)

    if liq >= 100_000:
        score += 25; good.append("liquidity ≥ $100k")
    elif liq >= MIN_LIQUIDITY_USD:
        score += 18; good.append("liquidity passes threshold")

    if v5 >= 25_000:
        score += 20; good.append("strong 5m volume")
    elif v5 >= MIN_VOLUME_5M_USD:
        score += 14; good.append("5m volume passes threshold")

    if v1 >= MIN_VOLUME_1H_USD:
        score += 10; good.append("1h volume passes threshold")

    if ratio >= 2:
        score += 15; good.append("buys materially exceed sells")
    elif ratio >= MIN_BUY_SELL_RATIO:
        score += 10; good.append("buy/sell ratio passes threshold")
    else:
        warnings.append("buy/sell ratio is weak")

    if buys + sells >= 60:
        score += 10; good.append("high 5m transaction activity")
    elif buys + sells >= MIN_TXNS_5M:
        score += 6; good.append("5m transaction activity passes threshold")

    if age is not None and 3 <= age <= 30:
        score += 10; good.append("new pair with a short observation window")
    elif age is not None and 30 < age <= MAX_TOKEN_AGE_MINUTES:
        score += 5; good.append("pair is within age limit")
    else:
        warnings.append("pair age is outside the preferred window or unknown")

    if 0 < st["change5m"] <= 15:
        score += 5; good.append("positive but not extreme 5m price movement")
    elif st["change5m"] > 25:
        warnings.append("price already moved sharply; chasing risk")
    elif st["change5m"] < -10:
        warnings.append("sharp 5m price decline")

    if sells >= buys * 1.5 and sells >= 8:
        score -= 20; warnings.append("heavy sell pressure")
    if liq > 0 and v5 / liq > 2:
        warnings.append("5m volume unusually high relative to liquidity; possible volatility/manipulation")
    if st["fdv"] > MAX_FDV_USD:
        warnings.append("FDV exceeds configured maximum")
    return max(0, min(100, score)), good, warnings


def send_alert(text: str) -> None:
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        url = f"{TELEGRAM}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        r = s.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        }, timeout=15)
        r.raise_for_status()
        print("Telegram alert sent.")
    else:
        print("\n" + text + "\n" + ("-" * 60))


def format_alert(pair: dict, st: dict, score: int, good: list[str],
                 warnings: list[str], auth: dict, rug_score: float) -> str:
    token = pair.get("baseToken") or {}
    address = token.get("address", "unknown")
    name = token.get("name", "Unknown")
    symbol = token.get("symbol", "?")
    url = pair.get("url") or f"https://dexscreener.com/solana/{address}"
    age = f"{st['age']:.0f} min" if st["age"] is not None else "unknown"
    risk_lines = warnings if warnings else ["No momentum warnings from configured rules"]
    return (
        f"🟡 SOLANA EARLY-TOKEN WATCHLIST ALERT\n\n"
        f"{name} (${symbol})\n"
        f"Momentum score: {score}/100 (NOT win probability)\n"
        f"Age: {age}\n"
        f"Liquidity: ${st['liquidity']:,.0f}\n"
        f"5m volume: ${st['volume5m']:,.0f} | 1h volume: ${st['volume1h']:,.0f}\n"
        f"5m buys/sells: {st['buys5m']}/{st['sells5m']}\n"
        f"5m price change: {st['change5m']:+.1f}% | FDV: ${st['fdv']:,.0f}\n"
        f"RugCheck score: {rug_score:g} (third-party signal, not a guarantee)\n"
        f"Mint authority: {'renounced/none' if not auth['mint_authority'] else 'PRESENT'}\n"
        f"Freeze authority: {'renounced/none' if not auth['freeze_authority'] else 'PRESENT'}\n\n"
        f"Positive signals: {', '.join(good) if good else 'None'}\n"
        f"Warnings: {', '.join(risk_lines)}\n\n"
        f"Mint address:\n{address}\n\n"
        f"Chart: {url}\n"
        f"Risk report: https://rugcheck.xyz/tokens/{address}\n\n"
        f"⚠ WATCHLIST ONLY. This is not a buy recommendation or a promise of gains. "
        f"Liquidity and volume can be faked; holders can sell; checks can be incomplete. "
        f"Verify the token and sellability independently before risking funds."
    )


def main() -> None:
    seen = load_seen()
    print("Conservative Solana token scanner running (alerts only; no trading).")
    print(f"Poll={POLL_SECONDS}s, min liquidity=${MIN_LIQUIDITY_USD:,.0f}, score≥{MIN_SCORE}")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials absent; alerts will print to terminal.")
    while True:
        try:
            profiles = get_json(f"{DEX}/token-profiles/latest/v1")
            if not isinstance(profiles, list):
                profiles = []
            candidates = []
            for profile in profiles:
                if profile.get("chainId") != "solana":
                    continue
                address = profile.get("tokenAddress")
                if not address or address in seen:
                    continue
                try:
                    pairs = get_json(f"{DEX}/token-pairs/v1/solana/{address}")
                    pairs = [p for p in pairs if p.get("chainId") == "solana"]
                    if not pairs:
                        continue
                    pair = max(pairs, key=lambda p: n((p.get("liquidity") or {}).get("usd")))
                    st = extract_stats(pair)
                    age = st["age"]

                    # Hard filters first; do not alert if basic market activity is weak.
                    if age is None or age > MAX_TOKEN_AGE_MINUTES:
                        seen.add(address); continue
                    if st["liquidity"] < MIN_LIQUIDITY_USD:
                        continue
                    if st["volume5m"] < MIN_VOLUME_5M_USD or st["volume1h"] < MIN_VOLUME_1H_USD:
                        continue
                    if st["buys5m"] + st["sells5m"] < MIN_TXNS_5M:
                        continue
                    if st["fdv"] > MAX_FDV_USD:
                        seen.add(address); continue

                    score, good, warnings = score_pair(st)
                    if score < MIN_SCORE:
                        continue

                    # Safety checks fail closed: if required checks are unavailable, no alert.
                    try:
                        auth = mint_authorities(address)
                    except Exception as exc:
                        print(f"Skipping {address}: Solana authority check unavailable ({exc})")
                        continue
                    if REQUIRE_NO_MINT_AUTHORITY and auth["mint_authority"]:
                        seen.add(address); continue
                    if REQUIRE_NO_FREEZE_AUTHORITY and auth["freeze_authority"]:
                        seen.add(address); continue

                    rug_score = None
                    if REQUIRE_RUGCHECK:
                        try:
                            report = rugcheck_report(address)
                            ok, risk_reasons, rug_score = risk_summary(report)
                        except Exception as exc:
                            print(f"Skipping {address}: RugCheck unavailable ({exc})")
                            continue
                        if not ok:
                            print(f"Filtered {address}: {', '.join(risk_reasons)}")
                            seen.add(address); continue
                    else:
                        rug_score = -1
                        warnings.append("RugCheck requirement disabled")

                    candidates.append((score, pair, st, good, warnings, auth, rug_score, address))
                except requests.RequestException as exc:
                    print(f"API error for {address}: {exc}")
                except (ValueError, KeyError, TypeError) as exc:
                    print(f"Skipping malformed token {address}: {exc}")

            candidates.sort(key=lambda x: x[0], reverse=True)
            for score, pair, st, good, warnings, auth, rug_score, address in candidates[:5]:
                try:
                    send_alert(format_alert(pair, st, score, good, warnings, auth, rug_score))
                except requests.RequestException as exc:
                    print(f"Alert delivery failed: {exc}")
                seen.add(address)
            save_seen(seen)
        except requests.RequestException as exc:
            print(f"API request failed: {exc}")
        except Exception as exc:
            print(f"Unexpected scanner error: {exc}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
