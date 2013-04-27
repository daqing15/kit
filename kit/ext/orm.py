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

from functools import partial
from sqlalchemy.ext.declarative import declarative_base, DeclarativeMeta
from sqlalchemy.orm import (backref as _backref, class_mapper,
  relationship as _relationship)
from sqlalchemy.orm.exc import UnmappedClassError

from ..util.sqlalchemy import Model as _Model, Query


class ORM(object):

  """The main ORM object.

  The session will be reconfigured to use ``query_class``.

  """

  def __init__(self, session, query_class=Query, persistent_cache=False):

    session.configure(query_cls=query_class)

    self.session = session
    self._registry = {}

    self.Model = declarative_base(cls=Model, class_registry=self._registry)
    self.Model.q = _QueryProperty(session)
    self.Model.t = _TableProperty(session)
    if not persistent_cache:
      self.Model._cache = {}

    self.backref = partial(_backref, query_class=query_class)
    self.relationship = partial(_relationship, query_class=query_class)

  def get_all_models(self):
    """All mapped models."""
    return {
      k: v
      for k, v in self._registry.items()
      if isinstance(v, DeclarativeMeta)
    }

  def create_all(self, checkfirst=True):
    """Create tables for all mapped models."""
    self.Model.metadata.create_all(
      self.session.get_bind(),
      checkfirst=checkfirst
    )


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


class Model(_Model):

  """Adding a few methods using the bound session."""

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
        if if_not_found == 'flush':
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
