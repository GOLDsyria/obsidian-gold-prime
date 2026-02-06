from datetime import datetime

def now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def format_signal(data):
    return f"""
🟡 GOLD SCALPING SIGNAL

📊 Symbol: {data['symbol']}
⏱ Timeframe: {data['timeframe']}
📈 Direction: {data['direction']}
💰 Price: {data['price']}

🕒 Time (UTC): {now()}
    """.strip()
