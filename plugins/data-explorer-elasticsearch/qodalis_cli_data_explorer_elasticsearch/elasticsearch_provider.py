"""Elasticsearch-backed Data Explorer provider."""

from __future__ import annotations

import json
import time
from typing import Any

from elasticsearch import AsyncElasticsearch

from qodalis_cli_server_abstractions import (
    DataExplorerExecutionContext,
    DataExplorerProviderOptions,
    DataExplorerResult,
    DataExplorerSchemaColumn,
    DataExplorerSchemaResult,
    DataExplorerSchemaTable,
    IDataExplorerProvider,
)


class ElasticsearchDataExplorerProvider(IDataExplorerProvider):
    """Executes requests against an Elasticsearch cluster."""

    def __init__(self, node: str = "http://localhost:9200") -> None:
        self._node = node

    async def execute_async(
        self, context: DataExplorerExecutionContext
    ) -> DataExplorerResult:
        start = time.monotonic()
        es: AsyncElasticsearch | None = None
        try:
            es = AsyncElasticsearch(self._node)

            query = (context.query or "").strip()
            lines = query.splitlines()

            # Parse first line: "VERB /path" or just "/path"
            first_line = lines[0].strip() if lines else ""
            parts = first_line.split(None, 1)

            if len(parts) == 2 and parts[0].upper() in {
                "GET", "POST", "PUT", "DELETE", "HEAD", "PATCH",
            }:
                method = parts[0].upper()
                path = parts[1].strip()
            else:
                # No verb — treat entire first line as path, default to GET
                method = "GET"
                path = first_line

            # Remaining lines form the JSON body
            body_lines = lines[1:]
            body_text = "\n".join(body_lines).strip()
            body: Any = None
            if body_text:
                body = json.loads(body_text)

            # Append ?format=json for _cat endpoints that lack it
            if "/_cat/" in path or path.startswith("_cat/"):
                if "format=json" not in path:
                    sep = "&" if "?" in path else "?"
                    path = f"{path}{sep}format=json"

            response = await es.perform_request(
                method=method,
                path=path,
                body=body,
            )

            raw = response.body if hasattr(response, "body") else response

            # Flatten _search hits into rows
            if isinstance(raw, dict) and "hits" in raw and isinstance(raw["hits"], dict):
                hits = raw["hits"].get("hits", [])
                if hits:
                    # Collect all field keys from _source (or the hit itself)
                    all_keys: list[str] = []
                    for hit in hits:
                        src = hit.get("_source", hit)
                        for k in src.keys():
                            if k not in all_keys:
                                all_keys.append(k)
                    columns = all_keys
                    rows = [
                        [hit.get("_source", hit).get(col) for col in columns]
                        for hit in hits
                    ]
                    return DataExplorerResult(
                        success=True,
                        source=context.options.name,
                        language=context.options.language,
                        default_output_format=context.options.default_output_format,
                        execution_time=int((time.monotonic() - start) * 1000),
                        columns=columns,
                        rows=rows,
                        row_count=len(rows),
                        truncated=False,
                        error=None,
                    )

            # _cat or other list responses
            if isinstance(raw, list):
                if raw:
                    columns = list(raw[0].keys()) if isinstance(raw[0], dict) else ["value"]
                    rows = (
                        [[item.get(col) for col in columns] for item in raw]
                        if isinstance(raw[0], dict)
                        else [[item] for item in raw]
                    )
                else:
                    columns = []
                    rows = []
                return DataExplorerResult(
                    success=True,
                    source=context.options.name,
                    language=context.options.language,
                    default_output_format=context.options.default_output_format,
                    execution_time=int((time.monotonic() - start) * 1000),
                    columns=columns,
                    rows=rows,
                    row_count=len(rows),
                    truncated=False,
                    error=None,
                )

            # Generic dict response — serialize as single JSON column
            raw_json = json.dumps(raw, default=str)
            return DataExplorerResult(
                success=True,
                source=context.options.name,
                language=context.options.language,
                default_output_format=context.options.default_output_format,
                execution_time=int((time.monotonic() - start) * 1000),
                columns=["result"],
                rows=[[raw_json]],
                row_count=1,
                truncated=False,
                error=None,
            )

        except Exception as exc:
            return DataExplorerResult(
                success=False,
                source=context.options.name,
                language=context.options.language,
                default_output_format=context.options.default_output_format,
                execution_time=int((time.monotonic() - start) * 1000),
                columns=None,
                rows=[],
                row_count=0,
                truncated=False,
                error=str(exc),
            )
        finally:
            if es is not None:
                await es.close()

    async def get_schema_async(
        self, options: DataExplorerProviderOptions
    ) -> DataExplorerSchemaResult | None:
        es: AsyncElasticsearch | None = None
        try:
            es = AsyncElasticsearch(self._node)

            # Fetch index list
            indices_response = await es.perform_request(
                method="GET",
                path="/_cat/indices?format=json",
            )
            indices_raw = (
                indices_response.body
                if hasattr(indices_response, "body")
                else indices_response
            )

            tables: list[DataExplorerSchemaTable] = []

            if isinstance(indices_raw, list):
                for index_info in indices_raw:
                    index_name: str = index_info.get("index", "")
                    if not index_name or index_name.startswith("."):
                        # Skip system indices
                        continue

                    try:
                        mapping_response = await es.perform_request(
                            method="GET",
                            path=f"/{index_name}/_mapping",
                        )
                        mapping_raw = (
                            mapping_response.body
                            if hasattr(mapping_response, "body")
                            else mapping_response
                        )

                        columns: list[DataExplorerSchemaColumn] = []
                        if isinstance(mapping_raw, dict):
                            index_mapping = mapping_raw.get(index_name, {})
                            mappings = index_mapping.get("mappings", {})
                            properties = mappings.get("properties", {})
                            for field_name, field_def in properties.items():
                                columns.append(
                                    DataExplorerSchemaColumn(
                                        name=field_name,
                                        type=field_def.get("type", "object"),
                                        nullable=True,
                                        primary_key=False,
                                    )
                                )

                        tables.append(
                            DataExplorerSchemaTable(
                                name=index_name,
                                type="index",
                                columns=columns,
                            )
                        )
                    except Exception:
                        # Skip indices we cannot fetch mappings for
                        tables.append(
                            DataExplorerSchemaTable(
                                name=index_name,
                                type="index",
                                columns=[],
                            )
                        )

            return DataExplorerSchemaResult(source=options.name, tables=tables)
        finally:
            if es is not None:
                await es.close()
