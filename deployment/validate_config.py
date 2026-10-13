from dotenv import dotenv_values
from urllib.parse import urlsplit
from pathlib import Path
import sys
values = dotenv_values('/root/POA/.env')
required = ('PASSWORD', 'DB_ID', 'DB_PASSWORD', 'DISCORD_WEBHOOK_URL')
ok = all(values.get(key) for key in required)
url = urlsplit(values.get('DISCORD_WEBHOOK_URL') or '')
ok = ok and url.scheme == 'https' and url.hostname in ('discord.com', 'discordapp.com')
ok = ok and any(values.get(ex + '_KEY') and values.get(ex + '_SECRET')
                for ex in ('BINANCE', 'BYBIT', 'OKX', 'UPBIT', 'BITGET'))
ok = ok and not (Path('/root/POA/.env').stat().st_mode & 0o077)
if not ok:
    print('POA_CONFIGURATION_INCOMPLETE_OR_INSECURE; inspect .env locally', file=sys.stderr)
    sys.exit(1)
