import threading
from logging import getLogger
from os import urandom
from hashlib import sha1

from redis import StrictRedis
from redis.exceptions import NoScriptError

__version__ = "2.3.0"

logger = getLogger(__name__)

UNLOCK_SCRIPT = b"""
    if redis.call("get", KEYS[1]) == ARGV[1] then
        redis.call("del", KEYS[2])
        redis.call("lpush", KEYS[2], 1)
        redis.call("expire", KEYS[2], 1)
        return redis.call("del", KEYS[1])
    else
        return 0
    end
"""
UNLOCK_SCRIPT_HASH = sha1(UNLOCK_SCRIPT).hexdigest()

EXTEND_SCRIPT = b"""
    if redis.call("ttl", KEYS[1]) >= 0 then
        redis.call("expire", KEYS[1], ARGV[1])
        return 0
    else
        return -1
    end
"""
EXTEND_SCRIPT_HASH = sha1(EXTEND_SCRIPT).hexdigest()

RESET_SCRIPT = b"""
    redis.call('del', KEYS[2])
    redis.call('lpush', KEYS[2], 1)
    redis.call('expire', KEYS[2], 1)
    return redis.call('del', KEYS[1])
"""

RESET_SCRIPT_HASH = sha1(RESET_SCRIPT).hexdigest()

RESET_ALL_SCRIPT = b"""
    local locks = redis.call('keys', 'lock:*')
    local signal
    for _, lock in pairs(locks) do
        signal = 'lock-signal:' .. string.sub(lock, 6)
        redis.call('del', signal)
        redis.call('lpush', signal, 1)
        redis.call('expire', signal, 1)
        redis.call('del', lock)
    end
    return #locks
"""

RESET_ALL_SCRIPT_HASH = sha1(RESET_ALL_SCRIPT).hexdigest()


class AlreadyAcquired(RuntimeError):
    pass


class NotAcquired(RuntimeError):
    pass


class AlreadyStarted(RuntimeError):
    pass


class TimeoutNotUsable(RuntimeError):
    pass


class InvalidTimeout(RuntimeError):
    pass


class TimeoutTooLarge(RuntimeError):
    pass


class NotExpirable(RuntimeError):
    pass


((UNLOCK, _, _,
  EXTEND, _, _,
  RESET, _, _,
  RESET_ALL, _, _),
SCRIPTS) = zip(*enumerate([
    UNLOCK_SCRIPT_HASH, UNLOCK_SCRIPT, 'UNLOCK_SCRIPT',
    EXTEND_SCRIPT_HASH, EXTEND_SCRIPT, 'EXTEND_SCRIPT',
    RESET_SCRIPT_HASH, RESET_SCRIPT, 'RESET_SCRIPT',
    RESET_ALL_SCRIPT_HASH, RESET_ALL_SCRIPT, 'RESET_ALL_SCRIPT'
]))


def _eval_script(redis, script_id, *args, **kwargs):
    """Tries to call ``EVALSHA`` with the `hash` and then, if it fails, calls
    regular ``EVAL`` with the `script`.
    """
    try:
        return redis.evalsha(SCRIPTS[script_id], *args, **kwargs)
    except NoScriptError:
        logger.warn("%s not cached.", SCRIPTS[script_id + 2])
        return redis.eval(SCRIPTS[script_id + 1], *args, **kwargs)


