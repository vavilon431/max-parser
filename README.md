# MAX Messenger Scout

Розвідувальний скрипт для пошуку API-ендпоінтів публічних каналів MAX.

## Встановлення

```bash
pip install -r requirements.txt
```

## Запуск

### З SOCKS5 проксі (рекомендовано — RU IP)
```bash
python scout.py --proxy socks5://user:pass@1.2.3.4:1080 --channels rt ria_novosti
```

### З HTTP проксі
```bash
python scout.py --proxy http://1.2.3.4:8080 --channels rt
```

### Без проксі (для тесту)
```bash
python scout.py --channels rt
```

## Результат

- Виводить у консоль всі відповіді з статус-кодами
- Зберігає детальні результати у `scout_results.json`
- Підсвічує успішні відповіді (200, 301, 302)

## Що шукає скрипт

1. Доступність доменів `max.ru`, `web.max.ru`, `app.max.ru`
2. Прямі URL каналів (`/rt`, `/ria_novosti` тощо)
3. Перебір API-шаблонів (`/api/v1/channels/rt`, `/v1/channels/rt/messages` тощо)
4. Загальні API-ендпоінти (`/openapi.json`, `/swagger.json` тощо)

## Після запуску

Скинь файл `scout_results.json` або вивід консолі — на основі цього буде написаний повноцінний парсер.
