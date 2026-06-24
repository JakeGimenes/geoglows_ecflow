"""
Module for reading config files.

Modified by Aquaveo

Copyright 2024 ecmwf

Licensed under the Apache License, Version 2.0 (the "License")

http://www.apache.org/licenses/LICENSE-2.0
"""



import os
import sys
import importlib.util
from importlib.machinery import SourceFileLoader
import collections.abc as collections

import datetime
import copy
from .py2 import basestring

# -----------------------------------
# module exceptions
# -----------------------------------


class ConfigError(Exception):
    """ Base exception """
    pass

class ConfigNotFoundError(ConfigError):
    pass

class ConfigLoadingError(ConfigError):
    pass

class ConfigItemNotFoundError(ConfigError):
    pass

class ConfigItemNotValueError(ConfigError):
    pass

class ConfigItemNotSectionError(ConfigError):
    pass

class ConfigItemTypeError(ConfigError):
    pass


# ----------------------------------------
# config file loaders for various formats
# ----------------------------------------


def _load_source(name, path):
    """Load a Python source file as a module and return it.

    An explicit ``SourceFileLoader`` is used so the file is parsed as Python
    source regardless of its extension (config files use ``.cfg``).
    """
    loader = SourceFileLoader(name, path)
    spec = importlib.util.spec_from_file_location(name, path, loader=loader)
    module = importlib.util.module_from_spec(spec)
    # Register before executing so the file's own relative imports resolve;
    # drop it again if execution fails, leaving no half-built module behind.
    sys.modules[name] = module
    try:
        loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


class ConfigSource(object):
    """
    Abstract Base Class.
    Derived classess should have the following attributes:
    .name -- config name
    .origin -- string describing the config source, e.g. config file path
    .data -- data loaded from config (a nested dict tree)
    """
    pass



class ConfigFile(ConfigSource):

    """
    Abstract Base Class.
    """

    search_path = [os.getenv('PWD')]

    def __init__(self, name):
        """
        Find config file corresponding to the
        given config name and load the file
        """
        self.name = name
        self.origin = self._find(name)
        self.data = self._load(self.origin)

    def _find(self, name):
        for dir_ in self.search_path:
            path = os.path.join(dir_, name+self.extension)
            if os.path.isfile(path):
                return path
        msg = 'Cannot find "{}"'.format(name)
        raise ConfigNotFoundError(msg)

    def _load(self, path):
        raise NotImplementedError()



class PythonConfigFile(ConfigFile):
    """
    Python config file
    """
    extension = '.cfg'
    def _load(self, path):
        try:
            old_dont_write_bytecode = sys.dont_write_bytecode
            sys.dont_write_bytecode = True
            data = _load_source('_sdeploy_config_'+path, path).__dict__
            sys.dont_write_bytecode = old_dont_write_bytecode
        except IOError as e:
            msg = path + ": " + e.strerror
            raise ConfigLoadingError(msg)
        except SyntaxError as e:
            msg = path + ": " + str(e) + "\n" + e.text
            raise ConfigLoadingError(msg)
        except NameError as e:
            msg = path + ": " + str(e)
            raise ConfigLoadingError(msg)
        return data



class PythonConfigPath(PythonConfigFile):
    """
    Like PythonConfigFile but 'name' argument
    in the constructor is treated as explicit
    path to config file rather than config ID.
    """
    def _find(self, name):
        if os.path.isfile(name):
            return os.path.join(os.environ['PWD'],name)
        msg = 'Cannot find "{}"'.format(name)
        raise ConfigNotFoundError(msg)



class YAMLConfigFile(ConfigFile):
    """
    YAML config file (not tested much..)
    """
    extension = '.yaml'
    def _load(self, path):
        try:
            f = open(path)
        except IOError as e:
            msg = path + ": " + e.strerror
            raise ConfigLoadingError(msg)
        config_data = yaml.load(f)
        return data


# ---------------------------------------------------------------------
# convenience functions for converting configuration strings to objects
# ---------------------------------------------------------------------

# Defined for convenience: these callables can be supplied as the optional
# 'type' argument when calling config.get(). These callables decode
# string item into something else or throw TypeError/ValueError
# when conversion is impossible. If type argument is not specified,
# the config.get() returns string.


