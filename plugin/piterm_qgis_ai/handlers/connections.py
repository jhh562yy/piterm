"""Database connections: list/add/remove pg & file connections, list tables,
load layers/queries, import layers and run SQL (read by default)."""

from __future__ import annotations

import os

from qgis.core import (QgsDataSourceUri, QgsProject, QgsProviderRegistry,
                       QgsSettings, QgsVectorLayer)

from .. import compat
from ..errors import bad_request, invalid, io_error, not_found
from ..registry import command
from .base import HandlerBase

_PROVIDER_GROUPS = {
    "postgres": ("PostgreSQL", "postgres"),
    "postgresql": ("PostgreSQL", "postgres"),
    "gpkg": ("GeoPackage", "ogr"),
    "geopackage": ("GeoPackage", "ogr"),
    "spatialite": ("SpatiaLite", "spatialite"),
    "mssql": ("MSSQL", "mssql"),
    "oracle": ("Oracle", "oracle"),
    "mysql": ("MySQL", "mysql"),
}

_SQL_WRITE_STARTS = ("insert", "update", "delete", "drop", "alter", "create",
                     "replace", "truncate", "grant", "revoke", "vacuum")


class ConnectionHandlers(HandlerBase):

    @command(name="db.connections_list", summary="Saved Browser connections "
             "(passwords never read)", risk="read", tags=("db",))
    def db_connections_list(self):
        settings = QgsSettings()
        out = []
        for label, (group, provider) in sorted(_PROVIDER_GROUPS.items()):
            if label != provider and label not in ("postgres", "gpkg", "spatialite"):
                continue
            settings.beginGroup("%s/connections" % group)
            names = settings.childGroups()
            settings.endGroup()
            for name in names:
                details = {}
                for key in ("host", "port", "database", "dbname", "username",
                            "user", "sslmode", "authcfg", "path"):
                    value = settings.value("%s/connections/%s/%s" % (group, name, key),
                                           None)
                    if value not in (None, ""):
                        details[key] = value
                out.append({"kind": label if label in ("postgres", "gpkg",
                                                       "spatialite") else provider,
                            "provider": provider, "name": name, "details": details})
        # de-duplicate postgres/postgresql aliases
        seen = set()
        unique = []
        for entry in out:
            key = (entry["provider"], entry["name"])
            if key in seen:
                continue
            seen.add(key)
            unique.append(entry)
        return {"count": len(unique), "connections": unique}

    @command(name="db.connection_add", summary="Save a connection (no password "
             "stored; use authcfg for credentials)", risk="write", tags=("db",))
    def db_connection_add(self, name, kind="postgres", host=None, port=None,
                          database=None, username=None, sslmode="prefer",
                          authcfg=None, path=None, save_username=True):
        group, provider = _group_for(kind)
        settings = QgsSettings()
        prefix = "%s/connections/%s" % (group, name)
        if group == "PostgreSQL":
            if not host or not database:
                raise bad_request("postgres connections need host and database")
            settings.setValue("%s/host" % prefix, str(host))
            settings.setValue("%s/port" % prefix, str(port or 5432))
            settings.setValue("%s/database" % prefix, str(database))
            if username:
                settings.setValue("%s/username" % prefix, str(username))
                settings.setValue("%s/saveUsername" % prefix, bool(save_username))
            if sslmode:
                settings.setValue("%s/sslmode" % prefix, str(sslmode))
            if authcfg:
                settings.setValue("%s/authcfg" % prefix, str(authcfg))
        else:
            if not path:
                raise bad_request("%s connections need path=" % kind)
            resolved = self.check_path(path, for_write=False)
            settings.setValue("%s/database" % prefix, resolved)
            settings.setValue("%s/path" % prefix, resolved)
        return {"saved": name, "kind": kind, "group": group}

    @command(name="db.connection_remove", summary="Remove a saved connection",
             risk="destructive", tags=("db",))
    def db_connection_remove(self, name, kind="postgres"):
        group, _provider = _group_for(kind)
        settings = QgsSettings()
        settings.remove("%s/connections/%s" % (group, name))
        return {"removed": name, "kind": kind}

    @command(name="db.tables", summary="Tables of a connection (schemas, geometry "
             "column, CRS when available)", risk="read", tags=("db",))
    def db_tables(self, connection, kind="postgres", schema=None):
        provider, uri = _open_connection(self, connection, kind, schema)
        try:
            tables = provider.tables(schema or "") if schema else provider.tables()
        except Exception as exc:  # noqa: BLE001
            raise io_error("could not list tables: %s" % exc)
        out = []
        for table in tables or []:
            entry = {"name": table.tableName(), "schema": table.schema()}
            for attr in ("geometryColumn", "primaryKeyColumns", "geometryType",
                         "crs", "comment"):
                fn = getattr(table, attr, None)
                if callable(fn):
                    try:
                        value = fn()
                        entry[attr] = str(value) if value is not None else None
                    except Exception:  # noqa: BLE001
                        pass
            out.append(entry)
        schemas = []
        try:
            schemas = [s for s in (provider.schemas() or [])]
        except Exception:  # noqa: BLE001
            pass
        return {"connection": connection, "kind": kind, "count": len(out),
                "schemas": schemas, "tables": out}

    @command(name="db.add_layer", summary="Load a table or SQL query as a layer",
             risk="write", tags=("db",))
    def db_add_layer(self, connection, kind="postgres", table=None, sql=None,
                     geometry_column=None, schema=None, name=None, key=None):
        if not table and not sql:
            raise bad_request("pass table= or sql=")
        provider_id, uri = _connection_uri(self, connection, kind, schema, table,
                                           geometry_column, key)
        if sql:
            uri = _with_sql(uri, sql)
        display = name or (table or "query").split(".")[-1]
        layer = QgsVectorLayer(uri if len(uri) > 200 or kind in ("gpkg", "spatialite")
                               else uri, display, provider_id)
        if not layer.isValid():
            raise invalid("could not load %s from %s" % (table or "query", connection),
                          detail={"uri": uri[:300]})
        self.project.addMapLayer(layer)
        return {"layer": self.layer_summary(layer, with_extent=False)}

    @command(name="db.import_layer", summary="Write a vector layer into a "
             "connection as a new table", risk="destructive", tags=("db",))
    def db_import_layer(self, layer, connection, table, kind="postgres",
                        schema=None, overwrite=False, geometry_column="geom"):
        target = self.resolve_vector({"layer": layer})
        provider, _uri = _open_connection(self, connection, kind, schema)
        if not overwrite and _table_exists(provider, table, schema):
            raise invalid("table %r already exists; pass overwrite=true to replace"
                          % table)
        options = _vector_layer_options(table, schema, overwrite, geometry_column)
        attempts = []
        if options is not None:
            attempts.append(lambda: provider.createVectorLayer(target, options))
        attempts.append(lambda: provider.createVectorLayer(
            target, {"tableName": table, "schema": schema or "",
                     "overwrite": bool(overwrite),
                     "geometryColumnName": geometry_column}))
        errors = []
        for attempt in attempts:
            try:
                attempt()
                return {"imported": target.id(), "table": table,
                        "connection": connection}
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc)[:200])
        raise io_error("import failed", detail={"errors": errors})

    @command(name="db.sql", summary="Run SQL on a connection (read-only unless "
             "allow_write=true and the layer is not protected)",
             risk="destructive", tags=("db",))
    def db_sql(self, connection, sql, kind="postgres", allow_write=False,
               limit=1000):
        text = str(sql).strip()
        if not text:
            raise bad_request("empty SQL")
        lowered = text.lstrip().lower()
        is_write = lowered.startswith(_SQL_WRITE_STARTS)
        if is_write and not allow_write:
            raise invalid("this statement looks like a write; pass allow_write=true "
                          "deliberately", detail={"statement": lowered[:40]})
        if kind in ("gpkg", "spatialite") and not is_write:
            rows = _sqlite_read(self, connection, text, int(limit))
            return {"rows": rows, "count": len(rows), "write": False}
        provider, _uri = _open_connection(self, connection, kind, None)
        try:
            if is_write:
                provider.executeSql(text)
                return {"write": True, "rows": [], "count": 0}
            result = provider.executeSql(text)
        except Exception as exc:  # noqa: BLE001
            raise io_error("SQL failed: %s" % exc)
        rows = []
        for row in result or []:
            if isinstance(row, dict):
                rows.append({str(k): _jsonify(v) for k, v in row.items()})
            else:
                rows.append({"value": _jsonify(row)})
            if len(rows) >= int(limit):
                break
        return {"rows": rows, "count": len(rows), "write": False}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _group_for(kind):
    entry = _PROVIDER_GROUPS.get(str(kind).lower())
    if entry is None:
        raise bad_request("unknown connection kind %r" % kind,
                          detail={"accepted": sorted(_PROVIDER_GROUPS)})
    return entry


