#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import inspect
from contextlib import closing
from datetime import datetime, timedelta
from unittest import mock
from unittest.mock import MagicMock

import pytest

from airflow.exceptions import AirflowProviderDeprecationWarning
from airflow.models.connection import Connection
from airflow.models.dag import DAG
from airflow.providers.common.sql.hooks.sql import DbApiHook
from airflow.providers.mysql.hooks.mysql import MySqlHook
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.utils import timezone

from tests_common.test_utils.compat import GenericTransfer
from tests_common.test_utils.operators.run_deferrable import execute_operator
from tests_common.test_utils.providers import get_provider_min_airflow_version

pytestmark = pytest.mark.db_test

DEFAULT_DATE = timezone.datetime(2015, 1, 1)
DEFAULT_DATE_ISO = DEFAULT_DATE.isoformat()
DEFAULT_DATE_DS = DEFAULT_DATE_ISO[:10]
TEST_DAG_ID = "unit_test_dag"
counter = 0


@pytest.mark.backend("mysql")
class TestMySql:
    def setup_method(self):
        args = {"owner": "airflow", "start_date": DEFAULT_DATE}
        dag = DAG(TEST_DAG_ID, schedule=None, default_args=args)
        self.dag = dag

    def teardown_method(self):
        from airflow.providers.mysql.hooks.mysql import MySqlHook

        drop_tables = {"test_mysql_to_mysql", "test_airflow"}
        with closing(MySqlHook().get_conn()) as conn:
            for table in drop_tables:
                # Previous version tried to run execute directly on dbapi call, which was accidentally working
                with closing(conn.cursor()) as cur:
                    cur.execute(f"DROP TABLE IF EXISTS {table}")

    @pytest.mark.parametrize(
        "client",
        [
            "mysqlclient",
            "mysql-connector-python",
        ],
    )
    def test_mysql_to_mysql(self, client):
        class MySqlContext:
            def __init__(self, client):
                self.client = client
                self.connection = MySqlHook.get_connection(MySqlHook.default_conn_name)
                self.init_client = self.connection.extra_dejson.get("client", "mysqlclient")

            def __enter__(self):
                self.connection.set_extra(f'{{"client": "{self.client}"}}')

            def __exit__(self, exc_type, exc_val, exc_tb):
                self.connection.set_extra(f'{{"client": "{self.init_client}"}}')

        with MySqlContext(client):
            sql = "SELECT * FROM connection;"
            op = GenericTransfer(
                task_id="test_m2m",
                preoperator=[
                    "DROP TABLE IF EXISTS test_mysql_to_mysql",
                    "CREATE TABLE IF NOT EXISTS test_mysql_to_mysql LIKE connection",
                ],
                source_conn_id="airflow_db",
                destination_conn_id="airflow_db",
                destination_table="test_mysql_to_mysql",
                sql=sql,
                dag=self.dag,
            )
            op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

    @mock.patch("airflow.providers.common.sql.hooks.sql.DbApiHook.insert_rows")
    def test_mysql_to_mysql_replace(self, mock_insert):
        sql = "SELECT * FROM connection LIMIT 10;"
        op = GenericTransfer(
            task_id="test_m2m",
            preoperator=[
                "DROP TABLE IF EXISTS test_mysql_to_mysql",
                "CREATE TABLE IF NOT EXISTS test_mysql_to_mysql LIKE connection",
            ],
            source_conn_id="airflow_db",
            destination_conn_id="airflow_db",
            destination_table="test_mysql_to_mysql",
            sql=sql,
            dag=self.dag,
            insert_args={"replace": True},
        )
        op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)
        assert mock_insert.called
        _, kwargs = mock_insert.call_args
        assert "replace" in kwargs


@pytest.mark.backend("postgres")
class TestPostgres:
    def teardown_method(self):
        tables_to_drop = ["test_postgres_to_postgres", "test_airflow"]
        with PostgresHook().get_conn() as conn:
            with conn.cursor() as cur:
                for table in tables_to_drop:
                    cur.execute(f"DROP TABLE IF EXISTS {table}")

    def test_postgres_to_postgres(self, dag_maker):
        sql = "SELECT * FROM INFORMATION_SCHEMA.TABLES LIMIT 100;"
        with dag_maker(default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True):
            op = GenericTransfer(
                task_id="test_p2p",
                preoperator=[
                    "DROP TABLE IF EXISTS test_postgres_to_postgres",
                    "CREATE TABLE IF NOT EXISTS test_postgres_to_postgres (LIKE INFORMATION_SCHEMA.TABLES)",
                ],
                source_conn_id="postgres_default",
                destination_conn_id="postgres_default",
                destination_table="test_postgres_to_postgres",
                sql=sql,
            )
        dag_maker.create_dagrun()
        op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

    @mock.patch("airflow.providers.common.sql.hooks.sql.DbApiHook.insert_rows")
    def test_postgres_to_postgres_replace(self, mock_insert, dag_maker):
        sql = "SELECT id, conn_id, conn_type FROM connection LIMIT 10;"
        with dag_maker(default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True):
            op = GenericTransfer(
                task_id="test_p2p",
                preoperator=[
                    "DROP TABLE IF EXISTS test_postgres_to_postgres",
                    "CREATE TABLE IF NOT EXISTS test_postgres_to_postgres (LIKE connection INCLUDING INDEXES)",
                ],
                source_conn_id="postgres_default",
                destination_conn_id="postgres_default",
                destination_table="test_postgres_to_postgres",
                sql=sql,
                insert_args={
                    "replace": True,
                    "target_fields": ("id", "conn_id", "conn_type"),
                    "replace_index": "id",
                },
            )
        dag_maker.create_dagrun()
        op.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)
        assert mock_insert.called
        _, kwargs = mock_insert.call_args
        assert "replace" in kwargs


