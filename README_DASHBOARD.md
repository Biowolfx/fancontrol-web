Dashboard enhancements
====================

Что добавлено:

- Drag & resize layout с использованием Gridstack (CDN).
- Сохранение layout в `localStorage` и попытка синхронизации на сервере через `/api/dashboard`.
- Пресеты layout: `/api/dashboard/presets` (GET/POST) и `/api/dashboard/presets/<name>` (DELETE).
- Динамические карточки: Overview, Fans (по вентилятору мини-график), Disks, Sensors, Events.
- Per-card настройки (threshold, refresh) — хранятся в `localStorage` (`fc_card_settings`).
- Lazy-render графиков (IntersectionObserver) и per-card polling (если задан `refresh`).

Как использовать:

- Откройте дашборд и перетащите карточки, затем нажмите `Save` или `Save Preset`.
- Для сохранения пресета введите имя; пресеты доступны через селект в тулбаре.
- Откройте настройки карточки через значок ⚙ и задайте `Threshold` (подсветка) или `Refresh` (ms).

Разработка:

- Файлы фронтенда: `templates/js/dashboard.js`, `templates/index.html`.
- Серверные эндпоинты: `server/routes.py` — `/api/dashboard*`, `/api/history`.

Тесты:

- Backend tests остаются работоспособны (pytest passed).