def boolean(s):
    if s in ['TRUE', 'True', 'true', 'YES', 'Yes', 'yes', 'T', 'Y', 't', 'y', '1', True]:
        return True
    elif s in ['FALSE', 'False', 'false', 'NO', 'No', 'no', 'F', 'N', 'f', 'n', '0', False]:
        return False
    else:
        raise ValueError


class List(object):
    def __init__(self, type=str):
        if not callable(type):
            raise ValueError('{} must be callable'.format(type_name))
        self.type = type
    def __call__(self, s):
        items = s.replace('\n', '').split(',')
        return [self.type(x) for x in items]


def word(s):
    if not s.strip():
        raise ValueError
    return s


def path(s):
    return os.path.expanduser(os.path.expandvars(word(s)))


def pathlist(s):
    """ s must be path or a list of comma separated paths """
    return [path(p).strip() for p in s.split(',')]


def ymd(s):
    datetime.datetime.strptime(s, "%Y%m%d")
    return s


def ymdh(s):
    datetime.datetime.strptime(s, "%Y%m%d%H")
    return s



# ------------------------------------------
# An interface to the config data structure.
# ------------------------------------------


class Config(object):

    def __init__(self, source):
        self._source = source

    def data(self, path, default='__no_default__'):
        """
        Return "raw data" (dict or value)
        """
        if path == '' or path is None:
            return self._data
        keys = path.split('.')
        value = self._data
        for key in keys:
            try:
                value = value[key]
            except (KeyError, TypeError):
                if default == '__no_default__':
                    msg = "item \"" + path + "\" not found in " + self.origin
                    raise ConfigItemNotFoundError(msg)
                else:
                    return default
        if not hasattr(value, '__iter__'):
            value = str(value)
        return value

    def item(self, path, default='__no_default__'):
        """
        Return config item (section or value)
        """
        data = self.data(path, default)
        if isinstance(data, dict):
            return Section(parent=self, data_subset=data)
        return data

    def get(self, path, default='__no_default__', type=str, choices=None):
        """
        Return value of the config item specified by the path
        """
        item = self.item(path, default)
        type_name = getattr(type, '__name__', repr(type))
        if not callable(type):
            raise ValueError('{} must be callable'.format(type_name))
        if isinstance(item, Section) and default=='__no_default__':
            msg = '{}: item "{}" is not a value'.format(self.origin, path)
            raise ConfigItemNotValueError(msg)
        if item == default and not isinstance(default, basestring):
            return default
        try:
            final_item = type(item)
        except (TypeError, ValueError):
            msg = '{}: "{}" has invalid value "{}" (expected {})'.format(
                    self.origin, path, item, type_name)
            raise ConfigItemTypeError(msg)
        if choices is not None:
            if not isinstance(choices, collections.Iterable):
                raise ValueError('choices must be iterable')
            if item not in choices and final_item not in choices:
                msg = '{}: item "{}": unexpected value "{}"'.format(
                      self.origin, path, item)
                raise ConfigItemTypeError(msg)
        return final_item

    def set(self, path, value):
        """
        Add/update config item specified by the path
        """
        keys = reversed(path.split('.'))
        for key in keys:
            data = {}
            data[key] = value
            value = data
        update(self._data, data)

    def section(self, path, default='__no_default__'):
        """
        Return Section object or throw an exception if an item is a value
        """
        item = self.item(path, default)
        if not isinstance(item, Section) and default=='__no_default__':
            msg = '{}: item "{}" is not a section'.format(self.origin, path)
            raise ConfigItemNotSectionError(msg)
        return item

    @property
    def _name(self):
        return self._source.name

    @property
    def _data(self):
        return self._source.data

    @property
    def origin(self):
        return self._source.origin



class Section(Config):

    def __init__(self, parent, data_subset):
        self._source = parent._source
        self._data_subset = data_subset

    @property
    def _data(self):
        return self._data_subset



# helper functions

def update(d, u):
    for k, v in u.items():
        if isinstance(v, collections.Mapping):
            d[k] = update(d.get(k, {}), v)
        else:
            d[k] = v
    return d