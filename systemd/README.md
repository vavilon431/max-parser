# Systemd setup для max-parser

Два unit-файли: парсер (`ws_parser.py`) та дашборд (`dashboard.py`).
Автозапуск після перезавантаження VPS, автоперезапуск при падінні (20c парсер / 15с дашборд).
Логи — через **systemd journal** з обмеженням розміру (max 500 МБ, retention 30 днів).

## Інсталяція (на VPS, виконати один раз)

```bash
# 1. Скопіювати unit-файли і конфіг journald з локалки:
scp systemd/*.service root@85.192.56.53:/etc/systemd/system/
ssh root@85.192.56.53 "mkdir -p /etc/systemd/journald.conf.d"
scp systemd/journald-max-parser.conf root@85.192.56.53:/etc/systemd/journald.conf.d/max-parser.conf

# 2. На VPS — перезавантажити systemd і застосувати конфіг journald:
systemctl daemon-reload
systemctl restart systemd-journald
systemctl enable max-parser.service max-dashboard.service

# 3. Запустити (або рестартнути) сервіси:
systemctl restart max-parser.service max-dashboard.service

# 4. Перевірити статус і логи:
systemctl status max-parser max-dashboard
journalctl -u max-parser -n 20
journalctl -u max-dashboard -n 20
```

## Керування

```bash
# статус (active = працює)
systemctl status max-parser max-dashboard

# перезапуск після змін коду
systemctl restart max-parser max-dashboard

# перегляд логів у реальному часі (журнал)
journalctl -u max-parser -f
journalctl -u max-dashboard -f

# тільки помилки за добу
journalctl -u max-parser -p err --since '24 hours ago'

# логи в межах періоду
journalctl -u max-parser --since '1 hour ago' --until now

# розмір журналу і скільки місця займає
journalctl --disk-usage

# зупинити
systemctl stop max-parser max-dashboard

# вимкнути автозапуск
systemctl disable max-parser max-dashboard
```

## Перевірка автозапуску

Після `systemctl enable` — при `reboot` обидві служби стартують автоматично.

Тест без реального reboot:
```bash
systemctl is-enabled max-parser      # повинно показати "enabled"
systemctl is-enabled max-dashboard   # повинно показати "enabled"
```

## Обмеження пам'яті

У unit-файлах є `MemoryMax=`:
- `max-parser` — 600 MB
- `max-dashboard` — 800 MB (бо Natasha моделі)

Якщо процес перевищить ліміт, systemd його **вб'є** і перезапустить (OOM-kill через cgroups, без зачеплення всього VPS). Якщо постійні падіння — підняти ліміт у файлі і `systemctl daemon-reload && systemctl restart`.
