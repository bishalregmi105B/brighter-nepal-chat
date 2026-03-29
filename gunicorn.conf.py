"""
Gunicorn production config for brighter-nepal-chat (gevent WebSocket server).
Optimised for 4 vCPU / 8 GB RAM VPS.
Run: gunicorn -c gunicorn.conf.py app:app
"""
import os as _os

# gevent workers handle WebSocket + long-lived connections via greenlets
worker_class = 'geventwebsocket.gunicorn.workers.GeventWebSocketWorker'

# 2 workers is enough — gevent handles thousands of connections per worker.
# More workers just waste RAM on this small VPS.
workers = int(_os.environ.get('CHAT_CONCURRENCY', '2'))

# Each worker can hold up to 3000 persistent WebSocket connections
worker_connections = 3000

# Longer timeout for WebSocket — connections are persistent
timeout = 300
graceful_timeout = 30
keepalive = 75

backlog = 2048

bind = f"0.0.0.0:{_os.environ.get('PORT', '5001')}"

accesslog = '-'
errorlog  = '-'
loglevel  = 'warning'

preload_app = True