def _settings_details(handler, connection, kind):
    group, provider = _group_for(kind)
    settings = QgsSettings()
    prefix = "%s/connections/%s" % (group, connection)
    out = {}
    for key in ("host", "port", "database", "dbname", "username", "user",
                "sslmode", "authcfg", "path"):
        value = settings.value("%s/%s" % (prefix, key), None)
        if value not in (None, ""):
            out[key] = value
    if not out:
        raise not_found("no saved %s connection named %r" % (kind, connection),
                        detail={"hint": "db.connections_list shows what exists"})
    if kind in ("gpkg", "spatialite"):
        path = out.get("database") or out.get("path")
        if not path:
            raise not_found("connection %r has no path" % connection)
        out["path"] = handler.check_path(path, for_write=False)
    return provider, out


def _connection_uri(handler, connection, kind, schema, table, geometry_column, key):
    provider, details = _settings_details(handler, connection, kind)
    if kind in ("gpkg", "spatialite"):
        path = details["path"]
        if table:
            uri = "%s|layername=%s" % (path, table)
        else:
            uri = path
        return provider if provider != "ogr" else "ogr", uri
    uri = QgsDataSourceUri()
    uri.setConnection(str(details.get("host", "localhost")),
                      str(details.get("port", "5432")),
                      str(details.get("database", details.get("dbname", ""))),
                      str(details.get("username", details.get("user", ""))),
                      "", str(details.get("sslmode", "prefer")))
    if details.get("authcfg"):
        uri.setAuthConfigId(str(details["authcfg"]))
    if table:
        if "." in table and schema is None:
            schema, table = table.split(".", 1)
        uri.setDataSource(schema or "public", table, geometry_column or "geom",
                          "", key or "")
    return "postgres", uri.uri(True) if hasattr(uri, "uri") else str(uri)


