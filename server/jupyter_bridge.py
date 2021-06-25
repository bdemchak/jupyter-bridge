
"""Jupyter-bridge is a Flask service that executes on a server accessible to both a remote Jupyter server and browser-based
Jupyter client. Code running on the server calls Jupyter-bridge to queue a request that the client will execute, and the
client will use Jupyter-bridge to return a reply. This enables the client to execute an HTTP request on a client-based
application such as Cytoscape, and then return the Cytoscape result. This is almost possible via the Jupyter server's
%%javascript magic combined with the client's IPython.notebook.kernel.execute() function, except that the server
won't see the reply until all cells are executed -- too late.

A channel is identified as a UUID that both the client and server share before trying to use this bridge. Initially,
the client waits on a request on that channel, and the server eventually sends it. The client then executes the HTTP
operation identifies in the request. The server waits for a reply on that channel, and the client eventually sends it.
The request and reply operations are symmetrical, and so share common code. However, the request operation saves
the request in a request map (keyed by channel ID), and the reply operation saves the reply in a reply map.

Queuing requests is not allowed. If the server sends a request before the client operates on it, an error occurs.
Likewise, when the client sends a reply, it assumes the server will receive it before the client needs to send a
subsequent reply.

Python maps are not thread-safe, so the request and reply maps are protected by semaphore. A message is stored in
a Queue of length 1 so that the message receiver can block while waiting for it.

Violation of an integrity assertion implies either a calling error at either the Jupyter server or Jupyter client. If
no violation occurs, a Jupyter-bridge call will return OK (for send operations) or OK and payload (for receive
operations). If an error occurs, an HTTP 500 will be returned.

Requests are assumed to be a JSON structure that describes the Cytoscape HTTP call. Replies are assumed to be the
raw text returned by Cytoscape, and may include JSON that will be recovered by the requestor when it receives the
reply.

"""
from flask import Flask, request, Response
import sys
import time
import logging
import os
from logging.handlers import RotatingFileHandler


app = Flask(__name__)

JUPYTER_BRIDGE_VERSION = '0.0.3'


# Set up detail logger
logger = logging.getLogger('jupyter-bridge')
logger_handler = RotatingFileHandler('jupyter-bridge.log', maxBytes=1048576, backupCount=10, encoding='utf8')
logger_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
logger.setLevel('DEBUG')
logger.addHandler(logger_handler)

PAD_MESSAGE = True # For troubleshooting truncated FIN terminator that loses headers and data
DEQUEUE_TIMEOUT_SECS = float(os.environ.get('JUPYTER_DEQUEUE_TIMEOUT_SECS', 15)) # Something less that connection timeout, but long enough not to cause caller to create a dequeue blizzard
FAST_DEQUEUE_POLLING_SECS = float(os.environ.get('JUPYTER_FAST_BRIDGE_POLL_SECS', 0.1)) # A fast polling rate means overall fast response to clients
SLOW_DEQUEUE_POLLING_SECS = float(os.environ.get('JUPYTER_SLOW_BRIDGE_POLL_SECS', 2)) # A slow polling rate means saving redis bandwidth
ALLOWED_FAST_DEQUEUE_POLLS = int(os.environ.get('JUPYTER_ALLOWED_FAST_DEQUEUE_POLLS', 10)) # Count of polls before client drops from FAST to SLOW
EXPIRE_SECS = 60 * 60 * 24 # How many seconds before an idle key dies

DEQUEUE_BUSY_STATUS = b'busy'
DEQUEUE_IDLE_STATUS = b'idle'

HTTP_OK = 200
HTTP_SYS_ERR = 500
HTTP_TIMEOUT = 408
HTTP_TOO_MANY = 429

# Redis message format:
MESSAGE = b'message'
POSTED_TIME = b'posted_time'
PICKUP_TIME = b'pickup_time'
DEQUEUE_BUSY = b'dequeue_busy'
REPLY_FAST_POLLS_LEFT = b'reply_fast_polls_left'

# Redis key constants
REPLY = 'reply'
REQUEST = 'request'
STATISTIC = 'stat'
COUNT = 'count'

logger.debug('Starting Jupyter-bridge with python environment: \n' + '\n'.join(sys.path))
logger.debug(f'Jupyter-bridge polling timeout: {DEQUEUE_TIMEOUT_SECS}, slow poll: {SLOW_DEQUEUE_POLLING_SECS}, fast poll: {FAST_DEQUEUE_POLLING_SECS}')

