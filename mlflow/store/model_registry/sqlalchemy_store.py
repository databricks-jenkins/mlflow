import time

import logging
import sqlalchemy

from mlflow.entities.model_registry.model_version_stages import get_canonical_stage, \
    DEFAULT_STAGES_FOR_GET_LATEST_VERSIONS
from mlflow.exceptions import MlflowException
from mlflow.protos.databricks_pb2 import INVALID_PARAMETER_VALUE, RESOURCE_ALREADY_EXISTS, \
    INVALID_STATE, RESOURCE_DOES_NOT_EXIST
import mlflow.store.db.utils
from mlflow.store.db.base_sql_model import Base
from mlflow.store.entities.paged_list import PagedList
from mlflow.store.model_registry.abstract_store import AbstractStore
from mlflow.store.model_registry.dbmodels.models import SqlRegisteredModel, SqlModelVersion
from mlflow.utils.search_utils import SearchUtils
from mlflow.utils.uri import extract_db_type_from_uri

_logger = logging.getLogger(__name__)

# For each database table, fetch its columns and define an appropriate attribute for each column
# on the table's associated object representation (Mapper). This is necessary to ensure that
# columns defined via backreference are available as Mapper instance attributes (e.g.,
# ``SqlRegisteredModel.model_versions``). For more information, see
# https://docs.sqlalchemy.org/en/latest/orm/mapping_api.html#sqlalchemy.orm.configure_mappers
# and https://docs.sqlalchemy.org/en/latest/orm/mapping_api.html#sqlalchemy.orm.mapper.Mapper
sqlalchemy.orm.configure_mappers()


