import json

with open('live_config.json', 'r') as f:
    config = json.load(f)

config['max_drawdown_pct'] = 0.99
config['reset_equity_high_watermark'] = True

with open('live_config.json', 'w') as f:
    json.dump(config, f, indent=2)
print("Updated live_config.json successfully!")
