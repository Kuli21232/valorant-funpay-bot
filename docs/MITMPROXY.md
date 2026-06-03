# mitmproxy — для реверса Riot Mobile

mitmproxy нужен для одного: **посмотреть, какие HTTP-запросы делает Riot Mobile (Android-приложение)** к серверам Riot. На основе этих данных мы будем имплементировать `mobile/device_backend.py` / `mobile/emulator_backend.py`.

## Что увидим

- `POST /api/v1/login` на каких хостах
- Body-формат (часто JSON или protobuf)
- Headers (`X-Riot-ClientVersion`, `X-Riot-Entitlements-JWT`, и т.п.)
- Cert pinning — есть или нет (если есть, нужен Frida bypass)

## Установка

```cmd
.venv\Scripts\pip install mitmproxy
```

Это даст утилиты `mitmweb`, `mitmproxy`, `mitmdump` в `.venv\Scripts\`.

## Use case A — записать веб-flow Riot (быстро)

Сначала проверим базовый сценарий на простом веб-логине account.riotgames.com. Это **уже работает** без танцев с Android (бот будет ходить через те же эндпоинты).

```cmd
capture_web.bat
```

1. Открывается `mitmweb` (UI в браузере на http://127.0.0.1:8081)
2. В Chrome ставите системный прокси `127.0.0.1:8080`
3. Открываете `account.riotgames.com`, логинитесь
4. В mitmweb видите все запросы — фильтруйте по `riotgames.com`
5. Правый клик на нужный запрос → **Save** → `.flow` файл

## Use case Б — Android-эмулятор (LDPlayer)

1. В LDPlayer: Настройки → Прокси → `<IP компа>:8080`
2. На эмуляторе установите Riot CA-сертификат:
   - откройте `http://mitm.it` → Download Android cert
   - Settings → Security → Install from storage
3. Запустите Riot Mobile App → log in
4. В mitmweb смотрите трафик `auth.riotgames.com`, `riot-geo.pas.si.riotgames.com`, etc.

Если cert pinning есть (Riot Mobile использует) — придётся обходить через **Frida** (отдельная инструкция, делается позже).

## Use case В — физический Android-телефон

1. На ПК: `mitmweb --listen-host 0.0.0.0` (доступен из локальной сети)
2. На телефоне: подключитесь к той же Wi-Fi, в настройках Wi-Fi → ручной прокси `<IP компа>:8080`
3. На телефоне в браузере: `mitm.it` → установите CA-серт
4. Запустите Riot Mobile App

## Парсинг сохранённых flow-файлов

```python
from mitmproxy import io
with open("riot_login.flow", "rb") as f:
    for flow in io.FlowReader(f).stream():
        print(flow.request.method, flow.request.url)
        print(flow.request.text[:200])
        print("--")
```

## Что делать дальше

Когда снимем рабочий flow Riot Mobile login:
1. Извлекаем endpoint, headers, body shape
2. Реализуем `mobile/api_client.py` — повторяет тот же flow в Python
3. Подключаем как `device_backend` или новый `mobile_api_backend`
4. Тестируем end-to-end: бот логинится в свой Riot Mobile → апрувит QR покупателя

При cert pinning — добавляется промежуточный шаг с Frida (~2-3 дня работы).