# Start the Redis client ... assume that the redis server has already started
try:
    import redis
    redis_db = redis.Redis('localhost')
    logger.debug('started redis connection')
except Exception as e:
    logger.debug(f'exception starting redis: {e!r}')


# Clear out all keys in case prior server instance was in the middle of any operations
def _del_key(key):
    try:
        if redis_db.delete(key) == 1:
            logger.debug(f'Deleted key {key}')
        else:
            logger.debug(f'Failed deleting key {key}')
    except Exception as e:
        logger.debug(f'Exception deleting key {key}: {e}')

for key in redis_db.keys(f'*:{REPLY}'):
    _del_key(key)

for key in redis_db.keys(f'*:{REQUEST}'):
    _del_key(key)

@app.route('/ping', methods=['GET'])
def ping():
    logger.debug('into ping')
    try:
        return Response(f'pong {JUPYTER_BRIDGE_VERSION}', status=200, content_type='text/plain', headers={'Access-Control-Allow-Origin': '*'})
    finally:
        logger.debug('out of ping')

@app.route('/stats', methods=['GET'])
def stats():
    logger.debug('into stats')

    try:
        # Find all statistics records
        keys = redis_db.keys(f'{STATISTIC}:*')

        # Create map of statistic lines
        csv_dict = {}
        for day in keys:
            day_string = day.decode('utf-8')[len(STATISTIC) + 1 : ]
            counts = ['' if count is None else count.decode('utf-8')   for count in redis_db.hmget(day, [f'{COUNT}:{REQUEST}', REQUEST, f'{COUNT}:{REPLY}', REPLY])]
            csv_dict[day_string] = f"{day_string},{','.join(counts)}"

        # Sort the statistics by date and create the list of dates and counts
        sorted_csv = dict(sorted(csv_dict.items(), key=lambda item: item[0]))
        csv = '\n'.join(list(sorted_csv.values()))

        return Response(
            f"date,{COUNT}({REQUEST}),{REQUEST} bytes,{COUNT}({REPLY}),{REPLY} bytes\n{csv}",
            mimetype="text/csv",
            headers={"Content-disposition":
                         "attachment; filename=jupyter-bridge.csv"})
    finally:
        logger.debug('out of stats')

@app.route('/queue_request', methods=['POST'])
def queue_request():
    local_transaction = _get_transaction_id()

    logger.debug(f'into queue_request ({local_transaction})')
    try:
        if 'channel' in request.args:
            channel = request.args['channel']

            # Send new request
            if request.content_type.startswith('application/json'):
                message = request.get_data()

                # Verify that the reply to a previous request was picked up before issuing new request
                reply_key = f'{channel}:{REPLY}'
                last_reply = redis_db.hget(reply_key, MESSAGE)
                if last_reply:
                    logger.debug(f'Warning: queue_request ({local_transaction}) Reply not picked up before new request. Reply: {last_reply}, Request: {message}')
                    _del_message(reply_key)

                _enqueue(local_transaction, REQUEST, channel, message)
                return Response('', status=HTTP_OK, content_type='text/plain', headers={'Access-Control-Allow-Origin': '*'})
            else:
                raise Exception('Payload must be application/json')
        else:
            raise Exception('Channel is missing in parameter list')
    except Exception as e:
        logger.debug(f'queue_request ({local_transaction}) exception {e!r}')
        return Response(_exception_message(e), status=HTTP_SYS_ERR, content_type='text/plain', headers={'Access-Control-Allow-Origin': '*'})
    finally:
        logger.debug(f'out of queue_request ({local_transaction})')

@app.route('/queue_reply', methods=['POST'])
def queue_reply():
    local_transaction = _get_transaction_id()

    logger.debug(f'into queue_reply ({local_transaction})')
    try:
        if 'channel' in request.args:
            channel = request.args['channel']
            if request.content_type.startswith('text/plain'):
                message = request.get_data()
                _enqueue(local_transaction, REPLY, channel, message)
                return Response('', status=HTTP_OK, content_type='text/plain', headers={'Access-Control-Allow-Origin': '*'})
            else:
                raise Exception('Payload must be text/plain')
        else:
            raise Exception('Channel is missing in parameter list')
    except Exception as e:
        logger.debug(f'queue_reply ({local_transaction}) exception {e!r}')
        return Response(_exception_message(e), status=HTTP_SYS_ERR, content_type='text/plain', headers={'Access-Control-Allow-Origin': '*'})
    finally:
        logger.debug(f'out of queue_reply ({local_transaction})')

