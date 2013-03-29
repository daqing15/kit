#!/usr/bin/env python

"""ORM Extension

This extension provides a customized SQLAlchemy model base and query.

Setup is straightforward:

.. code:: python

  from flasker import current_project as pj
  from flasker.ext import ORM

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
please refer to the documentation for :class:`flasker.util._sqlalchemy.Model`.

Models can be queried in several ways:

.. code:: python

  # the two following queries are equivalent
  query = pj.session.query(Cat)
  query = Cat.q

Both queries above are instances of :class:`flasker.ext.orm.Query`, which are
customized ``sqlalchemy.orm.Query`` objects (cf. below for the list of
available methods). If relationships (and backrefs) are defined using the
``orm.relationship`` and ``orm.backref`` functions, appender queries will
also return custom queries:

.. code:: python

  house = House.q.first()
  relationship_query = house.cats   # instance of flasker.ext.orm.Query


"""

from functools import partial
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import (backref as _backref, class_mapper,
  relationship as _relationship)
from sqlalchemy.orm.exc import UnmappedClassError

from ..util.sqlalchemy import Model, Query


class ORM(object):

  """The main ORM object.

  :param project: the project against which the extension will be registered
  :type project: flasker.project.Project
  :param create_all: whether or not to automatically create tables for the
    models defined (``True`` by default). Tables will only be created for
    models which do not have one already.
  :type create_all: bool

  """

  def __init__(self, project, create_all=True):

    project.conf['SESSION']['QUERY_CLS'] = Query

    self.Model = declarative_base(cls=Model)
    self.Model.q = _QueryProperty(project)
    self.Model.t = _TableProperty(project)

    self.backref = partial(_backref, query_class=Query)
    self.relationship = partial(_relationship, query_class=Query)

    @project.run_after_module_imports
    def orm_after_imports(project):
      if create_all:
        self.Model.metadata.create_all(
          project.session.get_bind(),
          checkfirst=True
        )

    project.logger.debug('orm extension initialized')


class _QueryProperty(object):

  """To make queries accessible directly on model classes."""

  def __init__(self, project):
    self.project = project

  def __get__(self, obj, cls):
    try:
      mapper = class_mapper(cls)
      if mapper:
        return Query(mapper, session=self.project.session())
    except UnmappedClassError:
      return None


class _TableProperty(object):

  """Bound table for faster batch executes."""

  def __init__(self, project):
    self.project = project

  def __get__(self, obj, cls):
    try:
      mapper = class_mapper(cls)
      if mapper:
        table = mapper.mapped_table
        table.metadata.bind = self.project.session.connection()
        return table
    except UnmappedClassError:
      return None

