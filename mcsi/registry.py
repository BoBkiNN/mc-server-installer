from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, ValidationError, ValidationInfo
from pydantic_core import InitErrorDetails
from typing import get_args, Literal, Any
import inspect

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

    def keys(self) -> list[str]:
        return list(self._entries.keys())

    def size(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        cls = self.registry_type
        fqn = f"{cls.__module__}.{cls.__qualname__}"
        return f"Registry<{fqn}>({self.size()} entries)"
    
    def dump_entry(self, entry: T) -> Any:
        return str(entry)
    
    def dump(self, to: dict[str, Any]):
        cls = self.registry_type
        fqn = f"{cls.__module__}.{cls.__qualname__}"
        to["entry_type"] = fqn
        to["type"] = "default"
        to["size"] = self.size()
        entries: dict[str, Any] = {}
        for key, entry in self.all().items():
            entries[key] = self.dump_entry(entry)
        to["entries"] = entries


M = TypeVar("M", bound=BaseModel)


class TypedModel(BaseModel):
    # TODO cache type

    @classmethod
    def get_type(cls) -> str:
        if inspect.isabstract(cls):
            raise TypeError("Abstract base class cannot have type")
        ann = cls.model_fields["type"].annotation
        if getattr(ann, "__origin__", None) is Literal:
            args = get_args(ann)
            if len(args) == 1 and isinstance(args[0], str):
                return args[0]
            raise TypeError(
                f"{cls.__name__}.type must be Literal with one string")
        raise TypeError(f"{cls.__name__}.type must be Literal")


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
        ann = getattr(field.annotation, "__origin__", None)
        if ann is Literal:
            literal_args = get_args(field.annotation)
            if len(literal_args) == 1:
                key = literal_args[0]
            else:
                raise ValueError(f"Discriminator {discriminator!r} in model {t} must be a Literal with a single value")
        else:
            key = field.default
        if not key:
            raise ValueError(f"No default value or Literal for {discriminator!r} provided to use as key")
        self.register(str(key), t)
    
    def register_models(self, *args: type[M], discriminator: str = "type"):
        for m in args:
            self.register_model(m, discriminator)
    
    def dump_entry(self, entry: type[M]) -> Any:
        fqn = f"{entry.__module__}.{entry.__qualname__}"
        fields = [name for name in entry.model_fields]
        return {
            "type": fqn,
            "fields": fields
        }
    
    def dump(self, to: dict[str, Any]):
        super().dump(to)
        to["type"] = "model"


class Registries(Registry[Registry]):
    def __init__(self):
        super().__init__(Registry)
        self.registry_types: dict[str, type] = {}

    def create_model_registry(self, key: str, t: type[M]) -> ModelRegistry[M]:
        reg = ModelRegistry(t)
        self.registry_types[key] = t
        self.register(key, reg)
        return reg
    
    def create_registry(self, key: str, t: type[T]) -> Registry[T]:
        reg = Registry(t)
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
    
    def dump_entry(self, entry: Registry) -> Any:
        d = {}
        entry.dump(d)
        return d
    
    def dump(self, to: dict[str, Any]):
        super().dump(to)
        to["type"] = "root"


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


def raise_unknown_type(field: str, input: str, keys: list[str]):
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
        raise_unknown_type(field, type, reg.keys())
    return entry  # type: ignore
