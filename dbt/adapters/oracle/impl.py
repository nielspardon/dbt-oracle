"""
Copyright (c) 2023, Oracle and/or its affiliates.
Copyright (c) 2020, Vitor Avancini

  Licensed under the Apache License, Version 2.0 (the "License");
  you may not use this file except in compliance with the License.
  You may obtain a copy of the License at

     https://www.apache.org/licenses/LICENSE-2.0

  Unless required by applicable law or agreed to in writing, software
  distributed under the License is distributed on an "AS IS" BASIS,
  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
  See the License for the specific language governing permissions and
  limitations under the License.
"""
import datetime
from typing import (
    Optional, List, Set
)
from itertools import chain
from typing import (
    Any,
    Callable,
    Dict)

import agate
import requests

import dbt.exceptions
from dbt.adapters.base.relation import BaseRelation, InformationSchema
from dbt.adapters.base.impl import GET_CATALOG_MACRO_NAME, ConstraintSupport
from dbt.adapters.sql import SQLAdapter
from dbt.adapters.base.meta import available
from dbt.adapters.oracle import OracleAdapterConnectionManager
from dbt.adapters.oracle.column import OracleColumn
from dbt.adapters.oracle.relation import OracleRelation
from dbt.contracts.graph.manifest import Manifest
from dbt.contracts.graph.nodes import ConstraintType
from dbt.events import AdapterLogger

from dbt.utils import filter_null_values

from dbt.adapters.oracle.keyword_catalog import KEYWORDS
from dbt.adapters.oracle.python_submissions import OracleADBSPythonJob
from dbt.adapters.oracle.connections import AdapterResponse

logger = AdapterLogger("oracle")

# Added 6 random hex letters (56c36b) to table_a and table_b to avoid ORA-32031.
# Some dbt test cases use relation names table_a and table_b
# Oracle error: ORA-32031: illegal reference of a query name in WITH clause
COLUMNS_EQUAL_SQL = '''
with diff_count as (
    SELECT
        1 as id,
        COUNT(*) as num_missing FROM (
            (SELECT {columns} FROM {relation_a} {except_op}
             SELECT {columns} FROM {relation_b})
             MINUS
            (SELECT {columns} FROM {relation_b} {except_op}
             SELECT {columns} FROM {relation_a})
        ) a
), table_a_56c36b as (
    SELECT COUNT(*) as num_rows FROM {relation_a}
), table_b_56c36b as (
    SELECT COUNT(*) as num_rows FROM {relation_b}
), row_count_diff as (
    select
        1 as id,
        table_a_56c36b.num_rows - table_b_56c36b.num_rows as difference
    from table_a_56c36b, table_b_56c36b
)
select
    row_count_diff.difference as row_count_difference,
    diff_count.num_missing as num_mismatched
from row_count_diff
join diff_count using (id)
'''.strip()

LIST_RELATIONS_MACRO_NAME = 'list_relations_without_caching'
GET_DATABASE_MACRO_NAME = 'get_database_name'


