r"""
fastapi/app/queue/kafka/consumers/raw_logs.py

Kafka consumer for triggering the AI pipeline on new log ingestions.

Subscribes to:
  - Pattern: ^raw\.logs\..*

Parses the "log.ingested" event and fires the `parse_log` Celery task.
"""

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from aiokafka import AIOKafkaConsumer
from aiokafka.errors import KafkaError

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class RawLogConsumer:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._consumer: Optional[AIOKafkaConsumer] = None
        self._running: bool = False

    async def start(self) -> None:
        logger.info(
            "raw_log_consumer_starting",
            extra={
                "bootstrap_servers": self._settings.KAFKA_BOOTSTRAP_SERVERS,
            },
        )

        self._running = True

        while self._running:
            try:
                if self._consumer is None:
                    self._consumer = AIOKafkaConsumer(
                        bootstrap_servers=self._settings.KAFKA_BOOTSTRAP_SERVERS,
                        group_id="neuralops_fastapi_raw_logs_group",
                        auto_offset_reset="earliest",
                        enable_auto_commit=False,
                        value_deserializer=lambda raw: (
                            raw.decode("utf-8") if raw else None
                        ),
                        key_deserializer=lambda raw: (
                            raw.decode("utf-8") if raw else None
                        ),
                    )
                    self._consumer.subscribe(pattern=r"^raw\.logs\..*")

                await self._consumer.start()
                logger.info("raw_log_consumer_started")
                await self._consume_loop()
            except KafkaError as exc:
                logger.error(
                    "raw_log_kafka_error",
                    extra={"error": str(exc)},
                    exc_info=True,
                )
            except asyncio.CancelledError:
                logger.info("raw_log_consumer_cancelled")
                break
            except Exception as exc:
                logger.error(
                    "raw_log_unexpected_error",
                    extra={"error": str(exc)},
                    exc_info=True,
                )
            finally:
                if self._consumer:
                    await self._consumer.stop()
                    self._consumer = None

            if self._running:
                await asyncio.sleep(5)

        logger.info("raw_log_consumer_stopped")

    async def stop(self) -> None:
        logger.info("raw_log_consumer_stopping")
        self._running = False
        if self._consumer:
            await self._consumer.stop()

    async def _consume_loop(self) -> None:
        from app.worker.tasks.parse_log import parse_log

        async for message in self._consumer:
            if not self._running:
                break

            try:
                payload: Dict[str, Any] = json.loads(message.value)
            except Exception as exc:
                logger.error(
                    "raw_log_json_decode_error",
                    extra={"error": str(exc)},
                )
                await self._consumer.commit()
                continue

            event_type = payload.get("event_type")
            if event_type == "log.ingested":
                tenant_id = payload.get("tenant_id")
                incident_id = payload.get("incident_id")
                s3_path = payload.get("s3_path")
                service_name = payload.get("service_name")
                environment = payload.get("environment")
                trigger = payload.get("trigger")
                sdk_meta = payload.get("sdk_meta")
                file_path = payload.get("file_path")
                line_number = payload.get("line_number")
                error_type = payload.get("error_type")

                if (
                    tenant_id
                    and incident_id
                    and s3_path
                    and service_name
                    and environment
                ):
                    logger.info(
                        "raw_log_triggering_parse_log",
                        extra={"tenant_id": tenant_id, "incident_id": incident_id},
                    )
                    parse_log.delay(
                        tenant_id=tenant_id,
                        incident_id=incident_id,
                        s3_path=s3_path,
                        service_name=service_name,
                        environment=environment,
                        trigger=trigger,
                        sdk_meta=sdk_meta,
                        file_path=file_path,
                        line_number=line_number,
                        error_type=error_type,
                    )
                else:
                    logger.warning("raw_log_missing_fields", extra={"payload": payload})

            await self._consumer.commit()
