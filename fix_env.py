import os

def fix_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    orig = content
    content = content.replace("os.getenv('BINANCE_SPOT_API_SECRET', '')", "os.getenv('BINANCE_SPOT_API_SECRET', '')")
    content = content.replace("os.getenv('BINANCE_SPOT_API_KEY', '')", "os.getenv('BINANCE_SPOT_API_KEY', '')")
    content = content.replace('os.getenv("BINANCE_API_SECRET", "")', 'os.getenv("BINANCE_API_SECRET", "")')
    content = content.replace('os.getenv("BINANCE_API_KEY", "")', 'os.getenv("BINANCE_API_KEY", "")')
    content = content.replace('(api_secret or "").encode', '(api_secret or "").encode')
    content = content.replace('(creds.get("futures_api_secret") or "").encode', '(creds.get("futures_api_secret") or "").encode')
    content = content.replace("(creds.get('futures_api_secret') or '').encode", "(creds.get('futures_api_secret') or '').encode")
    
    if orig != content:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Fixed {path}")

for root, _, files in os.walk('.'):
    for f in files:
        if f.endswith('.py'):
            fix_file(os.path.join(root, f))
