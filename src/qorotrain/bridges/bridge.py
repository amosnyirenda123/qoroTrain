import logging
import os
import sqlite3
import threading
import uuid

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Literal, List

from ..connectors.connector import MQTTConnector
from ..routes.route import Route
from ..tuners.tuner import Tuner
from ..messages.message import Message
from ..config import BrokerConfig, MQTTConfig, RabbitMQConfig, KafkaConfig


log = logging.getLogger(__name__)


class Bridge:
    """
    Coordinates broker connectors, routing and tuner communication.

    The Bridge is responsible for managing the lifecycle of
    connectors and forwarding incoming telemetry to a Tuner.
    """

    def __init__(
            self,
            broker: Literal['mqtt', 'rabbitmq', 'kafka'],
            broker_config: BrokerConfig,
            n_jobs: int = -1,
            routes: List[Route] | None = None,
            tuner: Tuner | None = None,
            bridge_table_path: str = 'bridge.db'
    ):
        self._broker = broker
        self._broker_config = broker_config
        self._n_jobs = n_jobs
        self._routes = routes or []
        self._tuner = tuner
        self._bridge_table_path = bridge_table_path

        self._connectors = {}
        self._futures = {}

        self._num_active_connectors = 0

        self._db_conn = None
        self._db_lock = threading.Lock()

        self._thread_pool = None
        self._stop_event = threading.Event()

        self._started = False

    def _init_thread_pool(self):
        if self._n_jobs == -1:
            max_workers = min(32, (os.cpu_count() or 1) + 4)
        else:
            if self._n_jobs < 1:
                raise ValueError('n_jobs must be -1 or positive.')
            max_workers = self._n_jobs

        self._thread_pool = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix='bridge-connector'
        )

    def _init_db(self):
        self._db_conn = sqlite3.connect(
            self._bridge_table_path,
            check_same_thread=False
        )

        with self._db_lock:
            self._db_conn.execute("""
                CREATE TABLE IF NOT EXISTS bridge_table (
                    connector_id TEXT PRIMARY KEY,
                    connector_ip TEXT,
                    broker_id TEXT,
                    broker_ip TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_checked TEXT
                )
            """)
            self._db_conn.commit()

    def _init_mqtt(self, config):
        connector = MQTTConnector(
            config=config
        )

        return [connector]

    def _init_rabbitmq(self, config):
        raise NotImplementedError("RabbitMQ is not supported yet.")

    def _init_kafka(self, config):
        raise NotImplementedError("Kafka is not supported yet.")

    def _check_config(self):
        match self._broker:
            case 'mqtt':
                return isinstance(self._broker_config, MQTTConfig)
            case 'rabbitmq':
                return isinstance(self._broker_config, RabbitMQConfig)
            case 'kafka':
                return isinstance(self._broker_config, KafkaConfig)

    def _init_connectors(self):

        match self._broker:
            case "mqtt":
                if not self._check_config():
                    raise TypeError(
                        "broker_config must be an MQTTConfig for broker='mqtt'."
                    )
                return self._init_mqtt(self._broker_config)

            case "rabbitmq":
                if not self._check_config():
                    raise TypeError(
                        "broker_config must be a RabbitMQConfig for broker='rabbitmq'."
                    )
                return self._init_rabbitmq(self._broker_config)

            case "kafka":
                if not self._check_config():
                    raise TypeError(
                        "broker_config must be a KafkaConfig for broker='kafka'."
                    )
                return self._init_kafka(self._broker_config)

            case _:
                raise ValueError(
                    f"Unsupported broker: {self._broker}"
                )

    def register(self, connector):
        connector_id = str(uuid.uuid4())

        self._connectors[connector_id] = connector

        now = datetime.now(timezone.utc).isoformat()

        with self._db_lock:
            self._db_conn.execute(
                """
                INSERT INTO bridge_table (
                    connector_id,
                    status,
                    created_at
                )
                VALUES (?, ?, ?)
                """,
                (connector_id, "registered", now)
            )

            self._db_conn.commit()

        return connector_id

    def unregister(self, connector_id: str):

        connector = self._connectors.pop(connector_id, None)

        if connector is None:
            return False

        connector.close()

        with self._db_lock:
            self._db_conn.execute(
                """
                UPDATE bridge_table
                SET status = ?, last_checked = ?
                WHERE connector_id = ?
                """,
                (
                    "stopped",
                    datetime.now(timezone.utc).isoformat(),
                    connector_id
                )
            )

            self._db_conn.commit()

        return True

    def _update_status(self, connector_id, status):

        with self._db_lock:
            self._db_conn.execute(
                """
                UPDATE bridge_table
                SET status = ?, last_checked = ?
                WHERE connector_id = ?
                """,
                (
                    status,
                    datetime.now(timezone.utc).isoformat(),
                    connector_id
                )
            )

            self._db_conn.commit()

    def status(self):

        with self._db_lock:
            cursor = self._db_conn.execute(
                """
                SELECT connector_id, status, created_at, last_checked
                FROM bridge_table
                """
            )

            return cursor.fetchall()

    def attach(self, tuner: Tuner) -> "Bridge":

        self._tuner = tuner
        return self

    def detach(self):
        self._tuner = None

    def _dispatch(self, route, message):

        if self._tuner is None:
            log.warning("Message received but no tuner is attached.")
            return
        self._tuner.update(route, message)

    # Handling biderectional communication between Tuner and Connector

    def _update_tuner(self, route, message):
        self._dispatch(route, message)

    # One direction (Connector to Tuner through bridge)
    def _sync(self, connector_id, connector):
        self._update_status(connector_id, 'running')

        try:
            connector.connect()

            # subscribe to all routes
            for route in self._routes:
                connector.subscribe(
                    route.topic,
                    lambda msg, _route=route: self._update_tuner(_route, msg)
                )

            while not self._stop_event.is_set():
                self._stop_event.wait(1.0)

        except Exception:
            self._update_status(connector_id, 'error')
            raise

    # Another direction (Tuner to Connector Through bridge)

    def publish(self, message: Message, connector_id: str | None = None):
        if not self._connectors:
            raise RuntimeError("No connectors are registered.")

        if connector_id is None:
            connector = next(iter(self._connectors.values()))
        else:
            connector = self._connectors.get(connector_id)
            if connector is None:
                raise KeyError(f"Unknown connector_id: {connector_id}")

        connector.publish(message)

    def start(self):

        if self._started:
            raise RuntimeError("Bridge is already running.")

        self._init_db()
        self._init_thread_pool()

        connectors = self._init_connectors()

        try:
            for connector in connectors:

                connector_id = self.register(connector)

                future = self._thread_pool.submit(
                    self._sync,
                    connector_id,
                    connector
                )

                self._futures[connector_id] = future

            self._num_active_connectors = len(connectors)
            self._started = True

        except Exception:
            self.close()
            raise

        return self

    def close(self):

        self._stop_event.set()

        for connector in self._connectors.values():
            try:
                connector.close()
            except Exception:
                log.exception("Error closing connector.")

        if self._thread_pool is not None:
            self._thread_pool.shutdown(
                wait=True,
                cancel_futures=True
            )

        self._num_active_connectors = 0
        self._started = False

        if self._db_conn is not None:
            self._db_conn.close()
            self._db_conn = None

        self._thread_pool = None
        self._connectors.clear()
        self._futures.clear()

    def __enter__(self):

        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):

        self.close()

        return False