class Lock(object):
    """
    A Lock context manager implemented via redis SETNX/BLPOP.
    """

    def __init__(self, redis_client, name, expire=None, id=None, auto_renewal=False):
        """
        :param redis_client:
            An instance of :class:`~StrictRedis`.
        :param name:
            The name (redis key) the lock should have.
        :param expire:
            The lock expiry time in seconds. If left at the default (None)
            the lock will not expire.
        :param id:
            The ID (redis value) the lock should have. A random value is
            generated when left at the default.
        :param auto_renewal:
            If set to True, Lock will automatically renew the lock so that it
            doesn't expire for as long as the lock is held (acquire() called
            or running in a context manager).

            Implementation note: Renewal will happen using a daemon thread with
            an interval of expire*2/3. If wishing to use a different renewal
            time, subclass Lock, call super().__init__() then set
            self._lock_renewal_interval to your desired interval.
        """
        assert isinstance(redis_client, StrictRedis)
        if auto_renewal and expire is None:
            raise ValueError("Expire may not be None when auto_renewal is set")

        self._client = redis_client
        self._expire = expire if expire is None else int(expire)
        self._id = urandom(16) if id is None else id
        self._held = False
        self._name = 'lock:'+name
        self._signal = 'lock-signal:'+name
        self._lock_renewal_interval = expire*2/3 if auto_renewal else None
        self._lock_renewal_thread = None

    def reset(self):
        """
        Forcibly deletes the lock. Use this with care.
        """
        _eval_script(self._client, RESET, 2, self._name, self._signal)

    @property
    def id(self):
        return self._id

    def get_owner_id(self):
        return self._client.get(self._name)

    def acquire(self, blocking=True, timeout=None):
        """
        :param blocking:
            Boolean value specifying whether lock should be blocking or not.
        :param timeout:
            An integer value specifying the maximum number of seconds to block.
        """
        logger.debug("Getting %r ...", self._name)

        if self._held:
            raise AlreadyAcquired("Already acquired from this Lock instance.")

        if not blocking and timeout is not None:
            raise TimeoutNotUsable("Timeout cannot be used if blocking=False")

        timeout = timeout if timeout is None else int(timeout)
        if timeout is not None and timeout <= 0:
            raise InvalidTimeout("Timeout (%d) cannot be less than or equal to 0" % timeout)

        if timeout and self._expire and timeout > self._expire:
            raise TimeoutTooLarge("Timeout (%d) cannot be greater than expire (%d)" % (timeout, self._expire))

        busy = True
        blpop_timeout = timeout or self._expire or 0
        timed_out = False
        while busy:
            busy = not self._client.set(self._name, self._id, nx=True, ex=self._expire)
            if busy:
                if timed_out:
                    return False
                elif blocking:
                    timed_out = not self._client.blpop(self._signal, blpop_timeout)
                else:
                    logger.debug("Failed to get %r.", self._name)
                    return False

        logger.debug("Got lock for %r.", self._name)
        self._held = True
        if self._lock_renewal_interval is not None:
            self._start_lock_renewer()
        return True

    def extend(self, expire=None):
        """Extends expiration time of the lock.

        :param expire:
            New expiration time. If ``None`` - `expire` provided during
            lock initialization will be taken.
        """
        if expire is None and self._expire is not None:
            expire = self._expire
        elif expire is None and self._expire is None:
            raise TypeError(
                "To extend a lock 'expire' must be provided as an argument "
                "to extend() method or at initialization time."
            )
        if _eval_script(self._client, EXTEND, 1, self._name, expire) != 0:
            raise RuntimeError('Failed to extend lock %s' % self._name)

    def _lock_renewer(self, interval):
        """
        Renew the lock key in redis every `interval` seconds for as long
        as `self._lock_renewal_thread.should_exit` is False.
        """
        log = getLogger("%s.lock_refresher" % __name__)
        while not self._lock_renewal_thread.wait_for_exit_request(timeout=interval):
            log.debug("Refreshing lock")
            self.extend(expire=self._expire)
        log.debug("Exit requested, stopping lock refreshing")

    def _start_lock_renewer(self):
        """
        Starts the lock refresher thread.
        """
        if self._lock_renewal_thread is not None:
            raise AlreadyStarted("Lock refresh thread already started")

        logger.debug(
            "Starting thread to refresh lock every %s seconds",
            self._lock_renewal_interval
        )
        self._lock_renewal_thread = InterruptableThread(
            group=None,
            target=self._lock_renewer,
            kwargs={'interval': self._lock_renewal_interval}
        )
        self._lock_renewal_thread.setDaemon(True)
        self._lock_renewal_thread.start()

    def _stop_lock_renewer(self):
        """
        Stop the lock renewer.

        This signals the renewal thread and waits for its exit.
        """
        if self._lock_renewal_thread is None or not self._lock_renewal_thread.is_alive():
            return
        logger.debug("Signalling the lock refresher to stop")
        self._lock_renewal_thread.request_exit()
        self._lock_renewal_thread.join()
        self._lock_renewal_thread = None
        logger.debug("Lock refresher has stopped")

    def __enter__(self):
        acquired = self.acquire(blocking=True)
        assert acquired, "Lock wasn't acquired, but blocking=True"
        return self

    def __exit__(self, exc_type=None, exc_value=None, traceback=None, force=False):
        if not (self._held or force):
            raise NotAcquired("This Lock instance didn't acquire the lock.")
        if self._lock_renewal_thread is not None:
            self._stop_lock_renewer()
        logger.debug("Releasing %r.", self._name)
        _eval_script(self._client, UNLOCK,
                     2, self._name, self._signal, self._id)

        self._held = False

    def release(self, force=False):
        """Releases the lock, that was acquired in the same Python context.

        :param force:
            If ``False`` - fail with exception if this instance was not in
            acquired state in the same Python context.
            If ``True`` - fail silently.
        """
        return self.__exit__(force=force)


class InterruptableThread(threading.Thread):
    """
    A Python thread that can be requested to stop by calling request_exit()
    on it.

    Code running inside this thread should periodically check the
    `should_exit` property (or use wait_for_exit_request) on the thread
    object and stop further processing once it returns True.
    """
    def __init__(self, *args, **kwargs):
        self._should_exit = threading.Event()
        super(InterruptableThread, self).__init__(*args, **kwargs)

    def request_exit(self):
        """
        Signal the thread that it should stop performing more work and exit.
        """
        self._should_exit.set()

    @property
    def should_exit(self):
        return self._should_exit.isSet()

    def wait_for_exit_request(self, timeout=None):
        """
        Wait until the thread has been signalled to exit.

        If timeout is specified (as a float of seconds to wait) then wait
        up to this many seconds before returning the value of `should_exit`.
        """
        should_exit = self._should_exit.wait(timeout)
        if should_exit is None:
            # Python 2.6 compatibility which doesn't return self.__flag when
            # calling Event.wait()
            should_exit = self.should_exit
        return should_exit


def reset_all(redis_client):
    """
    Forcibly deletes all locks if its remains (like a crash reason). Use this with care.
    """
    _eval_script(redis_client, RESET_ALL, 0)
