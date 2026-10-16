# Agente de inventario

API de inventario con FastAPI y un agente conversacional de terminal. El inventario se guarda en `products.csv`; el agente usa tools propias, implementadas con el cliente OpenAI compatible, sin frameworks de agentes.

## Instalación y configuración

Requiere Python 3.10 o superior. Instala las dependencias:

```bash
python -m pip install -r requirements.txt
```

Configura el archivo `.env` en la raíz del proyecto. Si todavía no existe, puedes partir de `.env.example`:

```dotenv
LLM_API_KEY=tu_clave_de_4geeks
LLM_BASE_URL=https://llm.4geeks.ai
LLM_MODEL=litellm/online/openrouter/deepseek/deepseek-v4-flash
INVENTORY_API_URL=http://127.0.0.1:8000
```

No compartas ni subas `.env` al repositorio. El agente quita el prefijo `litellm/` al enviar el modelo al gateway de 4Geeks.

## Ejecución

Inicia la API desde la raíz del proyecto:

```bash
uvicorn api.app:app --reload --host 127.0.0.1 --port 8000
```

En otra terminal, inicia el agente:

```bash
python agent.py
```

Escribe una solicitud en lenguaje natural. Usa `/salir` para terminar. La API debe estar activa antes de iniciar el agente. La documentación interactiva de FastAPI está en `http://127.0.0.1:8000/docs`.

## API

- `GET /inventory`: lista todos los productos.
- `POST /inventory`: crea un producto con `name`, `quantity` y `unit`.
- `PATCH /inventory/{product_id}`: cambia el stock con `delta` positivo para añadir o negativo para reducir. Rechaza cambios que dejarían el stock por debajo de cero.
- `GET /inventory/alerts?limit=10`: lista productos cuyo stock es estrictamente inferior al límite; `10` es el valor predeterminado.

Ejemplo para registrar un producto:

```bash
curl -X POST http://127.0.0.1:8000/inventory \
	-H 'Content-Type: application/json' \
	-d '{"name":"Café arábica","quantity":12,"unit":"bolsas"}'
```

Los productos persisten en `products.csv`, incluso al reiniciar la API. Cada sesión del agente conserva el historial de mensajes en memoria y añade eventos a `conversation_log.csv` con los campos `actor`, `message`, `tool_call` y `timestamp`; el log es append-only.

El sistema no estima cuántas semanas durará el stock porque no guarda historial ni previsión de ventas. Sí puede consultar cantidades y alertas de stock bajo.
