"""Loopback-only broker used by the optional real MQTT integration test."""

import asyncio
import sys

from amqtt.broker import Broker


async def serve():
    broker = Broker(
        {
            "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{sys.argv[1]}"}},
            "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}},
        }
    )
    await broker.start()
    try:
        await asyncio.Event().wait()
    finally:
        await broker.shutdown()


if __name__ == "__main__":
    asyncio.run(serve())