@app.route('/dequeue_request', methods=['GET'])
def dequeue_request():
    local_transaction = _get_transaction_id()

    logger.debug(f'into dequeue_request ({local_transaction})')
    try:
        if 'channel' in request.args:
            channel = request.args['channel']
            message, valid_reader = _dequeue(local_transaction, REQUEST, channel, 'reset' in request.args) # Will block waiting for message
            if valid_reader:
                if message is None:
                    return Response('', status=HTTP_TIMEOUT, content_type='text/plain', headers={'Access-Control-Allow-Origin': '*'})
                else:
                    message = _add_padding(message)
                    return Response(message, status=HTTP_OK, content_type='application/json', headers={'Access-Control-Allow-Origin': '*'})
            else:
                return Response('', status=HTTP_TOO_MANY, content_type='text/plain', headers={'Access-Control-Allow-Origin': '*'})
        else:
            raise Exception('Channel is missing in parameter list')
    except Exception as e:
        logger.debug(f'dequeue_request ({local_transaction}) exception {e!r}')
        return Response(_exception_message(e), status=HTTP_SYS_ERR, content_type='text/plain', headers={'Access-Control-Allow-Origin': '*'})
    finally:
        logger.debug(f'out of dequeue_request ({local_transaction})')

@app.route('/dequeue_reply', methods=['GET'])
def dequeue_reply():
    local_transaction = _get_transaction_id()

    logger.debug(f'into dequeue_reply ({local_transaction})')
    try:
        if 'channel' in request.args:
            channel = request.args['channel']
            message, valid_reader = _dequeue(local_transaction, REPLY, channel, 'reset' in request.args) # Will block waiting for message
            if valid_reader:
                if message is None:
                    return Response('', status=HTTP_TIMEOUT, content_type='text/plain', headers={'Access-Control-Allow-Origin': '*'})
                else:
                    message = _add_padding(message)
                    return Response(message, status=HTTP_OK, content_type='application/json',
                                    headers={'Access-Control-Allow-Origin': '*'})
            else:
                return Response('', status=HTTP_TOO_MANY, content_type='text/plain', headers={'Access-Control-Allow-Origin': '*'})
        else:
            raise Exception('Channel is missing in parameter list')
    except Exception as e:
        logger.debug(f'dequeue_reply ({local_transaction}) exception {e!r}')
        return Response(_exception_message(e), status=HTTP_SYS_ERR, content_type='text/plain', headers={'Access-Control-Allow-Origin': '*'})
    finally:
        logger.debug(f'out of dequeue_reply ({local_transaction})')

def _enqueue(local_transaction, operation, channel, msg):
    key = f'{channel}:{operation}'
    logger.debug(f' into _enqueue ({local_transaction}): key: {key}, msg: {msg}')

    try:
        cur_value = redis_db.hgetall(key)
        if len(cur_value) == 0 or not MESSAGE in cur_value:
            _set_key_value(key, {MESSAGE: msg, PICKUP_TIME: '', POSTED_TIME: time.asctime()})
            _expire(key)

            _update_stats(operation, msg)

        else:
            raise Exception(f'Channel {key} contains unprocessed message')
    finally:
        logger.debug(f' out of _enqueue ({local_transaction})')

