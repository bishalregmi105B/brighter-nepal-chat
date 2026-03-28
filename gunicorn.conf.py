"""
Gunicorn production config for brighter-nepal-chat (gevent WebSocket server).
Run: gunicorn -c gunicorn.conf.py app:app
"""
import multiprocessing

# gevent workers are required for WebSocket + long-lived connections
worker_class = 'geventwebsocket.gunicorn.workers.GeventWebSocketWorker'

# For async WebSocket servers, 1–2 workers per core is fine
# gevent handles thousands of connections within each worker via coroutines
workers = max(2, multiprocessing.cpu_count())

# Each worker can handle thousands of greenlet connections
worker_connections = 5000

# Longer timeout for WebSocket servers — connections are persistent
timeout = 300
graceful_timeout = 30
keepalive = 75

backlog = 2048

# Binding — Render (and most PaaS) inject $PORT dynamically
import os as _os
bind = f"0.0.0.0:{_os.environ.get('PORT', '5001')}"

# Logging
accesslog = '-'
errorlog  = '-'
loglevel  = 'warning'

preload_app = True
