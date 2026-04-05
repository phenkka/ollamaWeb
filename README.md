# Cyber-Poligon WebUI

Минимальный веб-интерфейс для общения с LLM (через Ollama), адаптированный под задачи киберполигона.

## Основа

Проект основан на оригинальном `ollama-webui`:

https://github.com/ollama-webui/ollama-webui


## Запуск
### Через Docker Compose

```bash
docker compose up -d --build
```

После запуска открой:

http://localhost:3000

## Конфигурация (env)

Файл `.env` используется для настройки.

- `WEBUI_AUTH`
  - `false` — режим гостя (по умолчанию)
  - `true` — включить авторизацию

- `WEBUI_MINIMAL`
  - `true` — минимальный режим UI (для киберполигона)
  - `false` — полный режим

- `OLLAMA_API_BASE_URL`
  - URL Ollama (например `http://127.0.0.1:11434` или адрес удалённого сервера)

## Лицензия

См. файл `LICENSE`.
