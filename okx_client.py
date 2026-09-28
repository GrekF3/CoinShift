from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any
from urllib.parse import urlencode

import requests


class OkxError(RuntimeError):
    pass


class OkxConfigError(OkxError):
    pass


PLACEHOLDER_VALUES = {
    "",
    "PUT_YOUR_API_KEY_HERE",
    "PUT_YOUR_API_SECRET_HERE",
    "PUT_YOUR_API_PASSPHRASE_HERE",
}


@dataclass(frozen=True)
class BalanceSummary:
    currency: str
    funding_available: Decimal
    trading_available: Decimal

    @property
    def total(self) -> Decimal:
        return self.funding_available + self.trading_available


class OkxClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        base_url: str = "https://www.okx.com",
        timeout: int = 15,
    ) -> None:
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.passphrase = passphrase.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def validate_credentials(self) -> None:
        if (
            self.api_key in PLACEHOLDER_VALUES
            or self.api_secret in PLACEHOLDER_VALUES
            or self.passphrase in PLACEHOLDER_VALUES
        ):
            raise OkxConfigError(
                "Заполните API_KEY, API_SECRET и API_PASSPHRASE в config.py."
            )

    def get_bnb_deposit_address(self) -> dict[str, Any]:
        data = self._request("GET", "/api/v5/asset/deposit-address", {"ccy": "BNB"})
        if not data:
            raise OkxError("OKX не вернул депозитные адреса для BNB.")

        preferred = [
            item
            for item in data
            if _looks_like_bsc_chain(str(item.get("chain", "")))
        ]
        if not preferred:
            chains = ", ".join(str(item.get("chain", "")) for item in data)
            raise OkxError(f"Не найден BNB Smart Chain/BSC адрес. Доступные сети: {chains}")

        return preferred[0]

    def get_deposit_history(self, currency: str = "BNB", limit: int = 20) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/api/v5/asset/deposit-history",
            {"ccy": currency, "limit": str(limit)},
        )

    def get_funding_balances(self, currency: str | None = None) -> list[dict[str, Any]]:
        params = {"ccy": currency} if currency else None
        return self._request("GET", "/api/v5/asset/balances", params)

    def get_trading_balances(self, currency: str | None = None) -> list[dict[str, Any]]:
        params = {"ccy": currency} if currency else None
        rows = self._request("GET", "/api/v5/account/balance", params)
        if not rows:
            return []
        return rows[0].get("details", [])

    def get_combined_balances(self) -> list[BalanceSummary]:
        funding_rows = self.get_funding_balances()
        trading_rows = self.get_trading_balances()

        currencies = {
            str(row.get("ccy", ""))
            for row in funding_rows + trading_rows
            if str(row.get("ccy", "")).strip()
        }

        balances: list[BalanceSummary] = []
        for ccy in sorted(currencies):
            funding_available = sum(
                _to_decimal(row.get("availBal"))
                for row in funding_rows
                if row.get("ccy") == ccy
            )
            trading_available = sum(
                _to_decimal(row.get("availBal") or row.get("availEq"))
                for row in trading_rows
                if row.get("ccy") == ccy
            )
            if funding_available != 0 or trading_available != 0:
                balances.append(BalanceSummary(ccy, funding_available, trading_available))
        return balances

    def get_currency_balance(self, currency: str) -> BalanceSummary:
        funding_available = sum(
            _to_decimal(row.get("availBal"))
            for row in self.get_funding_balances(currency)
            if row.get("ccy") == currency
        )
        trading_available = sum(
            _to_decimal(row.get("availBal") or row.get("availEq"))
            for row in self.get_trading_balances(currency)
            if row.get("ccy") == currency
        )
        return BalanceSummary(currency, funding_available, trading_available)

    def transfer_funding_to_trading(self, currency: str, amount: Decimal) -> dict[str, Any]:
        if amount <= 0:
            raise OkxError("Нет доступного баланса для перевода в Trading.")
        body = {
            "ccy": currency,
            "amt": _decimal_to_api(amount),
            "from": "6",
            "to": "18",
        }
        data = self._request("POST", "/api/v5/asset/transfer", body=body)
        if not data:
            raise OkxError("OKX не вернул transId после перевода.")
        transfer = data[0]
        trans_id = transfer.get("transId")
        if trans_id:
            self._wait_for_transfer_success(str(trans_id))
        return transfer

    def get_transfer_state(self, trans_id: str) -> dict[str, Any]:
        data = self._request(
            "GET",
            "/api/v5/asset/transfer-state",
            {"transId": trans_id, "type": "0"},
        )
        if not data:
            raise OkxError(f"OKX не вернул статус transfer {trans_id}.")
        return data[0]

    def convert_bnb_to_usdt(self) -> dict[str, Any]:
        balance = self.get_currency_balance("BNB")
        if balance.funding_available > 0:
            self.transfer_funding_to_trading("BNB", balance.funding_available)
            balance = self.get_currency_balance("BNB")

        amount = balance.trading_available
        if amount <= 0:
            raise OkxError("Нет доступного BNB для конвертации.")

        try:
            return {
                "method": "convert",
                "result": self._convert_via_convert_api("BNB", "USDT", amount),
            }
        except OkxError as convert_error:
            return {
                "method": "spot_fallback",
                "convert_error": str(convert_error),
                "result": self._sell_bnb_spot_market(amount),
            }

    def _convert_via_convert_api(
        self, base_currency: str, quote_currency: str, amount: Decimal
    ) -> dict[str, Any]:
        pair_data = self._request(
            "GET",
            "/api/v5/asset/convert/currency-pair",
            {"fromCcy": base_currency, "toCcy": quote_currency},
        )
        if not pair_data:
            raise OkxError("Convert API не поддерживает BNB -> USDT для этого аккаунта.")

        pair = pair_data[0]
        base_min = _to_decimal(pair.get("baseCcyMin"))
        if base_min > 0 and amount < base_min:
            raise OkxError(f"Сумма BNB меньше минимума Convert API: {base_min}.")

        quote_data = self._request(
            "POST",
            "/api/v5/asset/convert/estimate-quote",
            body={
                "baseCcy": base_currency,
                "quoteCcy": quote_currency,
                "side": "sell",
                "rfqSz": _decimal_to_api(amount),
                "rfqSzCcy": base_currency,
            },
        )
        if not quote_data:
            raise OkxError("OKX не вернул quoteId для Convert.")

        quote = quote_data[0]
        trade_data = self._request(
            "POST",
            "/api/v5/asset/convert/trade",
            body={
                "baseCcy": base_currency,
                "quoteCcy": quote_currency,
                "side": "sell",
                "sz": _decimal_to_api(_to_decimal(quote.get("baseSz")) or amount),
                "szCcy": base_currency,
                "quoteId": quote["quoteId"],
            },
        )
        if not trade_data:
            raise OkxError("OKX не вернул результат Convert trade.")

        trade = trade_data[0]
        if trade.get("state") != "fullyFilled":
            raise OkxError(f"Convert не исполнен: {trade.get('state')}")
        return trade

    def _sell_bnb_spot_market(self, amount: Decimal) -> dict[str, Any]:
        inst = self._get_instrument("BNB-USDT")
        lot_size = _to_decimal(inst.get("lotSz")) or Decimal("0.000001")
        min_size = _to_decimal(inst.get("minSz")) or Decimal("0")
        rounded_amount = _round_down_to_step(amount, lot_size)

        if rounded_amount <= 0:
            raise OkxError("BNB balance is too small after lot size rounding.")
        if min_size > 0 and rounded_amount < min_size:
            raise OkxError(f"BNB меньше минимального размера spot ордера: {min_size}.")

        data = self._request(
            "POST",
            "/api/v5/trade/order",
            body={
                "instId": "BNB-USDT",
                "tdMode": "cash",
                "side": "sell",
                "ordType": "market",
                "sz": _decimal_to_api(rounded_amount),
            },
        )
        if not data:
            raise OkxError("OKX не вернул результат spot ордера.")
        return data[0]

    def _get_instrument(self, instrument_id: str) -> dict[str, Any]:
        data = self._request(
            "GET",
            "/api/v5/account/instruments",
            {"instType": "SPOT", "instId": instrument_id},
        )
        if not data:
            data = self._request(
                "GET",
                "/api/v5/public/instruments",
                {"instType": "SPOT", "instId": instrument_id},
                auth=False,
            )
        if not data:
            raise OkxError(f"Инструмент {instrument_id} не найден.")
        return data[0]

    def _wait_for_transfer_success(self, trans_id: str) -> None:
        last_state = "unknown"
        for _ in range(5):
            state = self.get_transfer_state(trans_id)
            last_state = str(state.get("state", "unknown"))
            if last_state == "success":
                return
            if last_state == "failed":
                raise OkxError(f"Transfer {trans_id} завершился failed.")
            time.sleep(1)
        raise OkxError(f"Transfer {trans_id} не подтвердился, последний статус: {last_state}.")

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> list[dict[str, Any]]:
        if auth:
            self.validate_credentials()

        method = method.upper()
        query = f"?{urlencode(params)}" if params else ""
        request_path = f"{path}{query}"
        url = f"{self.base_url}{request_path}"
        body_text = json.dumps(body, separators=(",", ":")) if body else ""

        headers = {"Content-Type": "application/json"}
        if auth:
            timestamp = _timestamp()
            headers.update(
                {
                    "OK-ACCESS-KEY": self.api_key,
                    "OK-ACCESS-SIGN": self._sign(timestamp, method, request_path, body_text),
                    "OK-ACCESS-TIMESTAMP": timestamp,
                    "OK-ACCESS-PASSPHRASE": self.passphrase,
                }
            )

        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                data=body_text if body_text else None,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise OkxError(f"Ошибка сети OKX: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise OkxError(f"OKX вернул не-JSON ответ HTTP {response.status_code}.") from exc

        if response.status_code >= 400:
            raise OkxError(f"HTTP {response.status_code}: {payload}")

        if payload.get("code") != "0":
            raise OkxError(f"OKX error {payload.get('code')}: {payload.get('msg')}")

        return payload.get("data") or []

    def _sign(self, timestamp: str, method: str, request_path: str, body: str) -> str:
        message = f"{timestamp}{method}{request_path}{body}"
        mac = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode("utf-8")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _looks_like_bsc_chain(chain: str) -> bool:
    normalized = chain.upper().replace(" ", "")
    return any(token in normalized for token in ("BSC", "BEP20", "BNBSMARTCHAIN"))


def _to_decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _decimal_to_api(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step
