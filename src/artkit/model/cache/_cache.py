# -----------------------------------------------------------------------------
# © 2024 Boston Consulting Group. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# -----------------------------------------------------------------------------

"""
Internal DB implementation for the cache.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = [
    "CacheDB",
]

#
# Constants
#

_ERR_INVALID_PARAMETER_TYPE = (
    "Model parameters must be strings, integers, floats, or booleans, but got "
    "parameter {key}={value!r}"
)


#
# Classes
#


class CacheDB:
    """
    A model cache implemented as an SQLite database.
    """

    TBL_MODEL_PARAMS = "ModelParams"
    TBL_MODEL_RESPONSES = "ModelResponses"
    TBL_MODEL_CACHE = "ModelCache"

    def __init__(self, database: str | Path) -> None:
        """
        Initializes the CacheDB with a connection to the SQLite database.

        :param database: path to the SQLite database file; or ``:memory:`` to create an
            in-memory database
        """
        # Ensure the directory for the database exists
        if database != ":memory:":
            # Only attempt to create directory if not using an in-memory database
            directory = os.path.dirname(database)
            if directory and not os.path.exists(directory):
                log.warning(
                    f"Cache directory does not exist. Creating new directory: {directory}"
                )
                os.makedirs(directory)

        self.conn = sqlite3.connect(
            database=database,
            # We allow multiple threads to access the database connection; this is
            # necessary since a new thread may need to be created to start a new
            # asyncio event loop. We take care to ensure that DB transactions are
            # serialized by using the 'with self.conn' context manager.
            check_same_thread=False,
        )
        self._create_tables_and_indexes()

    def _create_tables_and_indexes(self) -> None:
        """
        Creates the necessary tables and indexes in the SQLite database.
        """
        with self.conn:
            self.conn.execute(
                """\
