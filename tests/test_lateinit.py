import pytest
from mcsi.utils import LateInit   # adjust to your module


class Dummy:
    value: LateInit[int] = LateInit()


def test_access_before_set_raises():
    obj = Dummy()
    with pytest.raises(AttributeError):
        _ = obj.value


def test_set_then_get_returns_value():
    obj = Dummy()
    obj.value = 42
    assert obj.value == 42


def test_delete_resets_initialization():
    obj = Dummy()
    obj.value = 10
    del obj.value
    with pytest.raises(AttributeError):
        _ = obj.value


def test_values_are_per_instance_not_shared():
    a = Dummy()
    b = Dummy()

    a.value = 1
    b.value = 2

    assert a.value == 1
    assert b.value == 2


def test_descriptor_access_from_class_returns_descriptor():
    assert isinstance(Dummy.value, LateInit)