def _dequeue(local_transaction, operation, channel, reset_first):
    key = f'{channel}:{operation}'
    logger.debug(f' into _dequeue ({local_transaction}): key: {key}, reset_first: {reset_first}')
    message = None
    valid_reader = True
    try:
        dequeue_busy = redis_db.hget(key, DEQUEUE_BUSY) or DEQUEUE_IDLE_STATUS
        if dequeue_busy == DEQUEUE_BUSY_STATUS:
            valid_reader = False
            logger.debug(f'  _dequeue ({local_transaction}) detected redundant reader: {operation}, channel: {channel}')
        else:
            _set_key_value(key, {DEQUEUE_BUSY: DEQUEUE_BUSY_STATUS})
            if reset_first: # Clear out any (presumably dead) reader ... assume first dequeue precedes first enqueue
                _del_message(key, permissive=True)
            _set_key_value(key, {PICKUP_TIME: ''})
            _expire(key) # Needed in case nothing ever adds to this queue (via _enqueue)

            # Use a heuristic to figure out how often to poll redis for a request or reply. This is useful because
            # there are known to be zombie waiters, particularly because the browser create virtual machines
            # that keep executing, but on behalf of no client. Zombies could also exist simply because a user
            # isn't calling Cytoscape, or Cytoscape is taking a long time to return a result. If we allow
            # zombies to poll rapidly, redis bandwidth for legitimate users is unavailable. For this heuristic,
            # we allow fast polling for some number of _dequeue calls, but then drop down to a much slower
            # polling after that. All of this can go away if we wait asynchronously for a key value, but that's
            # for another day.
            fast_polls_left = redis_db.hget(key, REPLY_FAST_POLLS_LEFT)
            if fast_polls_left is None:
                fast_polls_left = ALLOWED_FAST_DEQUEUE_POLLS
            else:
                fast_polls_left = int(fast_polls_left.decode('utf-8'))
            if fast_polls_left > 0:
                fast_polls_left -= 1
                _set_key_value(key, {REPLY_FAST_POLLS_LEFT: fast_polls_left})
                dequeue_polling_secs = FAST_DEQUEUE_POLLING_SECS
            else:
                dequeue_polling_secs = SLOW_DEQUEUE_POLLING_SECS

            # Keep trying to read a message until we have to give up
            message = redis_db.hget(key, MESSAGE)
            dequeue_timeout_secs_left = DEQUEUE_TIMEOUT_SECS
            while message is None and dequeue_timeout_secs_left > 0:
                time.sleep(dequeue_polling_secs)
                dequeue_timeout_secs_left -= dequeue_polling_secs
                message = redis_db.hget(key, MESSAGE)
            # TODO: Polling is good enough for now, but for scaling, replace with await

            if message:
                logger.debug(f'  _dequeue ({local_transaction}) returns: {message}')
                _del_message(key)
                _set_key_value(key, {PICKUP_TIME: time.asctime(), REPLY_FAST_POLLS_LEFT: ALLOWED_FAST_DEQUEUE_POLLS})
            else:
                logger.debug(f'  _dequeue ({local_transaction}) timed out: {operation}, channel: {channel}, fast polls left: {fast_polls_left}, polling seconds: {dequeue_polling_secs}')
    finally:
        if valid_reader:
            _set_key_value(key, {DEQUEUE_BUSY: DEQUEUE_IDLE_STATUS})
        logger.debug(f' out of _dequeue ({local_transaction})')

    return message, valid_reader

def _add_padding(message):
    if PAD_MESSAGE:
        if isinstance(message, str):
            message += ' ' * 1500
        elif isinstance(message, bytes):
            message += (' ' * 1500).encode('ascii')
    return message

def _exception_message(e):
    try:
        return e.response.text if e.response and e.response.text else ''
    except:
        return str(e)

def _set_key_value(key, value):
    if not redis_db.hmset(key, value):
        raise Exception(f'redis failed setting {key} to {value}')

def _del_message(key, permissive=False):
    if redis_db.hdel(key, MESSAGE) != 1 and not permissive:
        raise Exception(f'redis failed deleting {key} subkey {MESSAGE}')

def _update_stats(operation, msg):
    stat_key = time.strftime(f'{STATISTIC}:%Y-%m-%d')
    redis_db.hincrby(stat_key, f'{COUNT}:{operation}', 1)
    redis_db.hincrby(stat_key, operation, len(msg))

def _expire(key):
    if redis_db.expire(key, EXPIRE_SECS) != 1:
        raise Exception(f'redis failed expiring {key}')

import threading
transaction_id = 0 # useful for matching messages during debug
transaction_sem = threading.Semaphore() # Environment may have multiple threads calling this module

def _get_transaction_id():
    global transaction_id
    transaction_sem.acquire()
    transaction = transaction_id
    transaction_id += 1
    transaction_sem.release()
    return transaction


if __name__=='__main__':
    debug = False
    if len(sys.argv) > 1:
        host_ip = sys.argv[1]
        debug = True
    else:
        host_ip = '0.0.0.0'
    app.run(debug=debug, host=host_ip)
