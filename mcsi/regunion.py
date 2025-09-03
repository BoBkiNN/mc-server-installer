from typing import Annotated, Any, Type

from pydantic import BaseModel, GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue
from pydantic_core import core_schema
from registry import *


class RegistriesGenerateJsonSchema(GenerateJsonSchema):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def get_registries(self) -> Registries:
        ...
    
    # def generate(self, schema: core_schema.CoreSchema, mode: JsonSchemaMode = 'validation'):
    #    v = super().generate(schema, mode)
    #    v["$schema"] = GenerateJsonSchema.schema_dialect
    #    return v

    @classmethod
    def __class_getitem__(cls, item: Registries):
        return make_registry_schema_generator(item)


def make_registry_schema_generator(registries: Registries) -> type[RegistriesGenerateJsonSchema]:
    class RegistryAwareGenerateJsonSchemaImpl(RegistriesGenerateJsonSchema):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.registries = registries

        def get_registries(self) -> Registries:
            return self.registries
    return RegistryAwareGenerateJsonSchemaImpl


class RegistryUnion:
    """Usage: 
    >>> Annotated[Animal, RegistryUnion("animals")]
    """

    def __init__(self, registry_key: str, discriminator: str = "type", add_ref: bool = True):
        self.registry_key = registry_key
        self.discriminator = discriminator
        self.ref = f"RegistryUnion__{registry_key}" if add_ref else None

    def __get_pydantic_core_schema__(self, source: Type[Any], 
                                     handler: GetCoreSchemaHandler
                                     ) -> core_schema.CoreSchema:
        def union_validator(value, info: core_schema.ValidationInfo):
            if isinstance(value, BaseModel):
                return value
            context = info.context or {}

            registries: Registries = context[REGISTRIES_CONTEXT_KEY]
            registry = registries.get_model_registry(self.registry_key)
            if not registry:
                raise ValueError(f"Unknown registry {self.registry_key}")
            # print("value: ", value, type(value))
            if self.discriminator not in value:
                raise ValidationError.from_exception_data(
                    "Missing discriminator field", line_errors=[
                        InitErrorDetails(type="missing", loc=(
                            self.discriminator,), input=None)
                    ])
            key = value[self.discriminator]
            model = registry.get(key)
            if model is None:
                keys = registry.keys()
                raise ValidationError.from_exception_data(
                    f"Unknown registry key for registry '{self.registry_key}'", line_errors=[
                        InitErrorDetails(type="enum", loc=(
                            self.discriminator,), input=key, ctx={"expected": str(keys)})
                    ])
            return model.model_validate(value, context=info.context)
        
        ret = core_schema.with_info_after_validator_function(union_validator, 
                                                              core_schema.any_schema(),
                                                              ref=self.ref)
        print(f"=== Ret core schema in {self.registry_key}", ret)
        return ret

    def __get_pydantic_json_schema__(self, schema: core_schema.CoreSchema, 
                                     handler: GetJsonSchemaHandler
                                     ) -> JsonSchemaValue:
        generate = getattr(handler, "generate_json_schema")
        registries: Registries | None
        if isinstance(generate, RegistriesGenerateJsonSchema):
            registries = generate.get_registries()
        else:
            registries = None
        assert isinstance(generate, GenerateJsonSchema)

        # fallback
        if registries is None:
            registries = schema.get("metadata", {}).get("registries")

        if not registries:
            return {"type": "object", "title": f"RegistryUnion[{self.registry_key}]"}
        registry = registries.get_model_registry(self.registry_key)
        if not registry:
            raise ValueError(f"Unknown registry {self.registry_key}")
        tagged_choices: dict[str, core_schema.CoreSchema] = {}
        for key, model in registry.all().items():
            # type: ignore
            tagged_choices[key] = model.__pydantic_core_schema__

        ret_schema = core_schema.tagged_union_schema(
            tagged_choices,
            self.discriminator,
            metadata=schema.get("metadata", {}) or {},
            ref=self.ref
        )
        ret = generate.tagged_union_schema(ret_schema)
        # ret = handler(ret_schema)
        return ret


class RegistryKey:
    """Usage: 
    >>> Annotated[str, RegistryKey("animals")]
    """

    def __init__(self, registry_key: str, add_ref: bool = True):
        self.registry_key = registry_key
        self.ref = f"RegistryKey__{registry_key}" if add_ref else None

    def __get_pydantic_core_schema__(self, source: Type[Any], handler) -> core_schema.CoreSchema:
        return core_schema.str_schema(ref=self.ref)

    def __get_pydantic_json_schema__(self, schema: core_schema.CoreSchema, handler) -> JsonSchemaValue:
        generate = getattr(handler, "generate_json_schema", None)
        registries: Registries | None
        if isinstance(generate, RegistriesGenerateJsonSchema):
            registries = generate.get_registries()
        else:
            registries = None

        # fallback
        if registries is None:
            registries = schema.get("metadata", {}).get("registries")

        if not registries:
            return {"type": "string", "title": f"RegistryKey[{self.registry_key}]"}

        registry = registries.get(self.registry_key)
        if not registry:
            raise ValueError(f"Unknown registry {self.registry_key}")

        keys = list(registry.keys())
        # sch = core_schema.enum_schema(str, keys, sub_type="str")
        # sch = core_schema.union_schema([
        #     core_schema.str_schema(),
        #     core_schema.literal_schema(keys)
        # ], ref=self.ref)
        sch = core_schema.literal_schema(keys, ref=self.ref)
        return handler(sch)
        # return {
        #     "type": "string",
        #     "enum": keys,
        #     "title": f"RegistryKey[{self.registry_key}]"
        # }

if __name__ == "__main__":
    import json
    from pathlib import Path

    class Animal(BaseModel):
        pass

    class Dog(Animal):
        barks: int
        type: str = "dog"

    class Cat(Animal):
        meows: int

    root = Registries()
    animals = root.create_model_registry("animals", Animal)
    animals.register("dog", Dog)
    animals.register("cat", Cat)

    class TestModel(BaseModel):
        pet: Annotated[Animal, RegistryUnion("animals")]
        pet_type: Annotated[str, RegistryKey("animals")]

    m = TestModel.model_validate(TestModel(pet=Dog(barks=3), pet_type="dog"), context={
                                 REGISTRIES_CONTEXT_KEY: root})
    d = m.model_dump()
    print("d:", d)
    p = TestModel.model_validate(d, context={REGISTRIES_CONTEXT_KEY: root})
    print("p:", p)

    sch = TestModel.model_json_schema(
        schema_generator=RegistriesGenerateJsonSchema[root])
    Path("run/test_schema.json").write_text(json.dumps(sch, indent=2))
