import csv
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from dotenv import load_dotenv
from openai import OpenAI, OpenAIError


ROOT = Path(__file__).resolve().parent
LOG_FIELDS = ["actor", "message", "tool_call", "timestamp"]
MAX_TOOL_ROUNDS = 8

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_inventory",
            "description": "Lista los productos y su stock actual. Úsala antes de ajustar un producto por nombre para identificar su id.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_product",
            "description": "Registra un producto nuevo con su nombre, cantidad inicial y unidad.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Nombre del producto."},
                    "quantity": {"type": "number", "minimum": 0, "description": "Stock inicial."},
                    "unit": {"type": "string", "description": "Unidad, por ejemplo bolsas, litros o unidades."},
                },
                "required": ["name", "quantity", "unit"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "adjust_stock",
            "description": "Ajusta el stock existente usando un delta: positivo para una entrega y negativo para una venta o reducción. Requiere el id del producto.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string", "description": "Id devuelto por list_inventory."},
                    "delta": {"type": "number", "not": {"const": 0}, "description": "Cambio relativo del stock; negativo para ventas."},
                },
                "required": ["product_id", "delta"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_low_stock_alerts",
            "description": "Consulta productos cuyo stock está por debajo del límite; el límite por defecto es 10.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "number", "minimum": 0, "default": 10, "description": "Límite de stock exclusivo; se alertan cantidades inferiores."},
                },
                "additionalProperties": False,
            },
        },
    },
]

SYSTEM_PROMPT = """Eres el asistente de inventario de una tienda de suministros para cafeterías.
Responde en español, de forma breve y clara. Usa las herramientas para consultar o cambiar el inventario; nunca inventes cantidades ni afirmes que una acción se completó sin confirmación de la API.
Para ajustar un producto mencionado por nombre, consulta primero el inventario y usa el id del producto que coincida exactamente. Si hay más de una coincidencia o falta información, pregunta antes de modificar stock. Usa deltas positivos para entregas y negativos para ventas. Puedes encadenar herramientas en una misma solicitud, por ejemplo crear un producto y luego consultar alertas. El límite predeterminado de alertas es 10. No calcules cobertura semanal: no hay historial de demanda disponible."""


