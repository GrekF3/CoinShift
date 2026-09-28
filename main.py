from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from decimal import Decimal
from tkinter import ttk
from typing import Any, Callable

import config
from okx_client import BalanceSummary, OkxClient, OkxError


@dataclass
class BnbSnapshot:
    deposit_address: dict[str, Any] | None
    deposits: list[dict[str, Any]]
    balance: BalanceSummary


class OkxConventorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("OKX CONVENTOR")
        self.geometry("980x650")
        self.minsize(900, 560)

        self.client = OkxClient(
            config.API_KEY,
            config.API_SECRET,
            config.API_PASSPHRASE,
            config.BASE_URL,
        )
        self.results: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.active_tasks: set[str] = set()
        self.polling_enabled = True

        self.address_var = tk.StringVar(value="Загрузка...")
        self.chain_var = tk.StringVar(value="-")
        self.deposit_status_var = tk.StringVar(value="-")
        self.funding_bnb_var = tk.StringVar(value="0")
        self.trading_bnb_var = tk.StringVar(value="0")
        self.total_bnb_var = tk.StringVar(value="0")
        self.status_var = tk.StringVar(value="Готово")

        self._configure_style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.after(100, self._process_results)
        self.refresh_convert()
        self.refresh_assets()
        self.after(config.POLL_INTERVAL_SECONDS * 1000, self._poll_convert)

    def _configure_style(self) -> None:
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self.style.configure("TNotebook.Tab", padding=(18, 8))
        self.style.configure("Heading.TLabel", font=("Segoe UI", 14, "bold"))
        self.style.configure("Value.TLabel", font=("Segoe UI", 10))
        self.style.configure("Status.TLabel", foreground="#475569")
        self.style.configure("Accent.TButton", padding=(12, 7))

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(root)
        header.pack(fill=tk.X, pady=(0, 12))
        ttk.Label(header, text="OKX CONVENTOR", style="Heading.TLabel").pack(side=tk.LEFT)
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel").pack(
            side=tk.RIGHT
        )

        notebook = ttk.Notebook(root)
        notebook.pack(fill=tk.BOTH, expand=True)

        self.convert_tab = ttk.Frame(notebook, padding=16)
        self.assets_tab = ttk.Frame(notebook, padding=16)
        notebook.add(self.convert_tab, text="Convert")
        notebook.add(self.assets_tab, text="Assets")

        self._build_convert_tab()
        self._build_assets_tab()

    def _build_convert_tab(self) -> None:
        address_box = ttk.LabelFrame(self.convert_tab, text="BNB BEP20 / BSC deposit")
        address_box.pack(fill=tk.X, pady=(0, 14))

        ttk.Label(address_box, text="Chain").grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))
        ttk.Label(address_box, textvariable=self.chain_var, style="Value.TLabel").grid(
            row=0, column=1, sticky="w", padx=12, pady=(12, 6)
        )

        ttk.Label(address_box, text="Address").grid(row=1, column=0, sticky="nw", padx=12, pady=6)
        address_entry = ttk.Entry(address_box, textvariable=self.address_var)
        address_entry.grid(row=1, column=1, sticky="ew", padx=12, pady=6)
        ttk.Button(address_box, text="Copy", command=self.copy_address).grid(
            row=1, column=2, sticky="e", padx=12, pady=6
        )

        address_box.columnconfigure(1, weight=1)

        balances = ttk.LabelFrame(self.convert_tab, text="BNB balance")
        balances.pack(fill=tk.X, pady=(0, 14))

        fields = [
            ("Deposit status", self.deposit_status_var),
            ("Funding BNB", self.funding_bnb_var),
            ("Trading BNB", self.trading_bnb_var),
            ("Total BNB", self.total_bnb_var),
        ]
        for index, (label, variable) in enumerate(fields):
            ttk.Label(balances, text=label).grid(row=index, column=0, sticky="w", padx=12, pady=7)
            ttk.Label(balances, textvariable=variable, style="Value.TLabel").grid(
                row=index, column=1, sticky="w", padx=12, pady=7
            )
        balances.columnconfigure(1, weight=1)

        actions = ttk.Frame(self.convert_tab)
        actions.pack(fill=tk.X, pady=(0, 14))
        ttk.Button(actions, text="Refresh", command=self.refresh_convert).pack(side=tk.LEFT)
        ttk.Button(
            actions,
            text="Convert BNB -> USDT",
            style="Accent.TButton",
            command=self.convert_bnb,
        ).pack(side=tk.LEFT, padx=(10, 0))

        log_box = ttk.LabelFrame(self.convert_tab, text="Status")
        log_box.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(log_box, height=8, wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    def _build_assets_tab(self) -> None:
        toolbar = ttk.Frame(self.assets_tab)
        toolbar.pack(fill=tk.X, pady=(0, 10))
        ttk.Button(toolbar, text="Refresh assets", command=self.refresh_assets).pack(side=tk.LEFT)

        columns = ("asset", "funding", "trading", "total")
        self.assets_tree = ttk.Treeview(
            self.assets_tab,
            columns=columns,
            show="headings",
            height=18,
        )
        self.assets_tree.heading("asset", text="Asset")
        self.assets_tree.heading("funding", text="Funding available")
        self.assets_tree.heading("trading", text="Trading available")
        self.assets_tree.heading("total", text="Total")

        self.assets_tree.column("asset", width=120, anchor=tk.W)
        self.assets_tree.column("funding", width=210, anchor=tk.E)
        self.assets_tree.column("trading", width=210, anchor=tk.E)
        self.assets_tree.column("total", width=210, anchor=tk.E)

        scrollbar = ttk.Scrollbar(
            self.assets_tab,
            orient=tk.VERTICAL,
            command=self.assets_tree.yview,
        )
        self.assets_tree.configure(yscrollcommand=scrollbar.set)
        self.assets_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def refresh_convert(self) -> None:
        self._run_task("convert_snapshot", self._load_convert_snapshot)

    def refresh_assets(self) -> None:
        self._run_task("assets", self.client.get_combined_balances)

    def convert_bnb(self) -> None:
        self._run_task("convert_bnb", self.client.convert_bnb_to_usdt)

    def copy_address(self) -> None:
        address = self.address_var.get().strip()
        if not address or address == "Загрузка...":
            self._log("Нет адреса для копирования.")
            return
        self.clipboard_clear()
        self.clipboard_append(address)
        self._set_status("Адрес скопирован.")

    def _load_convert_snapshot(self) -> BnbSnapshot:
        address = self.client.get_bnb_deposit_address()
        deposits = self.client.get_deposit_history("BNB")
        balance = self.client.get_currency_balance("BNB")
        return BnbSnapshot(address, deposits, balance)

    def _run_task(self, name: str, func: Callable[[], Any]) -> None:
        if name in self.active_tasks:
            return
        self.active_tasks.add(name)
        self._set_status(f"Выполняется: {name}")

        def worker() -> None:
            try:
                self.results.put((name, func()))
            except Exception as exc:
                self.results.put((f"{name}:error", exc))

        threading.Thread(target=worker, daemon=True).start()

    def _process_results(self) -> None:
        while True:
            try:
                name, payload = self.results.get_nowait()
            except queue.Empty:
                break

            task_name = name.replace(":error", "")
            self.active_tasks.discard(task_name)

            if name.endswith(":error"):
                self._handle_error(task_name, payload)
            elif name == "convert_snapshot":
                self._apply_convert_snapshot(payload)
            elif name == "assets":
                self._apply_assets(payload)
            elif name == "convert_bnb":
                self._apply_convert_result(payload)

        self.after(100, self._process_results)

    def _apply_convert_snapshot(self, snapshot: BnbSnapshot) -> None:
        if snapshot.deposit_address:
            self.address_var.set(str(snapshot.deposit_address.get("addr", "")))
            self.chain_var.set(str(snapshot.deposit_address.get("chain", "")))

        self.funding_bnb_var.set(_fmt_decimal(snapshot.balance.funding_available))
        self.trading_bnb_var.set(_fmt_decimal(snapshot.balance.trading_available))
        self.total_bnb_var.set(_fmt_decimal(snapshot.balance.total))
        self.deposit_status_var.set(_format_deposit_status(snapshot.deposits))
        self._set_status("Convert данные обновлены.")

    def _apply_assets(self, balances: list[BalanceSummary]) -> None:
        for item in self.assets_tree.get_children():
            self.assets_tree.delete(item)

        for balance in balances:
            self.assets_tree.insert(
                "",
                tk.END,
                values=(
                    balance.currency,
                    _fmt_decimal(balance.funding_available),
                    _fmt_decimal(balance.trading_available),
                    _fmt_decimal(balance.total),
                ),
            )
        self._set_status("Assets обновлены.")

    def _apply_convert_result(self, result: dict[str, Any]) -> None:
        method = result.get("method")
        api_result = result.get("result", {})
        if method == "convert":
            self._log(f"Convert выполнен: {api_result}")
        else:
            self._log(f"Convert API недоступен, выполнен spot fallback: {api_result}")
            self._log(f"Причина fallback: {result.get('convert_error')}")

        self.refresh_convert()
        self.refresh_assets()

    def _handle_error(self, task_name: str, error: Exception) -> None:
        message = str(error)
        if isinstance(error, OkxError):
            self._log(f"{task_name}: {message}")
        else:
            self._log(f"{task_name}: unexpected error: {message}")
        self._set_status("Ошибка. Смотрите Status.")

    def _poll_convert(self) -> None:
        if self.polling_enabled:
            self.refresh_convert()
            self.after(config.POLL_INTERVAL_SECONDS * 1000, self._poll_convert)

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _log(self, text: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"{text}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _on_close(self) -> None:
        self.polling_enabled = False
        self.destroy()


def _format_deposit_status(deposits: list[dict[str, Any]]) -> str:
    if not deposits:
        return "Нет последних BNB депозитов"

    latest = sorted(deposits, key=lambda item: str(item.get("ts", "")), reverse=True)[0]
    state_map = {
        "0": "ожидает подтверждения",
        "1": "зачислен",
        "2": "успешен",
        "8": "депозит временно приостановлен",
        "11": "адрес в blacklist",
        "12": "аккаунт/депозит заморожен",
        "13": "sub-account interception",
        "14": "KYC limit",
    }
    state = state_map.get(str(latest.get("state", "")), f"state={latest.get('state')}")
    amount = latest.get("amt", "?")
    chain = latest.get("chain", "?")
    return f"{amount} BNB, {chain}, {state}"


def _fmt_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


if __name__ == "__main__":
    app = OkxConventorApp()
    app.mainloop()
