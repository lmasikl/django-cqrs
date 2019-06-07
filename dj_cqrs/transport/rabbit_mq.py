from __future__ import unicode_literals

import logging
import time
from socket import gaierror

import ujson
from django.conf import settings
from pika import exceptions, BasicProperties, BlockingConnection, ConnectionParameters, credentials

from dj_cqrs.controller import consumer
from dj_cqrs.dataclasses import TransportPayload
from dj_cqrs.registries import ReplicaRegistry
from dj_cqrs.transport import BaseTransport

logger = logging.getLogger()


class RabbitMQTransport(BaseTransport):
    CONSUMER_RETRY_TIMEOUT = 5

    @classmethod
    def consume(cls):
        consumer_rabbit_settings = cls._get_consumer_settings()
        common_rabbit_settings = cls._get_common_settings()

        while True:
            try:
                _, channel = cls._get_consumer_rmq_objects(
                    *(common_rabbit_settings + consumer_rabbit_settings)
                )
                channel.start_consuming()
            except (exceptions.AMQPError, gaierror):
                logger.error('AMQP connection error... Reconnecting.')
                time.sleep(cls.CONSUMER_RETRY_TIMEOUT)
                continue

    @classmethod
    def produce(cls, payload):
        rmq_settings = cls._get_common_settings()
        exchange = rmq_settings[-1]

        try:
            # Decided not to create context-manager to stay within the class
            connection, channel = cls._get_producer_rmq_objects(*rmq_settings)

            cls._produce_message(channel, exchange, payload)
            cls._log_produced(payload)

            connection.close()
        except exceptions.AMQPError:
            logger.error("CQRS couldn't be published: pk = {} ({}).".format(
                payload.pk, payload.cqrs_id,
            ))

    @classmethod
    def _consume_message(cls, *args):
        body = args[-1]
        try:
            dct = ujson.loads(body)
        except ValueError:
            logger.error("CQRS couldn't be parsed: {}.".format(body))
            return

        payload = TransportPayload(dct['signal_type'], dct['cqrs_id'], dct['instance_data'])

        cls._log_consumed(payload)
        consumer.consume(payload)

    @classmethod
    def _produce_message(cls, channel, exchange, payload):
        routing_key = payload.cqrs_id

        channel.basic_publish(
            exchange=exchange,
            routing_key=routing_key,
            body=ujson.dumps(payload.to_dict()),
            mandatory=True,
            properties=BasicProperties(content_type='text/plain', delivery_mode=2)
        )

    @classmethod
    def _get_consumer_rmq_objects(cls, host, port, creds, exchange, queue_name, prefetch_count):
        connection = BlockingConnection(
            ConnectionParameters(host=host, port=port, credentials=creds),
        )
        channel = connection.channel()
        channel.basic_qos(prefetch_count=prefetch_count)

        channel.exchange_declare(exchange=exchange, exchange_type='topic')
        channel.queue_declare(queue_name, durable=True, exclusive=False)

        for cqrs_id, replica_model in ReplicaRegistry.models.items():
            channel.queue_bind(exchange=exchange, queue=queue_name, routing_key=cqrs_id)

        channel.basic_consume(
            queue=queue_name, on_message_callback=cls._consume_message, auto_ack=True,
            exclusive=False,
        )

        return connection, channel

    @classmethod
    def _get_producer_rmq_objects(cls, host, port, creds, exchange):
        connection = BlockingConnection(
            ConnectionParameters(
                host=host, port=port, credentials=creds,
                blocked_connection_timeout=10,
            ),
        )
        channel = connection.channel()
        channel.exchange_declare(
            exchange=exchange,
            exchange_type='topic',
        )
        return connection, channel

    @staticmethod
    def _get_common_settings():
        host = settings.CQRS.get('host', ConnectionParameters.DEFAULT_HOST)
        port = settings.CQRS.get('port', ConnectionParameters.DEFAULT_PORT)
        user = settings.CQRS.get('user', ConnectionParameters.DEFAULT_USERNAME)
        password = settings.CQRS.get('password', ConnectionParameters.DEFAULT_PASSWORD)
        exchange = settings.CQRS.get('exchange', 'cqrs')
        return (
            host,
            port,
            credentials.PlainCredentials(user, password, erase_on_connect=True),
            exchange,
        )

    @staticmethod
    def _get_consumer_settings():
        queue_name = settings.CQRS['queue']
        consumer_prefetch_count = settings.CQRS.get('consumer_prefetch_count', 10)
        return (
            queue_name,
            consumer_prefetch_count,
        )

    @staticmethod
    def _log_consumed(payload):
        """
        :param dj_cqrs.dataclasses.TransportPayload payload: Transport payload from master model.
        """
        pass

    @staticmethod
    def _log_produced(payload):
        """
        :param dj_cqrs.dataclasses.TransportPayload payload: Transport payload from master model.
        """
        logger.info('CQRS is published: pk = {} ({}).'.format(payload.pk, payload.cqrs_id))
