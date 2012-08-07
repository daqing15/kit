#!/usr/bin/env python

"""Global configuration module."""

# Celery worker configuration

from app.conf.celery import *

BROKER_URL = 'redis://localhost:6379/1'
CELERY_RESULT_BACKEND = 'redis://localhost:6379/0'