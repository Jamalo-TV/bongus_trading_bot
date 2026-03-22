import asyncio

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from bongus.ipc.telemetry import TelemetryClient

console = Console()

class DashboardState:
    def __init__(self):
        self.prices = {}
        self.statuses = {}
        self.messages = []

    def update_price(self, symbol, bid, ask):
        self.prices[symbol] = {"bid": bid, "ask": ask}
        self.statuses[symbol] = "CONNECTED"

    def status(self, symbol, status):
        self.statuses[symbol] = status

    def log(self, msg):
        self.messages.append(msg)
        if len(self.messages) > 10:
            self.messages.pop(0)

state = DashboardState()

def generate_table() -> Table:
    table = Table(title="[bold green]Bongus Multi-Asset Arbitrage Orchestrator[/bold green]", expand=True)
    table.add_column("Symbol", justify="center", style="cyan", no_wrap=True)
    table.add_column("Status", justify="center", style="magenta")
    table.add_column("Bid Price", justify="right", style="green")
    table.add_column("Ask Price", justify="right", style="red")

    for symbol, st in sorted(state.statuses.items()):
        p = state.prices.get(symbol, {"bid": 0.0, "ask": 0.0})
        status_color = "[green]" if st == "CONNECTED" else "[red]"
        table.add_row(
            symbol,
            f"{status_color}{st}[/]",
            f"{p['bid']:.4f}",
            f"{p['ask']:.4f}"
        )
    return table

def generate_log_panel() -> Panel:
    return Panel("\n".join(state.messages), title="Recent Events", style="white")

async def tcp_client():
    client = TelemetryClient(host='127.0.0.1', port=9000)
    state.log("Connecting to Rust Execution Engine via TelemetryClient...")

    try:
        async for data in client.stream_events():
            if data is None:
                continue
            if data.get("event") == "BookTicker":
                state.update_price(data["symbol"], data["bid_price"], data["ask_price"])
            elif data.get("event") == "Connected":
                state.status(data.get("symbol", "UNKNOWN"), "CONNECTED")
            elif data.get("event") == "Disconnected":
                state.status(data.get("symbol", "UNKNOWN"), "DISCONNECTED")
            else:
                state.log(f"EVENT: {data}")
    except Exception as e:
        state.log(f"IPC Error: {e}")
    state.log("Disconnected from Rust Engine.")

async def main():
    layout = Layout()
    layout.split_column(
        Layout(name="upper", ratio=3),
        Layout(name="lower")
    )

    asyncio.create_task(tcp_client())

    with Live(layout, refresh_per_second=4, screen=True) as live:
        while True:
            layout["upper"].update(generate_table())
            layout["lower"].update(generate_log_panel())
            await asyncio.sleep(0.25)

if __name__ == "__main__":
    asyncio.run(main())
