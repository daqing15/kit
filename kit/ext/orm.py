#!/usr/bin/env python

"""ORM Extension

This extension provides a customized SQLAlchemy model base and query.

Setup is straightforward:

.. code:: python

  from kit import current_project as pj
  from kit.ext import ORM

  orm = ORM(pj)

  Model = orm.Model                 # the customized base
  relationship = orm.relationship   # the customized relationship
  backref = orm.backref             # the associated backref

Models can now be created by subclassing ``orm.Model`` as follows:

.. code:: python

  from sqlalchemy import Column, ForeignKey, Integer, String

  class House(Model):

    id = Column(Integer, primary_key=True)
    address = Column(String(128))

  class Cat(Model):
      
    id = Column(Integer, primary_key=True)
    name = Column(String(64))
    house_id = Column(ForeignKey('houses.id'))

    house = relationship('House', backref=backref('cats', lazy='dynamic'))

Note that tablenames are automatically generated by default. For an
exhaustive list of all the properties and methods provided by ``orm.Model``
please refer to the documentation for :class:`kit.util._sqlalchemy.Model`.

Models can be queried in several ways:

.. code:: python

  # the two following queries are equivalent
  query = pj.session.query(Cat)
  query = Cat.q

Both queries above are instances of :class:`kit.ext.orm.Query`, which are
customized ``sqlalchemy.orm.Query`` objects (cf. below for the list of
available methods). If relationships (and backrefs) are defined using the
``orm.relationship`` and ``orm.backref`` functions, appender queries will
also return custom queries:

.. code:: python

  house = House.q.first()
  relationship_query = house.cats   # instance of kit.ext.orm.Query


"""

from flask import abort
from functools import partial
from random import randint
from sqlalchemy import Column, func
from sqlalchemy.ext.associationproxy import AssociationProxy
from sqlalchemy.ext.declarative import (declared_attr, declarative_base,
  DeclarativeMeta)
from sqlalchemy.orm import (backref as _backref, class_mapper,
  Query as _Query, relationship as _relationship)
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.orm.properties import ColumnProperty, RelationshipProperty
from sqlalchemy.orm.exc import UnmappedClassError

from ..util import (Cacheable, JSONEncodedDict, Loggable, uncamelcase,
  query_to_dataframe, query_to_models, query_to_records, to_json)

try:
  from pandas import DataFrame
except ImportError:
  pass


class Query(_Query):

  """Base query class.

  All queries and relationships/backrefs defined using this extension will
  return an instance of this class.

  """

  def get_or_404(self, model_id):
    """Like get but aborts with 404 if not found.

    :param model_id: the model's primary key
    :type model_id: varies
    :rtype: model or HTTPError

    This method is from Flask-SQLAlchemy.
    
    """
    instance = self.get(model_id)
    if instance is None:
      abort(404)
    return instance

  def first_or_404(self):
    """Like first but aborts with 404 if not found.
    
    :rtype: model or HTTPError

    This method is from Flask-SQLAlchemy.
    
    """
    instance = self.first()
    if instance is None:
      abort(404)
    return instance

  def fast_count(self):
    """Fast counting, bypassing subqueries.

    By default SQLAlchemy count queries use subqueries (which are very slow
    on MySQL). This method is useful when counting over large numbers of rows
    (10k and more), as the following benchmark shows (~250k rows):

    .. code:: python

      In [1]: %time Cat.q.count()
      CPU times: user 0.01 s, sys: 0.00 s, total: 0.01 s
      Wall time: 1.36 s
      Out[1]: 281992L

      In [2]: %time Cat.c.scalar()
      CPU times: user 0.00 s, sys: 0.00 s, total: 0.00 s
      Wall time: 0.06 s
      Out[2]: 281992L

    """
    models = query_to_models(self)
    if len(models) != 1:
      # initial query is over more than one model
      # not clear how to implement the count in that case
      raise ValueError('Fast count unavailable for this query.')
    count_query = self.__class__(func.count(), session=self.session)
    count_query = count_query.select_from(models[0])
    count_query._criterion = self._criterion
    return count_query.scalar()

  def random(self, n_instances=1, dialect=None):
    """Returns random model instances.

    :param n_instances: the number of instances to return
    :type n_instances: int
    :param dialect: the engine dialect (the implementation of random differs
      between MySQL and SQLite among others). By default will look up on the
      query for the dialect used. If no random function is available for the 
      chosen dialect, the fallback implementation uses total row count to 
      generate random offsets.
    :type dialect: str
    :rtype: model instances
    
    """
    if dialect is None:
      dialect = self.session.get_bind().dialect.name
    if dialect == 'mysql':
      instances = self.order_by(func.rand()).limit(n_instances).all()
    elif dialect in ['sqlite', 'postgresql']:
      instances = self.order_by(func.random()).limit(n_instances).all()
    else: # fallback implementation
      count = self.count()
      instances = [
        self.offset(randint(0, count - 1)).first()
        for _ in range(n_instances)
      ]
    if len(instances) == 1:
      return instances[0]
    return instances

  def to_dataframe(self, load_objects=False, **kwargs):
    """Loads a dataframe with the records from the query and returns it.

    :param load_objects: whether or not to load the underlying objects. If set
      to ``False``, the dataframe will be populated with the contents of
      ``to_json`` of the models, otherwise it will only contain the columns
      existing in the database (default behavior). If lazy is ``True``, this
      method also accepts the same keyword arguments as
      :func:`kit.util.query_to_dataframe`. For convenience, if no
      ``exclude`` kwarg is specified, it will default to ``['_cache']``.
    :type load_objects: bool
    :rtype: pandas.DataFrame

    Requires the ``pandas`` library to be installed.

    """
    if not load_objects:
      kwargs.setdefault('exclude', ['__cache__'])
      return query_to_dataframe(
        self,
        connection=self.session.connection(),
        **kwargs
      )
    else:
      return DataFrame([model.to_json() for model in self])

  def to_records(self, **kwargs):
    """Raw execute of the query into a generator.

    :rtype: generator

    This method accepts the same keyword arguments as 
    :func:`kit.util.query_to_records`.
    
    """
    return query_to_records(
      self,
      connection=self.session.connection(),
      **kwargs
    )


