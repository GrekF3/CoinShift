# CoinShift

A desktop utility for viewing account assets and converting a configured balance between supported currencies.

## Features

- Deposit address and balance views
- Asset table with funding and trading balances
- Conversion flow with API fallback handling

## Run locally

```bash
python -m venv .venv
pip install -r requirements.txt
copy config.example.py config.py
python main.py
```

Use API keys with the minimum required permissions and withdrawals disabled.