class OracleAdapter(SQLAdapter):
    ConnectionManager = OracleAdapterConnectionManager
    Relation = OracleRelation
    Column = OracleColumn

    CONSTRAINT_SUPPORT = {
        ConstraintType.check: ConstraintSupport.ENFORCED,
        ConstraintType.not_null: ConstraintSupport.ENFORCED,
        ConstraintType.unique: ConstraintSupport.ENFORCED,
        ConstraintType.primary_key: ConstraintSupport.ENFORCED,
        ConstraintType.foreign_key: ConstraintSupport.ENFORCED,
    }

    def debug_query(self) -> None:
        self.execute("select 1 as id from dual")

    @classmethod
    def date_function(cls):
        return 'CURRENT_DATE'

    @classmethod
    def convert_text_type(cls, agate_table, col_idx):
        column = agate_table.columns[col_idx]
        lens = (len(d.encode("utf-8")) for d in column.values_without_nulls())
        max_len = max(lens) if lens else 64
        length = max_len if max_len > 16 else 16
        return "varchar2({})".format(length)

    @classmethod
    def convert_date_type(cls, agate_table, col_idx):
        return "timestamp"

    @classmethod
    def convert_datetime_type(cls, agate_table, col_idx):
        return "timestamp"

    @classmethod
    def convert_boolean_type(cls, agate_table, col_idx):
        return "number(1)"

    @classmethod
    def convert_number_type(cls, agate_table, col_idx):
        decimals = agate_table.aggregate(agate.MaxPrecision(col_idx))
        return "number"

    @classmethod
    def convert_time_type(cls, agate_table, col_idx):
        return "timestamp"

    @available
    def verify_database(self, database):
        if database.startswith('"'):
            database = database.strip('"')
        expected = self.config.credentials.database
        if expected and database.lower() != expected.lower():
            raise dbt.exceptions.DbtRuntimeError(
                'Cross-db references not allowed in {} ({} vs {})'
                .format(self.type(), database, expected)
            )
        # return an empty string on success so macros can call this
        return ''

    def _make_match_kwargs(self, database, schema, identifier):
        quoting = self.config.quoting
        if identifier is not None and quoting["identifier"] is False:
            identifier = identifier.upper()

        if schema is not None and quoting["schema"] is False:
            schema = schema.upper()

        if database is not None and quoting["database"] is False:
            database = database.upper()

        return filter_null_values(
            {"identifier": identifier, "schema": schema, "database": database}
        )

    def get_rows_different_sql(
        self,
        relation_a: OracleRelation,
        relation_b: OracleRelation,
        column_names: Optional[List[str]] = None,
        except_operator: str = 'MINUS',
    ) -> str:
        """Generate SQL for a query that returns a single row with a two
        columns: the number of rows that are different between the two
        relations and the number of mismatched rows.
        """
        # This method only really exists for test reasons.
        names: List[str]
        if column_names is None:
            columns = self.get_columns_in_relation(relation_a)
            # names = sorted((self.quote(c.name) for c in columns)
            names = sorted((c.name for c in columns))
        else:
            # names = sorted((self.quote(n) for n in column_names))
            names = sorted((n for n in column_names))
        columns_csv = ', '.join(names)

        sql = COLUMNS_EQUAL_SQL.format(
            columns=columns_csv,
            relation_a=str(relation_a),
            relation_b=str(relation_b),
            except_op=except_operator,
        )

        return sql

    def timestamp_add_sql(
        self, add_to: str, number: int = 1, interval: str = 'hour'
    ) -> str:
        # for backwards compatibility, we're compelled to set some sort of
        # default. A lot of searching has lead me to believe that the
        # '+ interval' syntax used in postgres/redshift is relatively common
        # and might even be the SQL standard's intention.
        return f"{add_to} + interval '{number}' {interval}"

    def get_relation(self, database: str, schema: str, identifier: str) -> Optional[BaseRelation]:
        if database == 'None':
            database = self.config.credentials.database
        return super().get_relation(database, schema, identifier)

    def _get_one_catalog(
            self,
            information_schema: InformationSchema,
            schemas: Set[str],
            manifest: Manifest,
    ) -> agate.Table:

        kwargs = {"information_schema": information_schema, "schemas": schemas}
        table = self.execute_macro(
            GET_CATALOG_MACRO_NAME,
            kwargs=kwargs,
            # pass in the full manifest so we get any local project
            # overrides
            manifest=manifest,
        )
        # In case database is not defined, we can use the the configured database which we set as part of credentials
        for node in chain(manifest.nodes.values(), manifest.sources.values()):
            if not node.database or node.database == 'None':
                node.database = self.config.credentials.database

        results = self._catalog_filter_table(table, manifest)
        return results

    def list_relations_without_caching(
            self, schema_relation: BaseRelation,
    ) -> List[BaseRelation]:

        # Set database if not supplied
        if not self.config.credentials.database:
            self.config.credentials.database = self.execute_macro(GET_DATABASE_MACRO_NAME)

        kwargs = {'schema_relation': schema_relation}
        results = self.execute_macro(
            LIST_RELATIONS_MACRO_NAME,
            kwargs=kwargs
        )
        relations = []
        for _database, name, _schema, _type in results:
            try:
                _type = self.Relation.get_relation_type(_type)
            except ValueError:
                _type = self.Relation.External
            relations.append(self.Relation.create(
                database=_database,
                schema=_schema,
                identifier=name,
                quote_policy=self.config.quoting,
                type=_type
            ))
        return relations

    @staticmethod
    def is_valid_identifier(identifier) -> bool:
        """Returns True if an identifier is valid

        An identifier is considered valid if the following conditions are True

            1. First character is alphabetic
            2. Rest of the characters is either alphanumeric or any one of the literals '#', '$', '_'

        """
        # The first character should be alphabetic
        if not identifier[0].isalpha():
            return False
        # Rest of the characters is either alphanumeric or any one of the literals '#', '$', '_'
        idx = 1
        while idx < len(identifier):
            identifier_chr = identifier[idx]
            if not identifier_chr.isalnum() and identifier_chr not in ('#', '$', '_'):
                return False
            idx += 1
        return True

    @available
    def should_identifier_be_quoted(self,
                                    identifier,
                                    models_column_dict=None) -> bool:
        """Returns True if identifier should be quoted else False

        An identifier should be quoted in the following 3 cases:

            - 1. Identifier is an Oracle keyword

            - 2. Identifier is not valid according to the following rules
                - First character is alphabetic
                - Rest of the characters is either alphanumeric or any one of the literals '#', '$', '_'

            - 3. User has enabled quoting for the column in the model configuration

        """
        if identifier.upper() in KEYWORDS:
            return True
        elif not self.is_valid_identifier(identifier):
            return True
        elif models_column_dict and identifier in models_column_dict:
            return models_column_dict[identifier].get('quote', False)
        elif models_column_dict and self.quote(identifier) in models_column_dict:
            return models_column_dict[self.quote(identifier)].get('quote', False)
        return False

    @available
    def check_and_quote_identifier(self, identifier, models_column_dict=None) -> str:
        if self.should_identifier_be_quoted(identifier, models_column_dict):
            return self.quote(identifier)
        else:
            return identifier

    @available
    def quote_seed_column(
            self, column: str, quote_config: Optional[bool]
    ) -> str:
        quote_columns: bool = False
        if isinstance(quote_config, bool):
            quote_columns = quote_config
        elif self.should_identifier_be_quoted(column):
            quote_columns = True
        elif quote_config is None:
            pass
        else:
            raise dbt.exceptions.CompilationError(f'The seed configuration value of "quote_columns" '
                                                  f'has an invalid type {type(quote_config)}')

        if quote_columns:
            return self.quote(column)
        else:
            return column

    def valid_incremental_strategies(self):
        return ["append", "merge"]

    @available
    @classmethod
    def render_raw_columns_constraints(cls, raw_columns: Dict[str, Dict[str, Any]]) -> List:
        rendered_column_constraints = []

        for v in raw_columns.values():
            rendered_column_constraint = [f"{v['name']}"]
            for con in v.get("constraints", None):
                constraint = cls._parse_column_constraint(con)
                c = cls.process_parsed_constraint(constraint, cls.render_column_constraint)
                if c is not None:
                    rendered_column_constraint.append(c)
            rendered_column_constraints.append(" ".join(rendered_column_constraint))

        return rendered_column_constraints

    def get_oml_auth_token(self) -> str:
        if self.config.credentials.oml_auth_token_uri is None:
            raise dbt.exceptions.DbtRuntimeError("oml_auth_token_uri should be set to run dbt-py models")
        data = {
            "grant_type": "password",
            "username": self.config.credentials.user,
            "password": self.config.credentials.password
        }
        try:
            r = requests.post(url=self.config.credentials.oml_auth_token_uri,
                              json=data)
            r.raise_for_status()
        except requests.exceptions.RequestException:
            raise dbt.exceptions.DbtRuntimeError("Error getting OML OAuth2.0 token")
        else:
            return r.json()["accessToken"]

    def submit_python_job(self, parsed_model: dict, compiled_code: str):
        """Submit user defined Python function
        https://docs.oracle.com/en/database/oracle/machine-learning/oml4py/1/mlepe/op-py-scripts-v1-do-eval-scriptname-post.html


        """
        identifier = parsed_model["alias"]
        py_q_script_name = f"{identifier}_dbt_py_script"
        py_q_create_script = f"""
            BEGIN
              sys.pyqScriptCreate('{py_q_script_name}', '{compiled_code.strip()}', FALSE, TRUE);
            END;
        """
        response, _ = self.execute(sql=py_q_create_script)
        python_job = OracleADBSPythonJob(parsed_model=parsed_model,
                                         credential=self.config.credentials)
        python_job()
        py_q_drop_script = f"""
                 BEGIN
                   sys.pyqScriptDrop('{py_q_script_name}');
                 END;
             """

        response, _ = self.execute(sql=py_q_drop_script)
        logger.info(response)
        return response