class Model(Cacheable, Loggable):

  """The custom model class.

  Along with the methods described below, the following conveniences are
  provided:

  * Automatic table naming (to the model's class name uncamelcased with an
    extra s appended for good measure). To disable this behavior, simply
    override the ``__tablename__`` argument (setting it to ``None`` for
    single table inheritance).

  * Default implementation of ``__repr__`` with model class and primary keys

  * Caching (inherited from :class:`kit.util.Cacheable`). The cache is not
    persistent by default.

  * Logging (inherited from :class:`kit.util.Loggable`)

  """

  @declared_attr
  def __tablename__(cls):
    """Automatically create the table name."""
    return '%ss' % uncamelcase(cls.__name__)

  @classmethod
  def __declare_last__(cls):
    """Creates the ``__json__`` attribute.
    
    Varnames that get JSONified. Doesn't emit any additional queries!

    TODO: use _get_columns and other methods to generate thist list.

    """
    cls.__json__ = list(
      varname
      for varname in dir(cls)
      if not varname.startswith('_')  # don't show private properties
      if (
        isinstance(getattr(cls, varname), property) 
      ) or (
        isinstance(getattr(cls, varname), InstrumentedAttribute) and
        isinstance(getattr(cls, varname).property, ColumnProperty)
      ) or (
        isinstance(getattr(cls, varname), InstrumentedAttribute) and
        isinstance(getattr(cls, varname).property, RelationshipProperty) and
        getattr(cls, varname).property.lazy in [False, 'joined', 'immediate']
      ) or (
        isinstance(getattr(cls, varname), AssociationProxy) and
        getattr(
          cls, getattr(cls, varname).target_collection
        ).property.lazy in [False, 'joined', 'immediate']
      )
    )

  @classmethod
  def _get_columns(cls, show_private=False):
    """Dictionary of columns."""
    return {
      c.key: c
      for c in class_mapper(cls).columns
      if show_private or not c.key.startswith('_')
    }

  @classmethod
  def _get_related_models(cls, show_private=False):
    """Dictionary of relationship key to related model class."""
    return {
      k: v.mapper.class_
      for k, v in cls._get_relationships(show_private).items()
    }

  @classmethod
  def _get_relationships(cls, show_private=False, lazy=None, uselist=None):
    """Dictionary of relationships."""
    return {
      rel.key: rel
      for rel in class_mapper(cls).relationships.values()
      if show_private or not rel.key.startswith('_')
      if lazy is None or rel.lazy in lazy
      if uselist is None or rel.uselist == uselist
    }

  @classmethod
  def _get_association_proxies(cls, show_private=False):
    """Dictionary of association proxies."""
    return {
      varname: getattr(cls, varname)
      for varname in dir(cls)
      if isinstance(getattr(cls, varname), AssociationProxy)
      if show_private or not varname.startswith('_')
    }

  def __repr__(self):
    primary_keys = ', '.join(
      '%s=%r' % (k, getattr(self, k))
      for k, v in self.get_primary_key().items()
    )
    return '<%s (%s)>' % (self.__class__.__name__, primary_keys)

  def get_primary_key(self, as_tuple=False):
    """Returns a dictionary of primary keys for the given model.

    :param as_tuple: if set to ``True``, this method will return a tuple with
      the model's primary key values. Otherwise a dictionary is returned.
    :type as_tuple: bool
    :rtype: dict, tuple

    """
    if as_tuple:
      return tuple(
        getattr(self, k.name)
        for k in class_mapper(self.__class__).primary_key
      )
    else:
      return dict(
        (k.name, getattr(self, k.name))
        for k in class_mapper(self.__class__).primary_key
      )

  def to_json(self, depth=1):
    """Serializes the model into a dictionary.

    :param depth:
    :type depth: int
    :rtype: dict

    The following attributes are included in the returned JSON:

    * all non private columns
    * all non private properties
    * all non private relationships which have their ``lazy`` attribute set to
      one of ``False, 'joined', 'immediate'``

    A consequence of this is that this method will never issue extra queries
    to populate the JSON. Furthermore, all the attribute names to be
    included are computed at class declaration so this method is very fast.

    .. note::

      To change which attributes are included in the dictionary, you can 
      override the ``__json__`` attribute.

    """
    if depth <= 0:
      return self.get_primary_key()
    instance_json = {}
    for varname in self.__json__:
      try:
        instance_json[varname] = to_json(getattr(self, varname), depth - 1)
      except ValueError as err:
        instance_json[varname] = err.message
    return instance_json

  @classmethod
  def retrieve(cls, from_key=False, flush_if_missing=False, **kwargs):
    """Given constructor arguments will return a match or create one.

    :param flush_if_missing: whether or not to create and flush the model if 
      created (this can be used to generate its ``id``).
    :type flush_if_missing: bool
    :param from_key: instead of issuing a filter on kwargs, this will issue
      a get query by id using this parameter. Note that in this case, any other
      keyword arguments will only be used if a new instance is created.
    :type from_key: bool
    :param kwargs: constructor arguments
    :rtype: varies

    If ``flush_if_missing`` is ``True``, this method returns a tuple ``(model,
    flag)`` where ``model`` is of the corresponding class and ``flag`` is
    ``True`` if the model was just created and ``False`` otherwise. If
    ``flush_if_missing`` is ``False``, this methods simply returns an instance
    if found and ``None`` otherwise.

    """
    if from_key:
      model_primary_key = tuple(
        kwargs[k.name]
        for k in class_mapper(cls).primary_key
      )
      instance = cls.q.get(model_primary_key)
    else:
      instance = cls.q.filter_by(**kwargs).first()
    if not flush_if_missing:
      return instance
    else:
      if instance:
        return instance, False
      else:
        instance = cls(**kwargs)
        instance.flush()
      return instance, True

  def delete(self):
    """Mark the model for deletion.

    It will be removed from the database on the next commit.

    """
    self.q.session.delete(self)

  def flush(self, merge=False):
    """Add the model to the session and flush.
    
    :param merge: if ``True``, will merge instead of add.
    :type merge: bool
    
    """
    session = self.q.session
    if merge:
      session.merge(self)
    else:
      session.add(self)
    session.flush([self])


