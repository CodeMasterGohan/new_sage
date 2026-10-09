"""A fake ``pymilvus`` package for schema tests.

Lets us exercise :mod:`milvus_setup.schema`'s builder functions (which
lazy-import pymilvus) without a real pymilvus install or a live Milvus.
Install with ``install_fake_pymilvus(monkeypatch)`` before importing the
builders; teardown via the fixture's monkeypatch undo.

Captured kwargs are exposed on the fake classes:
``FakeFieldSchema.instances``, ``FakeFunction.instances``,
``FakeCollectionSchema.last``, ``FakeIndexParams.last``.
"""

from __future__ import annotations

import sys
import types


class _DataType:
    def __class_getitem__(cls, name):  # noqa: N805
        return f"DataType.{name}"


class _FunctionType:
    def __class_getitem__(cls, name):  # noqa: N805
        return f"FunctionType.{name}"


class FakeFieldSchema:
    instances: list["FakeFieldSchema"] = []

    def __init__(self, name, dtype, **kwargs):
        self.name = name
        self.dtype = dtype
        self.kwargs = kwargs
        FakeFieldSchema.instances.append(self)


class FakeFunction:
    instances: list["FakeFunction"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakeFunction.instances.append(self)


class FakeCollectionSchema:
    last: "FakeCollectionSchema | None" = None

    def __init__(self, fields, **kwargs):
        self.fields = fields
        self.kwargs = kwargs
        FakeCollectionSchema.last = self


class FakeIndexParams:
    last: "FakeIndexParams | None" = None

    def __init__(self):
        self.indexes: list[dict] = []
        FakeIndexParams.last = self

    def add_index(self, **kwargs):
        self.indexes.append(kwargs)

    def __iter__(self):  # schema iterates index params for ensure_indexes
        return iter(self.indexes)


def install_fake_pymilvus(monkeypatch) -> None:
    """Insert the fake pymilvus package (and submodule) into sys.modules."""
    pkg = types.ModuleType("pymilvus")
    pkg.DataType = _DataType
    pkg.FieldSchema = FakeFieldSchema
    pkg.Function = FakeFunction
    pkg.FunctionType = _FunctionType
    pkg.CollectionSchema = FakeCollectionSchema

    index_mod = types.ModuleType("pymilvus.milvus_client.index")
    index_mod.IndexParams = FakeIndexParams
    milvus_client_mod = types.ModuleType("pymilvus.milvus_client")
    milvus_client_mod.index = index_mod

    monkeypatch.setitem(sys.modules, "pymilvus", pkg)
    monkeypatch.setitem(sys.modules, "pymilvus.milvus_client", milvus_client_mod)
    monkeypatch.setitem(sys.modules, "pymilvus.milvus_client.index", index_mod)

    # Reset capture state so tests are isolated.
    FakeFieldSchema.instances = []
    FakeFunction.instances = []
    FakeCollectionSchema.last = None
    FakeIndexParams.last = None
