#!/usr/bin/env python

"""Thin wrapper around some sklearn algorithms.

Allows for automatic parameter saving and recovery. Saving fitted algorithms
for later reuse (convenient for parallel computing).

Exposes two methods: train and test that accept pandas dataframes and series
as input.

Test results are persisted along with a few metrics.

Currently supported:

* ``sklearn.linear_model.logistic.LogisticRegression``

Example usage:

.. code:: python

  >>> clf = Classifier.from_engine(LogisticRegression, C=3)
  >>> fitted_clf = clf.train(X_train, y_train)
  >>> test = fitted_clf.test(X_test, y_test)
  >>> test.precision
  0.9576324

"""

from datetime import datetime
from time import time
from flasker.util import JSONEncodedDict
from os import makedirs
from os.path import exists, join
from pandas import concat, DataFrame, Series
from sklearn.externals import joblib
from sqlalchemy import (Boolean, Column, Float, ForeignKey, Integer, String,
  DateTime, Numeric, Text)
from sys import modules


class SKL(object):

  def __init__(self, project, orm, param_class=None, fit_class=None,
               test_class=None, table_prefix='skl_', folder_path='skl',
               autocommit=False):

    self.root_dir = join(project.root_dir, folder_path)
    if not exists(self.root_dir): makedirs(self.root_dir)

    Model = orm.Model
    backref = orm.backref
    relationship = orm.relationship


    class Param(param_class or Model):

      __tablename__ = '%sparams' % table_prefix

      _cache = None
      id = Column(Integer, primary_key=True)
      type = Column(String(64))
      description = Column(Text)
      added = Column(DateTime, default=datetime.now)
      penalty = Column(String(32))
      dual = Column(Boolean)
      C = Column(Numeric(precision=64, scale=30))
      fit_intercept = Column(Boolean)
      intercept_scaling = Column(Numeric(precision=64, scale=30))
      random_state = Column(Boolean)
      class_weight = Column(JSONEncodedDict)
      tol = Column(Numeric(precision=64, scale=30))
      n_estimators = Column(Integer)
      criterion = Column(String(32))
      max_features = Column(String(32))
      max_depth = Column(Integer)
      min_samples_split = Column(Integer)
      min_samples_leaf = Column(Integer)
      min_density = Column(Numeric(precision=64, scale=30))
      bootstrap = Column(Boolean)
      oob_score = Column(Boolean)
      n_jobs = Column(Integer)
      verbose = Column(Boolean)
      compute_importances = Column(Boolean)

      @classmethod
      def _serialize_params(cls, **kwargs):
        if 'max_features' in kwargs:
          kwargs['max_features'] = str(kwargs['max_features'])
        return kwargs

      @classmethod
      def _unserialize_params(cls, **kwargs):
        if 'class_weight' in kwargs and kwargs['class_weight']:
          kwargs['class_weight'] = {
            int(k): v for k, v in kwargs['class_weight'].items()
          }
        if 'max_features' in kwargs and kwargs['max_features']:
          try:
            kwargs['max_features'] = int(kwargs['max_features'])
          except ValueError:
            pass
        return kwargs

      def _get_unfitted_engine(self):
        module, cls = self.type.rsplit('.', 1)
        __import__(module)
        engine_factory = getattr(modules[module], cls)
        valid_kwargs = engine_factory._get_param_names()
        # dict keys are transformed to unicode by json, need to convert them back
        kwargs = self._unserialize_params(**self.to_json())
        return engine_factory(
          **{k: v for k, v in kwargs.items() if k in valid_kwargs}
        )


    class Fit(fit_class or Model):

      __tablename__ = '%sfits' % table_prefix
      folder_path = join(self.root_dir, 'fit')

      id = Column(Integer, primary_key=True)
      param_id = Column(ForeignKey('%s.id' % Param.__tablename__))
      description = Column(Text)
      added = Column(DateTime, default=datetime.now)
      duration = Column(Float)

      param = relationship('Param', backref=backref('fits', lazy='dynamic'))

      def get_coefs(self):
        return DataFrame.load(join(self._folderpath, 'coefs.pkl'))

      @property
      def _folderpath(self):
        path = join(self.folder_path, str(self.id))
        if not exists(path):
          makedirs(path)
        return path

      def _get_fitted_engine(self):
        return joblib.load(join(self._folderpath, 'engine.joblib.pkl'))


    class Test(test_class or Model):

      __tablename__ = '%stests' % table_prefix
      folder_path = join(self.root_dir, 'test')

      id = Column(Integer, primary_key=True)
      fit_id = Column(ForeignKey('%s.id' % Fit.__tablename__))
      description = Column(Text)
      added = Column(DateTime, default=datetime.now)
      duration = Column(Float)
      true_neg = Column(Integer)
      false_neg = Column(Integer)
      true_pos = Column(Integer)
      false_pos = Column(Integer)
      precision = Column(Float)
      recall = Column(Float)
      fpr = Column(Float)
      _results = None

      fit = relationship('Fit', backref=backref('tests', lazy='dynamic'))

      def get_results(self):
        """Returns a dataframe with the true and predicted labels.

        :rtype: pandas.DataFrame

        The dataframe has two columns: ``'truth'`` and ``'prediction'``.
        The dataframe is persisted in a ``.npy`` file and loaded the first time it
        is accessed.

        """
        return DataFrame.load(self._filepath)

      @property
      def _filepath(self):
        return join(self.folder_path, '%s.pkl' % self.id)

      def _set_results(self, results):
        # counts
        c = results.groupby('truth')['prediction'].value_counts()
        def _get_or_zero(series, key):
          try:
            return int(series.get_value(key))
          except KeyError:
            return 0.0
        self.true_pos = _get_or_zero(c, (1,1))
        self.true_neg = _get_or_zero(c, (0,0))
        self.false_pos = _get_or_zero(c, (0,1))
        self.false_neg = _get_or_zero(c, (1,0))
        # metrics
        try:
          self.precision = float(self.true_pos) / (self.true_pos + self.false_pos)
        except ZeroDivisionError:
          self.precision = 0.0
        try:
          self.recall = float(self.true_pos) / (self.true_pos + self.false_neg)
        except ZeroDivisionError:
          self.recall = 1.0
        try:
          self.fpr = float(self.false_pos) / (self.false_pos + self.true_neg)
        except ZeroDivisionError:
          self.fpr = 0.0
        # saving original series
        self._results = results
        self.flush()
        self._results.save(self._filepath)


    class Classifier(object):

      """The general classifier method.

      It shouldn't be instantiated directly, the 3 classmethods should be used
      instead.

      Parameters are not duplicated. If a classifier is initialized with the same
      values as those of a parameter found in the database, all new fits and tests
      will be attributed to the unique same parameter.
      
      """

      @classmethod
      def from_engine(cls, engine, **kwargs):
        """Wrap an sklearn algorithm.

        :param engine: sklearn algorithm or algorithm factory
        :type engine: varies
        :rtype: flasker.ext.skl.UnfittedClassifier

        """
        if callable(engine): # this is a factory
          engine = engine()
        engine.set_params(**kwargs)
        param, flag = Param.retrieve(
          type='%s.%s' % (
            engine.__module__,
            engine.__class__.__name__
          ),
          **Param._serialize_params(**engine.get_params())
        )
        if flag:
          param.flush()
        return UnfittedClassifier(param, engine)

      @classmethod
      def from_param_id(cls, param_id):
        """Load an unfitted classifier from its parameter id.

        :param param_id: the id of the parameter to use to load the engine
        :type param_id: int
        :rtype: flasker.ext.skl.UnfittedClassifier

        The parameter is persisted in the database and its kwargs are passed to 
        the classifier's constructor.

        """
        param = Param.q.get(param_id)
        return UnfittedClassifier(param)

      @classmethod
      def from_fit_id(cls, fit_id):
        """Load an unfitted classifier from a fitted id.

        :param param_id: the id of the parameter to use to load the engine
        :type param_id: int
        :rtype: flasker.ext.skl.FittedClassifier

        """
        fit = Fit.q.get(fit_id)
        return FittedClassifier(fit)


    class UnfittedClassifier(Classifier):

      def __init__(self, param, engine=None):
        self.param = param
        if engine:
          self.engine = engine
        else:
          self.engine = param._get_unfitted_engine()

      def train(self, Xdf, ys, description, test_in_sample=True):
        """Fit then test in sample.

        :param Xdf: features
        :type Xdf: pandas.DataFrame
        :param ys: labels
        :type ys: pandas.Series
        :param description: a description of the training set
        :type description: str
        :param test_in_sample: if ``True``, the classifier will be run on its
          training sample right after being fit
        :type test_in_sample: bool

        This method has to be called before running any tests if the classifier
        wasn't loaded from an existing fit.

        """
        now = time()
        fit = Fit(param=self.param, description=description)
        self.engine.fit(Xdf.values, ys.values)
        fit.duration = time() - now
        fit.flush()
        if hasattr(self.engine, 'coef_'):
          coefs = DataFrame(self.engine.coef_, columns=Xdf.keys())
          if self.engine.intercept_:
            coefs['intercept'] = Series(self.engine.intercept_)
          coefs.save(join(fit._folderpath, 'coefs.pkl'))
        joblib.dump(self.engine, join(fit._folderpath, 'engine.joblib.pkl'))
        fclf = FittedClassifier(fit, engine=self.engine)
        if test_in_sample:
          fclf.test(Xdf, ys, 'in sample')
        return fclf


    class FittedClassifier(Classifier):

      def __init__(self, fit, engine=None):
        self.param = fit.param
        self.fit = fit
        if engine:
          self.engine = engine
        else:
          self.engine = fit._get_fitted_engine()

      def test(self, Xdf, ys, description):
        """Test.

        :param Xdf:
        :type Xdf: pandas.DataFrame
        :param ys: optional truth
        :type ys: pandas.Series
        :param description: a description of the testing set
        :type description: str

        The results of the test are automatically stored afterwards in a new
        ``flasker.ext.skl.Test`` instance.

        """
        now = time()
        test = Test(fit=self.fit, description=description)
        prediction = self.engine.predict(Xdf.values)
        test.duration = time() - now
        test._set_results(DataFrame({'truth': ys, 'prediction': prediction}))
        return test
        

    self.Param = Param
    self.Fit = Fit
    self.Test = Test
    self.Classifier = Classifier