class TestGenericTransfer:
    def test_templated_fields(self):
        dag = DAG(
            "test_dag",
            schedule=None,
            start_date=datetime(2024, 10, 10),
            render_template_as_native_obj=True,
        )
        operator = GenericTransfer(
            task_id="test_task",
            sql="{{ sql }}",
            destination_table="{{ destination_table }}",
            source_conn_id="{{ source_conn_id }}",
            destination_conn_id="{{ destination_conn_id }}",
            preoperator="{{ preoperator }}",
            insert_args="{{ insert_args }}",
            dag=dag,
        )
        operator.render_template_fields(
            {
                "sql": "my_sql",
                "destination_table": "my_destination_table",
                "source_conn_id": "my_source_conn_id",
                "destination_conn_id": "my_destination_conn_id",
                "preoperator": "my_preoperator",
                "insert_args": {"commit_every": 5000, "executemany": True, "replace": True},
            }
        )
        assert operator.sql == "my_sql"
        assert operator.destination_table == "my_destination_table"
        assert operator.source_conn_id == "my_source_conn_id"
        assert operator.destination_conn_id == "my_destination_conn_id"
        assert operator.preoperator == "my_preoperator"
        assert operator.insert_args == {"commit_every": 5000, "executemany": True, "replace": True}

    def test_paginated_read(self):
        """
        This unit test is based on the example described in the medium article:
        https://medium.com/apache-airflow/transfering-data-from-sap-hana-to-mssql-using-the-airflow-generictransfer-d29f147a9f1f
        """

        def create_get_records_side_effect():
            records = [
                [[1, 2], [11, 12], [3, 4], [13, 14]],
                [[3, 4], [13, 14]],
            ]

            def side_effect(sql: str):
                if records:
                    return records.pop(0)
                return []

            return side_effect

        get_records_side_effect = create_get_records_side_effect()

        def get_hook(conn_id: str, hook_params: dict | None = None):
            mocked_hook = MagicMock(conn_name_attr=conn_id, spec=DbApiHook)
            mocked_hook.get_records.side_effect = get_records_side_effect
            return mocked_hook

        def get_connection(conn_id: str):
            mocked_hook = get_hook(conn_id=conn_id)
            mocked_conn = MagicMock(conn_id=conn_id, spec=Connection)
            mocked_conn.get_hook.return_value = mocked_hook
            return mocked_conn

        with mock.patch("airflow.hooks.base.BaseHook.get_connection", side_effect=get_connection):
            with mock.patch("airflow.hooks.base.BaseHook.get_hook", side_effect=get_hook):
                operator = GenericTransfer(
                    task_id="transfer_table",
                    source_conn_id="my_source_conn_id",
                    destination_conn_id="my_destination_conn_id",
                    sql="SELECT * FROM HR.EMPLOYEES",
                    destination_table="NEW_HR.EMPLOYEES",
                    page_size=1000,  # Fetch data in chunks of 1000 rows for pagination
                    insert_args={
                        "commit_every": 1000,  # Number of rows inserted in each batch
                        "executemany": True,  # Enable batch inserts
                        "fast_executemany": True,  # Boost performance for MSSQL inserts
                        "replace": True,  # Used for upserts/merges if needed
                    },
                    execution_timeout=timedelta(hours=1),
                )

                results, events = execute_operator(operator)

                assert not results
                assert len(events) == 3
                assert events[0].payload["results"] == [[1, 2], [11, 12], [3, 4], [13, 14]]
                assert events[1].payload["results"] == [[3, 4], [13, 14]]
                assert not events[2].payload["results"]

    def test_when_provider_min_airflow_version_is_3_0_or_higher_remove_obsolete_method(self):
        """
        Once this test starts failing due to the fact that the minimum Airflow version is now 3.0.0 or higher
        for this provider, you should remove the obsolete get_hook method in the GenericTransfer and use the
        one from BaseHook and remove this test.  This test was added to make sure to not forget to remove the
        fallback code for backward compatibility with Airflow 2.8.x which isn't need anymore once this
        provider depends on Airflow 3.0.0 or higher.
        """
        min_airflow_version = get_provider_min_airflow_version("apache-airflow-providers-standard")

        # Check if the current Airflow version is 3.0.0 or higher
        if min_airflow_version[0] >= 3:
            method_source = inspect.getsource(GenericTransfer.get_hook)
            raise AirflowProviderDeprecationWarning(
                f"Remove obsolete get_hook method in GenericTransfer:\n\r\n\r\t\t\t{method_source}"
            )
