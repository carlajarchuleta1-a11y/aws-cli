# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
import datetime
import hashlib
import io
import json
import os
import socket
import sys
import threading
from functools import cached_property
from pathlib import Path

from botocore.useragent import UserAgentComponent

from awscli.compat import is_windows
from awscli.utils import add_component_to_user_agent_extra

_CACHE_DIR = Path('~/.aws/cli/cache').expanduser()
_SESSION_LENGTH_SECONDS = 60 * 30


class CLISession:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        self._lock = threading.Lock()

    @cached_property
    def session_id(self):
        if (cached_session_id := self._check_cache()) is not None:
            return cached_session_id
        session_id = self._generate_md5_hash(
            self._hostname, self._tty, self._timestamp
        )
        session_data = {'sid': session_id, 'ts': self._timestamp}
        self._write_to_cache(session_data, self._cachefile)
        return session_id

    @cached_property
    def _tty(self):
        # os.ttyname is only available on Unix platforms.
        if is_windows:
            return
        try:
            return os.ttyname(sys.stdin.fileno())
        # Standard input was redirected to a pseudofile.
        # This can happen when running tests on IDEs or
        # running scripts with redirected input.
        except (OSError, io.UnsupportedOperation):
            return

    @cached_property
    def _hostname(self):
        return socket.gethostname()

    @cached_property
    def _timestamp(self):
        return int(datetime.datetime.now(datetime.timezone.utc).timestamp())

    @cached_property
    def _cachefile(self):
        return (
            _CACHE_DIR
            / f"sid-{self._generate_md5_hash(self._hostname, self._tty)}.json"
        )

    def _generate_md5_hash(self, *args):
        str_to_hash = ""
        for arg in args:
            if arg is not None:
                str_to_hash += str(arg)
        return hashlib.md5(str_to_hash.encode('utf-8')).hexdigest()

    def _check_cache(self):
        if not self._cachefile.exists():
            return
        cache = self._read_from_cache(self._cachefile)
        if self._cached_session_expired(cache['ts']):
            return
        cache['ts'] = self._timestamp
        self._write_to_cache(cache, self._cachefile)
        return cache['sid']

    def _read_from_cache(self, path):
        with open(path) as f:
            cache = json.load(f)
        return cache

    def _write_to_cache(self, contents, path):
        if not _CACHE_DIR.exists():
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with open(path, 'w') as f:
                json.dump(contents, f)

    def _cached_session_expired(self, cached_timestamp):
        return (
            int(cached_timestamp) + _SESSION_LENGTH_SECONDS < self._timestamp
        )

    def sweep_cache(self):
        try:
            t = threading.Thread(
                # Daemon threads are immediately killed once the main
                # process exits. So it may not evict all expired cachefiles,
                # but it prevents the main process from being blocked.
                target=self._sweep_cache,
                daemon=True,
            )
            t.start()
        except Exception:
            # Since this is just a background cleanup task,
            # we want to avoid raising exceptions and interrupting
            # the main process.
            pass

    def _sweep_cache(self):
        if not _CACHE_DIR.exists():
            return
        for filepath in _CACHE_DIR.iterdir():
            # Avoids a race condition where an expired cachefile is read
            # by both `_check_cache` and `_sweep_cache` threads, updated by
            # `_check_cache`, and then deleted by `_sweep_cache`.
            # In other words, never delete the current resolved cachefile
            # since it's guaranteed to be updated with the current timestamp.
            if (
                not filepath.name.startswith('sid-')
                or filepath == self._cachefile
            ):
                continue
            cache = self._read_from_cache(filepath)
            if self._cached_session_expired(cache['ts']):
                os.remove(filepath)


def add_session_id_component_to_user_agent_extra(session):
    add_component_to_user_agent_extra(
        session, UserAgentComponent("sid", CLISession().session_id)
    )
