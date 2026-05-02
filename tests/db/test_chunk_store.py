"""Tests for the chunk-store selector and Mongo driver scaffolding (S18-MONGO-DRIVER-01)."""

from __future__ import annotations

import importlib

import pytest
from gecko_core.db import chunk_store as chunk_store_mod
from gecko_core.db import mongo as mongo_mod


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    """Reset lru_cached singletons between tests so env mutations are picked up."""
    chunk_store_mod.get_chunk_store.cache_clear()
    mongo_mod._client.cache_clear()
    yield
    chunk_store_mod.get_chunk_store.cache_clear()
    mongo_mod._client.cache_clear()


class TestChunkStoreSelector:
    def test_default_is_supabase(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Test name asserts the default — must NOT be polluted by developer .env
        # (pydantic-settings loads .env in addition to process env, so delenv
        # alone is insufficient when a dev `.env` pins GECKO_CHUNK_STORE=mongo).
        # Construct settings with _env_file=None to skip dotenv entirely.
        monkeypatch.delenv("GECKO_CHUNK_STORE", raising=False)
        assert chunk_store_mod.ChunkStoreSettings(_env_file=None).kind == "supabase"  # type: ignore[call-arg]

    def test_explicit_mongo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GECKO_CHUNK_STORE", "mongo")
        assert chunk_store_mod.get_chunk_store() == "mongo"

    def test_invalid_value_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GECKO_CHUNK_STORE", "redis")
        with pytest.raises(Exception):  # pydantic validation
            chunk_store_mod.ChunkStoreSettings()


class TestMongoDriver:
    def test_mongo_uri_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MONGODB_URI", raising=False)
        monkeypatch.delenv("MONGO_URI", raising=False)
        assert mongo_mod.mongo_uri() is None

    def test_mongo_uri_ssm_sentinel_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONGODB_URI", "__unset__")
        assert mongo_mod.mongo_uri() is None

    def test_chunk_store_not_configured_when_supabase(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GECKO_CHUNK_STORE", "supabase")
        monkeypatch.setenv("MONGODB_URI", "mongodb+srv://example")
        assert mongo_mod.is_chunk_store_configured() is False

    def test_chunk_store_not_configured_when_uri_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GECKO_CHUNK_STORE", "mongo")
        monkeypatch.delenv("MONGODB_URI", raising=False)
        monkeypatch.delenv("MONGO_URI", raising=False)
        assert mongo_mod.is_chunk_store_configured() is False

    def test_chunk_store_configured_when_both_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GECKO_CHUNK_STORE", "mongo")
        monkeypatch.setenv("MONGODB_URI", "mongodb+srv://example")
        assert mongo_mod.is_chunk_store_configured() is True

    def test_chunk_db_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MONGODB_CHUNK_DB", raising=False)
        assert mongo_mod.chunk_db_name() == "gecko_rag"

    def test_chunk_db_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONGODB_CHUNK_DB", "gecko_rag_test")
        assert mongo_mod.chunk_db_name() == "gecko_rag_test"

    def test_index_names_are_module_constants(self) -> None:
        assert mongo_mod.VECTOR_INDEX_NAME == "chunks_vector"
        assert mongo_mod.SEARCH_INDEX_NAME == "chunks_text"

    def test_collection_names_are_module_constants(self) -> None:
        assert mongo_mod.CHUNKS_COLLECTION == "chunks"
        assert mongo_mod.CACHE_COLLECTION == "chunk_embedding_cache"
        assert mongo_mod.AUDIT_COLLECTION == "chunks_write_audit"


class TestPublicSurface:
    def test_db_package_reexports_supabase_factory(self) -> None:
        """The db.py → db/ refactor must keep legacy import paths working."""
        mod = importlib.import_module("gecko_core.db")
        assert hasattr(mod, "create_supabase_client")
        assert hasattr(mod, "SupabaseSettings")
        assert hasattr(mod, "get_chunk_store")
        assert hasattr(mod, "ChunkStore")
