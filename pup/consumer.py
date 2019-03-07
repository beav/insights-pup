import aiohttp
import asyncio
import json
import logging
import os

from tempfile import NamedTemporaryFile
from aiohttp.client_exceptions import ClientConnectionError, ServerDisconnectedError
from prometheus_async.aio import time

from pup.utils import mnm, configuration
from pup.utils.fact_extract import extract_facts

logger = logging.getLogger('advisor-pup')


async def handle_file(msgs, produce_queue):

    for msg in msgs:
        try:
            data = json.loads(msg.value)
        except ValueError:
            logger.error("handle_file(): unable to decode msg as json: {}".format(msg.value))
            continue

        logger.info(data)
        machine_id = data['metadata'].get('machine_id') if data.get('metadata') else None
        mnm.total.inc()
        try:
            result = await validate(data['url'])
        except (ServerDisconnectedError, ClientConnectionError):
            logger.error('Connection to S3 Failed')
            continue

        if 'error' not in result:
            if result['insights_id'] != machine_id:
                response = await post_to_inventory(result, data)
            else:
                response = None

            mnm.valid.inc()
            logger.info("payload_id [%s] validation successful", data['payload_id'])
            data_to_produce = {
                'topic': 'platform.upload.validation',
                'msg': {
                    'id': response.get('id') if response else None,
                    'service': data['service'],
                    'payload_id': data['payload_id'],
                    'account': data['account'],
                    'principal': data['principal'],
                    'b64_identity': data.get('b64_identity'),
                    'validation': 'success'
                }
            }
        else:
            mnm.invalid.inc()
            logger.info("payload_id [%s] validation failed with error: %s", data['payload_id'], result['error'])
            data_to_produce = {
                'topic': 'platform.upload.validation',
                'msg': {
                    'payload_id': data['payload_id'],
                    'validation': 'failure'
                }
            }

        produce_queue.append(data_to_produce)
        logger.info(
            "data for topic [%s], payload_id [%s] put on produce queue (qsize now: %d): %s",
            data_to_produce['topic'], data_to_produce['msg']['payload_id'], len(produce_queue), data_to_produce
        )


@time(mnm.validation_time)
async def validate(url):

    temp = NamedTemporaryFile(delete=False).name

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            open(temp, 'wb').write(await response.read())

    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, extract_facts, temp)
    finally:
        os.remove(temp)


@time(mnm.inventory_post_time)
async def post_to_inventory(facts, msg):

    system_profile = facts.pop('system_profile')
    post = {**facts, **msg}
    post['account'] = post.pop('rh_account')
    post['facts'] = []
    if post.get('metadata'):
        post['facts'].append({'facts': post.pop('metadata'),
                              'namespace': 'insights-client'})

    post['facts'].append({'facts': system_profile,
                          'namespace': 'system_profile'})

    headers = {'x-rh-identity': post['b64_identity'],
               'Content-Type': 'application/json'}

    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession() as session:
            async with session.post(configuration.INVENTORY_URL, data=json.dumps([post]), headers=headers, timeout=timeout) as response:
                response_json = await response.json()
                if response.status != 207:
                    error = response_json.get('detail')
                    logger.error('Failed to post to inventory: %s', error)
                elif response_json['data'][0]['status'] != 200 and response_json['data'][0]['status'] != 201:
                    mnm.inventory_post_failure.inc()
                    logger.error(
                        'payload_id [%s] failed to post to inventory: %s', msg['payload_id'], await response.text()
                    )
                else:
                    mnm.inventory_post_success.inc()
                    logger.info("payload_id [%s] posted to inventory: ID [%s]",
                                msg['payload_id'],
                                response_json['data'][0]['host']['id'])
                    return response_json['data'][0]['host']
    except ClientConnectionError as e:
        logger.error("payload_id [%s] failed to post to inventory, unable to connect: %s", msg['payload_id'], e)
        return {"error": "Unable to update inventory. Service unavailable"}
