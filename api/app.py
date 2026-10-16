import csv
import math
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field


CSV_FIELDS = ["id", "name", "quantity", "unit"]
load_dotenv()


class InventoryError(ValueError):
    pass


class ProductNotFoundError(InventoryError):
    pass


class InsufficientStockError(InventoryError):
    pass


class InventoryStorageError(InventoryError):
    pass


class ProductCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    quantity: float = Field(ge=0, allow_inf_nan=False)
    unit: str = Field(min_length=1, max_length=40)


class ProductResponse(BaseModel):
    id: str
    name: str
    quantity: float
    unit: str


class StockAdjustment(BaseModel):
    delta: float = Field(ne=0, allow_inf_nan=False)


class CSVInventory:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._read_rows()
        else:
            self._write_rows([])

    def list_products(self):
        with self._lock:
            return [self._to_product(row) for row in self._read_rows()]

    def create_product(self, name, quantity, unit):
        name = self._required_text(name, "name")
        unit = self._required_text(unit, "unit")
        quantity = self._number(quantity, "quantity", allow_negative=False)
        row = {
            "id": str(uuid.uuid4()),
            "name": name,
            "quantity": str(quantity),
            "unit": unit,
        }
        with self._lock:
            rows = self._read_rows()
            rows.append(row)
            self._write_rows(rows)
        return self._to_product(row)

    def adjust_stock(self, product_id, delta):
        delta = self._number(delta, "delta", allow_negative=True)
        if delta == 0:
            raise InventoryError("delta no puede ser cero.")

        with self._lock:
            rows = self._read_rows()
            row = next((item for item in rows if item["id"] == product_id), None)
            if row is None:
                raise ProductNotFoundError(f"No existe el producto {product_id}.")
            quantity = self._number(row["quantity"], "quantity", allow_negative=False)
            updated_quantity = quantity + delta
            if updated_quantity < 0:
                raise InsufficientStockError(
                    f"Stock insuficiente: hay {quantity:g} {row['unit']}."
                )
            row["quantity"] = str(updated_quantity)
            self._write_rows(rows)
            return self._to_product(row)

    def low_stock_products(self, limit):
        limit = self._number(limit, "limit", allow_negative=False)
        return [
            product for product in self.list_products()
            if product["quantity"] < limit
        ]

    def _read_rows(self):
        try:
            with self.path.open("r", newline="", encoding="utf-8") as file:
                reader = csv.DictReader(file)
                if reader.fieldnames != CSV_FIELDS:
                    raise InventoryStorageError(
                        f"products.csv debe tener las columnas: {', '.join(CSV_FIELDS)}."
                    )
                rows = list(reader)
        except OSError as error:
            raise InventoryStorageError("No se pudo leer products.csv.") from error

        for row in rows:
            if None in row or any(row.get(field) is None for field in CSV_FIELDS):
                raise InventoryStorageError("products.csv contiene una fila incompleta.")
            self._number(row["quantity"], "quantity", allow_negative=False)
        return rows

    def _write_rows(self, rows):
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                newline="",
                encoding="utf-8",
                dir=self.path.parent,
                delete=False,
            ) as file:
                temporary_path = Path(file.name)
                writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
                writer.writeheader()
                writer.writerows(rows)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
        except OSError as error:
            raise InventoryStorageError("No se pudo guardar products.csv.") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _to_product(row):
        return {
            "id": row["id"],
            "name": row["name"],
            "quantity": float(row["quantity"]),
            "unit": row["unit"],
        }

    @staticmethod
    def _required_text(value, field_name):
        if not isinstance(value, str) or not value.strip():
            raise InventoryError(f"{field_name} no puede estar vacío.")
        return value.strip()

    @staticmethod
    def _number(value, field_name, allow_negative):
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise InventoryError(f"{field_name} debe ser un número.") from error
        if not math.isfinite(number) or (not allow_negative and number < 0):
            qualifier = "finito" if allow_negative else "un número no negativo y finito"
            raise InventoryError(f"{field_name} debe ser {qualifier}.")
        return number


def create_app(inventory=None):
    if inventory is None:
        products_path = os.getenv(
            "PRODUCTS_CSV_PATH",
            str(Path(__file__).resolve().parent.parent / "products.csv"),
        )
        inventory = CSVInventory(products_path)

    application = FastAPI(title="API de inventario")
    application.state.inventory = inventory

    @application.exception_handler(InventoryError)
    async def handle_inventory_error(_, error):
        if isinstance(error, InventoryStorageError):
            status_code = 500
        elif isinstance(error, ProductNotFoundError):
            status_code = 404
        elif isinstance(error, InsufficientStockError):
            status_code = 409
        else:
            status_code = 422
        return JSONResponse(status_code=status_code, content={"detail": str(error)})

    @application.get("/inventory", response_model=list[ProductResponse])
    def list_inventory():
        return inventory.list_products()

    @application.post(
        "/inventory", response_model=ProductResponse, status_code=201
    )
    def create_product(product: ProductCreate):
        return inventory.create_product(**product.model_dump())

    @application.patch(
        "/inventory/{product_id}", response_model=ProductResponse
    )
    def adjust_stock(product_id: str, adjustment: StockAdjustment):
        return inventory.adjust_stock(product_id, adjustment.delta)

    @application.get("/inventory/alerts", response_model=list[ProductResponse])
    def inventory_alerts(limit: Annotated[float, Query(ge=0)] = 10):
        return inventory.low_stock_products(limit)

    return application


app = create_app()