class SqlAlchemyStore(AbstractStore):
    """
    Note:: Experimental: This entity may change or be removed in a future release without warning.
    SQLAlchemy compliant backend store for tracking meta data for MLflow entities. MLflow
    supports the database dialects ``mysql``, ``mssql``, ``sqlite``, and ``postgresql``.
    As specified in the
    `SQLAlchemy docs <https://docs.sqlalchemy.org/en/latest/core/engines.html#database-urls>`_ ,
    the database URI is expected in the format
    ``<dialect>+<driver>://<username>:<password>@<host>:<port>/<database>``. If you do not
    specify a driver, SQLAlchemy uses a dialect's default driver.

    This store interacts with SQL store using SQLAlchemy abstractions defined for MLflow entities.
    :py:class:`mlflow.store.model_registry.models.RegisteredModel` and
    :py:class:`mlflow.store.model_registry.models.ModelVersion`
    """

    CREATE_MODEL_VERSION_RETRIES = 3

    def __init__(self, db_uri):
        """
        Create a database backed store.

        :param db_uri: The SQLAlchemy database URI string to connect to the database. See
                       the `SQLAlchemy docs
                       <https://docs.sqlalchemy.org/en/latest/core/engines.html#database-urls>`_
                       for format specifications. Mlflow supports the dialects ``mysql``,
                       ``mssql``, ``sqlite``, and ``postgresql``.
        :param default_artifact_root: Path/URI to location suitable for large data (such as a blob
                                      store object, DBFS path, or shared NFS file system).
        """
        super(SqlAlchemyStore, self).__init__()
        self.db_uri = db_uri
        self.db_type = extract_db_type_from_uri(db_uri)
        self.engine = sqlalchemy.create_engine(db_uri, pool_pre_ping=True)
        Base.metadata.create_all(self.engine)
        # Verify that all model registry tables exist.
        SqlAlchemyStore._verify_registry_tables_exist(self.engine)
        Base.metadata.bind = self.engine
        SessionMaker = sqlalchemy.orm.sessionmaker(bind=self.engine)
        self.ManagedSessionMaker = mlflow.store.db.utils._get_managed_session_maker(SessionMaker)
        # TODO: verify schema here once we add logic to initialize the registry tables if they
        # don't exist (schema verification will fail in tests otherwise)
        # mlflow.store.db.utils._verify_schema(self.engine)

    @staticmethod
    def _verify_registry_tables_exist(engine):
        # Verify that all tables have been created.
        inspected_tables = set(sqlalchemy.inspect(engine).get_table_names())
        expected_tables = [
            SqlRegisteredModel.__tablename__,
            SqlModelVersion.__tablename__,
        ]
        if any([table not in inspected_tables for table in expected_tables]):
            # TODO: Replace the MlflowException with the following line once it's possible to run
            # the registry against a different DB than the tracking server:
            # mlflow.store.db.utils._initialize_tables(self.engine)
            raise MlflowException("Database migration in unexpected state. Run manual upgrade.")

    def _save_to_db(self, session, objs):
        """
        Store in db
        """
        if type(objs) is list:
            session.add_all(objs)
        else:
            # single object
            session.add(objs)

    def create_registered_model(self, name):
        """
        Create a new registered model in backend store.

        :param name: Name of the new model. This is expected to be unique in the backend store.

        :return: A single object of :py:class:`mlflow.entities.model_registry.RegisteredModel`
        created in the backend.
        """
        if name is None or name == "":
            raise MlflowException('Registered model name cannot be empty.', INVALID_PARAMETER_VALUE)

        with self.ManagedSessionMaker() as session:
            try:
                now = int(time.time() * 1000)
                registered_model = SqlRegisteredModel(name=name, creation_time=now,
                                                      last_updated_time=now)
                self._save_to_db(session, registered_model)
                session.flush()
                return registered_model.to_mlflow_entity()
            except sqlalchemy.exc.IntegrityError as e:
                raise MlflowException('Registered Model (name={}) already exists. '
                                      'Error: {}'.format(name, str(e)), RESOURCE_ALREADY_EXISTS)

    @classmethod
    def _get_registered_model(cls, session, name):
        rms = session.query(SqlRegisteredModel).filter(SqlRegisteredModel.name == name).all()

        if len(rms) == 0:
            raise MlflowException('Registered Model with name={} not found'.format(name),
                                  RESOURCE_DOES_NOT_EXIST)
        if len(rms) > 1:
            raise MlflowException('Expected only 1 registered model with name={}. '
                                  'Found {}.'.format(name, len(rms)), INVALID_STATE)
        return rms[0]

    def update_registered_model(self, registered_model, new_name=None, description=None):
        """
        Updates metadata for RegisteredModel entity. Either ``new_name`` or ``description`` should
        be non-None. Backend raises exception if a registered model with given name does not exist.

        :param registered_model: :py:class:`mlflow.entities.model_registry.RegisteredModel` object.

        :param new_name: (Optional) New proposed name for the registered model.
        :param description: (Optional) New description.

        :return: A single updated :py:class:`mlflow.entities.model_registry.RegisteredModel` object.
        """
        with self.ManagedSessionMaker() as session:
            sql_registered_model = self._get_registered_model(session, registered_model.name)
            try:
                if new_name is not None:
                    sql_registered_model.name = new_name
                    for sql_model_version in sql_registered_model.model_versions:
                        sql_model_version.name = new_name
                if description is not None:
                    sql_registered_model.description = description
                self._save_to_db(session,
                                 [sql_registered_model] + sql_registered_model.model_versions)
                session.flush()
                return sql_registered_model.to_mlflow_entity()
            except sqlalchemy.exc.IntegrityError as e:
                raise MlflowException('Registered Model (name={}) already exists. '
                                      'Error: {}'.format(new_name, str(e)), RESOURCE_ALREADY_EXISTS)

    def delete_registered_model(self, registered_model):
        """
        Delete registered model.
        Backend raises exception if a registered model with given name does not exist.

        :param registered_model: :py:class:`mlflow.entities.model_registry.RegisteredModel` object.

        :return: None
        """
        with self.ManagedSessionMaker() as session:
            sql_registered_model = self._get_registered_model(session, registered_model.name)
            session.delete(sql_registered_model)

    def list_registered_models(self):
        """
        List of all registered models.

        :return: List of :py:class:`mlflow.entities.model_registry.RegisteredModel` objects.
        """
        with self.ManagedSessionMaker() as session:
            return [sql_registered_model.to_mlflow_detailed_entity()
                    for sql_registered_model in session.query(SqlRegisteredModel).all()]

    def get_registered_model_details(self, registered_model):
        """
        :param registered_model: :py:class:`mlflow.entities.model_registry.RegisteredModel` object.

        :return: A single :py:class:`mlflow.entities.model_registry.RegisteredModelDetailed` object.
        """
        with self.ManagedSessionMaker() as session:
            return self._get_registered_model(session,
                                              registered_model.name).to_mlflow_detailed_entity()

    def get_latest_versions(self, registered_model, stages=None):
        """
        Latest version models for each requested stage. If no ``stages`` argument is provided,
        returns the latest version for each stage.

        :param registered_model: :py:class:`mlflow.entities.model_registry.RegisteredModel` object.
        :param stages: List of desired stages. If input list is None, return latest versions for
                       for 'Staging' and 'Production' stages.

        :return: List of `:py:class:`mlflow.entities.model_registry.ModelVersionDetailed` objects.
        """
        with self.ManagedSessionMaker() as session:
            sql_registered_model = self._get_registered_model(session, registered_model.name)
            # Convert to RegisteredModelDetailed entity first and then extract latest_versions
            latest_versions = sql_registered_model.to_mlflow_detailed_entity().latest_versions
            if stages is None or len(stages) == 0:
                expected_stages = set([get_canonical_stage(stage) for stage
                                       in DEFAULT_STAGES_FOR_GET_LATEST_VERSIONS])
            else:
                expected_stages = set([get_canonical_stage(stage) for stage in stages])
            return [mv for mv in latest_versions if mv.current_stage in expected_stages]

    # CRUD API for ModelVersion objects

    def create_model_version(self, name, source, run_id):
        """
        Create a new model version from given source and run ID.

        :param name: Name ID for containing registered model.
        :param source: Source path where the MLflow model is stored.
        :param run_id: Run ID from MLflow tracking server that generated the model

        :return: A single object of :py:class:`mlflow.entities.model_registry.ModelVersion`
        created in the backend.
        """
        def next_version(sql_registered_model):
            if sql_registered_model.model_versions:
                return max([mv.version for mv in sql_registered_model.model_versions]) + 1
            else:
                return 1
        with self.ManagedSessionMaker() as session:
            now = int(time.time() * 1000)
            for attempt in range(self.CREATE_MODEL_VERSION_RETRIES):
                try:
                    sql_registered_model = self._get_registered_model(session, name)
                    model_version = SqlModelVersion(name=name,
                                                    version=next_version(sql_registered_model),
                                                    creation_time=now, last_updated_time=now,
                                                    source=source, run_id=run_id)
                    session.add(model_version)
                    session.flush()
                    return model_version.to_mlflow_entity()
                except sqlalchemy.exc.IntegrityError:
                    more_retries = self.CREATE_MODEL_VERSION_RETRIES - attempt - 1
                    _logger.info('Model Version creation error (name=%s) Retrying %s more time%s.',
                                 name, str(more_retries), 's' if more_retries > 1 else '')
        raise MlflowException('Model Version creation error (name={}). Giving up after '
                              '{} attempts.'.format(name, self.CREATE_MODEL_VERSION_RETRIES))

    @classmethod
    def _get_sql_model_version(cls, session, model_version):
        name = model_version.get_name()
        version = model_version.version
        versions = session.query(SqlModelVersion).filter(SqlModelVersion.name == name,
                                                         SqlModelVersion.version == version).all()

        if len(versions) == 0:
            raise MlflowException('Model Version (name={}, version{}) '
                                  'not found'.format(name, version), RESOURCE_DOES_NOT_EXIST)
        if len(versions) > 1:
            raise MlflowException('Expected only 1 model version with (name={}, version{}). '
                                  'Found {}.'.format(name, version, len(versions)),
                                  INVALID_STATE)
        return versions[0]

    def update_model_version(self, model_version, stage=None, description=None):
        """
        Update metadata associated with a model version in backend.

        :param model_version: :py:class:`mlflow.entities.model_registry.ModelVersion` object.
        :param stage: New desired stage for this model version.
        :param description: New description.

        :return: None.
        """
        with self.ManagedSessionMaker() as session:
            sql_model_version = self._get_sql_model_version(session, model_version)
            if stage is not None:
                sql_model_version.current_stage = get_canonical_stage(stage)
            if description is not None:
                sql_model_version.description = description
            self._save_to_db(session, sql_model_version)

    def delete_model_version(self, model_version):
        """
        Delete model version in backend.

        :param model_version: :py:class:`mlflow.entities.model_registry.ModelVersion` object.

        :return: None
        """
        with self.ManagedSessionMaker() as session:
            sql_model_version = self._get_sql_model_version(session, model_version)
            session.delete(sql_model_version)

    def get_model_version_details(self, model_version):
        """
        :param model_version: :py:class:`mlflow.entities.model_registry.ModelVersion` object.

        :return: A single :py:class:`mlflow.entities.model_registry.ModelVersionDetailed` object.
        """
        with self.ManagedSessionMaker() as session:
            sql_model_version = self._get_sql_model_version(session, model_version)
            return sql_model_version.to_mlflow_detailed_entity()

    def get_model_version_download_uri(self, model_version):
        """
        Get the download location in Model Registry for this model version.
        NOTE: For first version of Model Registry, since the models are not copied over to another
              location, download URI points to input source path.

        :param model_version: :py:class:`mlflow.entities.model_registry.ModelVersion` object.

        :return: A single URI location that allows reads for downloading.
        """
        with self.ManagedSessionMaker() as session:
            sql_model_version = self._get_sql_model_version(session, model_version)
            return sql_model_version.source

    def search_model_versions(self, filter_string):
        """
        Search for model versions in backend that satisfy the filter criteria.

        :param filter_string: A filter string expression. Currently supports a single filter
                              condition either name of model like ``name = 'model_name'`` or
                              ``run_id = '...'``.

        :return: PagedList of :py:class:`mlflow.entities.model_registry.ModelVersionDetailed`
                 objects.
        """
        parsed_filter = SearchUtils.parse_filter_for_model_registry(filter_string)
        if len(parsed_filter) == 0:
            conditions = []
        elif len(parsed_filter) == 1:
            filter_dict = parsed_filter[0]
            if filter_dict["comparator"] != "=":
                raise MlflowException('Model Registry search filter only supports equality(=) '
                                      'comparator. Input filter string: %s' % filter_string,
                                      error_code=INVALID_PARAMETER_VALUE)
            if filter_dict["key"] == "name":
                conditions = [SqlModelVersion.name == filter_dict["value"]]
            elif filter_dict["key"] == "source_path":
                conditions = [SqlModelVersion.source == filter_dict["value"]]
            elif filter_dict["key"] == "run_id":
                conditions = [SqlModelVersion.run_id == filter_dict["value"]]
            else:
                raise MlflowException('Invalid filter string: %s' % filter_string,
                                      error_code=INVALID_PARAMETER_VALUE)
        else:
            raise MlflowException('Model Registry expects filter to be one of '
                                  '"name = \'<model_name>\'" or '
                                  '"source_path = \'<source_path>\'" or "run_id = \'<run_id>\'.'
                                  'Input filter string: %s. ' % filter_string,
                                  error_code=INVALID_PARAMETER_VALUE)

        with self.ManagedSessionMaker() as session:
            sql_model_version = session.query(SqlModelVersion).filter(*conditions).all()
            model_versions_detailed = [mv.to_mlflow_detailed_entity() for mv in sql_model_version]
            return PagedList(model_versions_detailed, None)