class _QueryProperty(object):

  """To make queries accessible directly on model classes."""

  def __init__(self, session):
    self.session = session

  def __get__(self, obj, cls):
    try:
      mapper = class_mapper(cls)
      if mapper:
        return Query(mapper, session=self.session())
    except UnmappedClassError:
      return None


class _TableProperty(object):

  """Bound table for faster batch executes."""

  def __init__(self, session):
    self.session = session

  def __get__(self, obj, cls):
    try:
      mapper = class_mapper(cls)
      if mapper:
        table = mapper.mapped_table
        # We bind the metadata to a connection to allow use of `execute`
        # directly on the statement objects. This connection will be closed
        # when the session is removed.
        table.metadata.bind = self.session.connection()
        return table
    except UnmappedClassError:
      return None


class ORM(object):

  """The main ORM object.

  :param session: the session to attach the ORM instance to.
  :type session: `sqlalchemy.orm.scoped.scoped_session`
  :param model_class: the base model class used by all models in this
    extension.
  :param model_class: `kit.ext.orm.Model`
  :param query_class: the base query class used by all queries made on the
    session. The session will be reconfigured in place.
  :param query_class: `sqlalchemy.orm.Query`
  :param persistent_cache: whether or not to store each model's cache in the
    database. If so, a text column storing the JSON encoded dictionary will
    be created.
  :type persistent_cache: bool

  """

  #: The declarative base generated from the `model_class`. All models in this
  #: ORM extension should inherit from this class.
  Model = None

  #: The relationship factory function using `query_class`. If you use
  #: `sqlalchemy.relationship` instead, your dynamic queries will not subclass
  #: `query_class`.
  relationship = None

  #: The backref factory function using `query_class`. If you use
  #: `sqlalchemy.backref` instead, your dynamic queries will not subclass
  #: `query_class`.
  backref = None

  def __init__(self, session, model_class=Model, query_class=Query,
               persistent_cache=False):

    session.configure(query_cls=query_class)

    self.session = session
    self._registry = {}

    self.Model = declarative_base(cls=Model, class_registry=self._registry)
    self.Model.q = _QueryProperty(session)
    self.Model.t = _TableProperty(session)

    if persistent_cache:
      def __cache__(cls):
        return Column(JSONEncodedDict)
      self.Model.__cache__ = declared_attr(__cache__)

    self.backref = partial(_backref, query_class=query_class)
    self.relationship = partial(_relationship, query_class=query_class)

  def get_all_models(self):
    """All mapped models."""
    return {
      k: v.__mapper__.class_
      for k, v in self._registry.items()
      if isinstance(v, DeclarativeMeta)
    }

  def create_all(self, checkfirst=True):
    """Create tables for all mapped models.

    :param checkfirst: whether or not to check if tables already exist before
      creating them.
    :type checkfirst: bool
    
    """
    self.Model.metadata.create_all(
      self.session.get_bind(),
      checkfirst=checkfirst
    )
