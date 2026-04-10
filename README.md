# Nostradam — Phase 1: Paper Trading MVP

BTC 5-minute prediction market inefficiency scanner for Polymarket.

## What it does
- Auto-discovers active BTC 5-min prediction markets on Polymarket
- Analyzes order book odds, spread, volume for microstructure inefficiencies
- Paper trades both YES and NO when it detects mispricing
- Logs everything to SQLite
- Web dashboard to monitor performance

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure API keys
```bash
cp .env.example .env
# Edit .env with your Polymarket credentials
```

**How to get your Polymarket API key:**
1. Go to https://polymarket.com and log in
2. Open browser DevTools (F12) → Network tab
3. Click on any market, look for requests to `clob.polymarket.com`
4. In request headers, copy the value of `POLY_API_KEY`
5. Your wallet private key is from the wallet connected to your Polymarket account

### 3. Edit config
Edit `config.yaml` to set your bankroll, risk limits, etc.

### 4. Run
```bash
# Start the bot + dashboard
python main.py
```

Dashboard available at `http://localhost:5050`

### 5. Run on your DigitalOcean server
```bash
# Use screen or tmux so it persists
screen -S nostradam
python main.py
# Ctrl+A, D to detach

# Or use systemd (see nostradam.service)
```

## Project Structure
```
nostradam/
├── main.py              # Entry point — runs bot loop + dashboard
├── config.yaml          # All adjustable parameters
├── .env.example         # API key template
├── core/
│   ├── scanner.py       # Discovers active BTC 5-min markets
│   ├── analyzer.py      # Detects odds inefficiencies
│   ├── trader.py        # Paper trade execution & logging
│   ├── database.py      # SQLite schema & queries
│   └── config.py        # Config loader
├── dashboard/
│   ├── app.py           # Flask web dashboard
│   └── templates/
│       └── index.html   # Dashboard UI
└── requirements.txt
```
