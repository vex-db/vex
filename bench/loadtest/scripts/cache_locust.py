"""Cache-aside journeys; run through Locust, using REDIS_URL and CACHE_PREFIX."""
import os
import random
import time

import gevent
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from locust import LoadTestShape, User, between, events, task

KEYS = int(os.getenv("CACHE_KEYS", "10000"))
PREFIX = os.environ["CACHE_PREFIX"]
STAGE_SECONDS = int(os.getenv("CACHE_STAGE_SECONDS", "60"))
BASE_USERS = int(os.getenv("CACHE_USERS", "32"))
BACKEND_MS = float(os.getenv("CACHE_BACKEND_MS", "5"))
assert KEYS >= 10 and STAGE_SECONDS > 0 and BASE_USERS > 0 and BACKEND_MS >= 0
assert PREFIX.startswith("vex-realistic:"), "use a dedicated benchmark prefix"
SIZES = (32, 256, 1024, 4096)
PAYLOADS = {n: bytes(range(256)) * (n // 256) + bytes(range(n % 256)) for n in SIZES}


class CacheUser(User):
    wait_time = between(.005, .02)

    def on_start(self):
        self.rng = random.Random()
        self.client = redis.Redis.from_url(os.environ["REDIS_URL"], socket_timeout=2,
                                          socket_connect_timeout=2, max_connections=1,
                                          retry=Retry(NoBackoff(), 0), protocol=2)

    def on_stop(self):
        self.client.close()
        self.client.connection_pool.disconnect()

    def key(self):
        # 80% of accesses target the hottest 20% of keys.
        hot = KEYS // 5
        index = self.rng.randrange(hot) if self.rng.random() < .8 else self.rng.randrange(hot, KEYS)
        return PREFIX + str(index)

    def call(self, command, *args, **kwargs):
        started = time.perf_counter()
        error, result = None, None
        try:
            result = getattr(self.client, command.lower())(*args, **kwargs)
            return result
        except redis.RedisError as exc:
            error = exc
            raise
        finally:
            name = "GET miss" if command == "GET" and result is None and error is None else command
            if command == "GET" and result is not None:
                name = "GET hit"
            self.environment.events.request.fire(request_type="REDIS", name=name,
                response_time=(time.perf_counter() - started) * 1000,
                response_length=len(result) if isinstance(result, bytes) else 0, exception=error)

    @task(9)
    def read_through(self):
        started = time.perf_counter()
        error = None
        try:
            key = self.key()
            value = self.call("GET", key)
            if value is None:
                gevent.sleep(BACKEND_MS / 1000)
                value = PAYLOADS[self.rng.choices(SIZES, weights=(50, 30, 15, 5))[0]]
                self.call("SET", key, value, ex=self.rng.randint(10, 60))
            if len(value) not in PAYLOADS or value != PAYLOADS[len(value)]:
                raise ValueError("unexpected cache value")
        except (redis.RedisError, ValueError) as exc:
            error = exc
        finally:
            self.environment.events.request.fire(request_type="JOURNEY", name="cache-aside",
                response_time=(time.perf_counter() - started) * 1000, response_length=0, exception=error)

    @task(1)
    def invalidate(self):
        try:
            self.call("DELETE", self.key())
        except redis.RedisError:
            pass  # Recorded as a failed REDIS request above.


class CacheTraffic(LoadTestShape):
    def tick(self):
        stage = int(self.get_run_time() // STAGE_SECONDS)
        if stage >= 3:
            return None
        users = BASE_USERS * (3 if stage == 1 else 1)
        return users, max(1, BASE_USERS)


@events.quitting.add_listener
def fail_on_errors(environment, **kwargs):
    if environment.stats.total.num_requests == 0 or environment.stats.total.num_failures:
        environment.process_exit_code = 1
