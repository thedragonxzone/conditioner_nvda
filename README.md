# SOLUSDT Trading Assistant

A real-time trading assistant desktop application for monitoring SOLUSDT trading setups with BTCUSDT market context.

## Features

- **Real-time Price Streaming**: Live ticker data from Binance WebSocket for SOLUSDT and BTCUSDT
- **State Machine**: Automated state tracking for trading setups (PENDING → ACTIVE → CLOSED_W/CLOSED_L/MISSED/INVALIDATED)
- **File Watching**: Auto-reloads setup configurations when `sol_btc_setup_schema.json` changes
- **Interactive Charts**: TradingView Lightweight Charts with live 1-minute candlestick data
- **Price Lines**: Visual Entry (blue dashed), Stop Loss (red), and Take Profit (green) lines on charts
- **Desktop Notifications**: Linux native notifications via `notify-send` on state transitions
- **Market Context**: Displays Fear & Greed Index and divergence warnings

## Requirements

- Python 3.8+
- Linux operating system (for `notify-send` notifications)
- PyQt6 and related dependencies

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Ensure `notify-send` is installed on your Linux system:
```bash
sudo apt-get install libnotify-bin  # Debian/Ubuntu
# or
sudo dnf install libnotify  # Fedora
```

## Usage

1. Place your trading setup configuration in `sol_btc_setup_schema.json` in the same directory as the application.

2. Run the application:
```bash
python main.py
```

## Configuration File Format

The `sol_btc_setup_schema.json` file defines trading setups and market context:

```json
{
  "timestamp": "2026-06-29T12:32:00Z",
  "ticker": "SOLUSDT",
  "market_context": {
    "btc_price_ref": 59907.9,
    "btc_sentiment": "neutral-to-weak",
    "btc_gatekeeper_level": 59400.0,
    "fear_and_greed_index": 12,
    "sol_btc_divergence_warning": {
      "is_active": true,
      "type": "bearish_divergence",
      "description": "Warning description here"
    }
  },
  "setups": [
    {
      "id": "setup_01",
      "name": "Setup Name",
      "bias": "LONG",
      "conditions": {
        "sol_trigger_price": 72.7,
        "sol_trigger_condition": "above",
        "btc_confirmation_price": 59400.0,
        "btc_confirmation_condition": "above"
      },
      "execution": {
        "entry_range": [72.7, 73.3],
        "stop_loss": 71.9,
        "take_profits": [74.2, 75.3]
      },
      "commentary": "Setup commentary here"
    }
  ]
}
```

## State Machine Logic

- **PENDING**: Waiting for SOL to enter entry_range AND BTC to meet btc_confirmation_price
- **ACTIVE**: Both SOL and BTC conditions are met simultaneously
- **MISSED**: SOL moves to TP or below SL before triggering entry
- **INVALIDATED**: BTC breaks its btc_gatekeeper_level before SOL triggers
- **CLOSED_W**: Active position hits Take Profit
- **CLOSED_L**: Active position hits Stop Loss

## UI Layout

- **Left Panel**: Notification toggle, live prices, Fear & Greed Index, divergence warning, active setups list, archive list
- **Right-Top Panel**: Interactive chart with live candlesticks and price lines
- **Right-Bottom Panel**: Detailed setup information and state history

## Technical Details

- **WebSocket**: Connects to `wss://stream.binance.com:9443/ws` for real-time data
- **Threading**: WebSocket client and file watcher run in separate QThreads to avoid blocking the GUI
- **Auto-reconnect**: WebSocket automatically reconnects on connection drops (up to 10 attempts)
- **File Watching**: Uses watchdog library to monitor JSON file changes with debouncing

## License

MIT License
# conditioner_nvda
