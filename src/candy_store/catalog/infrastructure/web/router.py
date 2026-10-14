"""Adaptador HTTP del catálogo (back-office)."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, StringConstraints

from candy_store.catalog.application.ports.repository import CatalogUnitOfWork
from candy_store.catalog.application.use_cases.create_product import (
    CreateProductCommand,
    CreateProductUseCase,
)
from candy_store.catalog.application.use_cases.get_products import (
    GetProductUseCase,
    ListProductsUseCase,
    ProductView,
)
from candy_store.catalog.application.use_cases.manage_kit_composition import (
    SetKitCompositionCommand,
    SetKitCompositionUseCase,
)
from candy_store.catalog.application.use_cases.update_product import (
    UpdateProductCommand,
    UpdateProductUseCase,
)
from candy_store.catalog.domain.errors import DuplicateSkuError, ProductNotFoundError
from candy_store.catalog.domain.value_objects import KitComponent
from candy_store.catalog.infrastructure.persistence.unit_of_work import (
    SqlAlchemyCatalogUnitOfWork,
)
from candy_store.shared.domain.value_objects import ProductId
from candy_store.shared.infrastructure.web.dependencies import SessionFactoryDep
from candy_store.shared.infrastructure.web.errors import ErrorStatusMap

ERROR_STATUS: ErrorStatusMap = {
    ProductNotFoundError: status.HTTP_404_NOT_FOUND,
    DuplicateSkuError: status.HTTP_409_CONFLICT,
}

Price = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class ComponentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: UUID
    quantity: PositiveInt

    def to_domain(self) -> KitComponent:
        return KitComponent(product_id=ProductId(self.product_id), quantity=self.quantity)


Components = Annotated[list[ComponentIn], Field(min_length=1, max_length=50)]


class CreateProductRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
    name: Name
    price: Price
    components: Components | None = Field(
        default=None, description="Si se informa, el producto es un kit pre-armado"
    )


class SetComponentsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    components: Components


class ComponentOut(BaseModel):
    product_id: UUID
    quantity: int


class UpdateProductRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name | None = None
    price: Price | None = None
    active: bool | None = None


class ProductResponse(BaseModel):
    product_id: UUID
    sku: str
    name: str
    price: Decimal
    currency: str
    active: bool
    is_kit: bool
    components: list[ComponentOut]

    @classmethod
    def of(cls, view: ProductView) -> ProductResponse:
        return cls(
            product_id=view.product_id.value,
            sku=view.sku,
            name=view.name,
            price=view.price.amount,
            currency=view.price.currency,
            active=view.active,
            is_kit=view.is_kit,
            components=[
                ComponentOut(product_id=c.product_id.value, quantity=c.quantity)
                for c in view.components
            ],
        )


def _catalog_uow(session_factory: SessionFactoryDep) -> CatalogUnitOfWork:
    return SqlAlchemyCatalogUnitOfWork(session_factory)


UowDep = Annotated[CatalogUnitOfWork, Depends(_catalog_uow)]

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.post("/products", status_code=status.HTTP_201_CREATED)
async def create_product(body: CreateProductRequest, uow: UowDep) -> ProductResponse:
    product_id = await CreateProductUseCase(uow).execute(
        CreateProductCommand(
            sku=body.sku,
            name=body.name,
            price=body.price,
            components=[c.to_domain() for c in body.components or []],
        )
    )
    return ProductResponse.of(await GetProductUseCase(uow).execute(product_id))


@router.get("/products")
async def list_products(
    uow: UowDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ProductResponse]:
    views = await ListProductsUseCase(uow).execute(limit=limit, offset=offset)
    return [ProductResponse.of(v) for v in views]


@router.get("/products/{product_id}")
async def get_product(product_id: UUID, uow: UowDep) -> ProductResponse:
    return ProductResponse.of(await GetProductUseCase(uow).execute(ProductId(product_id)))


@router.patch("/products/{product_id}")
async def update_product(
    product_id: UUID, body: UpdateProductRequest, uow: UowDep
) -> ProductResponse:
    pid = ProductId(product_id)
    await UpdateProductUseCase(uow).execute(
        UpdateProductCommand(product_id=pid, name=body.name, price=body.price, active=body.active)
    )
    return ProductResponse.of(await GetProductUseCase(uow).execute(pid))


@router.put("/products/{product_id}/components")
async def set_kit_components(
    product_id: UUID, body: SetComponentsRequest, uow: UowDep
) -> ProductResponse:
    """Cambia la receta del kit. Los kits ya armados no se modifican."""
    pid = ProductId(product_id)
    await SetKitCompositionUseCase(uow).execute(
        SetKitCompositionCommand(kit_id=pid, components=[c.to_domain() for c in body.components])
    )
    return ProductResponse.of(await GetProductUseCase(uow).execute(pid))
