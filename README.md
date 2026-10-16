# Inventory Agent

A FastAPI inventory API and a terminal-based conversational agent. Inventory data is stored in `products.csv`; the agent implements its own tool loop without an agent framework.

## Install and configure

Requires Python 3.10 or newer. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Configure `.env` in the project root. If it does not exist yet, start from `.env.example`:

```dotenv
LLM_API_KEY=your_4geeks_api_key
LLM_BASE_URL=https://llm.4geeks.ai
LLM_MODEL=litellm/online/openrouter/deepseek/deepseek-v4-flash
INVENTORY_API_URL=http://127.0.0.1:8000
```

Do not share or commit `.env`. The agent removes the `litellm/` prefix when sending the model name to the 4Geeks gateway.

## Run

Start the API from the project root:

```bash
uvicorn api.app:app --reload --host 127.0.0.1 --port 8000
```

In a second terminal, start the agent:

```bash
python agent.py
```

Enter requests in natural language and type `/salir` to exit. The API must be running before the agent. FastAPI's interactive documentation is available at `http://127.0.0.1:8000/docs`.

## API endpoints

- `GET /inventory`: list all products.
- `POST /inventory`: create a product with `name`, `quantity`, and `unit`.
- `PATCH /inventory/{product_id}`: adjust stock with a positive `delta` to add or a negative one to reduce. The API rejects adjustments that would make stock negative.
- `GET /inventory/alerts?limit=10`: list products whose stock is strictly below the limit; the default is `10`.

Create a product with:

```bash
curl -X POST http://127.0.0.1:8000/inventory \
	-H 'Content-Type: application/json' \
	-d '{"name":"Arabica coffee","quantity":12,"unit":"bags"}'
```

Products persist in `products.csv` across API restarts. Each agent session keeps its conversation history in memory and appends events to `conversation_log.csv` using the fields `actor`, `message`, `tool_call`, and `timestamp`.

The system does not estimate how many weeks stock will last because it does not store sales history or demand forecasts. It can report current quantities and low-stock alerts.
