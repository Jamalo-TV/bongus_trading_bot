import asyncio
import json
from rich.live import Live
from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel
from rich.console import Console

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
    reader, writer = await asyncio.open_connection('127.0.0.1', 9000)
    state.log("Connected to Rust Execution Engine IPC (127.0.0.1:9000)")
    
    while True:
        try:
            line = await reader.readline()
            if not line:
                break
            msg = line.decode('utf-8').strip()
            
            # Message Parsing Logic
            # We expect JSON lines or simple string formats. Let's parse JSON
            try:
                data = json.loads(msg)
                if data.get("event") == "BookTicker":
                    state.update_price(data["symbol"], data["bid_price"], data["ask_price"])
                elif data.get("event") == "Connected":
                    state.status(data.get("symbol", "UNKNOWN"), "CONNECTED")
                elif data.get("event") == "Disconnected":
                    state.status(data.get("symbol", "UNKNOWN"), "DISCONNECTED")
            except json.JSONDecodeError:
                # If not JSON, just log
                state.log(f"SYS: {msg}")

        except Exception as e:
            state.log(f"IPC Error: {e}")
            break
            
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
