"""`record(T, data)`: a mapping (a dict loaded from a file, say) into a record of type T, checked at runtime.

The checker can't check a dict's keys, so `(**d)` into a record type is an error there, and at runtime it
silently builds whatever keys `d` has. `record()` checks instead: every field of T present, no others
(unless T is open), and each value's type. In a `.pyn`, the call's type is T for the checker
(transform.py wraps it in `cast(T, ...)`).

At runtime a record type alias `type C = (a: int, b: str)` is `_typ_a__b[int, str]`: a Protocol whose
annotations list the fields in written order (`a: Final[T0]`) and whose subscript holds the field types.
`_opn_...` is an open type `(..., a: int)`.

Type checks follow the checker's rules where it's cheap: isinstance for classes, an int is a float,
a bool is an int, unions, None, Literal, Any/object. Nested record types are checked recursively,
and a mapping there is built into a record. Generics like `list[int]` are checked shallowly: the
container's type only, not its elements.
"""

import types
import typing
from collections import namedtuple
from collections.abc import Mapping
from typing import Any, TypeAliasType

from .transform import ORDER, PRELUDE

# the same record classes and methods every translated module gets: equal to records built there
_ns: dict[str, Any] = {}
exec(PRELUDE, _ns)  # noqa: S102 -- byname's own prelude, not user input
_classes: dict[tuple[str, ...], type] = {}


def _build(fields: tuple[str, ...], values: dict[str, object]) -> object:
    c = _classes.get(fields)
    if c is None:
        c = _classes[fields] = namedtuple("_rec_" + "__".join(fields), sorted(fields))
        _ns["_byname_setup"](c, fields)
    return c(**values)


def _record_type(tp: object) -> tuple[list[tuple[str, object]], bool] | None:
    """(fields with their types, is_open) if tp is a record type, else None. Aliases are unwrapped."""
    while isinstance(tp, TypeAliasType):
        tp = tp.__value__
    origin = typing.get_origin(tp) or tp
    name = getattr(origin, "__name__", "")
    if not isinstance(origin, type) or not name.startswith(("_typ_", "_opn_")):
        return None
    args = typing.get_args(tp)
    params = getattr(origin, "__type_params__", ())
    fields = []
    for field, ann in origin.__annotations__.items():  # `a: Final[T0]`, in written order
        (param,) = typing.get_args(ann)
        fields.append((field, args[params.index(param)] if args else Any))
    return fields, name.startswith("_opn_")


def _type_name(tp: object) -> str:
    if isinstance(tp, TypeAliasType):
        return tp.__name__
    if (rt := _record_type(tp)) is not None:
        fields, is_open = rt
        inner = ", ".join(f"{f}: {_type_name(t)}" for f, t in fields)
        return "(" + ("..., " if is_open else "") + inner + ")"
    if tp is None or tp is types.NoneType:
        return "None"
    if isinstance(tp, type) and not typing.get_args(tp):
        return tp.__name__
    return repr(tp).replace("typing.", "")


def _is_record(value: object) -> bool:
    return hasattr(type(value), ORDER)


def _is_mapping(value: object) -> bool:
    return isinstance(value, Mapping) or _is_record(value)


def _convert(value: object, tp: object, path: str, errors: list[str]) -> object:
    """value (the field at dotted path) checked against tp, problems appended to errors; a mapping where
    a record type is expected comes back as that record."""
    at = f"field {path!r}"
    while isinstance(tp, TypeAliasType) and _record_type(tp) is None:
        tp = tp.__value__  # a plain alias, `type Ids = list[int]`
    if tp is Any or tp is object:
        return value
    if _record_type(tp) is not None:
        if _is_mapping(value):
            return _from_mapping(tp, value, path, errors)
        errors.append(f"{at}: expected {_type_name(tp)}, got {type(value).__name__}")
        return value
    origin = typing.get_origin(tp)
    if origin is typing.Union or origin is types.UnionType:
        for member in typing.get_args(tp):
            member_errors: list[str] = []
            converted = _convert(value, member, path, member_errors)
            if not member_errors:
                return converted
        errors.append(f"{at}: expected {_type_name(tp)}, got {type(value).__name__} ({value!r})")
        return value
    if origin is typing.Literal:
        if value not in typing.get_args(tp):
            errors.append(f"{at}: expected {_type_name(tp)}, got {value!r}")
        return value
    if tp is None or tp is types.NoneType:
        if value is not None:
            errors.append(f"{at}: expected None, got {type(value).__name__} ({value!r})")
        return value
    cls = origin or tp  # shallow for generics: list[int] checks list
    if not isinstance(cls, type):
        return value  # TypeVars, Callable[...] and other forms: not checked
    ok = isinstance(value, cls)
    if cls is float:  # as for the checker, an int is a float, and both are complex
        ok = isinstance(value, (int, float))
    elif cls is complex:
        ok = isinstance(value, (int, float, complex))
    if not ok:
        errors.append(f"{at}: expected {_type_name(tp)}, got {type(value).__name__} ({value!r})")
    return value


def _from_mapping(tp: object, data: object, prefix: str, errors: list[str]) -> object:
    rt = _record_type(tp)
    assert rt is not None
    fields, is_open = rt
    keys = list(data.keys())  # pyright: ignore[reportAttributeAccessIssue]
    names = [f for f, _ in fields]
    values: dict[str, object] = {}
    for field, field_type in fields:
        path = f"{prefix}.{field}" if prefix else field
        if field not in keys:
            errors.append(f"missing field {path!r}")
            continue
        values[field] = _convert(data[field], field_type, path, errors)  # pyright: ignore[reportIndexIssue]
    extra = [k for k in keys if k not in names]
    for k in extra:
        if is_open:
            values[k] = data[k]  # pyright: ignore[reportIndexIssue]
        else:
            errors.append(f"unexpected field {(f'{prefix}.{k}' if prefix else k)!r}")
    order = tuple(names + extra) if is_open else tuple(names)
    return _build(order, values) if not errors else None


def record(t: object, data: object, /) -> object:
    """A record of type t from a mapping (or another record), checked at runtime. Raises TypeError naming
    every missing, unexpected or wrongly typed field at once."""
    if _record_type(t) is None:
        raise TypeError(f"record() needs a record type, got {_type_name(t)}")
    if not _is_mapping(data):
        raise TypeError(f"record() needs a mapping of field names, got {type(data).__name__}")
    errors: list[str] = []
    result = _from_mapping(t, data, "", errors)
    if errors:
        raise TypeError(f"{_type_name(t)}: " + "; ".join(errors))
    return result
