from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, ValidationError, ValidationInfo
from pydantic_core import InitErrorDetails

REGISTRIES_CONTEXT_KEY = "registries"

T = TypeVar("T")


class Registry(Generic[T]):

    def __init__(self, t: type[T]):
        self.registry_type = t
        self._entries: dict[str, T] = {}

    def register(self, key: str, value: T) -> None:
        self._entries[key] = value

    def get(self, key: str) -> Optional[T]:
        return self._entries.get(key)

    def by(self, value: type[T]) -> Optional[str]:
        for k, v in self._entries.items():
            if v == value:
                return k
        return None

    def all(self) -> dict[str, T]:
        return dict(self._entries)

    def keys(self) -> set[str]:
        return set(self._entries.keys())

    def size(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        cls = self.registry_type
        fqn = f"{cls.__module__}.{cls.__qualname__}"
        return f"Registry<{fqn}>({self.size()} entries)"


M = TypeVar("M", bound=BaseModel)


class ModelRegistry(Generic[M], Registry[type[M]]):
    def __init__(self, t: type[M]):
        super().__init__(t)  # type: ignore

    def load_model(self, root: "Registries", key: str, data: Any) -> M | None:
        model = self.get(key)
        if not model:
            return None
        return model.model_validate(data, context={REGISTRIES_CONTEXT_KEY: root})
    
    def register_model(self, t: type[M], discriminator: str = "type"):
        field = t.model_fields.get(discriminator)
        if not field:
            raise ValueError(
                f"Missing discriminator {discriminator!r} in model {t}")
        key = field.default
        if not key:
            raise ValueError(f"No default value for {discriminator!r} in model provided to use as key")
        self.register(str(key), t)


class Registries(Registry[Registry]):
    def __init__(self):
        super().__init__(Registry)
        self.registry_types: dict[str, type] = {}

    def create_model_registry(self, key: str, t: type[M]) -> ModelRegistry[M]:
        reg = ModelRegistry[t](t)
        self.registry_types[key] = t
        self.register(key, reg)
        return reg
    
    def create_registry(self, key: str, t: type[T]) -> Registry[T]:
        reg = Registry[t](t)
        self.registry_types[key] = t
        self.register(key, reg)
        return reg

    def get_registry(self, t: type[T]) -> Registry[T] | None:
        for r in self.all().values():
            if r.registry_type == t:
                return r
        return None

    def get_model_registry(self, key: str) -> ModelRegistry[BaseModel] | None:
        reg = self.get(key)
        if not reg:
            return None
        if isinstance(reg, ModelRegistry):
            return reg
        else:
            return None


def registry_from_info(data: dict, info: ValidationInfo, key: str = "type"):
    context = info.context or {}
    registry: Registries | None = context.get(REGISTRIES_CONTEXT_KEY)
    if not registry:
        raise ValueError("Missing registries context")
    t = data.get(key)
    if not isinstance(t, str):
        raise ValidationError.from_exception_data(
            title="type must be provided and be a string",
            line_errors=[InitErrorDetails(
                type="string_type",
                loc=(key,),
                input=t
            )]
        )
    return registry, t


def raise_unkown_type(field: str, input: str, keys: set[str]):
    raise ValidationError.from_exception_data(
        title="Unknown type",
        line_errors=[InitErrorDetails(
            type="enum",
            loc=(field,),
            input=input,
            ctx={
                "expected": str(keys)
            }
        )]
    )


def get_registry_entry_from_info(registry_type: type[M], data: dict, info: ValidationInfo, field: str = "type") -> type[M]:
    root, type = registry_from_info(data, info, field)
    reg = root.get_registry(registry_type)
    if not reg:
        raise ValueError(f"Unknown registry for type {registry_type}")
    entry = reg.get(type)
    if entry is None:
        raise_unkown_type(field, type, reg.keys())
    return entry  # type: ignore