class ConversationLogger:
    def __init__(self, path=None):
        self.path = Path(
            path or os.getenv("CONVERSATION_LOG_PATH", ROOT / "conversation_log.csv")
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._ensure_header()

    def log(self, actor, message, tool_call=""):
        row = {
            "actor": actor,
            "message": str(message),
            "tool_call": tool_call,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock, self.path.open("a", newline="", encoding="utf-8") as file:
            csv.DictWriter(file, fieldnames=LOG_FIELDS).writerow(row)
            file.flush()
            os.fsync(file.fileno())

    def _ensure_header(self):
        with self._lock:
            needs_header = not self.path.exists() or self.path.stat().st_size == 0
            if needs_header:
                with self.path.open("a", newline="", encoding="utf-8") as file:
                    csv.writer(file).writerow(LOG_FIELDS)
                    file.flush()
                    os.fsync(file.fileno())


class InventoryToolExecutor:
    def __init__(self, api_client):
        self.api_client = api_client

    def execute(self, name, arguments):
        try:
            if name == "list_inventory":
                response = self.api_client.get("/inventory")
            elif name == "create_product":
                response = self.api_client.post("/inventory", json=arguments)
            elif name == "adjust_stock":
                product_id = arguments["product_id"]
                response = self.api_client.patch(
                    f"/inventory/{product_id}", json={"delta": arguments["delta"]}
                )
            elif name == "get_low_stock_alerts":
                response = self.api_client.get(
                    "/inventory/alerts",
                    params={"limit": arguments.get("limit", 10)},
                )
            else:
                return {"error": f"Herramienta desconocida: {name}."}
        except (httpx.RequestError, KeyError, TypeError, ValueError) as error:
            return {"error": f"No se pudo ejecutar {name}: {error}"}

        try:
            result = response.json()
        except ValueError:
            result = {"detail": response.text}
        if response.is_error:
            if isinstance(result, dict):
                return {"error": result.get("detail", "Error de la API."), "status_code": response.status_code}
            return {"error": "Error de la API.", "status_code": response.status_code}
        return result


class InventoryAgent:
    def __init__(
        self,
        llm_client=None,
        api_client=None,
        logger=None,
        model=None,
        max_tool_rounds=MAX_TOOL_ROUNDS,
    ):
        load_dotenv(ROOT / ".env")
        if llm_client is None:
            api_key = os.getenv("LLM_API_KEY")
            if not api_key:
                raise ValueError("Falta configurar LLM_API_KEY en el archivo .env.")
            llm_base_url = self._versioned_base_url(
                os.getenv("LLM_BASE_URL", "https://llm.4geeks.ai")
            )
            llm_client = OpenAI(api_key=api_key, base_url=llm_base_url)

        self.llm_client = llm_client
        self.model = (model or os.getenv(
            "LLM_MODEL", "litellm/online/openrouter/deepseek/deepseek-v4-flash"
        )).removeprefix("litellm/")
        self.logger = logger or ConversationLogger()
        self._owns_api_client = api_client is None
        self.api_client = api_client or httpx.Client(
            base_url=os.getenv("INVENTORY_API_URL", "http://127.0.0.1:8000").rstrip("/"),
            timeout=15,
        )
        self.tools = InventoryToolExecutor(self.api_client)
        self.max_tool_rounds = max_tool_rounds
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    def respond(self, user_message):
        user_message = user_message.strip()
        if not user_message:
            return "Escribe una solicitud para consultar o actualizar el inventario."

        self.messages.append({"role": "user", "content": user_message})
        self.logger.log("user", user_message)

        for round_index in range(self.max_tool_rounds + 1):
            try:
                completion = self.llm_client.chat.completions.create(
                    model=self.model,
                    messages=self.messages,
                    tools=TOOL_DEFINITIONS,
                    tool_choice="auto",
                )
            except OpenAIError:
                message = "No pude conectarme con el modelo. Revisa la configuración y vuelve a intentarlo."
                self.logger.log("agent", message)
                return message

            assistant_message = completion.choices[0].message
            tool_calls = assistant_message.tool_calls or []
            if not tool_calls:
                answer = assistant_message.content or "No recibí una respuesta del modelo."
                self.messages.append({"role": "assistant", "content": answer})
                self.logger.log("agent", answer)
                return answer

            self.messages.append(self._assistant_tool_message(assistant_message))
            if round_index == self.max_tool_rounds:
                message = "Alcancé el límite de pasos de herramientas; no ejecuté más acciones. Intenta dividir la solicitud."
                self.logger.log("agent", message, tool_call=tool_calls[0].function.name)
                self.messages.append({"role": "assistant", "content": message})
                return message

            for tool_call in tool_calls:
                tool_name = tool_call.function.name
                arguments = {}
                try:
                    arguments = json.loads(tool_call.function.arguments or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("Los argumentos deben ser un objeto JSON.")
                    result = self.tools.execute(tool_name, arguments)
                except (json.JSONDecodeError, ValueError) as error:
                    result = {"error": f"Argumentos inválidos para {tool_name}: {error}"}

                self.logger.log(
                    "agent",
                    json.dumps(arguments, ensure_ascii=False),
                    tool_call=tool_name,
                )
                result_message = json.dumps(result, ensure_ascii=False)
                self.logger.log("tool", result_message, tool_call=tool_name)
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_message,
                    }
                )

        message = "No se pudo completar la conversación."
        self.logger.log("agent", message)
        return message

    def close(self):
        if self._owns_api_client:
            self.api_client.close()

    @staticmethod
    def _assistant_tool_message(assistant_message):
        return {
            "role": "assistant",
            "content": assistant_message.content,
            "tool_calls": [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                for tool_call in assistant_message.tool_calls
            ],
        }

    @staticmethod
    def _versioned_base_url(base_url):
        parts = urlsplit(base_url.rstrip("/"))
        path = parts.path.rstrip("/")
        if not path.endswith("/v1"):
            path = f"{path}/v1"
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def main():
    load_dotenv(ROOT / ".env")
    if not os.getenv("LLM_API_KEY"):
        print("Falta configurar LLM_API_KEY en el archivo .env.")
        return 1

    try:
        agent = InventoryAgent()
    except ValueError as error:
        print(error)
        return 1

    print("Asistente de inventario. Escribe /salir para terminar.")
    try:
        while True:
            try:
                user_message = input("Carla: ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if user_message.strip().casefold() in {"/salir", "salir", "exit", "quit"}:
                break
            print(f"Agente: {agent.respond(user_message)}")
    finally:
        agent.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