def _with_sql(uri, sql):
    parsed = QgsDataSourceUri(uri)
    parsed.setSql(str(sql))
    return parsed.uri(True) if hasattr(parsed, "uri") else str(parsed)


def _open_connection(handler, connection, kind, schema):
    provider_id, uri_text = _connection_uri(handler, connection, kind, schema,
                                            None, None, None)
    registry = QgsProviderRegistry.instance()
    try:
        metadata = registry.providerMetadata("postgres" if provider_id == "postgres"
                                             else ("ogr" if provider_id == "ogr"
                                                   else provider_id))
        if metadata is None:
            raise io_error("provider metadata not available for %s" % provider_id)
        connection_obj = metadata.createConnection(uri_text, {})
    except Exception as exc:  # noqa: BLE001
        raise io_error("could not open connection %r: %s" % (connection, exc))
    return connection_obj, uri_text


def _table_exists(connection_obj, table, schema):
    try:
        tables = connection_obj.tables(schema or "")
        return any(t.tableName() == table for t in tables)
    except Exception:  # noqa: BLE001
        return False


def _vector_layer_options(table, schema, overwrite, geometry_column):
    try:
        from qgis.core import QgsAbstractDatabaseProviderConnection as Conn
        options = Conn.VectorLayerOptions(str(table))
        if schema:
            options.setSchema(str(schema))
        options.setOverwriteExisting(bool(overwrite))
        try:
            options.setGeometryColumnName(str(geometry_column))
        except Exception:  # noqa: BLE001
            pass
        return options
    except Exception:  # noqa: BLE001
        return None


def _sqlite_read(handler, connection, sql, limit):
    import sqlite3
    _provider, details = _settings_details(handler, connection, "gpkg")
    path = details["path"]
    uri = "file:%s?mode=ro" % path.replace("\\", "/")
    rows = []
    try:
        connection_obj = sqlite3.connect(uri, uri=True)
    except Exception:
        connection_obj = sqlite3.connect(path)
    try:
        cursor = connection_obj.execute(sql)
        columns = [d[0] for d in (cursor.description or [])]
        for row in cursor:
            rows.append({columns[i]: _jsonify(row[i]) for i in range(len(columns))})
            if len(rows) >= limit:
                break
    except Exception as exc:  # noqa: BLE001
        raise io_error("SQL failed: %s" % exc)
    finally:
        try:
            connection_obj.close()
        except Exception:  # noqa: BLE001
            pass
    return rows


def _jsonify(value):
    from .base import value_to_json
    return value_to_json(value)
