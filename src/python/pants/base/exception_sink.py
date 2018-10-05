# coding=utf-8
# Copyright 2018 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import, division, print_function, unicode_literals

import datetime
import faulthandler
import logging
import os
import signal
import sys
import traceback
from builtins import object, str

from pants.base.exiter import Exiter
from pants.util.dirutil import safe_mkdir, safe_open
from pants.util.osutil import IntegerForPid


logger = logging.getLogger(__name__)


class ExceptionSink(object):
  """A mutable singleton object representing where exceptions should be logged to."""

  # NB: see the bottom of this file where we call reset_log_location() and other mutators in order
  # to properly setup global state.
  _log_dir = None
  # We need an exiter in order to know what to do after we log a fatal exception.
  _exiter = None
  # Where to log stacktraces to in a SIGUSR2 handler.
  _interactive_output_stream = None

  # These persistent open file descriptors are kept so faulthandler can figure out signal safety.
  _pid_specific_error_fileobj = None
  _shared_error_fileobj = None

  # Integer code to exit with on an unhandled exception.
  UNHANDLED_EXCEPTION_EXIT_CODE = 1

  def __new__(cls, *args, **kwargs):
    raise TypeError('Instances of {} are not allowed to be constructed!'
                    .format(cls.__name__))

  class ExceptionSinkError(Exception): pass

  # All reset_* methods are ~idempotent!
  @classmethod
  def reset_log_location(cls, new_log_location):
    """Re-acquire file handles to error logs based in the new location.

    Class state:
    - Overwrites `cls._log_dir`, `cls._pid_specific_error_fileobj`, and
      `cls._shared_error_fileobj`.
    OS state:
    - May create a new directory.
    - Overwrites signal handlers for many fatal signals.

    :raises: :class:`ExceptionSink.ExceptionSinkError` if the directory does not exist or is not
             writable.
    """
    # We could no-op here if the log locations are the same, but there's no reason not to have the
    # additional safety of re-acquiring file descriptors each time (and erroring out early if the
    # location is no longer writable).

    # Create the directory if possible, or raise if not writable.
    cls._check_or_create_new_destination(new_log_location)

    pid_specific_error_stream, shared_error_stream = cls._recapture_fatal_error_log_streams(
      new_log_location)

    # NB: mutate process-global state!
    if faulthandler.is_enabled():
      logger.debug('re-enabling faulthandler')
      # Drops a ref count for the previous error stream:
      # https://github.com/vstinner/faulthandler/blob/master/faulthandler.c
      faulthandler.disable()
    # Send a stacktrace to this file if interrupted by a fatal error.
    faulthandler.enable(file=pid_specific_error_stream, all_threads=True)

    # NB: mutate the class variables!
    cls._log_dir = new_log_location
    cls._pid_specific_error_fileobj = pid_specific_error_stream
    cls._shared_error_fileobj = shared_error_stream

  @classmethod
  def reset_exiter(cls, exiter):
    """
    Class state:
    - Overwrites `cls._exiter`.
    Python state:
    - Overwrites sys.excepthook.
    """
    assert(isinstance(exiter, Exiter))
    # NB: mutate the class variables! This is done before mutating the exception hook, because the
    # uncaught exception handler uses cls._exiter to exit.
    cls._exiter = exiter
    # NB: mutate process-global state!
    sys.excepthook = cls._log_unhandled_exception_and_exit

  @classmethod
  def reset_interactive_output_stream(cls, interactive_output_stream):
    """
    Class state:
    - Overwrites `cls._interactive_output_stream`.
    OS state:
    - Overwrites the SIGUSR2 handler.

    This is where the the error message on exit will be printed to as well.
    """
    # NB: mutate process-global state!
    if faulthandler.unregister(signal.SIGUSR2):
      logger.debug('re-registering a SIGUSR2 handler')
    # This permits a non-fatal `kill -31 <pants pid>` for stacktrace retrieval.
    faulthandler.register(signal.SIGUSR2, interactive_output_stream,
                          all_threads=True, chain=False)

    # NB: mutate the class variables!
    # We don't *necessarily* need to keep a reference to this, but we do here for clarity.
    cls._interactive_output_stream = interactive_output_stream

  @classmethod
  def exceptions_log_path(cls, for_pid=None, in_dir=None):
    """Get the path to either the shared or pid-specific fatal errors log file."""
    if for_pid is None:
      intermediate_filename_component = ''
    else:
      assert(isinstance(for_pid, IntegerForPid))
      intermediate_filename_component = '.{}'.format(for_pid)
    in_dir = in_dir or cls._log_dir
    return os.path.join(
      in_dir,
      'logs',
      'exceptions{}.log'.format(intermediate_filename_component))

  @classmethod
  def log_exception(cls, msg):
    """Try to log an error message to this process's error log and the shared error log.

    NB: Doesn't raise (logs an error instead).
    """
    pid = os.getpid()
    fatal_error_log_entry = cls._format_exception_message(msg, pid)

    # We care more about this log than the shared log, so write to it first.
    try:
      cls._try_write_with_flush(cls._pid_specific_error_fileobj, fatal_error_log_entry)
    except Exception as e:
      logger.error(
        "Error logging the message '{}' to the pid-specific file handle for {} at pid {}:\n{}"
        .format(msg, cls._log_dir, pid, e))

    # Write to the shared log.
    try:
      # TODO: we should probably guard this against concurrent modification by other pants
      # subprocesses somehow.
      cls._try_write_with_flush(cls._shared_error_fileobj, fatal_error_log_entry)
    except Exception as e:
      logger.error(
        "Error logging the message '{}' to the shared file handle for {} at pid {}:\n{}"
        .format(msg, cls._log_dir, pid, e))

  @classmethod
  def _try_write_with_flush(cls, fileobj, payload):
    """This method is here so that it can be patched to simulate write errors.

    This is because mock can't patch primitive objects like file objects.
    """
    fileobj.write(payload)
    fileobj.flush()

  @classmethod
  def _check_or_create_new_destination(cls, destination):
    try:
      safe_mkdir(destination)
    except Exception as e:
      raise cls.ExceptionSinkError(
        "The provided exception sink path at '{}' is not writable or could not be created: {}."
        .format(destination, str(e)),
        e)

  @classmethod
  def _recapture_fatal_error_log_streams(cls, new_log_location):
    # NB: We do not close old file descriptors under the assumption their lifetimes are managed
    # elsewhere.
    # We recapture both log streams each time.
    pid = os.getpid()
    pid_specific_log_path = cls.exceptions_log_path(for_pid=pid, in_dir=new_log_location)
    shared_log_path = cls.exceptions_log_path(in_dir=new_log_location)
    assert(pid_specific_log_path != shared_log_path)
    try:
      # Truncate the pid-specific error log file.
      pid_specific_error_stream = safe_open(pid_specific_log_path, mode='w')
      # Append to the shared error file.
      shared_error_stream = safe_open(shared_log_path, mode='a')
    except Exception as e:
      raise cls.ExceptionSinkError(
        "Error opening fatal error log streams for log location '{}': {}"
        .format(new_log_location, str(e)))

    return (pid_specific_error_stream, shared_error_stream)

  @classmethod
  def _iso_timestamp_for_now(cls):
    return datetime.datetime.now().isoformat()

  # NB: This includes a trailing newline, but no leading newline.
  _EXCEPTION_LOG_FORMAT = """\
timestamp: {timestamp}
args: {args}
pid: {pid}
{message}
"""

  @classmethod
  def _format_exception_message(cls, msg, pid):
    return cls._EXCEPTION_LOG_FORMAT.format(
      timestamp=cls._iso_timestamp_for_now(),
      args=sys.argv,
      pid=pid,
      message=msg)

  _traceback_omitted_default_text = '(backtrace omitted)'

  @classmethod
  def _format_traceback(cls, tb, should_print_backtrace):
    if should_print_backtrace:
      traceback_string = ''.join(traceback.format_tb(tb))
    else:
      traceback_string = cls._traceback_omitted_default_text
    return traceback_string

  _UNHANDLED_EXCEPTION_LOG_FORMAT = """\
Exception caught: ({exception_type})
{backtrace}
Exception message: {exception_message}{maybe_newline}
"""

  @classmethod
  def _format_unhandled_exception_log(cls, exc, tb, add_newline, should_print_backtrace):
    exception_message = str(exc) if exc else '(no message)'
    maybe_newline = '\n' if add_newline else ''
    return cls._UNHANDLED_EXCEPTION_LOG_FORMAT.format(
      exception_type=type(exc),
      backtrace=cls._format_traceback(tb, should_print_backtrace=should_print_backtrace),
      exception_message=exception_message,
      maybe_newline=maybe_newline)

  @classmethod
  def _exit_with_failure(cls, terminal_msg):
    # Exit with failure, printing a message to the terminal (or whatever the interactive stream is).
    cls._exiter.exit(result=cls.UNHANDLED_EXCEPTION_EXIT_CODE,
                     msg=terminal_msg,
                     out=cls._interactive_output_stream)

  @classmethod
  def _log_unhandled_exception_and_exit(cls, exc_class=None, exc=None, tb=None, add_newline=False):
    """A sys.excepthook implementation which logs the error and exits with failure."""
    exc_class = exc_class or sys.exc_info()[0]
    exc = exc or sys.exc_info()[1]
    tb = tb or sys.exc_info()[2]

    extra_err_msg = None
    try:
      # Always output the unhandled exception details into a log file, including the traceback.
      exception_log_entry = cls._format_unhandled_exception_log(exc, tb, add_newline,
                                                              should_print_backtrace=True)
      cls.log_exception(exception_log_entry)
    except Exception as e:
      extra_err_msg = 'Additional error logging unhandled exception {}: {}'.format(exc, e)
      logger.error(extra_err_msg)

    # Generate an unhandled exception report fit to be printed to the terminal (respecting the
    # Exiter's should_print_backtrace field).
    stderr_printed_error = cls._format_unhandled_exception_log(
      exc, tb, add_newline,
      should_print_backtrace=cls._exiter.should_print_backtrace)
    if extra_err_msg:
      stderr_printed_error = '{}\n{}'.format(stderr_printed_error, extra_err_msg)
    cls._exit_with_failure(stderr_printed_error)


# Setup global state such as signal handlers and sys.excepthook with probably-safe values at module
# import time.
# Sets fatal signal handlers with reasonable defaults to catch errors early in startup.
ExceptionSink.reset_log_location(os.getcwd())
# Sets except hook.
ExceptionSink.reset_exiter(Exiter(print_backtraces=True))
# Sets a SIGUSR2 handler.
ExceptionSink.reset_interactive_output_stream(sys.stderr)