CREATE TABLE IF NOT EXISTS ModelCache (
    id INTEGER PRIMARY KEY,
    model_id TEXT NOT NULL,
    ctime DATETIME DEFAULT CURRENT_TIMESTAMP,
    atime DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""
            )
            # We create a table for unique string values, to avoid redundancy
            # in the model parameters
            self.conn.execute(
                """\
CREATE TABLE IF NOT EXISTS UniqueStrings (
    id INTEGER PRIMARY KEY,
    value TEXT NOT NULL UNIQUE
);
"""
            )
            # Model parameters can bes stored as a reference to a unique string, or as a
            # integer, float, or boolean value
            self.conn.execute(
                """\
CREATE TABLE IF NOT EXISTS ModelParams (
    id INTEGER PRIMARY KEY,
    cache_id INTEGER,
    name TEXT,
    value_string_id INTEGER,
    value_int INTEGER,
    value_float REAL,
    FOREIGN KEY (cache_id) REFERENCES ModelCache(id)
);
"""
            )
            self.conn.execute(
                """\
CREATE TABLE IF NOT EXISTS ModelResponses (
    id INTEGER PRIMARY KEY,
    cache_id INTEGER,
    response TEXT NOT NULL,
    FOREIGN KEY (cache_id) REFERENCES ModelCache(id)
);
"""
            )
            self.conn.execute(
                """\
CREATE INDEX IF NOT EXISTS idx_prompt_model_id ON ModelCache(model_id);
"""
            )
            self.conn.execute(
                """\
CREATE INDEX IF NOT EXISTS idx_cache_id_name ON ModelParams(cache_id, name);
"""
            )

    def add_entry(
        self,
        *,
        model_id: str,
        responses: str | Iterable[str],
        **model_params: str | int | float | bool,
    ) -> None:
        """
        Add a new entry to the cache.

        :param model_id: the identifier of the model that generated the response
        :param responses: the response or responses generated by the model
        :param model_params: additional parameters used by the model
        :raises TypeError: if a model parameter is not a string, integer, float, or
            boolean
        """
        with self.conn:
            cursor = self.conn.execute(
                """\
INSERT INTO ModelCache (model_id)
VALUES (?);
""",
                (model_id,),
            )
            cache_id = cursor.lastrowid

            for response in [responses] if isinstance(responses, str) else responses:
                self.conn.execute(
                    """\
INSERT INTO ModelResponses (cache_id, response)
VALUES (?, ?);
""",
                    (cache_id, response),
                )

            if model_params:
                for key, value in model_params.items():
                    if isinstance(value, str):
                        cursor = self.conn.execute(
                            """\
INSERT OR IGNORE INTO UniqueStrings (value)
VALUES (?);
""",
                            (value,),
                        )
                        if cursor.rowcount == 0:
                            cursor = self.conn.execute(
                                """\
SELECT id FROM UniqueStrings WHERE value = ?;
""",
                                (value,),
                            )
                            value_string_id = cursor.fetchone()[0]
                        else:
                            value_string_id = cursor.lastrowid
                        self.conn.execute(
                            """\
INSERT INTO ModelParams (cache_id, name, value_string_id)
VALUES (?, ?, ?);
""",
                            (cache_id, key, value_string_id),
                        )
                    elif isinstance(value, (int, bool)):
                        self.conn.execute(
                            """\
INSERT INTO ModelParams (cache_id, name, value_int)
VALUES (?, ?, ?);
""",
                            (cache_id, key, value),
                        )
                    elif isinstance(value, float):
                        self.conn.execute(
                            """\
INSERT INTO ModelParams (cache_id, name, value_float)
VALUES (?, ?, ?);
""",
                            (cache_id, key, value),
                        )
                    else:
                        raise TypeError(
                            _ERR_INVALID_PARAMETER_TYPE.format(key=key, value=value)
                        )

    def get_entry(
        self,
        *,
        model_id: str,
        **model_params: str | int | float | bool,
    ) -> list[str] | None:
        """
        Retrieve an entry from the cache based on the prompt, model ID, and model
        parameters.

        :param model_id: the identifier of the model used
        :param model_params: additional used by the model
        :return: a list of responses generated by the model or ``None`` if no matching
            entry is found
        :raises TypeError: if a model parameter is not a string, integer, float, or
            boolean
        """
        base_query = """
    SELECT MC.id FROM ModelCache MC
    LEFT JOIN ModelParams MP ON MC.id = MP.cache_id
    WHERE MC.model_id = ?"""
        base_params = [model_id]

        param_query = ""
        param_params = []
        if model_params:
            subqueries = []
            for key, value in model_params.items():
                if isinstance(value, str):
                    subqueries.append(
                        "(MP.name = ? AND MP.value_string_id = "
                        "(SELECT id FROM UniqueStrings WHERE value = ?))"
                    )
                elif isinstance(value, (int, bool)):
                    subqueries.append("(MP.name = ? AND MP.value_int = ?)")
                elif isinstance(value, float):
                    subqueries.append("(MP.name = ? AND MP.value_float = ?)")
                else:
                    raise TypeError(
                        _ERR_INVALID_PARAMETER_TYPE.format(key=key, value=value)
                    )
                param_params.extend([key, value])
            param_query = f"""
    AND ({' OR '.join(subqueries)})"""

        final_query = f"""\
SELECT id
FROM ({base_query} {param_query}
    GROUP BY MC.id
    HAVING COUNT(DISTINCT MP.name) = ?
) AS FilteredCache
WHERE (SELECT COUNT(*) FROM ModelParams WHERE cache_id = FilteredCache.id) = ?
"""

        n_model_params = len(model_params) if model_params else 0
        final_params = [*base_params, *param_params, n_model_params, n_model_params]

        with self.conn:
            cursor = self.conn.execute(final_query, final_params)
            row = cursor.fetchone()

            if row:
                cache_id = row[0]

                response_cursor = self.conn.execute(
                    """\
SELECT response FROM ModelResponses WHERE cache_id = ?;
""",
                    (cache_id,),
                )
                responses = [
                    response_row[0] for response_row in response_cursor.fetchall()
                ]

                # update the last accessed time stamp
                self.conn.execute(
                    """\
UPDATE ModelCache
SET atime = CURRENT_TIMESTAMP
WHERE id = ?;
""",
                    (cache_id,),
                )

                return responses

        return None

    def count_entries(self) -> dict[str, int]:
        """
        Count the number of entries in the cache per model ID.

        :return: a dictionary with the number of entries for each model ID
        """
        with self.conn:
            cursor = self.conn.execute(
                """\
SELECT model_id, COUNT(*) FROM ModelCache GROUP BY model_id;
"""
            )
            return dict(cursor.fetchall())

    def get_earliest_creation_times(self) -> dict[str, datetime]:
        """
        Get the earliest creation time for each model ID in the cache.

        :return: a dictionary with the earliest creation time for each model ID
        """
        return self._get_times_per_model(field="ctime", func="MIN")

    def get_latest_creation_times(self) -> dict[str, datetime]:
        """
        Get the latest creation time for each model ID in the cache.

        :return: a dictionary with the latest creation time for each model ID
        """
        return self._get_times_per_model(field="ctime", func="MAX")

    def get_earliest_access_times(self) -> dict[str, datetime]:
        """
        Get the earliest access time for each model ID in the cache.

        :return: a dictionary with the earliest access time for each model ID
        """
        return self._get_times_per_model(field="atime", func="MIN")

    def get_latest_access_times(self) -> dict[str, datetime]:
        """
        Get the latest access time for each model ID in the cache.

        :return: a dictionary with the latest access time for each model ID
        """
        return self._get_times_per_model(field="atime", func="MAX")

    def clear(
        self,
        *,
        model_id: str | None = None,
        accessed_before: datetime | None = None,
        created_before: datetime | None = None,
    ) -> None:
        """
        Clears all entries from the cache, or only entries that match all specified
        conditions.

        :param model_id: delete only cache entries for this model ID (optional)
        :param accessed_before: delete only cache entries that were accessed before this
            iso_timestamp (optional)
        :param created_before: delete only cache entries that were created before this
            iso_timestamp (optional)
        """

        conn = self.conn

        def _as_UTC_string(dt: datetime) -> str:
            # convert to UTC and format as string
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        with conn:
            conditions = []
            params = []
            if model_id:
                conditions.append("model_id = ?")
                params.append(model_id)
            if accessed_before:
                conditions.append("atime < ?")
                params.append(_as_UTC_string(accessed_before))
            if created_before:
                conditions.append("ctime < ?")
                params.append(_as_UTC_string(created_before))
            if conditions:
                filter = " WHERE " + " AND ".join(conditions)
                filter_dependent = (
                    f" WHERE cache_id IN (SELECT id FROM ModelCache{filter})"
                )
            else:
                filter = filter_dependent = ""

            conn.execute("DELETE FROM ModelParams" + filter_dependent, params)
            conn.execute("DELETE FROM ModelResponses" + filter_dependent, params)
            conn.execute("DELETE FROM ModelCache" + filter, params)

            # Remove unused unique strings
            conn.execute(
                """\
DELETE FROM UniqueStrings
WHERE id NOT IN (SELECT DISTINCT value_string_id FROM ModelParams);
"""
            )

    def _get_times_per_model(self, *, field: str, func: str) -> dict[str, datetime]:
        with self.conn:
            cursor = self.conn.execute(
                f"""\
SELECT model_id, {func}({field}) FROM ModelCache GROUP BY model_id;
"""
            )
            return {
                model_id: _parse_iso_utc(timestamp) for model_id, timestamp in cursor
            }

    def __del__(self) -> None:
        """
        Closes the connection to the SQLite database.
        """
        self.conn.close()


#
# Auxiliary functions
#


def _parse_iso_utc(iso_timestamp: str) -> datetime:
    """
    Parse an ISO-formatted timestamp string and return a datetime object in UTC.

    If the timezone is not specified, the timestamp is assumed to be in UTC.

    If the timezone is specified, the timestamp will be converted to UTC.

    :param iso_timestamp: an ISO-formatted timestamp string
    :return: the corresponding datetime object in UTC
    """
    try:
        return datetime.fromisoformat(iso_timestamp + "Z")
    except ValueError:
        # Try to parse the iso_timestamp without the 'Z' suffix
        return datetime.fromisoformat(iso_timestamp).astimezone(timezone.utc)
